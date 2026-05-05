"""West Dunbartonshire Council planning portal scraper.

Classic ASP application at apps.west-dunbarton.gov.uk.
POST search form 'publicdisplay' with date range fields (vDateRcvFr/vDateRcvTo),
results return UIDs in a table. Detail pages at dcdisplayfull.asp with table rows.
"""
import re
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

BASE_URL = "https://apps.west-dunbarton.gov.uk"
SEARCH_URL = f"{BASE_URL}/dcsearch_appx.asp"
RESULTS_URL = f"{BASE_URL}/dcdisplayinitialx.asp"
DETAIL_URL = f"{BASE_URL}/dcdisplayfull.asp"


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%d %B %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _detail_url(uid: str) -> str:
    return f"{DETAIL_URL}?vPassword=&View1=View&vUPRN={quote_plus(uid)}"


def _extract_table_pairs(soup: BeautifulSoup) -> dict:
    """Extract label/value pairs from two-column table rows.

    Labels include the trailing colon (e.g. `'Reference Number:'`) which would
    miss every `data.get('Reference Number')` lookup downstream. Strip them.
    """
    pairs = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            label = tds[0].get_text(" ", strip=True).rstrip(":").strip()
            value = tds[1].get_text(" ", strip=True)
            if label and value:
                pairs[label] = value
    return pairs


class WestDunbartonScraper(BaseScraper):
    """Scraper for West Dunbartonshire's classic ASP planning portal."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=ctx,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        # GET the search form to seed the session and confirm field defaults
        search_resp = await self._client.get(SEARCH_URL)
        # The site is windows-1252 encoded; decode bytes explicitly so the
        # parser doesn't choke on the GBP/special-char bytes.
        form_html = search_resp.content.decode("windows-1252", errors="replace")
        soup = BeautifulSoup(form_html, "html.parser")
        form = soup.find("form")
        form_data = {}
        if form:
            for inp in form.find_all("input"):
                name = inp.get("name", "")
                if not name:
                    continue
                if (inp.get("type") or "").lower() == "submit":
                    form_data[name] = inp.get("value", "Search")
                else:
                    form_data[name] = inp.get("value", "") or ""
            for sel in form.find_all("select"):
                name = sel.get("name", "")
                if name:
                    form_data[name] = ""
        form_data["vDateRcvFr"] = date_from.strftime("%d/%m/%Y")
        form_data["vDateRcvTo"] = date_to.strftime("%d/%m/%Y")
        # The form is a GET (method=get on the <form> element). POST silently
        # returns the unfiltered list of ~100 random recent applications.
        resp = await self._client.get(RESULTS_URL, params=form_data)
        resp.raise_for_status()
        body = resp.content.decode("windows-1252", errors="replace")

        soup = BeautifulSoup(body, "html.parser")
        summaries = []
        seen = set()

        for link in soup.find_all("a", href=re.compile(r"dcdisplayfull", re.I)):
            href = link.get("href", "")
            uid = link.get_text(strip=True)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
            summaries.append(ApplicationSummary(uid=uid, url=full_url))

        if not summaries:
            for td in soup.find_all("td"):
                text = td.get_text(strip=True)
                if re.match(r"DC\d{2}/\d+", text) and text not in seen:
                    seen.add(text)
                    summaries.append(ApplicationSummary(
                        uid=text, url=_detail_url(text),
                    ))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url or _detail_url(application.uid)
        resp = await self._client.get(url)
        resp.raise_for_status()

        body = resp.content.decode("windows-1252", errors="replace")
        soup = BeautifulSoup(body, "html.parser")
        data = _extract_table_pairs(soup)

        return ApplicationDetail(
            reference=data.get("Reference Number", application.uid),
            address=data.get("Address of Proposal", data.get("Address", "")),
            description=data.get("Proposal", ""),
            url=url,
            application_type=data.get("Type of Application"),
            status=data.get("Status"),
            decision=data.get("Decision Date") if data.get("Status") in ("Approved", "Refused") else None,
            date_received=_parse_date(data.get("Date Received")),
            date_validated=_parse_date(data.get("Date Valid")),
            ward=data.get("Ward"),
            parish=data.get("Community Council"),
            applicant_name=data.get("Applicant Name"),
            case_officer=data.get("Officer"),
            raw_data=data,
        )
