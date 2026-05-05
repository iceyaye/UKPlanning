"""Tascomi platform scraper (Dartmoor, Stoke, Gloucestershire, Denbighshire,
and others).

Tascomi portals serve a `getReceivedWeeklyList` form that requires a `week`
parameter (Monday of an ISO week, formatted as YYYY-MM-DD). A bare GET shows
either the most-recent week's apps (some councils) or nothing (most councils).
To cover a date range we enumerate every ISO-week-starting Monday between
`date_from` and `date_to` and POST each, accumulating the resulting rows.

Detail pages return HTTP 202 (async render) so we extract all fields from
the weekly-list table directly.
"""
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

COUNCIL_URLS = {
    "dartmoor": "https://dartmoor-online.tascomi.com",
    "barking": "https://online-befirst.lbbd.gov.uk",
    "blaenaugwent": "https://developmentservices.blaenau-gwent.gov.uk",
    "ceredigion": "https://ceredigion-online.tascomi.com",
    "cheshireeast": "https://pa.cheshireeast.gov.uk",
    "denbighshire": "https://developments.denbighshire.gov.uk",
    "warrington": "https://online.warrington.gov.uk",
    "gwynedd": "https://amg.gwynedd.llyw.cymru",
    "hackney": "https://developmentandhousing.hackney.gov.uk",
    "wirral": "https://online.wirral.gov.uk",
    "stoke": "https://development.stoke.gov.uk",
    "easthampshire": "https://publicaccess.easthants.gov.uk",
    "gloucestershire": "https://planningonline.gloucestershire.gov.uk",
    "merton": "https://rspandlp.merton.gov.uk",
}


class TascomiScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base_url = COUNCIL_URLS.get(
            config.authority_code, config.base_url.rstrip("/")
        )
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            follow_redirects=True,
            timeout=30,
        )

    @staticmethod
    def _iso_week_mondays(date_from: date, date_to: date) -> List[date]:
        """Return every Monday on/before each ISO week that intersects
        [date_from, date_to]. Tascomi's weekpicker keys lists by the Monday
        of an ISO week, so we POST one request per intersecting week."""
        first_monday = date_from - timedelta(days=date_from.weekday())
        last_monday = date_to - timedelta(days=date_to.weekday())
        mondays = []
        cur = first_monday
        while cur <= last_monday:
            mondays.append(cur)
            cur += timedelta(days=7)
        return mondays

    async def _fetch_week(self, week_monday: date) -> str:
        """POST the weekly-list form for the given Monday and return HTML."""
        url = f"{self._base_url}/planning/index.html"
        resp = await self._client.post(
            url,
            data={
                "fa": "getReceivedWeeklyList",
                "week": week_monday.strftime("%Y-%m-%d"),
            },
        )
        resp.raise_for_status()
        return resp.text

    def _parse_week_rows(self, html: str) -> List[ApplicationDetail]:
        """Extract every application row from a weekly-list HTML page."""
        soup = BeautifulSoup(html, "html.parser")
        details: List[ApplicationDetail] = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            link = row.find("a", href=re.compile(r"getApplication.*id=\d+"))
            if not link:
                continue
            href = link.get("href", "")
            app_url = f"{self._base_url}{href}" if href.startswith("/") else href
            details.append(ApplicationDetail(
                reference=cells[0].get_text(strip=True),
                address=cells[1].get_text(strip=True),
                description=cells[2].get_text(strip=True),
                url=app_url,
                ward=cells[3].get_text(strip=True) or None,
                parish=cells[4].get_text(strip=True) or None,
            ))
        return details

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Walk every intersecting ISO week and accumulate application IDs."""
        seen_ids: Dict[str, str] = {}
        for monday in self._iso_week_mondays(date_from, date_to):
            try:
                html = await self._fetch_week(monday)
            except httpx.HTTPError:
                continue
            for d in self._parse_week_rows(html):
                # Use the numeric id from the URL as uid since references can
                # differ in formatting across councils
                m = re.search(r"id=(\d+)", d.url or "")
                uid = m.group(1) if m else d.reference
                if uid and uid not in seen_ids:
                    seen_ids[uid] = d.url
        return [ApplicationSummary(uid=u, url=url) for u, url in seen_ids.items()]

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Re-walk the relevant weekly list and return the matched row's data.

        scrape() does this in one pass, so this path is only hit when the
        worker uses the legacy gather_ids+fetch_detail pipeline.
        """
        # Use the Monday of "today" as a starting point and walk back a few
        # weeks looking for the application — most apps appear in the week
        # they were received, so a 6-week window is plenty.
        today_monday = date.today() - timedelta(days=date.today().weekday())
        for offset in range(0, 6):
            monday = today_monday - timedelta(days=7 * offset)
            try:
                html = await self._fetch_week(monday)
            except httpx.HTTPError:
                continue
            for d in self._parse_week_rows(html):
                m = re.search(r"id=(\d+)", d.url or "")
                if m and m.group(1) == application.uid:
                    return d
        return ApplicationDetail(
            reference=application.uid,
            address="",
            description="",
            url=application.url,
        )

    async def scrape(self, date_from: date, date_to: date) -> ScrapeResult:
        """Single-pass scrape: walk every intersecting ISO week and extract
        all rows. Faster than gather_ids+fetch_detail because we only fetch
        each weekly list once."""
        try:
            seen: set = set()
            details: List[ApplicationDetail] = []
            for monday in self._iso_week_mondays(date_from, date_to):
                try:
                    html = await self._fetch_week(monday)
                except httpx.HTTPError:
                    continue
                for d in self._parse_week_rows(html):
                    if d.reference in seen:
                        continue
                    seen.add(d.reference)
                    details.append(d)
            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
