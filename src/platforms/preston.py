"""Preston City Council - weekly list scraper.

Preston publishes weekly planning lists as public HTML pages.
The search portal has image CAPTCHA, so we scrape the weekly lists instead.

Index page: https://www.preston.gov.uk/weekly-lists
Week pages: https://www.preston.gov.uk/weekly-lists/Week-{N}

Each week page lists applications as h2 headings with structured p/strong fields:
  <h2>Application 06/2026/0183</h2>
  <a href="...">View application details for 06/2026/0183</a>
  <strong>Ward:</strong> ...
  <strong>Location:</strong> ...
  <strong>Proposal:</strong> ...
  <strong>Case Officer:</strong> ...
  <strong>Valid Date:</strong> ...
"""

import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup, Tag

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

INDEX_URL = "https://www.preston.gov.uk/weekly-lists"


def _parse_week_date(text: str) -> Optional[date]:
    """Parse the date from a weekly list link text like 'Week 4 - 24.04.26'."""
    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})', text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_valid_date(text: str) -> Optional[date]:
    """Parse a valid date like '20 April 2026' or '6 April 2026'."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class PrestonScraper(BaseScraper):
    """Scraper for Preston City Council weekly planning lists."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
        )

    async def _fetch_week_links(self) -> List[Tuple[str, date]]:
        """Fetch the index page and return (url, end_date) for each week."""
        resp = await self._client.get(INDEX_URL)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        weeks = []
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            if not re.match(r'Week\s+\d+', text, re.I):
                continue
            week_date = _parse_week_date(text)
            if not week_date:
                continue
            href = link["href"]
            if href.startswith("/"):
                href = f"https://www.preston.gov.uk{href}"
            weeks.append((href, week_date))

        return weeks

    def _weeks_in_range(
        self, weeks: List[Tuple[str, date]], date_from: date, date_to: date
    ) -> List[Tuple[str, date]]:
        """Filter weeks whose date range overlaps [date_from, date_to].

        Each week link shows its end date. We assume each week covers ~7 days,
        so a week overlaps if its end_date >= date_from and (end_date - 7) <= date_to.
        We add a 1-day buffer on each side to avoid missing edge cases.
        """
        result = []
        for url, end_date in weeks:
            week_start = end_date - timedelta(days=8)
            if end_date >= date_from and week_start <= date_to:
                result.append((url, end_date))
        return result

    def _parse_applications(self, html: str) -> List[Dict[str, str]]:
        """Parse application blocks from a weekly list page.

        Each application is a heading (h2 or h3) containing 'Application ...'
        followed by sibling elements with strong-tagged field labels.
        """
        soup = BeautifulSoup(html, "html.parser")
        apps = []

        headings = soup.find_all(["h2", "h3"], string=re.compile(r'Application\s+\S+', re.I))

        for heading in headings:
            ref_match = re.search(r'Application\s+(\S+)', heading.get_text(strip=True), re.I)
            if not ref_match:
                continue

            app: Dict[str, str] = {"reference": ref_match.group(1)}

            # Walk siblings until the next heading to collect fields
            sibling = heading.next_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name in ("h2", "h3"):
                    break

                if isinstance(sibling, Tag):
                    # Check for detail link
                    detail_link = sibling.find("a", href=True) if sibling.name != "a" else sibling
                    if isinstance(sibling, Tag) and sibling.name == "a" and sibling.get("href"):
                        detail_link = sibling
                    elif isinstance(sibling, Tag):
                        detail_link = sibling.find("a", href=True)

                    if detail_link and "View application" in detail_link.get_text("", strip=True):
                        app["url"] = detail_link["href"]

                    # Extract strong-tagged fields
                    for strong in (sibling.find_all("strong") if sibling.name != "strong" else [sibling]):
                        label = strong.get_text(strip=True).rstrip(":")
                        # Value is the text after the strong tag (in the same parent)
                        value = ""
                        next_node = strong.next_sibling
                        while next_node:
                            if isinstance(next_node, Tag) and next_node.name == "strong":
                                break
                            if isinstance(next_node, Tag):
                                value += next_node.get_text(" ", strip=True)
                            elif isinstance(next_node, str):
                                value += next_node
                            next_node = next_node.next_sibling
                        value = value.strip()
                        if label and value:
                            app[label.lower()] = value

                sibling = sibling.next_sibling

            if app.get("reference"):
                apps.append(app)

        return apps

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        weeks = await self._fetch_week_links()
        relevant = self._weeks_in_range(weeks, date_from, date_to)

        summaries = []
        for url, _ in relevant:
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError:
                continue

            for app in self._parse_applications(resp.text):
                ref = app["reference"]
                detail_url = app.get("url", "")
                summaries.append(ApplicationSummary(uid=ref, url=detail_url))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        return ApplicationDetail(
            reference=application.uid,
            address="",
            description="",
            url=application.url,
        )

    async def scrape(self, date_from: date, date_to: date) -> ScrapeResult:
        """Override to extract all data from weekly list pages directly."""
        try:
            weeks = await self._fetch_week_links()
            relevant = self._weeks_in_range(weeks, date_from, date_to)

            details = []
            for url, _ in relevant:
                try:
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPStatusError:
                    continue

                for app in self._parse_applications(resp.text):
                    details.append(ApplicationDetail(
                        reference=app.get("reference", ""),
                        address=app.get("location", ""),
                        description=app.get("proposal", ""),
                        url=app.get("url", ""),
                        date_validated=_parse_valid_date(app.get("valid date", "")),
                        ward=app.get("ward"),
                        case_officer=app.get("case officer"),
                    ))

            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
