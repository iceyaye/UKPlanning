"""Elmbridge Borough Council planning scraper.

Astun iShare/PublisherCMS GIS system. GET requests with query parameters,
HTML table results, detail pages with dt/dd pairs.
"""
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

BASE_URL = "https://emaps.elmbridge.gov.uk/ebc_planning.aspx"


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _detail_url(ref: str) -> str:
    encoded = quote(ref, safe="")
    return (
        f"{BASE_URL}?requesttype=parsetemplate"
        f"&template=PlanningDetailsTab.tmplt"
        f"&basepage=ebc_planning.aspx"
        f"&Filter=^APPLICATION_NUMBER^=%27{encoded}%27"
        f"&appno:PARAM={encoded}"
    )


class ElmbridgeScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._base_url = config.base_url or BASE_URL
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=False,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        url = (
            f"{self._base_url}"
            f"?requestType=parseTemplate"
            f"&template=AdvancedSearchResultsTab.tmplt"
            f"&pageno=1"
            f"&daterec_from:PARAM={date_from.strftime('%Y-%m-%d')}"
            f"&daterec_to:PARAM={date_to.strftime('%Y-%m-%d')}"
            f"&SearchType:PARAM=Advanced"
            f"&orderxyz:PARAM=REG_DATE_DT:DESCENDING"
            f"&pagerecs=2000"
        )
        resp = await self._client.get(url)
        resp.raise_for_status()
        return self._parse_results(resp.text)

    def _parse_results(self, html: str) -> List[ApplicationSummary]:
        soup = BeautifulSoup(html, "html.parser")
        summaries = []

        for table in soup.find_all("table"):
            header_text = table.get_text(" ", strip=True)[:200].lower()
            if not any(kw in header_text for kw in ["search results", "reference", "application number", "application"]):
                continue

            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                ref = cells[0].get_text(strip=True)
                if not ref or ref.lower() == "reference":
                    continue
                # Skip rows that don't look like planning references
                if not re.search(r'\d{4}/', ref):
                    continue

                link = row.find("a", href=True)
                url = link["href"] if link else _detail_url(ref)

                address = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                summary = ApplicationSummary(uid=ref, url=url)
                summary._address = address
                summaries.append(summary)

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        url = application.url or _detail_url(application.uid)
        # Ensure absolute URL
        if url.startswith("/"):
            url = f"https://emaps.elmbridge.gov.uk{url}"

        resp = await self._client.get(url)
        resp.raise_for_status()

        fields = self._parse_dt_dd(resp.text)
        address = getattr(application, "_address", "") or fields.get("address", "") or fields.get("site address", "")

        return ApplicationDetail(
            reference=application.uid,
            address=address,
            description=fields.get("description", "") or fields.get("proposal", ""),
            url=url,
            application_type=fields.get("application type", ""),
            status=fields.get("status", ""),
            decision=fields.get("decision", "") or fields.get("decision type", "") or None,
            date_received=_parse_date(fields.get("date received", "") or fields.get("registered", "")),
            date_validated=_parse_date(fields.get("date validated", "") or fields.get("valid from", "")),
            ward=fields.get("ward", ""),
            parish=fields.get("parish", ""),
            case_officer=fields.get("case officer", "") or fields.get("officer", ""),
            raw_data=fields,
        )

    def _parse_dt_dd(self, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        fields = {}
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                key = dt.get_text(strip=True).lower().rstrip(":")
                val = dd.get_text(strip=True)
                if key and val:
                    fields[key] = val
        return fields
