"""PlanningExplorer platform scraper (~20 councils including Birmingham, Liverpool, Camden)."""
from datetime import date
from urllib.parse import urljoin

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.parser import PageParser
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

PE_SEARCH_SELECTORS = {
    "result_links": "table.display_table td a",
    "result_uids": "table.display_table td a",
    "next_page": "a:has(img[title*='next page'])",
    "dates_link": "a:-soup-contains('Application Dates')",
}

PE_DETAIL_SELECTORS = {
    "reference": "li:has(span:-soup-contains('Application Number'))",
    "address": "li:has(span:-soup-contains('Site Address'))",
    "description": "li:has(span:-soup-contains('Proposal'))",
    "date_validated": "li:has(span:-soup-contains('Application Registered'))",
    "application_type": "li:has(span:-soup-contains('Application Type'))",
    "status": "li:has(span:-soup-contains('Status'))",
    "case_officer": "li:has(span:-soup-contains('Case Officer'))",
    "ward": "li:has(span:-soup-contains('Ward'))",
    "parish": "li:has(span:-soup-contains('Parish'))",
}

PE_DATES_SELECTORS = {
    "date_received": "li:has(span:-soup-contains('Received'))",
    "date_validated": "li:has(span:-soup-contains('Validated'))",
    "target_date": "li:has(span:-soup-contains('Target Date'))",
    "decision_date": "li:has(span:-soup-contains('Decision Date'))",
}


class PlanningExplorerScraper(BaseScraper):
    SEARCH_PATH = "/GeneralSearch.aspx"
    DATE_FORMAT = "%d/%m/%Y"
    DATE_FROM_FIELD = "dateStart"
    DATE_TO_FIELD = "dateEnd"

    def __init__(self, config):
        super().__init__(config)
        self._parser = PageParser()
        self._client = HttpClient(timeout=120, rate_limit_delay=config.rate_limit_delay)
        self._search_selectors = {**PE_SEARCH_SELECTORS}
        self._detail_selectors = {**PE_DETAIL_SELECTORS}
        self._dates_selectors = {**PE_DATES_SELECTORS}
        if config.selectors:
            for key, val in config.selectors.items():
                for sel_dict in (self._search_selectors, self._detail_selectors, self._dates_selectors):
                    if key in sel_dict:
                        sel_dict[key] = val

    async def _accept_disclaimer(self, response, search_url=None):
        """Handle disclaimer pages that some PE sites show before search."""
        from bs4 import BeautifulSoup
        html = response.text
        if "Disclaimer" not in html and "disclaimer" not in str(response.url).lower():
            return response
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

    async def gather_ids(self, date_from, date_to):
        search_url = self.config.base_url + self.SEARCH_PATH
        response = await self._client.get(search_url)
        response = await self._accept_disclaimer(response, search_url=search_url)
        search_html = response.text
        form_data = self._extract_aspnet_fields(search_html)
        form_data[self.DATE_FROM_FIELD] = date_from.strftime(self.DATE_FORMAT)
        form_data[self.DATE_TO_FIELD] = date_to.strftime(self.DATE_FORMAT)
        form_data["cboSelectDateValue"] = "DATE_RECEIVED"
        form_data["csbtnSearch"] = "Search"
        form_data["rbGroup"] = "rbRange"
        origin = "/".join(search_url.split("/")[:3])
        response = await self._client.post(
            search_url, data=form_data,
            headers={"Origin": origin, "Referer": search_url},
        )
        html = response.text
        results_base = str(response.url)
        applications = []
        while True:
            page_apps = self._parse_results(html, results_base)
            applications.extend(page_apps)
            next_el = self._parser.select_one(html, self._search_selectors["next_page"])
            if next_el is None:
                break
            next_url = urljoin(results_base, next_el.get("href", ""))
            response = await self._client.get(next_url)
            html = response.text
            results_base = str(response.url)
        return applications

    def _parse_results(self, html, base_url):
        links = self._parser.extract_list(html, self._search_selectors["result_links"], attr="href")
        uids = self._parser.extract_list(html, self._search_selectors["result_uids"])
        results = []
        for i, link in enumerate(links):
            uid = uids[i] if i < len(uids) else None
            if uid:
                results.append(ApplicationSummary(uid=uid, url=urljoin(base_url, link)))
        return results

    async def fetch_detail(self, application):
        detail_html = await self._client.get_html(application.url)
        detail_data = self._extract_li_fields(detail_html, self._detail_selectors)
        dates_data = {}
        dates_el = self._parser.select_one(detail_html, self._search_selectors["dates_link"])
        if dates_el:
            dates_url = urljoin(application.url, dates_el.get("href", ""))
            dates_html = await self._client.get_html(dates_url)
            dates_data = self._extract_li_fields(dates_html, self._dates_selectors)
        raw = {k: v for d in (detail_data, dates_data) for k, v in d.items() if v is not None}
        return ApplicationDetail(
            reference=detail_data.get("reference") or application.uid,
            address=detail_data.get("address") or "",
            description=detail_data.get("description") or "",
            url=application.url,
            application_type=detail_data.get("application_type"),
            status=detail_data.get("status"),
            date_received=self._parse_date(dates_data.get("date_received")),
            date_validated=self._parse_date(detail_data.get("date_validated")),
            ward=detail_data.get("ward"),
            parish=detail_data.get("parish"),
            case_officer=detail_data.get("case_officer"),
            raw_data=raw,
        )

    @staticmethod
    def _extract_aspnet_fields(html):
        """Extract ASP.NET hidden fields (__VIEWSTATE, __EVENTVALIDATION, etc.)."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        fields = {}
        for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            el = soup.find("input", {"name": name})
            if el:
                fields[name] = el.get("value", "")
        return fields

    @staticmethod
    def _extract_all_fields(html):
        """Extract all form fields including hidden inputs, selects, and radio defaults."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if not form:
            form = soup
        fields = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            if not name:
                continue
            input_type = inp.get("type", "").lower()
            if input_type == "radio":
                if inp.get("checked") is not None:
                    fields[name] = inp.get("value", "")
                elif name not in fields:
                    fields[name] = ""
                continue
            if input_type == "checkbox":
                continue
            fields[name] = inp.get("value", "")
        for sel in form.find_all("select"):
            name = sel.get("name", "")
            if not name:
                continue
            selected = sel.find("option", selected=True)
            fields[name] = selected.get("value", "") if selected else ""
        return fields

    def _extract_li_fields(self, html, selectors):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        result = {}
        for field_name, selector in selectors.items():
            el = soup.select_one(selector)
            if el:
                span = el.find("span")
                if span:
                    span.decompose()
                result[field_name] = el.get_text(strip=True)
            else:
                result[field_name] = None
        return result

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        from dateutil import parser as dateutil_parser
        try:
            return dateutil_parser.parse(date_str, dayfirst=True).date()
        except (ValueError, TypeError):
            return None
