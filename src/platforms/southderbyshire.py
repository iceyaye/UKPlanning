"""South Derbyshire District Council scraper (Laravel/Livewire portal).

The planning portal at planning.southderbyshire.gov.uk uses Laravel Livewire.
Application data is embedded as JSON in wire:snapshot/wire:effects attributes
on initial page load. Date-filtered searches use the /livewire/update endpoint.
"""
import json
import re
from datetime import date, datetime
from html import unescape
from typing import Dict, List, Optional
from urllib.parse import unquote

import httpx

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

BASE_URL = "https://planning.southderbyshire.gov.uk"
DETAIL_BASE = "https://planning.southderbyshire.gov.uk/application"


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, AttributeError):
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_snapshot_json(html: str) -> Optional[Dict]:
    """Extract the advanced-search wire:snapshot JSON from page HTML."""
    pattern = r'wire:snapshot="([^"]+)"'
    for match in re.finditer(pattern, html):
        raw = unescape(match.group(1))
        try:
            data = json.loads(raw)
            if data.get("memo", {}).get("name") == "advanced-search":
                return data
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def _extract_effects_json(html: str) -> Optional[Dict]:
    """Extract the advanced-search wire:effects JSON from page HTML."""
    pattern = r'wire:effects="([^"]+)"'
    for match in re.finditer(pattern, html):
        raw = unescape(match.group(1))
        try:
            data = json.loads(raw)
            if "dispatches" in data:
                return data
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def _extract_records_from_effects(effects: Dict) -> List[Dict]:
    """Pull application records from the wire:effects dispatches."""
    records = []
    for dispatch in effects.get("dispatches", []):
        if dispatch.get("name") != "updateMap":
            continue
        for param in dispatch.get("params", []):
            if isinstance(param, dict) and "data" in param:
                records.extend(param["data"])
            elif isinstance(param, list):
                for item in param:
                    if isinstance(item, dict) and "data" in item:
                        records.extend(item["data"])
    return records


def _extract_pagination_from_effects(effects: Dict) -> Dict:
    """Pull pagination info from the wire:effects dispatches."""
    for dispatch in effects.get("dispatches", []):
        if dispatch.get("name") != "updateMap":
            continue
        for param in dispatch.get("params", []):
            if isinstance(param, dict) and "last_page" in param:
                return param
            elif isinstance(param, list):
                for item in param:
                    if isinstance(item, dict) and "last_page" in item:
                        return item
    return {}


def _record_to_detail(record: Dict) -> ApplicationDetail:
    """Convert a raw JSON record to ApplicationDetail."""
    sf_id = record.get("salesforce_id", "")
    url_ref = record.get("url_reference", "")
    url = f"{DETAIL_BASE}/{sf_id}/{url_ref}" if sf_id and url_ref else record.get("url", "")

    return ApplicationDetail(
        reference=record.get("Name", ""),
        address=record.get("Site_Address__c", ""),
        description=record.get("Short_Proposal__c", ""),
        url=url,
        application_type=record.get("Type__c"),
        status=record.get("Status__c"),
        decision=record.get("Current_Decision__c") or None,
        date_received=None,
        date_validated=_parse_date(record.get("Validated_Date__c")),
        ward=record.get("Wards__c"),
        parish=record.get("Parishes__c"),
        case_officer=record.get("sddc_officer__c"),
        raw_data=record,
    )


class SouthDerbyshireScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=False,
        )
        self._csrf_token: Optional[str] = None
        self._session_cookies: Dict[str, str] = {}

    async def _init_session(self) -> str:
        """GET the homepage to obtain CSRF token and session cookies. Returns HTML."""
        resp = await self._client.get(f"{BASE_URL}/")
        resp.raise_for_status()
        self._session_cookies = dict(resp.cookies)

        meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
        if meta_match:
            self._csrf_token = meta_match.group(1)

        return resp.text

    async def _livewire_update(self, snapshot: Dict, updates: Dict, calls: List = None) -> Dict:
        """POST to /livewire/update to update component state."""
        xsrf = self._session_cookies.get("XSRF-TOKEN", "")
        if xsrf:
            xsrf = unquote(xsrf)

        payload = {
            "fingerprint": snapshot.get("memo", {}),
            "serverMemo": snapshot.get("memo", {}),
            "updates": updates,
            "calls": calls or [],
            "_token": self._csrf_token,
        }

        # Livewire 3 update format
        snapshot_str = json.dumps(snapshot)
        body = [
            {
                "snapshot": snapshot_str,
                "updates": updates,
                "calls": calls or [],
            }
        ]

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/html, application/xhtml+xml",
            "X-Livewire": "true",
            "X-CSRF-TOKEN": self._csrf_token or "",
            "X-XSRF-TOKEN": xsrf,
        }

        resp = await self._client.post(
            f"{BASE_URL}/livewire/update",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def _search_by_date(self, snapshot: Dict, date_from: date, date_to: date, page: int = 1) -> List[Dict]:
        """Use Livewire update to search by validation date range."""
        updates = {
            "dateType": 1,
            "afterDate": date_from.isoformat(),
            "beforeDate": date_to.isoformat(),
            "perPage": 100,
            "sortBy": "Validation Date",
        }
        if page > 1:
            updates["paginators.page"] = page

        try:
            result = await self._livewire_update(snapshot, updates)
            # Livewire 3 returns array of component responses
            if isinstance(result, list) and result:
                component = result[0]
                effects = component.get("effects", {})
                records = _extract_records_from_effects(effects)
                if records:
                    return records

                # Try extracting from HTML in the response
                html = effects.get("html", "")
                if html:
                    nested_effects = _extract_effects_json(html)
                    if nested_effects:
                        return _extract_records_from_effects(nested_effects)
        except Exception:
            pass

        return []

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        html = await self._init_session()
        effects = _extract_effects_json(html)
        if not effects:
            return []

        records = _extract_records_from_effects(effects)
        summaries = []
        seen = set()
        for record in records:
            validated = _parse_date(record.get("Validated_Date__c"))
            if validated and date_from <= validated <= date_to:
                ref = record.get("Name", "")
                if ref and ref not in seen:
                    seen.add(ref)
                    sf_id = record.get("salesforce_id", "")
                    url_ref = record.get("url_reference", "")
                    url = f"{DETAIL_BASE}/{sf_id}/{url_ref}" if sf_id else ""
                    summaries.append(ApplicationSummary(uid=ref, url=url))
        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        return ApplicationDetail(
            reference=application.uid,
            address="",
            description="",
            url=application.url,
        )

    async def scrape(self, date_from: date, date_to: date) -> ScrapeResult:
        """Full scrape: extract data from initial page, then paginate via Livewire if needed."""
        try:
            html = await self._init_session()
            snapshot = _extract_snapshot_json(html)
            effects = _extract_effects_json(html)

            if not effects:
                return ScrapeResult(date_from=date_from, date_to=date_to, error="No Livewire effects found on page")

            # First try Livewire update for date-filtered search
            if snapshot:
                livewire_records = await self._search_by_date(snapshot, date_from, date_to)
                if livewire_records:
                    details = []
                    seen = set()
                    page = 1

                    while True:
                        for record in livewire_records:
                            ref = record.get("Name", "")
                            if not ref or ref in seen:
                                continue
                            seen.add(ref)
                            details.append(_record_to_detail(record))

                        # Check if we need more pages
                        if len(livewire_records) < 100:
                            break
                        page += 1
                        if page > 50:
                            break
                        livewire_records = await self._search_by_date(snapshot, date_from, date_to, page=page)
                        if not livewire_records:
                            break

                    if details:
                        return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)

            # Fallback: filter records from initial page load
            all_records = _extract_records_from_effects(effects)
            details = []
            seen = set()
            for record in all_records:
                validated = _parse_date(record.get("Validated_Date__c"))
                if validated and date_from <= validated <= date_to:
                    ref = record.get("Name", "")
                    if ref and ref not in seen:
                        seen.add(ref)
                        details.append(_record_to_detail(record))

            # If initial page had results in range, try paginating for more
            pagination = _extract_pagination_from_effects(effects)
            total_pages = pagination.get("last_page", 1)

            if snapshot and total_pages > 1 and len(details) < 25:
                # Paginate through to find more in-range records
                for page_num in range(2, min(total_pages + 1, 100)):
                    updates = {"paginators.page": page_num}
                    try:
                        result = await self._livewire_update(snapshot, updates)
                        if isinstance(result, list) and result:
                            page_effects = result[0].get("effects", {})
                            page_records = _extract_records_from_effects(page_effects)
                            if not page_records:
                                break

                            found_in_range = False
                            for record in page_records:
                                validated = _parse_date(record.get("Validated_Date__c"))
                                if validated and date_from <= validated <= date_to:
                                    ref = record.get("Name", "")
                                    if ref and ref not in seen:
                                        seen.add(ref)
                                        details.append(_record_to_detail(record))
                                        found_in_range = True
                                elif validated and validated < date_from:
                                    # Sorted by validation date desc, so we've gone past our range
                                    break

                            if not found_in_range:
                                break
                    except Exception:
                        break

            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
