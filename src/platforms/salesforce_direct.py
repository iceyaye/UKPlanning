"""Salesforce Direct Apex scraper.

Used by councils that expose planning data via direct Apex controller calls
(not the Arcus managed package). Two patterns exist:

Pattern A (PR_SearchCont): Anglesey, Carmarthenshire, Wiltshire
  - descriptor: apex://PR_SearchCont/ACTION$baseQuery
  - category search with searchable_resources + category_name

Pattern B (LCPublicRegCont): Eastleigh
  - descriptor: apex://LCPublicRegCont/ACTION$advancedSearch
  - date-range search with dateRecFrom/dateRecTo
"""
import json
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlencode

import httpx

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

# (base_url, path_prefix, mode, searchable_resources)
# mode: "pr_search" or "lc_public_reg"
COUNCIL_CONFIG = {
    "anglesey": (
        "https://ioacc.my.site.com",
        "",
        "pr_search",
        "be_searchables",
    ),
    "carmarthenshire": (
        "https://carmarthenshire.my.site.com",
        "/en",
        "pr_search",
        "be_searchables_CARM",
    ),
    "wiltshire": (
        "https://development.wiltshire.gov.uk",
        "/pr",
        "pr_search",
        "regserv_searchables,be_searchables,dev_searchables",
    ),
    "eastleigh": (
        "https://planning.eastleigh.gov.uk",
        "",
        "lc_public_reg",
        "",
    ),
}


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, AttributeError):
        return None


