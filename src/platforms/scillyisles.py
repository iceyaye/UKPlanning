"""Isles of Scilly Council planning scraper.

Drupal 7 planning register at scilly.gov.uk. Lists ALL applications
in a paginated HTML table (?page=N, 0-indexed). Detail pages use
Drupal field divs. Since there's no date search, we gather all
applications and filter by date_received.

Uses curl subprocess instead of httpx because Pantheon WAF blocks
Python HTTP clients via TLS fingerprinting.
"""
import asyncio
import re
from datetime import date, datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

LIST_PATH = "/planning-development/planning-applications"

_CURL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _curl_get(url: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-L",
        "-H", f"User-Agent: {_CURL_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}): {stderr.decode()}")
    return stdout.decode("utf-8", errors="replace")


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    # Drupal renders dates like "Friday, 1 May, 2026" — the long-form
    # weekday/comma layout doesn't match strptime cleanly, so strip the
    # weekday prefix and the comma after the day before parsing.
    s = re.sub(r"^[A-Za-z]+,\s*", "", s)
    s = s.replace(",", "")
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y"]:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _field_text(soup: BeautifulSoup, class_name: str) -> str:
    div = soup.find("div", class_=class_name)
    if not div:
        return ""
    content = div.find("div", class_="field-item") or div.find("div", class_="field-items")
    if content:
        return content.get_text(strip=True)
    return div.get_text(strip=True)


class ScillyIslesScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base = config.base_url.rstrip("/")
        # Cache parsed details so gather_ids can filter on date without
        # forcing fetch_detail to re-GET the same page.
        self._detail_cache: dict[str, ApplicationDetail] = {}

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Walk the paginated list newest-first and stop once the reference
        year falls below the start of the requested range.

        The list page has no date column, so we filter on the 2-digit year
        embedded in the reference (e.g. `P/26/035/LBC` → 2026). Apps whose
        year is in [date_from.year, date_to.year] are kept; the per-page
        ordering is descending by reference, so once an entire page is older
        than `date_from.year` we can stop paginating.
        """
        summaries = []
        page = 0
        max_pages = 100

        years_in_range = {date_from.year, date_to.year}
        # Add years between (in case a multi-year scrape ever happens)
        for y in range(date_from.year + 1, date_to.year):
            years_in_range.add(y)

        ref_year_re = re.compile(r"P/(\d{2})/")

        while page < max_pages:
            url = f"{self._base}{LIST_PATH}" if page == 0 else f"{self._base}{LIST_PATH}?page={page}"
            html = await _curl_get(url)
            soup = BeautifulSoup(html, "html.parser")

            table = soup.find("table")
            if not table:
                break

            rows = table.find("tbody")
            if rows:
                rows = rows.find_all("tr")
            else:
                rows = table.find_all("tr")[1:]  # skip header

            if not rows:
                break

            page_added = 0
            page_too_old = 0
            for row in rows:
                cells = row.find_all("td")
                if not cells:
                    continue
                link = cells[0].find("a", href=True)
                if not link:
                    continue
                ref = cells[0].get_text(strip=True)
                ref = re.sub(r"^Planning application:\s*", "", ref)

                m = ref_year_re.search(ref)
                if m:
                    yy = int(m.group(1))
                    full_year = 2000 + yy if yy < 80 else 1900 + yy
                    if full_year not in years_in_range:
                        if full_year < date_from.year:
                            page_too_old += 1
                        continue

                href = link["href"]
                if not href.startswith("http"):
                    href = f"{self._base}{href}"

                # Fetch the detail page now to apply exact date filtering.
                # The list page has no date column, so this is the only way
                # to honour the requested [date_from, date_to] window. The
                # parsed detail is cached for fetch_detail.
                detail = await self._fetch_and_parse_detail(ref, href)
                received = detail.date_received or detail.date_validated
                if received and (received < date_from or received > date_to):
                    continue

                self._detail_cache[ref] = detail
                summaries.append(ApplicationSummary(uid=ref, url=href))
                page_added += 1

            # Stop when the entire page is older than the requested range —
            # the listing is sorted newest-first, so older pages won't yield
            # in-range refs.
            if page_added == 0 and page_too_old > 0:
                break

            # Check for next page
            next_link = soup.find("li", class_="pager-next")
            if not next_link or not next_link.find("a"):
                break
            page += 1

        return summaries

    async def _fetch_and_parse_detail(self, ref: str, url: str) -> ApplicationDetail:
        """Internal: GET the detail page and parse it into ApplicationDetail."""
        html = await _curl_get(url)
        soup = BeautifulSoup(html, "html.parser")

        reference = _field_text(soup, "field-name-field-planning-app-num") or ref
        address = _field_text(soup, "field-name-field-site-address")
        # The body field starts with "Proposal :" label inline; strip it.
        description = _field_text(soup, "field-name-body")
        description = re.sub(r"^Proposal\s*:\s*", "", description).strip()

        application_type = _field_text(soup, "field-name-field-planning-app-type")
        decision = _field_text(soup, "field-name-field-decision")
        applicant_name = _field_text(soup, "field-name-field-planning-applicant-name")
        agent_name = _field_text(soup, "field-name-field-agent-name")
        decision_date_str = _field_text(soup, "field-name-field-decision-date")
        # The Drupal class is `field-date-received` but the visible label is
        # "Valid date" — Scilly only publishes the validation date, not the
        # original received date. Map it to date_validated for accuracy and
        # also expose it as date_received so existing dashboards work.
        valid_date_str = _field_text(soup, "field-name-field-date-received")
        target_date_str = _field_text(soup, "field-name-field-target-date")
        consultation_str = _field_text(soup, "field-name-field-consultation-ends")
        applicant_address = _field_text(soup, "field-name-field-applicant-address")
        agent_address = _field_text(soup, "field-name-field-agent-address")

        valid_date = _parse_date(valid_date_str)
        raw_data = {}
        if agent_name:
            raw_data["agent_name"] = agent_name
        if agent_address:
            raw_data["agent_address"] = agent_address
        if applicant_address:
            raw_data["applicant_address"] = applicant_address
        if decision_date_str:
            raw_data["decision_date"] = decision_date_str
        if target_date_str:
            raw_data["target_date"] = target_date_str
        if consultation_str:
            raw_data["consultation_ends"] = consultation_str

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            application_type=application_type or None,
            decision=decision or None,
            applicant_name=applicant_name or None,
            date_received=valid_date,
            date_validated=valid_date,
            raw_data=raw_data,
        )

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        # gather_ids has already fetched and cached the detail for date
        # filtering; reuse that to avoid a second GET per application.
        cached = self._detail_cache.pop(application.uid, None)
        if cached is not None:
            return cached
        return await self._fetch_and_parse_detail(application.uid, application.url)
