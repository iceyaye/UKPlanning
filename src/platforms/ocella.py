"""Ocella platform scraper (Arun, Great Yarmouth, Hillingdon, South Holland, Havering).

POST to /OcellaWeb/planningSearch with DD-MM-YY date format.
Results in HTML table with Reference, Location, Proposal, Status.
Detail pages at /OcellaWeb/planningDetails?reference={ref}.
"""
import re
from datetime import date, datetime
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

COUNCIL_URLS = {
    "arun": "https://www1.arun.gov.uk/aplanning/OcellaWeb",
    "greatyarmouth": "https://planning.great-yarmouth.gov.uk/OcellaWeb",
    "hillingdon": "https://planning.hillingdon.gov.uk/OcellaWeb",
    "southholland": "https://planning.sholland.gov.uk/OcellaWeb",
    "havering": "https://development.havering.gov.uk/OcellaWeb",
    "bridgend": "https://planning.bridgend.gov.uk/OcellaWeb",
}


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d-%m-%y", "%d-%m-%Y", "%d/%m/%Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


class OcellaScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base_url = COUNCIL_URLS.get(
            config.authority_code, config.base_url.rstrip("/")
        )
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=False,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        resp = await self._client.post(f"{self._base_url}/planningSearch", data={
            "receivedFrom": date_from.strftime("%d-%m-%y"),
            "receivedTo": date_to.strftime("%d-%m-%y"),
            "action": "Search",
        })
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        summaries = []

        for link in soup.find_all("a", href=re.compile(r"planningDetails\?reference=")):
            href = link.get("href", "")
            ref = link.get_text(strip=True)
            full_url = f"{self._base_url}/{href}" if not href.startswith("http") else href
            if ref:
                summaries.append(ApplicationSummary(uid=ref, url=full_url))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        resp = await self._client.get(application.url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        fields = {}

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True)
                    value = cells[1].get_text(strip=True)
                    if label:
                        fields[label] = value

        return ApplicationDetail(
            reference=fields.get("Reference", application.uid),
            address=fields.get("Location", ""),
            description=fields.get("Proposal", ""),
            url=application.url,
            status=fields.get("Status"),
            date_received=_parse_date(fields.get("Received", "")),
            date_validated=_parse_date(fields.get("Validated", "")),
            ward=fields.get("Ward"),
            parish=fields.get("Parish"),
            applicant_name=fields.get("Applicant"),
            case_officer=fields.get("Case Officer"),
        )
