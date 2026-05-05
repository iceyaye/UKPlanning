"""Rochford District Council - Astun GIS planning portal scraper.

Astun iShare/GIS portals serve template-based search pages at
DevelopmentControl.aspx?RequestType=ParseTemplate. Results come as dl/dt/dd
lists. Detail data is split across multiple template pages (Application,
Dates, Applicant, Agent).

URL: https://maps.rochford.gov.uk/DevelopmentControl.aspx
"""

import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.parser import PageParser
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


BASE_URL = "https://maps.rochford.gov.uk"

SEARCH_URL = (
    f"{BASE_URL}/DevelopmentControl.aspx"
    "?RequestType=ParseTemplate&template=DevelopmentControlAdvancedSearch.tmplt"
)

DETAIL_TEMPLATE = (
    f"{BASE_URL}/DevelopmentControl.aspx"
    "?requesttype=parsetemplate&template={{template}}"
    "&basepage=DevelopmentControl.aspx"
    "&Filter=^REFVAL^=%27{{ref}}%27"
)

# Detail page selectors (dl/dt/dd layout)
ROCHFORD_DETAIL_SELECTORS = {
    "reference": ["dt:-soup-contains('Application Reference') + dd"],
    "address": [
        "dt:-soup-contains('Address Of Proposal') + dd",
        "dt:-soup-contains('Address of Proposal') + dd",
        "dt:-soup-contains('Site Address') + dd",
    ],
    "description": [
        # The custom dl-pair walker in _extract_dl_pairs handles this cleanly;
        # the parser's :-soup-contains here is too broad ('Proposal' matches
        # 'Address Of Proposal' too) so leave the parser-based fallback for
        # other Astun sites only.
        "dt:-soup-contains('Description') + dd",
    ],
    "application_type": [
        "dt:-soup-contains('Type Of Application') + dd",
        "dt:-soup-contains('Type of Application') + dd",
    ],
    "status": ["dt:-soup-contains('Status') + dd"],
    "decision": ["dt:-soup-contains('Decision') + dd"],
    "decided_by": ["dt:-soup-contains('Decision Type') + dd"],
    "ward": ["dt:-soup-contains('Ward') + dd"],
    "parish": ["dt:-soup-contains('Parish') + dd"],
    "case_officer": ["dt:-soup-contains('Case Officer') + dd"],
    "planning_portal_id": ["dt:-soup-contains('Planning Portal Reference') + dd"],
    "district": ["dt:-soup-contains('District Reference') + dd"],
}

ROCHFORD_DATE_SELECTORS = {
    "date_received": ["dt:-soup-contains('Date Application Received') + dd"],
    "date_validated": ["dt:-soup-contains('Date Application Validated') + dd"],
    "target_decision_date": ["dt:-soup-contains('Target Determination Date') + dd"],
    "meeting_date": ["dt:-soup-contains('Actual Committee Date') + dd"],
    "decision_date": ["dt:-soup-contains('Date Decision Made') + dd"],
    "decision_issued_date": ["dt:-soup-contains('Date Decision Issued') + dd"],
    "consultation_start_date": ["dt:-soup-contains('Standard Consultations sent on') + dd"],
    "consultation_end_date": ["dt:-soup-contains('Expiry Date for Standard Consultations') + dd"],
    "neighbour_consultation_start_date": ["dt:-soup-contains('Neighbourhood Consultations sent on') + dd"],
    "neighbour_consultation_end_date": ["dt:-soup-contains('Expiry Date for Neighbour Consultations') + dd"],
    "last_advertised_date": ["dt:-soup-contains('Last Advertised on') + dd"],
    "site_notice_start_date": ["dt:-soup-contains('Latest Site Notice posted on') + dd"],
    "permission_expires_date": ["dt:-soup-contains('Permission Expiry Date') + dd"],
}

ROCHFORD_APPLICANT_SELECTORS = {
    "applicant_name": ["dt:-soup-contains('Name') + dd"],
    "applicant_address": ["dt:-soup-contains('Address') + dd"],
}

ROCHFORD_AGENT_SELECTORS = {
    "agent_name": ["dt:-soup-contains('Name') + dd"],
    "agent_address": ["dt:-soup-contains('Address') + dd"],
}


