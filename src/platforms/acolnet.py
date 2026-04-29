"""Acolnet (PlanTech) platform scraper for UK planning authorities.

Acolnet/PlanTech portals serve search pages at acolnetcgi.gov with
ACTION=UNWRAP&RIPNAME=Root.pgesearch. Results come back as HTML tables with
class "results-table". Detail pages use th/td pairs for field labels/values.

Currently enabled: Central Bedfordshire, Exeter.
"""

import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup

from src.core.browser import HttpClient
from src.core.config import CouncilConfig
from src.core.parser import PageParser
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# CSS selectors for Acolnet detail pages (th/td layout)
ACOLNET_DETAIL_SELECTORS = {
    "reference": [
        "th:-soup-contains('Application Number') + td",
        "th:-soup-contains('Application Number:') + td",
    ],
    "address": [
        "th:-soup-contains('Location') + td",
        "th:-soup-contains('Address') + td",
    ],
    "description": [
        "th:-soup-contains('Proposal') + td",
        "th:-soup-contains('Description') + td",
    ],
    "date_received": ["th:-soup-contains('Date Received') + td"],
    "date_validated": [
        "th:-soup-contains('Registration') + td",
        "th:-soup-contains('Statutory Start') + td",
    ],
    "application_type": ["th:-soup-contains('Application Type') + td"],
    "status": ["th:-soup-contains('Status') + td"],
    "decision": ["th:-soup-contains('Decision') + td"],
    "decision_date": ["th:-soup-contains('Date Decision Made') + td"],
    "decision_issued_date": [
        "th:-soup-contains('Date Decision Despatched') + td",
        "th:-soup-contains('Decision Issued') + td",
    ],
    "case_officer": ["th:-soup-contains('Case Officer') + td"],
    "ward": ["th:-soup-contains('Ward') + td"],
    "parish": ["th:-soup-contains('Parish') + td"],
    "applicant_name": ["th:-soup-contains('Applicant') + td"],
    "agent_name": ["th:-soup-contains('Agent') + td"],
    "decided_by": ["th:-soup-contains('Decision Level') + td"],
    "target_decision_date": ["th:-soup-contains('Target Date for Decision') + td"],
    "consultation_end_date": [
        "th:-soup-contains('Consultation Period Expires') + td",
        "th:-soup-contains('Consultation Period Ends') + td",
        "th:-soup-contains('Earliest Decision Date') + td",
    ],
    "consultation_start_date": [
        "th:-soup-contains('Consultation Start Date') + td",
        "th:-soup-contains('Consultation Period Starts') + td",
    ],
    "appeal_date": [
        "th:-soup-contains('Appeal Received Date') + td",
        "th:-soup-contains('Date Appeal Recieved') + td",
    ],
    "appeal_result": ["th:-soup-contains('Appeal Decision') + td"],
    "meeting_date": [
        "th:-soup-contains('Meeting Date') + td",
        "th:-soup-contains('Committee') + td",
    ],
}


