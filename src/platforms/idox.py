"""Idox platform scraper for UK planning authorities.

Idox is the dominant planning portal platform, used by ~250 UK councils.
This module defines the default selectors and the scraper class.
"""

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.parser import PageParser
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

IDOX_SEARCH_SELECTORS = {
    "result_links": "ul#searchresults li.searchresult > a",
    "result_meta": "ul#searchresults li.searchresult p.metaInfo",
    "next_page": "a.next",
    "dates_tab": "a#subtab_dates",
    "info_tab": "a#subtab_details",
}

IDOX_SELECTORS = {
    "reference": ["th:-soup-contains('Reference') + td", "th:-soup-contains('Application Number') + td"],
    "address": ["th:-soup-contains('Address') + td", "th:-soup-contains('Location') + td", "th:-soup-contains('Site') + td"],
    "description": ["th:-soup-contains('Proposal') + td", "th:-soup-contains('Description') + td"],
    "status": ["th:-soup-contains('Status') + td"],
    "alt_reference": ["th:-soup-contains('Alternative Reference') + td"],
}

IDOX_DATES_SELECTORS = {
    "date_received": ["th:-soup-contains('Application Received') + td", "th:-soup-contains('Received Date') + td"],
    "date_validated": ["th:-soup-contains('Validated') + td", "th:-soup-contains('Registration Date') + td"],
    "expiry_date": ["th:-soup-contains('Expiry Date') + td"],
    "target_date": ["th:-soup-contains('Target Date') + td"],
    "decision_date": ["th:-soup-contains('Decision Made Date') + td", "th:-soup-contains('Decision Issued Date') + td"],
    "consultation_expiry": ["th:-soup-contains('Standard Consultation Expiry') + td"],
}

IDOX_INFO_SELECTORS = {
    "application_type": ["th:-soup-contains('Application Type') + td"],
    "case_officer": ["th:-soup-contains('Case Officer') + td"],
    "parish": ["th:-soup-contains('Parish') + td"],
    "ward": ["th:-soup-contains('Ward') + td"],
    "applicant_name": ["th:-soup-contains('Applicant Name') + td", "th:-soup-contains('Applicant') + td"],
    "agent_name": ["th:-soup-contains('Agent Name') + td"],
    "decision_level": ["th:-soup-contains('Decision Level') + td", "th:-soup-contains('Actual Decision Level') + td"],
}


