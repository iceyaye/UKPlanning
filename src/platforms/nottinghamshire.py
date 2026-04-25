"""Nottinghamshire County Council planning scraper.

ASP.NET with Telerik RadDatePicker at nottinghamshire.gov.uk/planningsearch.
Two-phase flow: GET disclaimer -> POST advanced search with ViewState and
Telerik ClientState date fields -> parse paginated results -> fetch detail pages.
"""
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

BASE_URL = "https://www.nottinghamshire.gov.uk/planningsearch"
SEARCH_URL = f"{BASE_URL}/planappsrch.aspx"
DETAIL_URL = f"{BASE_URL}/plandisp.aspx"

TELERIK_DATE_FMT = (
    '{{"enabled":true,"emptyMessage":"","validationText":"{val}-00-00-00",'
    '"valueAsString":"{val}-00-00-00",'
    '"minDateStr":"01/01/1900","maxDateStr":"12/31/2099"}}'
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}


def _telerik_date(d: date) -> str:
    return TELERIK_DATE_FMT.format(val=d.strftime("%Y-%m-%d"))


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_hidden_fields(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for inp in soup.find_all("input", type="hidden"):
        name = inp.get("name", "")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def _field_value(soup: BeautifulSoup, name: str) -> str:
    el = soup.find("input", {"name": name})
    if el:
        return el.get("value", "").strip()
    el = soup.find("textarea", {"name": name})
    if el:
        return (el.string or "").strip()
    el = soup.find("select", {"name": name})
    if el:
        opt = el.find("option", selected=True)
        if opt:
            return opt.get_text(strip=True)
    return ""


class NottinghamshireScraper(BaseScraper):
    """Scraper for Nottinghamshire County Council planning portal."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._client = httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=60,
        )

    async def _accept_disclaimer(self) -> None:
        """Visit the home page to establish session cookie."""
        await self._client.get(f"{BASE_URL}/planhome.aspx")

    async def _handle_disclaimer(self, resp) -> httpx.Response:
        """Accept mid-flow disclaimer if the response is a disclaimer page."""
        if "Disclaimer" not in str(resp.url):
            return resp
        fields = _extract_hidden_fields(resp.text)
        fields["ctl00$MainContent$btnAccept"] = "Accept"
        resp = await self._client.post(str(resp.url), data=fields)
        resp.raise_for_status()
        return resp

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        await self._accept_disclaimer()

        # Phase 1: GET the search page for ViewState
        resp = await self._client.get(SEARCH_URL)
        resp.raise_for_status()
        fields = _extract_hidden_fields(resp.text)

        # Phase 2: POST search with date range via Telerik ClientState
        fields["__EVENTTARGET"] = ""
        fields["__EVENTARGUMENT"] = ""
        fields["ctl00$MainContent$txtOurReference"] = ""
        fields["ctl00$MainContent$txtAppNumber"] = ""
        fields["ctl00$MainContent$txtLocation"] = ""
        fields["ctl00$MainContent$txtProposal"] = ""
        fields["ctl00$MainContent$txtApplicantName"] = ""
        fields["ctl00$MainContent$txtDateReceivedFrom"] = date_from.strftime("%Y-%m-%d")
        fields["ctl00$MainContent$txtDateReceivedFrom$dateInput"] = date_from.strftime("%d/%m/%Y")
        fields["ctl00_MainContent_txtDateReceivedFrom_dateInput_ClientState"] = _telerik_date(date_from)
        fields["ctl00$MainContent$txtDateReceivedTo"] = date_to.strftime("%Y-%m-%d")
        fields["ctl00$MainContent$txtDateReceivedTo$dateInput"] = date_to.strftime("%d/%m/%Y")
        fields["ctl00_MainContent_txtDateReceivedTo_dateInput_ClientState"] = _telerik_date(date_to)
        fields["ctl00$MainContent$txtDateDeterminedFrom"] = ""
        fields["ctl00$MainContent$txtDateDeterminedFrom$dateInput"] = ""
        fields["ctl00$MainContent$txtDateDeterminedTo"] = ""
        fields["ctl00$MainContent$txtDateDeterminedTo$dateInput"] = ""
        fields["ctl00$MainContent$btnSearch"] = "Search"

        resp = await self._client.post(SEARCH_URL, data=fields)
        resp.raise_for_status()
        resp = await self._handle_disclaimer(resp)

        # Parse all pages of results
        summaries = []
        seen = set()
        max_pages = 50

        for page in range(max_pages):
            new_items = self._parse_results_page(resp.text, seen)
            summaries.extend(new_items)

            if not new_items:
                break

            # Check for next page button
            next_fields = _extract_hidden_fields(resp.text)
            soup = BeautifulSoup(resp.text, "html.parser")
            next_btn = soup.find("input", {"name": "ctl00$MainContent$lvResults$pager$ctl02$NextButton"})
            if not next_btn:
                break

            next_fields["__EVENTTARGET"] = ""
            next_fields["__EVENTARGUMENT"] = ""
            next_fields["ctl00$MainContent$lvResults$pager$ctl02$NextButton"] = next_btn.get("value", "Next")

            resp = await self._client.post(SEARCH_URL, data=next_fields)
            resp.raise_for_status()

        return summaries

    def _parse_results_page(self, html: str, seen: set) -> List[ApplicationSummary]:
        items = []
        soup = BeautifulSoup(html, "html.parser")

        # Results are in SearchResultRow divs with links to plandisp.aspx
        for link in soup.find_all("a", href=re.compile(r"plandisp\.aspx\?AppNo=")):
            href = link.get("href", "")
            uid = link.get_text(strip=True)
            if not uid or uid in seen:
                continue
            seen.add(uid)

            url = href if href.startswith("http") else f"{BASE_URL}/{href}"
            items.append(ApplicationSummary(uid=uid, url=url))

        # Fallback: look for any link with AppNo parameter
        if not items:
            for match in re.finditer(r'href="([^"]*plandisp\.aspx\?AppNo=([^"&]+))"', html):
                href = match.group(1)
                uid = match.group(2)
                if uid in seen:
                    continue
                seen.add(uid)
                url = href if href.startswith("http") else f"{BASE_URL}/{href}"
                items.append(ApplicationSummary(uid=uid, url=url))

        return items

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url or f"{DETAIL_URL}?AppNo={quote_plus(application.uid)}"

        resp = await self._client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        reference = _field_value(soup, "ctl00$MainContent$txtAppNumber") or application.uid
        address = _field_value(soup, "ctl00$MainContent$txtLocation")
        description = _field_value(soup, "ctl00$MainContent$txtProposal")
        date_received = _parse_date(_field_value(soup, "ctl00$MainContent$txtReceivedDate"))
        date_validated = _parse_date(_field_value(soup, "ctl00$MainContent$txtValidDate"))
        case_officer = _field_value(soup, "ctl00$MainContent$txtCaseOfficer")
        decision = _field_value(soup, "ctl00$MainContent$txtDecision")
        decision_date_str = _field_value(soup, "ctl00$MainContent$txtDecisionDate2")
        applicant_name = _field_value(soup, "ctl00$MainContent$txtAppName")
        district = _field_value(soup, "ctl00$MainContent$listDistricts")
        parish = _field_value(soup, "ctl00$MainContent$listParishes")

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            status=None,
            decision=decision or None,
            date_received=date_received,
            date_validated=date_validated,
            ward=district or None,
            parish=parish or None,
            applicant_name=applicant_name or None,
            case_officer=case_officer or None,
            raw_data={
                "decision_date": decision_date_str or None,
                "agent_name": _field_value(soup, "ctl00$MainContent$txtAgentsName") or None,
            },
        )