class RochfordScraper(BaseScraper):
    """Scraper for Rochford District Council (Astun GIS template portal).

    Search via POST with DATEAPRECV:FROM:DATE / DATEAPRECV:TO:DATE fields.
    Results paginate via pageno= parameter in the URL.
    Detail data is split across four template pages:
      - DevelopmentControlApplication (main)
      - DevelopmentControlApplication_Dates
      - DevelopmentControlApplication_Applicant
      - DevelopmentControlApplication_Agent
    """

    DATE_FORMAT = "%d/%m/%Y"
    DATE_FROM_FIELD = "DATEAPRECV:FROM:DATE"
    DATE_TO_FIELD = "DATEAPRECV:TO:DATE"
    MAX_PAGES = 80

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._parser = PageParser()
        self._client = HttpClient(
            timeout=30,
            rate_limit_delay=config.rate_limit_delay,
        )

    @staticmethod
    def _extract_search_form(html: str) -> Dict[str, str]:
        """Pull every hidden+text input default from the live search form.

        The hidden `history` field is a per-session token; without it the
        server treats the request as a fresh form-load instead of a search.
        """
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if not form:
            return {}
        fields: Dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            if not name:
                continue
            input_type = (inp.get("type") or "").lower()
            if input_type in ("submit", "button"):
                continue
            fields[name] = inp.get("value", "") or ""
        for sel in form.find_all("select"):
            name = sel.get("name", "")
            if not name:
                continue
            chosen = sel.find("option", selected=True) or sel.find("option")
            fields[name] = chosen.get("value", "") if chosen else ""
        return fields

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """GET date-range search via the search form's actual fields and
        paginate through results. The search form is a *GET* request to
        DevelopmentControl.aspx with `template=DevelopmentControlResults.tmpl`
        and a per-session `history` token from the search page; POSTing
        to the search-form URL just reshows the empty form."""
        search_resp = await self._client.get(SEARCH_URL)
        search_form = self._extract_search_form(search_resp.text)
        search_form[self.DATE_FROM_FIELD] = date_from.strftime(self.DATE_FORMAT)
        search_form[self.DATE_TO_FIELD] = date_to.strftime(self.DATE_FORMAT)
        search_form["maxrecords"] = "300"

        response = await self._client.get(BASE_URL + "/DevelopmentControl.aspx", params=search_form)
        html = response.text
        current_url = str(response.url)

        all_apps: List[ApplicationSummary] = []
        page_count = 0

        while page_count < self.MAX_PAGES:
            page_apps = self._parse_results_page(html, current_url)
            if not page_apps:
                break
            all_apps.extend(page_apps)
            page_count += 1

            next_url = self._next_page_url(current_url, page_count + 1)
            try:
                response = await self._client.get(next_url)
                html = response.text
                current_url = str(response.url)
            except Exception:
                break

        return all_apps

    def _parse_results_page(self, html: str, base_url: str) -> List[ApplicationSummary]:
        """Extract application UIDs and URLs from a results page.

        Rochford results use <div id="results"><dl> with <dt><a> for links
        and <dd class="last"> containing the reference.
        """
        soup = BeautifulSoup(html, "lxml")
        results = []

        results_div = soup.find("div", id="results")
        if not results_div:
            # Fallback: search the whole page
            results_div = soup

        for dt_tag in results_div.find_all("dt"):
            link = dt_tag.find("a", href=True)
            if not link:
                continue

            # Reference is the FIRST <strong> in the next dd.last sibling.
            # The dd is structured "Application reference: <strong>26/00323/DOC</strong>
            # received on <strong>21/04/2026</strong>" — take_get_text(strip=True)
            # would concatenate everything; pull the strong tag explicitly.
            uid = None
            dd_tag = dt_tag.find_next_sibling("dd", class_="last")
            if dd_tag:
                strong = dd_tag.find("strong")
                if strong:
                    uid = strong.get_text(strip=True)
                if not uid:
                    text = dd_tag.get_text(strip=True)
                    ref_match = re.search(
                        r"Application reference:\s*([^\s]+)", text, re.I
                    )
                    if ref_match:
                        uid = ref_match.group(1).strip()

            if not uid:
                uid = link.get_text(strip=True)
            if not uid:
                continue

            href = link["href"]
            abs_url = urljoin(base_url, href)
            results.append(ApplicationSummary(uid=uid, url=abs_url))

        # Fallback: also handle atSearchResults div format (other Astun variants)
        if not results:
            for div in soup.select("div.atSearchResults div"):
                link = div.find("a", href=True)
                if not link:
                    continue
                p_tags = div.find_all("p")
                uid = None
                for p in p_tags:
                    text = p.get_text(strip=True)
                    ref_match = re.search(r"Application reference:\s*(.+?)(?:\s+received|$)", text, re.I)
                    if not ref_match:
                        ref_match = re.search(r"Reference:\s*(.+?)(?:\s*\||$)", text, re.I)
                    if ref_match:
                        uid = ref_match.group(1).strip()
                        break
                if not uid:
                    uid = link.get_text(strip=True)
                if uid:
                    abs_url = urljoin(base_url, link["href"])
                    results.append(ApplicationSummary(uid=uid, url=abs_url))

        return results

    def _next_page_url(self, current_url: str, page_num: int) -> str:
        """Build URL for the next results page by replacing pageno= param."""
        if "pageno=" in current_url:
            return re.sub(r"pageno=\d+", f"pageno={page_num}", current_url)
        separator = "&" if "?" in current_url else "?"
        return f"{current_url}{separator}pageno={page_num}"

    @staticmethod
    def _extract_dl_pairs(html: str) -> Dict[str, str]:
        """Walk every dt/dd pair on the page and return a label→value map.

        This sidesteps a soupsieve gotcha: `dt:-soup-contains('Proposal') + dd`
        also matches 'Address Of Proposal:' which would mis-fill description
        with the site address. Walking pairs gives an exact-label lookup."""
        soup = BeautifulSoup(html, "lxml")
        result: Dict[str, str] = {}
        for dt in soup.find_all("dt"):
            label = dt.get_text(" ", strip=True).rstrip(":").strip()
            dd = dt.find_next_sibling("dd")
            if dd:
                result[label] = dd.get_text(" ", strip=True)
        return result

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch full details from multiple template pages."""
        # Main application page
        main_html = await self._client.get_html(application.url)
        main_data = self._parser.extract(main_html, ROCHFORD_DETAIL_SELECTORS)
        # Override description with an exact-label lookup so 'Address Of
        # Proposal' doesn't bleed into it.
        pairs = self._extract_dl_pairs(main_html)
        for k, v in pairs.items():
            kl = k.lower()
            if kl == "proposal" and not main_data.get("description"):
                main_data["description"] = v

        # Dates page
        dates_data = await self._fetch_sub_page(
            application.url, "DevelopmentControlApplication_Dates", ROCHFORD_DATE_SELECTORS
        )

        # Applicant page
        applicant_data = await self._fetch_sub_page(
            application.url, "DevelopmentControlApplication_Applicant", ROCHFORD_APPLICANT_SELECTORS
        )

        # Agent page
        agent_data = await self._fetch_sub_page(
            application.url, "DevelopmentControlApplication_Agent", ROCHFORD_AGENT_SELECTORS
        )

        raw = {}
        for d in (main_data, dates_data, applicant_data, agent_data):
            raw.update({k: v for k, v in d.items() if v is not None})

        return ApplicationDetail(
            reference=main_data.get("reference") or application.uid,
            address=main_data.get("address") or "",
            description=main_data.get("description") or "",
            url=application.url,
            application_type=main_data.get("application_type"),
            status=main_data.get("status"),
            decision=main_data.get("decision"),
            date_received=_parse_date(dates_data.get("date_received")),
            date_validated=_parse_date(dates_data.get("date_validated")),
            ward=main_data.get("ward"),
            parish=main_data.get("parish"),
            applicant_name=applicant_data.get("applicant_name"),
            case_officer=main_data.get("case_officer"),
            raw_data=raw,
        )

    async def _fetch_sub_page(
        self, main_url: str, template_suffix: str, selectors: Dict
    ) -> Dict[str, Optional[str]]:
        """Fetch a related template page by replacing the template name in the URL."""
        sub_url = main_url.replace(
            "DevelopmentControlApplication", template_suffix
        )
        # Only replace the first occurrence (the template name, not the basepage)
        if sub_url == main_url:
            # URL didn't contain the expected template - try building from ref
            sub_url = re.sub(
                r"template=\w+\.tmplt",
                f"template={template_suffix}.tmplt",
                main_url,
            )
        try:
            html = await self._client.get_html(sub_url)
            # Use div.details as the data block if present
            soup = BeautifulSoup(html, "lxml")
            details_div = soup.find("div", class_="details")
            parse_html = str(details_div) if details_div else html
            return self._parser.extract(parse_html, selectors)
        except Exception:
            return {k: None for k in selectors}

    async def fetch_detail_by_uid(self, uid: str) -> Optional[ApplicationDetail]:
        """Look up a single application by reference number."""
        url = (
            f"{BASE_URL}/DevelopmentControl.aspx"
            f"?requesttype=parsetemplate"
            f"&template=DevelopmentControlApplication.tmplt"
            f"&basepage=DevelopmentControl.aspx"
            f"&Filter=^REFVAL^=%27{uid}%27"
        )
        try:
            html = await self._client.get_html(url)
            detail_data = self._parser.extract(html, ROCHFORD_DETAIL_SELECTORS)
            if not detail_data.get("reference") and not detail_data.get("address"):
                return None
            app = ApplicationSummary(uid=uid, url=url)
            return await self.fetch_detail(app)
        except Exception:
            return None
