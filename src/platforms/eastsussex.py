"""East Sussex County Council planning scraper.

Blazor Server app at apps.eastsussex.gov.uk/environment/planning/applications/register.
Requires Playwright directly since the site uses SignalR/WebSocket for
data loading -- static HTTP requests only get empty prerendered shells.

Results URL: /register/results?sd=DD%2FMM%2FYYYY&ed=DD%2FMM%2FYYYY&typ=dmw_planning
Detail URL: /register/detail?typ=dmw_planning&appno={reference}

Results page uses definition lists (dt/dd) for each application:
  Type, Reference, Date, Location, Proposal
Detail page uses govuk-summary-list (dt/dd) with fields:
  Reference, Address, Proposal, District, Parish, Electoral division,
  Received, Consultation start/end, Case officer
"""
import asyncio
import logging
import re
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://apps.eastsussex.gov.uk/environment/planning/applications/register"


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class EastSussexScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._pw = None
        self._browser = None
        self._page = None

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        context = await self._browser.new_context()
        self._page = await context.new_page()

    async def _navigate_and_wait(self, url: str) -> str:
        """Navigate to a URL and wait for Blazor SignalR to render content."""
        await self._ensure_browser()
        await self._page.goto(url, timeout=60000)
        # Wait for Blazor to establish SignalR and render dynamic content.
        # Look for dt elements (definition terms used in both results and detail)
        # or "No results found" text.
        try:
            await self._page.wait_for_selector(
                "dt, p:has-text('No results found')",
                timeout=20000,
            )
        except Exception:
            pass
        # Extra settle time for any remaining Blazor rendering
        await asyncio.sleep(1)
        return await self._page.content()

    async def _click_page_button(self, page_num: int) -> str:
        """Click a pagination button and wait for content to update."""
        try:
            btn = self._page.get_by_role("button", name=str(page_num), exact=True)
            await btn.click(timeout=5000)
            await asyncio.sleep(2)
            return await self._page.content()
        except Exception:
            return ""

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        sd = quote(date_from.strftime("%d/%m/%Y"), safe="")
        ed = quote(date_to.strftime("%d/%m/%Y"), safe="")
        url = f"{BASE_URL}/results?sd={sd}&ed={ed}&typ=dmw_planning"

        html = await self._navigate_and_wait(url)

        results = self._parse_results(html)
        if not results:
            return []

        # Handle pagination by clicking page buttons
        total = self._extract_total(html)
        page_size = len(results)
        if total and page_size and total > page_size:
            num_pages = (total + page_size - 1) // page_size
            for page_num in range(2, min(num_pages + 1, 51)):
                page_html = await self._click_page_button(page_num)
                if not page_html:
                    break
                page_results = self._parse_results(page_html)
                if not page_results:
                    break
                results.extend(page_results)

        return results

    def _parse_results(self, html: str) -> List[ApplicationSummary]:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Each application is a block of dt/dd pairs inside a container div.
        # Reference is inside a button element within a dd following a dt "Reference:".
        for dt in soup.find_all("dt"):
            if "Reference" not in dt.get_text():
                continue
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue

            btn = dd.find("button") or dd.find("a")
            if btn:
                ref = btn.get_text(strip=True)
            else:
                ref = dd.get_text(strip=True)

            if not ref:
                continue

            detail_url = (
                f"{BASE_URL}/detail?typ=dmw_planning&appno={quote(ref, safe='')}"
            )
            results.append(ApplicationSummary(uid=ref, url=detail_url))

        return results

    def _extract_total(self, html: str) -> Optional[int]:
        """Extract total count from '1-10 of 42 applications' text."""
        m = re.search(r"of\s+(\d+)\s+application", html)
        if m:
            return int(m.group(1))
        return None

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url or (
            f"{BASE_URL}/detail?typ=dmw_planning&appno={quote(application.uid, safe='')}"
        )
        html = await self._navigate_and_wait(url)
        return self._parse_detail(html, url, application.uid)

    def _parse_detail(self, html: str, url: str, uid: str) -> ApplicationDetail:
        soup = BeautifulSoup(html, "html.parser")

        fields = {}
        for dt in soup.find_all("dt"):
            label = dt.get_text(strip=True).rstrip(":")
            dd = dt.find_next_sibling("dd")
            if dd:
                value = dd.get_text(strip=True)
                if value:
                    fields[label.lower()] = value

        reference = fields.get("reference", uid)
        address = fields.get("address", "")
        description = fields.get("proposal", "")
        app_type = None
        ward = fields.get("electoral division")
        parish = fields.get("parish")
        district = fields.get("district")
        case_officer = fields.get("case officer")
        date_received = _parse_date(fields.get("received", ""))

        # Application type from the h3 heading
        h3 = soup.find("h3")
        if h3:
            app_type = h3.get_text(strip=True)

        raw_data = {}
        if district:
            raw_data["district"] = district
        consultation_start = fields.get("consultation start")
        consultation_end = fields.get("consultation end")
        if consultation_start:
            raw_data["consultation_start"] = consultation_start
        if consultation_end:
            raw_data["consultation_end"] = consultation_end
        decision = fields.get("decision")
        decision_date = fields.get("decision date")
        if decision_date:
            raw_data["decision_date"] = decision_date

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            application_type=app_type,
            status=None,
            decision=decision,
            date_received=date_received,
            ward=ward,
            parish=parish,
            case_officer=case_officer,
            raw_data=raw_data,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
