"""SwiftLG platform scraper (~21 councils). Multiple HTML layout variants."""
from datetime import date
from urllib.parse import urljoin

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.parser import PageParser
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

SWIFTLG_SEARCH_SELECTORS = {
    "result_links": "form table td a",
    "result_uids": "form table td a",
    "next_page": "form a[href*='StartIndex']",
}

SWIFTLG_SPAN_SELECTORS = {
    "reference": "span:-soup-contains('Application Ref') + p",
    "date_validated": "span:-soup-contains('Registration Date') + p",
    "address": "span:-soup-contains('Main Location') + p",
    "description": "span:-soup-contains('Full Description') + p",
    "application_type": "span:-soup-contains('Application Type') + p",
    "date_received": "span:-soup-contains('Application Date') + p",
    "decision": "span:-soup-contains('Decision') + p",
    "case_officer": "span:-soup-contains('Case Officer') + p",
}

SWIFTLG_LABEL_SELECTORS = {
    "reference": "label:-soup-contains('Reference') + p",
    "date_validated": "label:-soup-contains('Registration Date') + p",
    "address": "label:-soup-contains('Main Location') + p",
    "description": "label:-soup-contains('Full Description') + p",
    "application_type": "label:-soup-contains('Application Type') + p",
    "date_received": "label:-soup-contains('Application Date') + p",
    "decision": "label:-soup-contains('Decision') + p",
    "case_officer": "label:-soup-contains('Case Officer') + p",
}


class SwiftLGScraper(BaseScraper):
    SEARCH_PATH = "/wphappcriteria.display"
    DATE_FORMAT = "%d/%m/%Y"
    DATE_FROM_FIELD = "REGFROMDATE.MAINBODY.WPACIS.1"
    DATE_TO_FIELD = "REGTODATE.MAINBODY.WPACIS.1"

    def __init__(self, config, detail_selectors=None):
        super().__init__(config)
        self._parser = PageParser()
        self._client = HttpClient(timeout=30, rate_limit_delay=config.rate_limit_delay)
        self._search_selectors = {**SWIFTLG_SEARCH_SELECTORS}
        self._detail_selectors = detail_selectors or {**SWIFTLG_SPAN_SELECTORS}
        if config.selectors:
            for key, val in config.selectors.items():
                for sel_dict in (self._search_selectors, self._detail_selectors):
                    if key in sel_dict:
                        sel_dict[key] = val

    async def _accept_disclaimer(self, response, search_url=None):
        """Handle disclaimer/login pages that some SwiftLG sites show first."""
        from bs4 import BeautifulSoup
        html = response.text
        soup = BeautifulSoup(html, "lxml")
        accept_form = soup.find("form", action=lambda a: a and "Disclaimer" in a)
        if accept_form:
            action = accept_form.get("action", "")
            accept_url = urljoin(str(response.url), action)
            hidden_fields = {}
            for inp in accept_form.find_all("input", {"type": "hidden"}):
                name = inp.get("name", "")
                if name:
                    hidden_fields[name] = inp.get("value", "")
            await self._client.post(accept_url, data=hidden_fields)
            if search_url:
                response = await self._client.get(search_url)
        return response

    @staticmethod
    def _extract_form_fields(html):
        """Extract all form fields and the form action URL."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        form = soup.find("form", action=lambda a: a and "WPHAPPCRITERIA" in a.upper())
        form_action = None
        if form:
            form_action = form.get("action", "")
        else:
            form = soup
        for el in form.find_all("input"):
            name = el.get("name", "")
            if not name:
                continue
            fields[name] = el.get("value", "")
        for el in form.find_all("select"):
            name = el.get("name", "")
            if not name:
                continue
            selected = el.find("option", selected=True)
            fields[name] = selected.get("value", "") if selected else ""
        return fields, form_action

    @staticmethod
    def _extract_aspnet_fields(html):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            el = soup.find("input", {"name": name})
            if el:
                fields[name] = el.get("value", "")
        return fields

    async def gather_ids(self, date_from, date_to):
        search_url = self.config.base_url + self.SEARCH_PATH
        response = await self._client.get(search_url)
        response = await self._accept_disclaimer(response, search_url=search_url)
        search_html = response.text
        form_data, form_action = self._extract_form_fields(search_html)
        form_data[self.DATE_FROM_FIELD] = date_from.strftime(self.DATE_FORMAT)
        form_data[self.DATE_TO_FIELD] = date_to.strftime(self.DATE_FORMAT)
        if form_action:
            post_url = urljoin(search_url, form_action)
        else:
            post_url = self.config.base_url + "/WPHAPPCRITERIA"
        response = await self._client.post(post_url, data=form_data)
        html = response.text
        applications = []
        while True:
            page_apps = self._parse_results(html)
            applications.extend(page_apps)
            next_el = self._parser.select_one(html, self._search_selectors["next_page"])
            if next_el is None:
                break
            base = self.config.base_url.rstrip("/") + "/"
            next_url = urljoin(base, next_el.get("href", ""))
            html = await self._client.get_html(next_url)
        return applications

    def _parse_results(self, html):
        links = self._parser.extract_list(html, self._search_selectors["result_links"], attr="href")
        uids = self._parser.extract_list(html, self._search_selectors["result_uids"])
        results = []
        for i, link in enumerate(links):
            uid = uids[i] if i < len(uids) else None
            if uid:
                results.append(ApplicationSummary(uid=uid, url=urljoin(self.config.base_url, link)))
        return results

    async def fetch_detail(self, application):
        html = await self._client.get_html(application.url)
        data = self._parser.extract(html, self._detail_selectors)
        raw = {k: v for k, v in data.items() if v is not None}
        return ApplicationDetail(
            reference=data.get("reference") or application.uid,
            address=data.get("address") or "",
            description=data.get("description") or "",
            url=application.url,
            application_type=data.get("application_type"),
            status=data.get("decision"),
            date_received=self._parse_date(data.get("date_received")),
            date_validated=self._parse_date(data.get("date_validated")),
            case_officer=data.get("case_officer"),
            raw_data=raw,
        )

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        from dateutil import parser as dateutil_parser
        try:
            return dateutil_parser.parse(date_str, dayfirst=True).date()
        except (ValueError, TypeError):
            return None


class SwiftLGLabelScraper(SwiftLGScraper):
    """Variant using <label> tags instead of <span> tags."""
    def __init__(self, config):
        super().__init__(config, detail_selectors={**SWIFTLG_LABEL_SELECTORS})
