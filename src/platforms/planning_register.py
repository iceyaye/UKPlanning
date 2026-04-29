"""Planning Register platform scraper (planning-register.co.uk and similar).

Used by ~30 UK councils. Two search approaches are supported:

1. /Search/Standard (GET) — older weekly-list endpoint that accepts date params
   directly without captcha. Used by most councils.
2. /Search/Results (POST) — newer AJAX endpoint used by councils that lack
   /Search/Standard (e.g. South Oxfordshire, Vale of White Horse). Requires
   accepting a disclaimer and submitting a form with a CSRF token. The reCAPTCHA
   key is empty on these sites so no captcha solve is needed.

Detail pages are fetched from /Planning/Display?applicationNumber=...
"""
import re
import ssl
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote, unquote

import httpx
from bs4 import BeautifulSoup


def _make_ssl_context():
    """Create an SSL context that works with older/stricter servers."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

# Map authority_code -> base URL
COUNCIL_URLS = {
    "daventry": "https://wnc.planning-register.co.uk",
    "northampton": "https://wnc.planning-register.co.uk",
    "southnorthamptonshire": "https://wnc.planning-register.co.uk",
    "suffolk": "https://suffolk.planning-register.co.uk",
    "westsussex": "https://westsussex.planning-register.co.uk",
    "barrow": "https://planningregister.westmorlandandfurness.gov.uk",
    "eden": "https://planningregister.westmorlandandfurness.gov.uk",
    "southlakeland": "https://planningregister.westmorlandandfurness.gov.uk",
    "crawley": "https://planningregister.crawley.gov.uk",
    "lancashire": "https://planningregister.lancashire.gov.uk",
    "redcar": "https://planning.redcar-cleveland.gov.uk",
    "wychavon": "https://plan.wychavon.gov.uk",
    "kent": "https://www.kentplanningapplications.co.uk",
    "exmoor": "https://exmoor.planning-register.co.uk",
    "devon": "https://planning.devon.gov.uk",
    "cherwell": "https://planningregister.cherwell.gov.uk",
    "fylde": "https://pa.fylde.gov.uk",
    "malvernhills": "https://plan.malvernhills.gov.uk",
    "norfolk": "https://eplanning.norfolk.gov.uk",
    "northdevon": "https://planning.northdevon.gov.uk",
    "welwynhatfield": "https://planning.welhat.gov.uk",
    "worcester": "https://plan.worcester.gov.uk",
    "leicestershire": "https://leicestershire.planning-register.co.uk",
    "southwestdevon": "https://westdevon.planning-register.co.uk",
    "surrey": "https://planning.surreycc.gov.uk",
    "northamptonshire": "https://wnc.planning-register.co.uk",
    "worcestershire": "https://worcestershire.planning-register.co.uk",
    "hampshire": "https://planning.hants.gov.uk",
    "northwarwickshire": "https://planning.northwarks.gov.uk",
    "southoxfordshire": "https://southoxfordshire.planning-register.co.uk",
    "whitehorse": "https://valeofwhitehorse.planning-register.co.uk",
    "bridgend": "https://planning.bridgend.gov.uk",
}

# Councils that lack /Search/Standard and must use POST /Search/Results
POST_SEARCH_COUNCILS = {
    "southoxfordshire",
    "whitehorse",
}


def _parse_date_str(s: str) -> Optional[date]:
    """Parse date strings in various formats."""
    if not s:
        return None
    s = s.strip()
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"]:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class PlanningRegisterScraper(BaseScraper):
    """Scraper for planning-register.co.uk and similar ASP.NET planning registers."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base_url = COUNCIL_URLS.get(
            config.authority_code, config.base_url.rstrip("/")
        )
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=_make_ssl_context(),
        )
        self._disclaimer_accepted = False

    async def _accept_disclaimer(self):
        """Accept the site disclaimer to get a session cookie."""
        if self._disclaimer_accepted:
            return
        resp = await self._client.get(
            f"{self._base_url}/Disclaimer?returnUrl=%2FSearch%2FAdvanced"
        )
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", action=lambda a: a and "Disclaimer" in a)
        if form:
            action = form.get("action", "")
            post_url = f"{self._base_url}{action}" if action.startswith("/") else action
            form_data = {}
            for inp in form.find_all("input", {"type": "hidden"}):
                name = inp.get("name", "")
                if name:
                    form_data[name] = inp.get("value", "")
            await self._client.post(post_url, data=form_data)
        else:
            # Fallback: try known endpoints
            await self._client.post(
                f"{self._base_url}/Disclaimer/Accept?returnUrl=%2FSearch%2FAdvanced"
            )
        self._disclaimer_accepted = True

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search for applications in a date range.

        Uses POST /Search/Results for councils in POST_SEARCH_COUNCILS,
        otherwise falls back to GET /Search/Standard.
        """
        await self._accept_disclaimer()

        if self.config.authority_code in POST_SEARCH_COUNCILS:
            return await self._gather_ids_post(date_from, date_to)
        return await self._gather_ids_standard(date_from, date_to)

    async def _gather_ids_post(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search via POST /Search/Results (AJAX form submission)."""
        # Get the CSRF token from the search page
        resp = await self._client.get(f"{self._base_url}/Search/Advanced")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        token_el = soup.find("input", {"name": "__RequestVerificationToken"})
        token = token_el.get("value", "") if token_el else ""

        # Try date field pairs in order of preference
        date_fields = [
            ("DateReceivedFrom", "DateReceivedTo"),
            ("DateValidFrom", "DateValidTo"),
            ("DateIssuedFrom", "DateIssuedTo"),
        ]

        for from_field, to_field in date_fields:
            form_data = {
                "Recaptcha.Response": "",
                "Recaptcha.Key": "",
                "SearchPlanning": "true",
                "SearchEnforcement": "false",
                "SearchBuildingControl": "false",
                "SearchTreePreservationOrders": "false",
                from_field: date_from.isoformat(),
                to_field: date_to.isoformat(),
                "AppealsSearch": "false",
                "ExcludeDecidedApps": "false",
                "__RequestVerificationToken": token,
            }
            resp = await self._client.post(
                f"{self._base_url}/Search/Results",
                data=form_data,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if resp.status_code == 500:
                continue
            resp.raise_for_status()
            if "/Planning/Display" in resp.text:
                break

        summaries = []
        seen = set()
        page_num = 1
        max_pages = 50

        while resp and page_num <= max_pages:
            soup = BeautifulSoup(resp.text, "html.parser")
            page_count = 0
            for link in soup.find_all("a", href=re.compile(r"/Planning/Display")):
                href = link.get("href", "")
                if href in seen:
                    continue
                seen.add(href)
                page_count += 1

                ref_match = re.search(
                    r"/Planning/Display[/?](?:applicationNumber=)?(.+)$", href
                )
                ref = unquote(ref_match.group(1)) if ref_match else unquote(href)
                full_url = f"{self._base_url}{href}" if href.startswith("/") else href
                summaries.append(ApplicationSummary(uid=ref, url=full_url))

            if page_count == 0:
                break

            next_href = self._find_next_page(soup, page_num)
            if not next_href:
                break

            next_url = (
                f"{self._base_url}{next_href}"
                if next_href.startswith("/")
                else next_href
            )
            page_num += 1
            resp = await self._client.get(
                next_url, headers={"X-Requested-With": "XMLHttpRequest"}
            )
            resp.raise_for_status()

        return summaries

    async def _gather_ids_standard(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search via GET /Search/Standard with date query parameters."""
        # Format dates as MM/DD/YYYY HH:MM:SS (US format as used by the weekly list URLs)
        from_str = quote(f"{date_from.month:02d}/{date_from.day:02d}/{date_from.year} 00:00:00")
        to_str = quote(f"{date_to.month:02d}/{date_to.day:02d}/{date_to.year} 00:00:00")

        # Try multiple date parameter names — different councils use different field names
        date_params = [
            ("AcknowledgeLetterDateFrom", "AcknowledgeLetterDateTo"),
            ("DateReceivedFrom", "DateReceivedTo"),
            ("DateValidFrom", "DateValidTo"),
            ("DateDeterminedFrom", "DateDeterminedTo"),
        ]

        resp = None
        for from_param, to_param in date_params:
            url = (
                f"{self._base_url}/Search/Standard"
                f"?SearchType=Planning"
                f"&{from_param}={from_str}&{to_param}={to_str}"
            )
            resp = await self._client.get(url)
            resp.raise_for_status()
            if "/Planning/Display/" in resp.text:
                break  # Found results

        summaries = []
        seen = set()
        page_num = 1
        max_pages = 50  # Safety limit

        while resp and page_num <= max_pages:
            soup = BeautifulSoup(resp.text, "html.parser")

            # Find all detail links regardless of page structure (table or div)
            page_count = 0
            for link in soup.find_all("a", href=re.compile(r"/Planning/Display")):
                href = link.get("href", "")
                if href in seen:
                    continue
                seen.add(href)
                page_count += 1

                ref_match = re.search(r"/Planning/Display[/?](?:applicationNumber=)?(.+)$", href)
                ref = unquote(ref_match.group(1)) if ref_match else unquote(href)

                full_url = f"{self._base_url}{href}" if href.startswith("/") else href
                summaries.append(ApplicationSummary(uid=ref, url=full_url))

            # Check for next page
            if page_count == 0:
                break

            next_href = self._find_next_page(soup, page_num)
            if not next_href:
                break

            next_url = f"{self._base_url}{next_href}" if next_href.startswith("/") else next_href
            page_num += 1
            resp = await self._client.get(
                next_url, headers={"X-Requested-With": "XMLHttpRequest"}
            )
            resp.raise_for_status()

        return summaries

    @staticmethod
    def _find_next_page(soup, current_page: int) -> Optional[str]:
        """Find the URL for the next page of results."""
        # Pattern 1: "Next" text link
        next_link = soup.find("a", string=re.compile(r"^\s*Next\s*$"))
        if next_link:
            return next_link.get("data-ajax-target") or next_link.get("href") or None

        # Pattern 2: » (right double angle) link
        next_link = soup.find("a", string=re.compile(r"^\s*[»›]\s*$"))
        if next_link:
            return next_link.get("data-ajax-target") or next_link.get("href") or None

        for pager in soup.find_all(["ul", "div"], class_=re.compile(r"ajax-pager|pager|pagination")):
            # Pattern 3: numbered page link on <a>
            page_link = pager.find("a", string=str(current_page + 1))
            if page_link:
                href = page_link.get("data-ajax-target") or page_link.get("href")
                if href and href != "#":
                    return href

            # Pattern 4: data-ajax-target on <span> inside pager (chevron-based pagers)
            page_span = pager.find("span", string=str(current_page + 1))
            if page_span:
                target = page_span.get("data-ajax-target")
                if target:
                    return target

        return None

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch application detail page."""
        await self._accept_disclaimer()

        resp = await self._client.get(application.url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        detail = {
            "reference": application.uid,
            "address": "",
            "description": "",
        }

        # Strategy 1: extract from <dl>/<dt>/<dd> pairs (newer pages)
        dt_dd_map = self._extract_dt_dd(soup)
        if dt_dd_map:
            detail["status"] = dt_dd_map.get("Status", "")
            detail["case_officer"] = dt_dd_map.get("Case Officer", "")
            detail["applicant_name"] = dt_dd_map.get("Applicant Name", "")
            detail["decision"] = dt_dd_map.get("Decision", "")
            detail["ward"] = dt_dd_map.get("Ward", "")
            detail["parish"] = dt_dd_map.get("Parish", "")
            detail["date_received"] = (
                dt_dd_map.get("Application Received Date", "")
                or dt_dd_map.get("Received Date", "")
            )
            detail["date_validated"] = (
                dt_dd_map.get("Valid Date", "")
                or dt_dd_map.get("Validated Date", "")
            )

        # Strategy 2: extract ref/address/description from headings
        # Format: <h1>AppType RefNumber</h1> <h2>Address</h2> <h3>Description</h3>
        # Find the h1 containing an application reference pattern (e.g. P26/S0713/HH)
        h1 = None
        for candidate in soup.find_all("h1"):
            if re.search(r"\S+/\S+", candidate.get_text()):
                h1 = candidate
                break
        if h1:
            h1_text = h1.get_text().strip()
            ref_match = re.search(r"(\S+/\S+)$", h1_text)
            if ref_match:
                detail["reference"] = ref_match.group(1)
                detail["application_type"] = h1_text[: ref_match.start()].strip()
            # Address is the next h2 sibling, description the next h3.fs-4
            parent = h1.parent
            if parent:
                h2 = parent.find("h2")
                if h2:
                    detail["address"] = h2.get_text().strip()
                h3 = parent.find("h3", class_="fs-4")
                if h3:
                    detail["description"] = h3.get_text().strip()

        # Strategy 3: fallback regex on page text (older pages)
        if not detail.get("address") or not detail.get("description"):
            page_text = soup.get_text()
            field_patterns = {
                "reference": r"Application Number\s+(.+?)(?=\n|Application Type)",
                "application_type": r"Application Type\s+(.+?)(?=\n|Status)",
                "status": r"Status\s+(.+?)(?=\n|Decision Level)",
                "case_officer": r"Case Officer\s+(.+?)(?=\n|Location)",
                "address": r"Location\s+(.+?)(?=\n|Proposal)",
                "description": r"Proposal\s+(.+?)(?=\n|Parish)",
                "parish": r"Parish\s+(.+?)(?=\n|Ward)",
                "ward": r"Ward\s+(.+?)(?=\n|Received)",
                "date_received": r"Received Date\s+(.+?)(?=\n|Valid)",
                "date_validated": r"Valid Date\s+(.+?)(?=\n|Weekly)",
                "decision": r"Decision\s+(.+?)(?=\n|Decision Issued)",
            }
            for field, pattern in field_patterns.items():
                if not detail.get(field):
                    match = re.search(pattern, page_text)
                    if match:
                        detail[field] = match.group(1).strip()

        return ApplicationDetail(
            reference=detail.get("reference", application.uid),
            address=detail.get("address", ""),
            description=detail.get("description", ""),
            url=application.url,
            application_type=detail.get("application_type"),
            status=detail.get("status"),
            decision=detail.get("decision") if detail.get("decision") else None,
            date_received=_parse_date_str(detail.get("date_received", "")),
            date_validated=_parse_date_str(detail.get("date_validated", "")),
            ward=detail.get("ward"),
            parish=detail.get("parish"),
            applicant_name=detail.get("applicant_name"),
            case_officer=detail.get("case_officer"),
        )

    @staticmethod
    def _extract_dt_dd(soup) -> dict:
        """Extract label/value pairs from <dl>/<dt>/<dd> elements."""
        result = {}
        for dl in soup.find_all("dl"):
            for dt in dl.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                if not dd:
                    continue
                label = dt.get_text().strip()
                value = dd.get_text().strip()
                if label and value and value != "N/A":
                    result[label] = value
        return result
