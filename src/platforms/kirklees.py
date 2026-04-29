"""Kirklees Council planning scraper.

Custom ASP.NET application at kirklees.gov.uk. Requires ViewState round-trip:
GET the search page, extract hidden fields, POST with date range, then
paginate through results via __EVENTTARGET postbacks.
"""
import re
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

BASE_URL = "https://www.kirklees.gov.uk/beta/planning-applications/search-for-planning-applications"
WEEKLY_URL = "https://www.kirklees.gov.uk/beta/planning-applications/weekly-list-of-planning-applications/default.aspx"
SEARCH_URL = f"{BASE_URL}/default.aspx"
DETAIL_URL = f"{BASE_URL}/detail.aspx"

DATE_FROM_FIELD = "ctl00$ctl00$cphPageBody$cphContent$txtDateFrom"
DATE_TO_FIELD = "ctl00$ctl00$cphPageBody$cphContent$txtDateTo"
SEARCH_SUBMIT = "ctl00$ctl00$cphPageBody$cphContent$btnAdvSearch"
WEEKLY_DROPDOWN = "ctl00$ctl00$cphPageBody$cphContent$ddlWeekList"
WEEKLY_TYPE = "ctl00$ctl00$cphPageBody$cphContent$radTypeList"
WEEKLY_SUBMIT = "ctl00$ctl00$cphPageBody$cphContent$btnSearch"
NEXT_PAGE_TARGET = "ctl00$ctl00$cphPageBody$cphContent$dpSearchResultsAbove$ctl02$ctl00"
FORM_ID = "aspnetForm"


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%d %B %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_hidden_fields(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for inp in soup.find_all("input", type="hidden"):
        name = inp.get("name", "")
        if name:
            fields[name] = inp.get("value", "")
    return fields


class KirkleesScraper(BaseScraper):
    """Scraper for Kirklees Council's ASP.NET planning portal."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search via weekly list form — select weeks overlapping the date range."""
        # GET the weekly list page
        resp = await self._client.get(WEEKLY_URL)
        resp.raise_for_status()
        fields = _extract_hidden_fields(resp.text)

        soup = BeautifulSoup(resp.text, "html.parser")
        select = soup.find("select", {"name": WEEKLY_DROPDOWN})
        if not select:
            return []

        # Find weeks that overlap our date range
        all_summaries: List[ApplicationSummary] = []
        seen_uids = set()

        for option in select.find_all("option"):
            val = option.get("value", "")
            if not val:
                continue
            # Value is like "20/04/2026 00:00:00"
            try:
                week_start = datetime.strptime(val.split(" ")[0], "%d/%m/%Y").date()
            except ValueError:
                continue
            week_end = week_start + __import__("datetime").timedelta(days=6)
            if week_end < date_from or week_start > date_to:
                continue

            # Submit the form for this week
            week_fields = dict(fields)
            week_fields[WEEKLY_DROPDOWN] = val
            week_fields[WEEKLY_TYPE] = "1"  # Received
            week_fields[WEEKLY_SUBMIT] = "Search"

            resp = await self._client.post(WEEKLY_URL, data=week_fields)
            resp.raise_for_status()

            page_summaries = self._parse_results_page(resp.text)
            for s in page_summaries:
                if s.uid not in seen_uids:
                    seen_uids.add(s.uid)
                    all_summaries.append(s)

            # Re-extract fields for next week submission
            fields = _extract_hidden_fields(resp.text)

        return all_summaries

    def _parse_results_page(self, html: str) -> List[ApplicationSummary]:
        """Extract application summaries from search or weekly list results."""
        soup = BeautifulSoup(html, "html.parser")

        summaries = []
        for link in soup.find_all("a", href=re.compile(r"detail\.aspx")):
            href = link.get("href", "")
            url = urljoin(SEARCH_URL, href)

            # Extract UID from heading like "Application 2024/12345"
            h4 = link.find("h4")
            if h4:
                text = h4.get_text(strip=True)
                uid_match = re.search(r"Application\s*(.+)", text)
                uid = uid_match.group(1).strip() if uid_match else text
            else:
                uid = link.get_text(strip=True)
                uid_match = re.search(r"Application\s+(.+)", uid)
                if uid_match:
                    uid = uid_match.group(1).strip()

            if uid:
                summaries.append(ApplicationSummary(uid=uid, url=url))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url
        if not url:
            url = f"{DETAIL_URL}?id={quote_plus(application.uid)}"

        resp = await self._client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        def span_text(span_id: str) -> str:
            el = soup.find("span", id=span_id)
            return el.get_text(strip=True) if el else ""

        prefix = "ctl00_ctl00_cphPageBody_cphContent_"

        reference = span_text(f"{prefix}lbl_number_formatted") or application.uid
        address = span_text(f"{prefix}lbl_development_locality")
        description = span_text(f"{prefix}lbl_development_description")

        # Optional fields
        ward = span_text(f"{prefix}lbl_ward")
        applicant = span_text(f"{prefix}lbl_applicant_name")
        agent = span_text(f"{prefix}lbl_agent_name")
        case_officer = span_text(f"{prefix}lbl_case_officer")
        decision = span_text(f"{prefix}lbl_decision_text")
        status = span_text(f"{prefix}lbl_status") or decision

        # Dates
        date_received = _parse_date(span_text(f"{prefix}lbl_received_date"))
        date_validated = _parse_date(span_text(f"{prefix}lbl_registration_date"))

        # Lat/lon from map link
        lat, lon = None, None
        map_link = soup.find("a", href=re.compile(r"map\.kirklees\.gov\.uk"))
        if map_link:
            href = map_link.get("href", "")
            lon_match = re.search(r"lon=([\d.-]+)", href)
            lat_match = re.search(r"lat=([\d.-]+)", href)
            if lon_match:
                lon = lon_match.group(1)
            if lat_match:
                lat = lat_match.group(1)

        raw = {}
        if agent:
            raw["agent_name"] = agent
        if lat:
            raw["latitude"] = lat
        if lon:
            raw["longitude"] = lon

        consultation_start = span_text(f"{prefix}lbl_public_consultation_start_date")
        consultation_end = span_text(f"{prefix}lbl_public_consultation_end_date")
        if consultation_start:
            raw["consultation_start_date"] = consultation_start
        if consultation_end:
            raw["consultation_end_date"] = consultation_end

        appeal_date = span_text(f"{prefix}lbl_appeal_lodged_date")
        if appeal_date:
            raw["appeal_date"] = appeal_date

        agent_address = span_text(f"{prefix}lbl_agent_address")
        if agent_address:
            raw["agent_address"] = agent_address

        decision_date = span_text(f"{prefix}lbl_decision_date")
        if decision_date:
            raw["decision_date"] = decision_date

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            status=status,
            decision=decision,
            date_received=date_received,
            date_validated=date_validated,
            ward=ward,
            applicant_name=applicant,
            case_officer=case_officer,
            raw_data=raw,
        )
