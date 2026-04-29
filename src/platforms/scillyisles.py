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
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y"]:
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

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        summaries = []
        page = 0
        max_pages = 100

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

            for row in rows:
                cells = row.find_all("td")
                if not cells:
                    continue
                link = cells[0].find("a", href=True)
                if not link:
                    continue
                ref = cells[0].get_text(strip=True)
                ref = re.sub(r"^Planning application:\s*", "", ref)
                href = link["href"]
                if not href.startswith("http"):
                    href = f"{self._base}{href}"
                summaries.append(ApplicationSummary(uid=ref, url=href))

            # Check for next page
            next_link = soup.find("li", class_="pager-next")
            if not next_link or not next_link.find("a"):
                break
            page += 1

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        html = await _curl_get(application.url)
        soup = BeautifulSoup(html, "html.parser")

        reference = _field_text(soup, "field-name-field-planning-app-num") or application.uid
        address = _field_text(soup, "field-name-field-site-address")
        description = _field_text(soup, "field-name-body")
        application_type = _field_text(soup, "field-name-field-planning-app-type")
        decision = _field_text(soup, "field-name-field-decision")
        applicant_name = _field_text(soup, "field-name-field-planning-applicant-name")
        agent_name = _field_text(soup, "field-name-field-agent-name")
        decision_date_str = _field_text(soup, "field-name-field-decision-date")
        date_received_str = _field_text(soup, "field-name-field-date-received")
        date_validated_str = _field_text(soup, "field-name-field-list-date")

        raw_data = {}
        if agent_name:
            raw_data["agent_name"] = agent_name
        if decision_date_str:
            raw_data["decision_date"] = decision_date_str

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=application.url,
            application_type=application_type,
            decision=decision or None,
            applicant_name=applicant_name or None,
            date_received=_parse_date(date_received_str),
            date_validated=_parse_date(date_validated_str),
            raw_data=raw_data,
        )
