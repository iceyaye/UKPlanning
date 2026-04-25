"""Salesforce Arcus platform scraper.

Used by ~9 UK councils via *.my.site.com domains (Salesforce Experience Cloud).
The Arcus Planning Register (arcuscommunity) exposes data via Salesforce Aura
framework POST calls to /s/sfsites/aura.

We first load the page to get the fwuid (framework UID) and app version,
then replay Aura ApexAction calls to search for planning applications.
"""
import json
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlencode

import httpx

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

# Map authority_code -> (base_url, path_prefix, register_name, ref_prefix)
# ref_prefix is optional — used for councils with non-standard reference formats
COUNCIL_CONFIG = {
    "allerdale": ("https://cumberlandcouncil.my.site.com", "/pr3", "Arcus_BE_Public_Register", None),
    "copeland": ("https://cumberlandcouncil.my.site.com", "/pr3", "Arcus_BE_Public_Register", None),
    "anglesey": ("https://ioacc.my.site.com", "", "Arcus_BE_Public_Register", None),
    "bromley": ("https://planningaccess.bromley.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "carmarthenshire": ("https://carmarthenshire.my.site.com", "/en", "Arcus_BE_Public_Register", None),
    "eppingforest": ("https://eppingforestdc.my.site.com", "/pr", "Arcus_BE_Public_Register", None),
    "haringey": ("https://publicregister.haringey.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "shepway": ("https://folkestonehythedc.my.site.com", "/PR3", "Arcus_BE_Public_Register", None),
    "southderbyshire": ("https://southderbyshire.my.site.com", "", "Arcus_BE_Public_Register", None),
    "wrexham": ("https://register.wrexham.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "eastleigh": ("https://planning.eastleigh.gov.uk", "", "Arcus_BE_Public_Register", None),
    "wiltshire": ("https://development.wiltshire.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "miltonkeynes": ("https://www.be.milton-keynes.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "salford": ("https://salfordcitycouncil.my.site.com", "/pr", "Arcus_BE_Public_Register", None),
    "ashford": ("https://ashfordboroughcouncil.my.site.com", "/pr", "Arcus_BE_Public_Register", None),
    "erewash": ("https://planning.erewash.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "havant": ("https://service.havant.gov.uk", "/pr", "Arcus_BE_Public_Register", "APP"),
    "reading": ("https://publicregister.reading.gov.uk", "/pr", "Arcus_BE_Public_Register", "PL"),
    "rochdale": ("https://account.rochdale.gov.uk", "/pr", "Arcus_BE_Public_Register", None),
    "bracknell": ("https://publicaccess.bracknell-forest.gov.uk", "", "Arcus_BE_Public_Register", None),
}


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, AttributeError):
        return None


class SalesforceArcusScraper(BaseScraper):
    """Scraper for Salesforce Arcus planning portals."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        council_cfg = COUNCIL_CONFIG.get(config.authority_code)
        if council_cfg and len(council_cfg) >= 3:
            self._base_url = council_cfg[0]
            self._path_prefix = council_cfg[1]
            self._register_name = council_cfg[2]
            self._ref_prefix = council_cfg[3] if len(council_cfg) > 3 else None
        else:
            self._base_url = config.base_url.rstrip("/")
            self._path_prefix = ""
            self._register_name = "Arcus_BE_Public_Register"
            self._ref_prefix = None

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

        # Try multiple page paths to find one that works
        page_url = None
        for path in [
            f"{self._path_prefix}/s/register-view",
            f"{self._path_prefix}/s/be-register-view",
            "/s/register-view",
            "/s/pr-english",
            "/s/",
        ]:
            try:
                resp = await self._client.get(f"{self._base_url}{path}")
                if "fwuid" in resp.text:
                    page_url = f"{self._base_url}{path}"
                    break
            except Exception:
                continue

        if not page_url:
            resp = await self._client.get(
                f"{self._base_url}{self._path_prefix}/s/register-view"
            )
        if "fwuid" not in resp.text:
            resp.raise_for_status()

        # Extract fwuid and app version from the bootstrap URL
        # The page contains a URL like /sfsites/l/{encoded_json}/bootstrap.js
        url_match = re.search(r'/sfsites/l/([^/]+)/bootstrap', resp.text)
        if url_match:
            try:
                decoded = unquote(url_match.group(1))
                config = json.loads(decoded)
                self._fwuid = config.get("fwuid", "")
                loaded = config.get("loaded", {})
                for key in loaded:
                    if key.startswith("APPLICATION@markup://siteforce:"):
                        self._app_name = key.replace("APPLICATION@markup://", "")
                        self._app_version = loaded[key]
                        break
            except (json.JSONDecodeError, KeyError):
                pass

        # Fallback: try regex patterns
        if not self._fwuid:
            fwuid_match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', resp.text)
            if fwuid_match:
                self._fwuid = fwuid_match.group(1)

        if not self._fwuid:
            raise RuntimeError("Could not extract Aura fwuid from page")

    async def _aura_call(self, classname: str, method: str, params: dict) -> dict:
        """Make an Aura ApexAction call."""
        await self._init_aura_context()

        message = {
            "actions": [{
                "id": "1;a",
                "descriptor": "aura://ApexActionController/ACTION$execute",
                "callingDescriptor": "UNKNOWN",
                "params": {
                    "namespace": "arcuscommunity",
                    "classname": classname,
                    "method": method,
                    "params": params,
                    "cacheable": False,
                    "isContinuation": False,
                },
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

        resp = await self._client.post(
            f"{self._aura_url}?r=1&aura.ApexAction.execute=1",
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

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search for planning applications using multiple search terms."""
        all_summaries = []
        seen_ids = set()

        years = {date_from.year, date_to.year}
        for year in sorted(years):
            yy = f"{year % 100:02d}"
            search_terms = [f"/{yy}", str(year), f"{yy}/"]
            if self._ref_prefix:
                search_terms.insert(0, f"{self._ref_prefix}/{yy}")
            for term in search_terms:
                try:
                    result = await self._aura_call("PR_SearchService", "search", {
                        "request": {
                            "registerName": self._register_name,
                            "searchType": "quick",
                            "searchTerm": term,
                            "searchName": "Planning_Applications",
                        },
                    })
                except Exception:
                    continue

                rv = result.get("returnValue", result)
                records = rv.get("records", []) if isinstance(rv, dict) else []

                for record in records:
                    received = _parse_date(
                        record.get("arcusbuiltenv__Received_Date__c")
                        or record.get("arcusbuiltenv__Valid_Date__c")
                    )
                    app_id = record.get("Id", "")
                    if not app_id or app_id in seen_ids:
                        continue
                    name = record.get("Name", "")
                    # Include if date matches range, or if no date available
                    if received and date_from <= received <= date_to:
                        seen_ids.add(app_id)
                        all_summaries.append(ApplicationSummary(
                            uid=app_id,
                            url=f"{self._base_url}{self._path_prefix}/s/planning-application/{app_id}",
                        ))
                    elif not received and (str(year) in name or f"/{yy}" in name or f"{yy}/" in name):
                        seen_ids.add(app_id)
                        all_summaries.append(ApplicationSummary(
                            uid=app_id,
                            url=f"{self._base_url}{self._path_prefix}/s/planning-application/{app_id}",
                        ))

        return all_summaries

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch application detail. Arcus search already returns most fields."""
        # For now, use the data from the search results (already captured in gather_ids)
        # A full detail call would need PR_SearchService.getRecord
        # But the search gives us enough for the basic fields
        return ApplicationDetail(
            reference=application.uid,  # Will be overridden if we enhance later
            address="",
            description="",
            url=application.url,
        )

    async def scrape(self, date_from: date, date_to: date):
        """Override scrape to extract details directly from search results."""
        from src.core.scraper import ScrapeResult
        try:
            await self._init_aura_context()

            years = {date_from.year, date_to.year}
            details = []

            seen_ids = set()
            for year in sorted(years):
                yy = f"{year % 100:02d}"
                search_terms = [f"/{yy}", str(year), f"{yy}/"]
                if self._ref_prefix:
                    search_terms.insert(0, f"{self._ref_prefix}/{yy}")
                for term in search_terms:
                    try:
                        result = await self._aura_call("PR_SearchService", "search", {
                            "request": {
                                "registerName": self._register_name,
                                "searchType": "quick",
                                "searchTerm": term,
                                "searchName": "Planning_Applications",
                            },
                        })
                    except Exception:
                        continue

                    rv = result.get("returnValue", result)
                    records = rv.get("records", []) if isinstance(rv, dict) else []

                    for record in records:
                        received = _parse_date(
                            record.get("arcusbuiltenv__Received_Date__c")
                            or record.get("arcusbuiltenv__Valid_Date__c")
                        )
                        app_id = record.get("Id", "")
                        if not app_id or app_id in seen_ids:
                            continue
                        if received and (received < date_from or received > date_to):
                            continue
                        name = record.get("Name", "")
                        if not received and str(year) not in name and f"/{yy}" not in name and f"{yy}/" not in name:
                            continue
                        seen_ids.add(app_id)
                        address = (
                            record.get("arcusbuiltenv__Site_Address__c")
                            or record.get("Hidden_PR_Site_address__c")
                            or record.get("BROM_Site_Address__c", "")
                        )
                        details.append(ApplicationDetail(
                            reference=record.get("Name", ""),
                            address=address,
                            description=record.get("arcusbuiltenv__Proposal__c", ""),
                            url=f"{self._base_url}{self._path_prefix}/s/planning-application/{app_id}",
                            application_type=record.get("arcusbuiltenv__Type__c"),
                            status=record.get("arcusbuiltenv__Status__c"),
                            decision=record.get("arcusbuiltenv__Current_Decision__c"),
                            date_received=received,
                            date_validated=None,
                            ward=None,
                            parish=None,
                            applicant_name=None,
                            case_officer=None,
                            raw_data=record,
                        ))

            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