class IdoxScraper(BaseScraper):
    """Scraper for Idox-based planning portals (~250 UK councils)."""

    SEARCH_PATH = "/search.do?action=advanced"
    RESULTS_PATH = "/advancedSearchResults.do?action=firstPage"
    DATE_FORMAT = "%d/%m/%Y"

    DATE_FROM_FIELD = "date(applicationReceivedStart)"
    DATE_TO_FIELD = "date(applicationReceivedEnd)"
    SEARCH_TYPE_FIELD = "searchType"
    SEARCH_TYPE_VALUE = "Application"

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._parser = PageParser()
        self._client = HttpClient(
            timeout=120,
            rate_limit_delay=config.rate_limit_delay,
        )
        self._search_selectors = {**IDOX_SEARCH_SELECTORS}
        self._summary_selectors = {**IDOX_SELECTORS}
        self._dates_selectors = {**IDOX_DATES_SELECTORS}
        self._info_selectors = {**IDOX_INFO_SELECTORS}
        if config.selectors:
            for key, val in config.selectors.items():
                for sel_dict in (self._search_selectors, self._summary_selectors,
                                 self._dates_selectors, self._info_selectors):
                    if key in sel_dict:
                        sel_dict[key] = val

    async def _accept_disclaimer(self, response):
        """Handle disclaimer pages that some Idox sites show before search."""
        url_str = str(response.url)
        if "Disclaimer" not in response.text and "disclaimer" not in url_str.lower():
            return response
        soup = BeautifulSoup(response.text, "lxml")
        form = soup.find("form", action=lambda a: a and "Disclaimer" in a)
        if form:
            action = form.get("action", "")
            post_url = urljoin(url_str, action)
            form_data = {}
            for inp in form.find_all("input"):
                name = inp.get("name", "")
                if name:
                    form_data[name] = inp.get("value", "")
            response = await self._client.post(post_url, data=form_data)
        return response

    async def gather_ids(self, date_from: date, date_to: date) -> list[ApplicationSummary]:
        """Search Idox portal for applications in date range, handling pagination
        and recursively splitting on 'Too many results found' errors.

        Some Idox installs (e.g. Hammersmith) only expose applicationValidated;
        others use applicationReceived. We try the configured field first, then
        fall back to applicationValidated, then applicationDecision."""
        search_url = self.config.base_url + self.SEARCH_PATH

        # Load search page once: get CSRF token, session cookies, real base URL (after redirects)
        response = await self._client.get(search_url)
        response = await self._accept_disclaimer(response)
        if "Disclaimer" in str(response.url):
            response = await self._client.get(search_url)
        search_html = response.text
        real_base = str(response.url).split("/search.do")[0]
        csrf_token = self._extract_csrf(search_html)
        results_url = real_base + self.RESULTS_PATH
        search_page_url = str(response.url)

        # Build list of date-field candidates: configured first, then sensible fallbacks
        date_fields = [(self.DATE_FROM_FIELD, self.DATE_TO_FIELD)]
        for f, t in [
            ("date(applicationValidatedStart)", "date(applicationValidatedEnd)"),
            ("date(applicationDecisionStart)", "date(applicationDecisionEnd)"),
        ]:
            if (f, t) not in date_fields and f in search_html:
                date_fields.append((f, t))

        for from_field, to_field in date_fields:
            seen = set()
            out = []
            for app in await self._search_range(
                date_from, date_to, csrf_token, results_url, search_page_url,
                from_field=from_field, to_field=to_field, depth=0,
            ):
                if app.uid in seen:
                    continue
                seen.add(app.uid)
                out.append(app)
            if out:
                return out
        return []

    async def _search_range(
        self,
        date_from: date,
        date_to: date,
        csrf_token: str,
        results_url: str,
        search_page_url: str,
        depth: int,
        from_field: str = None,
        to_field: str = None,
    ) -> list[ApplicationSummary]:
        from_field = from_field or self.DATE_FROM_FIELD
        to_field = to_field or self.DATE_TO_FIELD
        form_data = {
            from_field: date_from.strftime(self.DATE_FORMAT),
            to_field: date_to.strftime(self.DATE_FORMAT),
            self.SEARCH_TYPE_FIELD: self.SEARCH_TYPE_VALUE,
        }
        if csrf_token:
            form_data["_csrf"] = csrf_token

        response = await self._client.post(
            results_url, data=form_data,
            headers={"Referer": search_page_url},
        )
        html = response.text

        # Idox returns "Too many results found" when the limit (often ~500) is
        # exceeded. Bisect the date range and retry.
        if "Too many results found" in html and date_from < date_to and depth < 6:
            mid = date_from + (date_to - date_from) / 2
            left = await self._search_range(date_from, mid, csrf_token, results_url, search_page_url, depth + 1, from_field, to_field)
            right_from = mid + timedelta(days=1)
            right = (
                await self._search_range(right_from, date_to, csrf_token, results_url, search_page_url, depth + 1, from_field, to_field)
                if right_from <= date_to else []
            )
            return left + right

        applications = []
        while True:
            page_apps = self._parse_search_results(html)
            applications.extend(page_apps)

            next_el = self._parser.select_one(html, self._search_selectors["next_page"])
            if next_el is None:
                break
            next_url = urljoin(self.config.base_url, next_el["href"])
            html = await self._client.get_html(next_url)

        return applications

    def _extract_csrf(self, html: str) -> str:
        """Extract CSRF token from Idox search page."""
        el = self._parser.select_one(html, 'input[name="_csrf"]')
        if el:
            return el.get("value", "")
        return ""

    def _parse_search_results(self, html: str) -> list[ApplicationSummary]:
        """Extract application summaries from a single results page."""
        import re
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        results = []

        for li in soup.select("ul#searchresults li.searchresult"):
            link_el = li.select_one("a")
            meta_el = li.select_one("p.metaInfo") or li.select_one("p.metainfo")

            if not link_el:
                continue

            href = link_el.get("href", "")
            abs_url = urljoin(self.config.base_url + "/", href)

            uid = None
            if meta_el:
                meta_text = meta_el.get_text(" ", strip=True)
                ref_match = re.search(r"(?:Ref|Application|Case)\.?\s*No[.:]?\s*(\S+)", meta_text)
                if ref_match:
                    uid = ref_match.group(1).strip()

            if uid:
                results.append(ApplicationSummary(uid=uid, url=abs_url))

        return results

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch full application details from summary, dates, and info tabs."""
        summary_html = await self._client.get_html(application.url)
        summary_data = self._parser.extract(summary_html, self._summary_selectors)

        dates_data = {}
        dates_el = self._parser.select_one(summary_html, self._search_selectors["dates_tab"])
        if dates_el:
            dates_url = urljoin(self.config.base_url, dates_el["href"])
            dates_html = await self._client.get_html(dates_url)
            dates_data = self._parser.extract(dates_html, self._dates_selectors)

        info_data = {}
        info_el = self._parser.select_one(summary_html, self._search_selectors["info_tab"])
        if info_el:
            info_url = urljoin(self.config.base_url, info_el["href"])
            info_html = await self._client.get_html(info_url)
            info_data = self._parser.extract(info_html, self._info_selectors)

        raw = {k: v for d in (summary_data, dates_data, info_data) for k, v in d.items() if v is not None}

        return ApplicationDetail(
            reference=summary_data.get("reference") or application.uid,
            address=summary_data.get("address") or "",
            description=summary_data.get("description") or "",
            url=application.url,
            application_type=info_data.get("application_type"),
            status=summary_data.get("status"),
            date_received=self._parse_date(dates_data.get("date_received")),
            date_validated=self._parse_date(dates_data.get("date_validated")),
            ward=info_data.get("ward"),
            parish=info_data.get("parish"),
            applicant_name=info_data.get("applicant_name"),
            case_officer=info_data.get("case_officer"),
            raw_data=raw,
        )

    @staticmethod
    def _parse_date(date_str):
        """Parse Idox date strings like 'Mon 15 Jan 2024' or '15/01/2024'."""
        if not date_str:
            return None
        from dateutil import parser as dateutil_parser
        try:
            return dateutil_parser.parse(date_str, dayfirst=True).date()
        except (ValueError, TypeError):
            return None


class IdoxEndExcScraper(IdoxScraper):
    """Variant for Idox servers with exclusive end dates.

    Some Idox installations treat the end date as exclusive (not included
    in results). This variant adds 1 day to compensate.
    """

    async def gather_ids(self, date_from: date, date_to: date) -> list:
        adjusted_to = date_to + timedelta(days=1)
        return await super().gather_ids(date_from, adjusted_to)


class IdoxNIScraper(IdoxScraper):
    """Variant for Northern Ireland Idox councils.

    NI councils require searching by case reference prefix in addition
    to date range. Each council has specific prefixes (e.g. Belfast: LA04, Z/20).
    """

    REF_FIELD = "searchCriteria.reference"

    def __init__(self, config: CouncilConfig, case_prefixes=None):
        super().__init__(config)
        self._case_prefixes = case_prefixes or []

    async def gather_ids(self, date_from: date, date_to: date) -> list:
        if not self._case_prefixes:
            return await super().gather_ids(date_from, date_to)

        all_results = []
        seen_uids = set()
        for prefix in self._case_prefixes:
            results = await self._gather_ids_with_prefix(date_from, date_to, prefix)
            for app in results:
                if app.uid not in seen_uids:
                    seen_uids.add(app.uid)
                    all_results.append(app)
        return all_results

    async def _gather_ids_with_prefix(self, date_from: date, date_to: date, prefix: str) -> list:
        """Search with a specific case prefix."""
        search_url = self.config.base_url + self.SEARCH_PATH
        await self._client.get_html(search_url)

        results_url = self.config.base_url + self.RESULTS_PATH
        form_data = {
            self.DATE_FROM_FIELD: date_from.strftime(self.DATE_FORMAT),
            self.DATE_TO_FIELD: date_to.strftime(self.DATE_FORMAT),
            self.SEARCH_TYPE_FIELD: self.SEARCH_TYPE_VALUE,
            self.REF_FIELD: prefix,
        }
        response = await self._client.post(results_url, data=form_data)
        html = response.text

        applications = []
        while True:
            page_apps = self._parse_search_results(html)
            applications.extend(page_apps)
            next_el = self._parser.select_one(html, self._search_selectors["next_page"])
            if next_el is None:
                break
            next_url = urljoin(self.config.base_url, next_el["href"])
            html = await self._client.get_html(next_url)

        return applications


IDOX_CRUMB_SELECTORS = {
    "reference": "span.caseNumber",
    "address": "span.address",
    "description": "span.description",
    "status": "th:-soup-contains('Status') + td",
    "alt_reference": "th:-soup-contains('Alternative Reference') + td",
}


class IdoxCrumbScraper(IdoxScraper):
    """Variant for Idox portals using breadcrumb-style layout."""

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._summary_selectors = {**IDOX_CRUMB_SELECTORS}
        if config.selectors:
            for key, val in config.selectors.items():
                if key in self._summary_selectors:
                    self._summary_selectors[key] = val