class SalesforceDirectScraper(BaseScraper):
    """Scraper for Salesforce councils using direct Apex controllers."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        cfg = COUNCIL_CONFIG.get(config.authority_code)
        if cfg:
            self._base_url = cfg[0]
            self._path_prefix = cfg[1]
            self._mode = cfg[2]
            self._searchable_resources = cfg[3]
        else:
            self._base_url = config.base_url.rstrip("/")
            self._path_prefix = ""
            self._mode = "pr_search"
            self._searchable_resources = "be_searchables"

        self._aura_url = f"{self._base_url}{self._path_prefix}/s/sfsites/aura"
        self._fwuid = None
        self._app_version = None
        self._app_name = "siteforce:communityApp"
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            follow_redirects=True,
            timeout=30,
            verify=False,
        )

    async def _init_aura_context(self):
        """Load the page to extract fwuid and app version for Aura calls."""
        if self._fwuid:
            return

        for path in [
            f"{self._path_prefix}/s/register-view",
            f"{self._path_prefix}/s/be-register-view",
            f"{self._path_prefix}/s/public-register",
            f"{self._path_prefix}/s/pr-english",
            f"{self._path_prefix}/s/",
            "/s/register-view",
            "/s/",
        ]:
            try:
                resp = await self._client.get(f"{self._base_url}{path}")
                if "fwuid" in resp.text:
                    break
            except Exception:
                continue
        else:
            resp = await self._client.get(
                f"{self._base_url}{self._path_prefix}/s/"
            )

        if "fwuid" not in resp.text:
            resp.raise_for_status()

        url_match = re.search(r'/sfsites/l/([^/]+)/bootstrap', resp.text)
        if url_match:
            try:
                decoded = unquote(url_match.group(1))
                bootstrap_cfg = json.loads(decoded)
                self._fwuid = bootstrap_cfg.get("fwuid", "")
                loaded = bootstrap_cfg.get("loaded", {})
                for key in loaded:
                    if key.startswith("APPLICATION@markup://siteforce:"):
                        self._app_name = key.replace("APPLICATION@markup://", "")
                        self._app_version = loaded[key]
                        break
            except (json.JSONDecodeError, KeyError):
                pass

        if not self._fwuid:
            fwuid_match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', resp.text)
            if fwuid_match:
                self._fwuid = fwuid_match.group(1)

        if not self._fwuid:
            raise RuntimeError("Could not extract Aura fwuid from page")

    async def _aura_call(self, controller: str, method: str, params: dict) -> dict:
        """Make a direct Apex Aura call (not via ApexActionController)."""
        await self._init_aura_context()

        message = {
            "actions": [{
                "id": "1;a",
                "descriptor": f"apex://{controller}/ACTION${method}",
                "callingDescriptor": f"markup://c:{controller.replace('Cont', '')}",
                "params": params,
            }],
        }

        context = {
            "mode": "PROD",
            "fwuid": self._fwuid,
            "app": self._app_name,
            "loaded": {
                f"APPLICATION@markup://{self._app_name}": self._app_version or "",
            },
            "dn": [],
            "globals": {"srcdoc": True},
            "uad": True,
        }

        data = urlencode({
            "message": json.dumps(message),
            "aura.context": json.dumps(context),
            "aura.token": "null",
        })

        query_key = f"other.{controller}.{method}"
        resp = await self._client.post(
            f"{self._aura_url}?r=1&{query_key}=1",
            content=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        resp.raise_for_status()
        result = resp.json()

        actions = result.get("actions", [])
        if not actions:
            return {}

        action = actions[0]
        if action.get("state") == "ERROR":
            errors = action.get("error", [])
            msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
            raise RuntimeError(f"Aura error: {msg}")

        return action.get("returnValue", {})

    def _extract_records(self, result) -> List[dict]:
        """Extract records from various Aura return formats."""
        # String result — JSON-encoded (Anglesey pattern)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                return []

        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            # Direct records list
            if "records" in result:
                return result["records"]
            # Nested returnValue
            rv = result.get("returnValue", result)
            if isinstance(rv, str):
                try:
                    rv = json.loads(rv)
                except json.JSONDecodeError:
                    return []
            if isinstance(rv, dict) and "records" in rv:
                return rv["records"]
            if isinstance(rv, list):
                return rv
            # Try first dict value that's a list
            for v in result.values():
                if isinstance(v, list):
                    return v

        return []

    async def _search_pr(self, date_from: date, date_to: date) -> List[dict]:
        """Pattern A: PR_SearchCont — try baseQuery categories then keyword search."""
        # Try category-based search first
        for category in ["PApps7Days", "PApplicationDecided7Days", "PApplicationMajor"]:
            try:
                result = await self._aura_call("PR_SearchCont", "baseQuery", {
                    "searchable_resources": self._searchable_resources,
                    "category_name": category,
                })
                records = self._extract_records(result)
                if records:
                    return records
            except RuntimeError:
                continue

        # Fallback: keyword search by year
        all_records = []
        seen_ids = set()
        years = {date_from.year, date_to.year}
        for year in sorted(years):
            yy = f"{year % 100:02d}"
            for term in [f"/{yy}", str(year), f"{yy}/"]:
                try:
                    result = await self._aura_call("PR_SearchCont", "search", {
                        "searchable_resources": self._searchable_resources,
                        "searchable_name": "PApplication,Appeal,BCApp",
                        "keywords": term,
                    })
                    for rec in self._extract_records(result):
                        rid = rec.get("Id", "")
                        if rid and rid not in seen_ids:
                            seen_ids.add(rid)
                            all_records.append(rec)
                    if all_records:
                        break
                except RuntimeError:
                    continue
        return all_records

    async def _search_lc(self, date_from: date, date_to: date) -> List[dict]:
        """Pattern B: LCPublicRegCont.advancedSearch with date range."""
        result = await self._aura_call("LCPublicRegCont", "advancedSearch", {
            "keywords": "",
            "ward": "",
            "parish": "",
            "determination": "",
            "undecidedOnly": "",
            "recordTypeName": "",
            "dateRecFrom": date_from.strftime("%Y-%m-%d"),
            "dateRecTo": date_to.strftime("%Y-%m-%d"),
            "dateDecFrom": "",
            "dateDecTo": "",
        })
        return self._extract_records(result)

    def _record_to_detail(self, record: dict) -> Optional[ApplicationDetail]:
        """Convert a Salesforce record to ApplicationDetail.

        Different councils use different managed-package prefixes:
        - `arcusbuiltenv__` (Anglesey, Carmarthenshire, Wiltshire)
        - `arcusbuilt__` (Eastleigh — different package, no `env`)
        Plus per-council custom fields like `Portal_Site_Address__c`.
        """
        app_id = record.get("Id", "")
        if not app_id:
            return None

        def first(*keys):
            for k in keys:
                v = record.get(k)
                if v:
                    return v
            return None

        # Some address values live inside related-object dicts (Location__r)
        location_r = record.get("arcusbuilt__Location__r") or record.get("arcusbuiltenv__Location__r") or {}
        if isinstance(location_r, dict):
            related_address = (
                location_r.get("arcusgazetteer__Address__c")
                or location_r.get("Address__c")
            )
        else:
            related_address = None

        officer_r = record.get("arcusbuilt__PlanningOfficer__r") or record.get("arcusbuiltenv__PlanningOfficer__r") or {}
        case_officer = officer_r.get("Name") if isinstance(officer_r, dict) else None

        record_type = record.get("RecordType") or {}
        application_type = record_type.get("Name") if isinstance(record_type, dict) else None

        received = _parse_date(first(
            "arcusbuilt__ReceivedDate__c",
            "arcusbuiltenv__Received_Date__c",
            "arcusbuiltenv__Valid_Date__c",
            "Received_Date__c",
            "Valid_Date__c",
        ))
        validated = _parse_date(first(
            "arcusbuilt__Validation_Date__c",
            "arcusbuilt__Registration_Complete_Date__c",
            "arcusbuiltenv__Validation_Date__c",
        ))

        address = first(
            "Portal_Site_Address__c",
            "arcusbuiltenv__Site_Address__c",
            "Hidden_PR_Site_address__c",
            "Site_Address__c",
            "BROM_Site_Address__c",
        ) or related_address or ""

        description = first(
            "arcusbuilt__Proposal__c",
            "arcusbuiltenv__Proposal__c",
            "Proposal__c",
        ) or ""

        return ApplicationDetail(
            reference=record.get("Name", ""),
            address=address,
            description=description,
            url=f"{self._base_url}{self._path_prefix}/s/planning-application/{app_id}",
            application_type=application_type or first(
                "arcusbuiltenv__Type__c",
                "Type__c",
            ),
            status=first(
                "arcusbuilt__Status__c",
                "arcusbuiltenv__Status__c",
                "Status__c",
            ),
            decision=first(
                "arcusbuilt__Last_Decision__c",
                "arcusbuiltenv__Current_Decision__c",
                "Current_Decision__c",
            ),
            date_received=received,
            date_validated=validated,
            ward=first(
                "arcusbuilt__Wards__c",
                "arcusbuiltenv__Wards__c",
            ),
            parish=first(
                "arcusbuilt__Parishes__c",
                "arcusbuiltenv__Parishes__c",
            ),
            applicant_name=None,
            case_officer=case_officer,
            raw_data=record,
        )

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search for planning applications."""
        await self._init_aura_context()

        if self._mode == "lc_public_reg":
            records = await self._search_lc(date_from, date_to)
        else:
            records = await self._search_pr(date_from, date_to)

        summaries = []
        seen = set()
        for record in records:
            app_id = record.get("Id", "")
            if not app_id or app_id in seen:
                continue

            received = _parse_date(
                record.get("arcusbuiltenv__Received_Date__c")
                or record.get("arcusbuiltenv__Valid_Date__c")
                or record.get("Received_Date__c")
                or record.get("Valid_Date__c")
            )

            # For PR_Search mode, filter by date client-side
            if self._mode == "pr_search" and received:
                if received < date_from or received > date_to:
                    continue

            seen.add(app_id)
            summaries.append(ApplicationSummary(
                uid=app_id,
                url=f"{self._base_url}{self._path_prefix}/s/planning-application/{app_id}",
            ))

        return summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Return a minimal detail (search results provide most data)."""
        return ApplicationDetail(
            reference=application.uid,
            address="",
            description="",
            url=application.url,
        )

    async def scrape(self, date_from: date, date_to: date) -> ScrapeResult:
        """Full scrape returning details directly from search results."""
        try:
            await self._init_aura_context()

            if self._mode == "lc_public_reg":
                records = await self._search_lc(date_from, date_to)
            else:
                records = await self._search_pr(date_from, date_to)

            details = []
            seen = set()
            for record in records:
                app_id = record.get("Id", "")
                if not app_id or app_id in seen:
                    continue

                detail = self._record_to_detail(record)
                if not detail:
                    continue

                # For PR_Search mode, filter by date client-side
                if self._mode == "pr_search" and detail.date_received:
                    if detail.date_received < date_from or detail.date_received > date_to:
                        continue

                seen.add(app_id)
                details.append(detail)

            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
