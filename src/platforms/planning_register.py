"""Planning Register platform scraper (planning-register.co.uk and similar).

Used by ~9 UK councils. The search form requires reCAPTCHA, but the
/Search/Standard endpoint (used by weekly lists) accepts date parameters
directly without captcha.

We use /Search/Standard with AcknowledgeLetterDateFrom/To for date-based search,
then fetch /Planning/Display/{reference} for detail.
"""
import re
import ssl
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote

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
    "suffolk": "https://suffolk.planning-register.co.uk",
    "leicestershire": "https://leicestershire.planning-register.co.uk",
    "southwestdevon": "https://westdevon.planning-register.co.uk",
    "surrey": "https://planning.surreycc.gov.uk",
    "northamptonshire": "https://wnc.planning-register.co.uk",
    "worcestershire": "https://worcestershire.planning-register.co.uk",
    "hampshire": "https://planning.hants.gov.uk",
    "northwarwickshire": "https://planning.northwarks.gov.uk",
    "southoxfordshire": "https://southoxfordshire.planning-register.co.uk",
    "whitehorse": "https://valeofwhitehorse.planning-register.co.uk",
}


def _parse_date_str(s: str) -> Optional[date]:
    """Parse DD/MM/YYYY date string."""
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
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
        """Search via /Search/Standard with date parameters (no captcha needed)."""
        await self._accept_disclaimer()

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
        base_search_url = str(resp.url).split("&page=")[0]
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
                ref = ref_match.group(1) if ref_match else href

                full_url = f"{self._base_url}{href}" if href.startswith("/") else href
                summaries.append(ApplicationSummary(uid=ref, url=full_url))

            # Check for next page
            if page_count == 0:
                break

            next_link = soup.find("a", string=re.compile(r"^\s*Next\s*$"))
            if not next_link:
                break

            next_href = next_link.get("data-ajax-target") or next_link.get("href", "")
            if not next_href or next_href == "#":
                break

            next_url = f"{self._base_url}{next_href}" if next_href.startswith("/") else next_href
            page_num += 1
            resp = await self._client.get(
                next_url, headers={"X-Requested-With": "XMLHttpRequest"}
            )
            resp.raise_for_status()

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch application detail page."""
        await self._accept_disclaimer()

        resp = await self._client.get(application.url)
        resp.raise_for_status()

        text = resp.text
        detail = {
            "reference": application.uid,
            "address": "",
            "description": "",
        }

        # Extract fields by label pattern: "Label  Value" in the text
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

        # Parse using the page text
        page_text = BeautifulSoup(text, "html.parser").get_text()
        for field, pattern in field_patterns.items():
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
            applicant_name=None,
            case_officer=detail.get("case_officer"),
        )
