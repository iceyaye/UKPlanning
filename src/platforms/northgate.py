"""Northgate Planning Portal scraper (new-style, post-2024 migration).

Used by councils that migrated from PlanningExplorer to the new Northgate portal.
Search via /Search/Advanced → /Search/Results, detail pages at /Planning/Display/{ref}.
Many sites require accepting a disclaimer first.
"""
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper


class NorthgateScraper(BaseScraper):
    SEARCH_PATH = "/Search/Advanced"
    RESULTS_PATH = "/Search/Results"
    DATE_FORMAT = "%d/%m/%Y"

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._client = HttpClient(timeout=60, rate_limit_delay=config.rate_limit_delay)
        self._disclaimer_accepted = False

    async def _accept_disclaimer(self):
        """Accept disclaimer if present, then return to search page."""
        if self._disclaimer_accepted:
            return

        resp = await self._client.get(self.config.base_url + self.SEARCH_PATH)
        html = resp.text

        if "Disclaimer" in str(resp.url) or "Disclaimer" in html:
            soup = BeautifulSoup(html, "lxml")
            form = soup.find("form", action=lambda a: a and "Disclaimer" in a)
            if form:
                accept_url = urljoin(str(resp.url), form["action"])
                hidden_fields = {}
                for inp in form.find_all("input", {"type": "hidden"}):
                    name = inp.get("name", "")
                    if name:
                        hidden_fields[name] = inp.get("value", "")
                await self._client.post(accept_url, data=hidden_fields)

        self._disclaimer_accepted = True

    async def gather_ids(self, date_from: date, date_to: date) -> list[ApplicationSummary]:
        await self._accept_disclaimer()

        search_url = self.config.base_url + self.SEARCH_PATH
        resp = await self._client.get(search_url)
        soup = BeautifulSoup(resp.text, "lxml")

        form = soup.select_one("form[action*=Results], form[action*=results]")
        form_data = {}
        if form:
            for inp in form.find_all("input"):
                name = inp.get("name", "")
                if not name:
                    continue
                input_type = inp.get("type", "").lower()
                if input_type == "checkbox":
                    continue
                if input_type == "radio":
                    if inp.get("checked") is not None:
                        form_data[name] = inp.get("value", "")
                    continue
                form_data[name] = inp.get("value", "")
            if "SearchPlanning" in form_data and form_data["SearchPlanning"].lower() == "false":
                form_data["SearchPlanning"] = "True"

        form_data["DateReceivedFrom"] = date_from.strftime(self.DATE_FORMAT)
        form_data["DateReceivedTo"] = date_to.strftime(self.DATE_FORMAT)

        results_url = self.config.base_url + self.RESULTS_PATH
        resp = await self._client.post(results_url, data=form_data)

        applications = []
        while True:
            soup = BeautifulSoup(resp.text, "lxml")
            page_apps = self._parse_results(soup)
            applications.extend(page_apps)

            next_link = soup.select_one('a[aria-label*="Next"]')
            if not next_link:
                break
            next_url = urljoin(self.config.base_url, next_link["href"])
            resp = await self._client.get(next_url)

        return applications

    def _parse_results(self, soup: BeautifulSoup) -> list[ApplicationSummary]:
        results = []
        for link in soup.select('a[href*="/Planning/Display"]'):
            ref = link.get_text(strip=True)
            href = link.get("href", "")
            if ref and href:
                url = urljoin(self.config.base_url, href)
                results.append(ApplicationSummary(uid=ref, url=url))
        return results

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        html = await self._client.get_html(application.url)
        soup = BeautifulSoup(html, "lxml")

        fields = {}
        for li in soup.select("li.row"):
            label_el = li.select_one("label")
            input_el = li.select_one("input[readonly], textarea[readonly]")
            if label_el and input_el:
                label = label_el.get_text(strip=True)
                value = input_el.get("value", "").strip() or input_el.get_text(strip=True)
                if value:
                    fields[label] = value

        # Also extract from strong+text pairs (Location/Proposal sections)
        for strong in soup.select("strong"):
            label = strong.get_text(strip=True)
            parent = strong.parent
            if parent:
                parent_text = parent.get_text(" ", strip=True)
                value = parent_text.replace(label, "", 1).strip()
                if value and label not in fields:
                    fields[label] = value

        return ApplicationDetail(
            reference=fields.get("Application Number", application.uid),
            address=fields.get("Location", fields.get("Address", "")),
            description=fields.get("Proposal", ""),
            url=application.url,
            application_type=fields.get("Application Type"),
            status=fields.get("Status"),
            decision=fields.get("Decision"),
            date_received=self._parse_date(fields.get("Date Received")),
            date_validated=self._parse_date(fields.get("Date Valid")),
            ward=fields.get("Ward", fields.get("Electoral Division(s)")),
            parish=fields.get("Parish", fields.get("Parish(es)")),
            applicant_name=fields.get("Applicant Name"),
            case_officer=fields.get("Case Officer"),
            raw_data=fields,
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
