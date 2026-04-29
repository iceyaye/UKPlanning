"""Ipswich Borough Council scraper.

Custom legacy ASP system at ppc.ipswich.gov.uk/appnsearch.asp.
POST search with date range, parse results table, fetch detail pages.
"""
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _input_value(soup: BeautifulSoup, field_name: str) -> str:
    el = soup.find("input", {"name": field_name})
    if el:
        return el.get("value", "").strip()
    el = soup.find("textarea", {"name": field_name})
    if el:
        return el.get_text(strip=True)
    return ""


class IpswichScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base_url = config.base_url.rstrip("/")
        self._search_url = f"{self._base_url}/appnresults.asp"
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=False,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        # Dates are exclusive, so widen the range by 1 day each side
        search_from = date_from - timedelta(days=1)
        search_to = date_to + timedelta(days=1)

        resp = await self._client.post(self._search_url, data={
            "txtValStartDate": search_from.strftime("%d/%m/%Y"),
            "txtValEndDate": search_to.strftime("%d/%m/%Y"),
            "sType": "APP",
        })
        resp.raise_for_status()

        results = self._parse_results(resp.text)
        results = await self._paginate(resp.text, results)
        return results

    async def _paginate(self, html: str, results: List[ApplicationSummary]) -> List[ApplicationSummary]:
        max_pages = 100
        current_html = html
        for _ in range(max_pages):
            soup = BeautifulSoup(current_html, "html.parser")
            next_link = soup.find("img", {"alt": "Next Page"})
            if not next_link:
                next_link = soup.find("img", {"title": "Next Page"})
            if not next_link:
                break

            parent_a = next_link.find_parent("a", href=True)
            if not parent_a:
                break

            next_url = parent_a["href"]
            if not next_url.startswith("http"):
                next_url = urljoin(self._base_url + "/", next_url)

            resp = await self._client.get(next_url)
            resp.raise_for_status()
            current_html = resp.text
            page_results = self._parse_results(current_html)
            if not page_results:
                break
            results.extend(page_results)

        return results

    def _parse_results(self, html: str) -> List[ApplicationSummary]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", {"id": "dgSearchResults"})
        if not table:
            return []

        summaries = []
        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all("td")
            if not cells:
                continue

            uid = cells[0].get_text(strip=True)
            if not uid:
                continue

            link = row.find("a", href=True)
            url = ""
            if link:
                href = link["href"]
                if not href.startswith("http"):
                    url = urljoin(self._base_url + "/", href)
                else:
                    url = href
            else:
                url = f"{self._base_url}/appndetails.asp?iAppID={uid}"

            summaries.append(ApplicationSummary(uid=uid, url=url))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url
        if not url:
            url = f"{self._base_url}/appndetails.asp?iAppID={application.uid}"

        resp = await self._client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        reference = _input_value(soup, "txtAppNo") or application.uid
        address = _input_value(soup, "txtAddress")
        description = _input_value(soup, "txtProposal")
        date_received = _parse_date(_input_value(soup, "txtAppRec"))
        date_validated = _parse_date(_input_value(soup, "txtAppVal"))
        ward = _input_value(soup, "txtWard")
        parish = _input_value(soup, "txtParish")
        case_officer = _input_value(soup, "txtCaseOfficer")
        application_type = _input_value(soup, "txtAppType")
        status = _input_value(soup, "txtStatus")
        applicant_name = _input_value(soup, "txtApplicantName")
        agent_name = _input_value(soup, "txtAgentName")
        decision_date_str = _input_value(soup, "txtDecIssued")

        raw_data = {}
        if agent_name:
            raw_data["agent_name"] = agent_name
        if decision_date_str:
            raw_data["decision_date"] = decision_date_str

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            application_type=application_type,
            status=status,
            date_received=date_received,
            date_validated=date_validated,
            ward=ward,
            parish=parish,
            applicant_name=applicant_name,
            case_officer=case_officer,
            raw_data=raw_data,
        )