class AcolnetScraper(BaseScraper):
    """Scraper for Acolnet/PlanTech planning portals.

    Config fields dict can override:
        search_form: form name (default "frmSearchByParish")
        date_from_field: field name (default "regdate1")
        date_to_field: field name (default "regdate2")
        ref_field: reference search field (default "casefullref")
        uid_suffix: text appended to UID in links, e.g. " (click for more details)"
    """

    DATE_FORMAT = "%d/%m/%Y"
    MAX_PAGES = 80

    # Regex to strip scripts and WebMetric forms that break parsing
    _HTML_CLEAN_PATTERNS = [
        (re.compile(r"<script\s.*?</script>", re.S | re.I), ""),
        (re.compile(r"<form\s[^>]*?WebMetric[^>]*?>.*?</form>", re.S | re.I), ""),
    ]

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        self._parser = PageParser()
        self._client = HttpClient(
            timeout=30,
            rate_limit_delay=config.rate_limit_delay,
        )
        fields = config.fields or {}
        self._search_form = fields.get("search_form", "frmSearchByParish")
        self._date_from_field = fields.get("date_from_field", "regdate1")
        self._date_to_field = fields.get("date_to_field", "regdate2")
        self._ref_field = fields.get("ref_field", "casefullref")
        self._uid_suffix = fields.get("uid_suffix", "")

    def _clean_html(self, html: str) -> str:
        for pattern, replacement in self._HTML_CLEAN_PATTERNS:
            html = pattern.sub(replacement, html)
        return html

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """POST date-range search to Acolnet and paginate through results."""
        search_url = self.config.base_url

        # Load search page first (establishes session and gets form action with RIPSESSION)
        response = await self._client.get(search_url)
        search_html = response.text
        soup = BeautifulSoup(search_html, "lxml")
        form = soup.find("form", {"name": self._search_form})
        if form and form.get("action"):
            action = form["action"]
            search_url = urljoin(str(response.url), action)

        form_data = {
            self._date_from_field: date_from.strftime(self.DATE_FORMAT),
            self._date_to_field: date_to.strftime(self.DATE_FORMAT),
        }

        response = await self._client.post(search_url, data=form_data)
        html = self._clean_html(response.text)
        current_url = str(response.url)

        all_apps: List[ApplicationSummary] = []
        page_count = 0

        while page_count < self.MAX_PAGES:
            page_apps = self._parse_results_page(html, current_url)
            if not page_apps:
                break
            all_apps.extend(page_apps)
            page_count += 1

            next_url = self._find_next_page(html, current_url)
            if not next_url:
                break

            response = await self._client.get(next_url)
            html = self._clean_html(response.text)
            current_url = str(response.url)

        return all_apps

    def _parse_results_page(self, html: str, base_url: str) -> List[ApplicationSummary]:
        """Extract application UIDs and URLs from a results page."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        content_div = soup.find("div", id="contentcol")
        if not content_div:
            content_div = soup

        for table in content_div.find_all("table", class_="results-table"):
            for link in table.find_all("a", href=True):
                raw_text = link.get_text(strip=True)
                if not raw_text:
                    continue
                uid = raw_text
                # Strip known suffixes like "(click for more details)"
                uid = re.sub(r"\s*\(click for more details\)\s*$", "", uid)
                uid = re.sub(r"\s*-\s*link to more details\s*$", "", uid)
                uid = uid.strip()
                if not uid:
                    continue
                href = link["href"]
                abs_url = urljoin(base_url, href)
                results.append(ApplicationSummary(uid=uid, url=abs_url))

        return results

    def _find_next_page(self, html: str, base_url: str) -> Optional[str]:
        """Find the 'Next' pagination link."""
        soup = BeautifulSoup(html, "lxml")
        next_link = soup.find("a", id="lnkPageNext")
        if next_link and next_link.get("href"):
            return urljoin(base_url, next_link["href"])
        # Fallback: look for link with text containing "Next"
        for a_tag in soup.find_all("a", href=True):
            if "next" in a_tag.get_text(strip=True).lower():
                return urljoin(base_url, a_tag["href"])
        return None

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch full details from the application detail page."""
        html = await self._client.get_html(application.url)
        html = self._clean_html(html)

        data = self._parser.extract(html, ACOLNET_DETAIL_SELECTORS)

        # Fallback: also try extracting reference from title-style patterns
        if not data.get("reference"):
            match = re.search(
                r"Details of Planning Application\s*-\s*(\S+)", html
            )
            if match:
                data["reference"] = match.group(1).strip()

        raw = {k: v for k, v in data.items() if v is not None}

        return ApplicationDetail(
            reference=data.get("reference") or application.uid,
            address=data.get("address") or "",
            description=data.get("description") or "",
            url=application.url,
            application_type=data.get("application_type"),
            status=data.get("status"),
            decision=data.get("decision"),
            date_received=_parse_date(data.get("date_received")),
            date_validated=_parse_date(data.get("date_validated")),
            ward=data.get("ward"),
            parish=data.get("parish"),
            applicant_name=data.get("applicant_name"),
            case_officer=data.get("case_officer"),
            raw_data=raw,
        )

    async def fetch_detail_by_uid(self, uid: str) -> Optional[ApplicationDetail]:
        """Search for a single application by reference number."""
        search_url = self.config.base_url
        await self._client.get(search_url)

        form_data = {
            "ACTION": "UNWRAP",
            self._ref_field: uid,
        }
        response = await self._client.post(search_url, data=form_data)
        html = self._clean_html(response.text)
        current_url = str(response.url)

        apps = self._parse_results_page(html, current_url)
        for app in apps:
            if app.uid == uid and app.url:
                return await self.fetch_detail(app)
        return None


class CentralBedfordshireScraper(AcolnetScraper):
    """Central Bedfordshire - Acolnet/PlanTech portal.

    URL: https://plantech.centralbedfordshire.gov.uk/PLANTECH/DCWebPages/acolnetcgi.gov
    Uses frmSearchByParish form, regdate1/regdate2 fields.
    """

    def __init__(self, config: CouncilConfig):
        config = config.model_copy(
            update={
                "base_url": config.base_url or (
                    "https://plantech.centralbedfordshire.gov.uk/PLANTECH/DCWebPages/"
                    "acolnetcgi.gov?ACTION=UNWRAP&RIPNAME=Root.pgesearch"
                ),
                "fields": {
                    "search_form": "frmSearchByParish",
                    "date_from_field": "regdate1",
                    "date_to_field": "regdate2",
                    "ref_field": "casefullref",
                    **(config.fields or {}),
                },
            }
        )
        super().__init__(config)


class ExeterScraper(AcolnetScraper):
    """Exeter - Acolnet portal with SSL quirks.

    URL: http://pub.exeter.gov.uk/scripts/acolnet/planning/AcolnetCGI.gov
    Uses frmSearchByWard form.
    """

    def __init__(self, config: CouncilConfig):
        config = config.model_copy(
            update={
                "base_url": config.base_url or (
                    "http://pub.exeter.gov.uk/scripts/acolnet/planning/"
                    "AcolnetCGI.gov?ACTION=UNWRAP&RIPNAME=Root.pgesearch"
                ),
                "fields": {
                    "search_form": "frmSearchByWard",
                    "date_from_field": "regdate1",
                    "date_to_field": "regdate2",
                    "ref_field": "casefullref",
                    **(config.fields or {}),
                },
            }
        )
        super().__init__(config)
