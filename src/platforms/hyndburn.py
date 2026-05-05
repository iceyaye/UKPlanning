"""Hyndburn Borough Council - Northgate Assure planning portal scraper.

Northgate Assure is an ASP.NET MVC application with jQuery AJAX endpoints.
The search form at OnlinePlanningSearch POSTs serialised form data to
OnlinePlanningSearchResults, which returns an HTML fragment of results.
Detail pages are at OnlinePlanningOverview?applicationNumber=<ref>.

The server requires:
  - A valid ASP.NET session (established by GETting the search page first)
  - X-Requested-With: XMLHttpRequest header on POST requests
  - Referer header matching the search page

Endpoints:
  - Search:  /Northgate/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResults (POST)
  - Weekly:  /Northgate/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResultsForWeeklyMonthlyGo (POST)
  - Detail:  /Northgate/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=<ref>

This scraper can also serve other councils running the same Northgate Assure
platform by adding entries to COUNCIL_CONFIG.
"""
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper

COUNCIL_CONFIG = {
    "hyndburn": {
        "base_url": "https://planning.hyndburnbc.gov.uk/Northgate/ES/Presentation",
        "keywords": ["BB5", "BB1", "BB6", "BB4", "BB"],
    },
    "peakdistrict": {
        "base_url": "https://planning.peakdistrict.gov.uk/AssureLive/ES/Presentation",
        "keywords": ["DE", "SK", "S3"],
    },
    "charnwood": {
        "base_url": "https://planningexplorer.charnwood.gov.uk/Assure/ES/Presentation",
        "keywords": ["LE", "LE11", "LE12"],
    },
    "hounslow": {
        "base_url": "https://planningandbuilding.hounslow.gov.uk/NECSWS/ES/Presentation",
        "keywords": ["TW", "W4", "W3", "TW3", "TW4"],
    },
    "broxbourne": {
        "base_url": "https://planning.broxbourne.gov.uk/LPAssure/ES/Presentation",
        "keywords": ["EN", "EN10", "EN7", "EN8"],
    },
}

# Paths relative to base_url
SEARCH_PAGE = "/Planning/OnlinePlanning/OnlinePlanningSearch"
SEARCH_RESULTS = "/Planning/OnlinePlanning/OnlinePlanningSearchResults"
WEEKLY_MONTHLY_RESULTS = "/Planning/OnlinePlanning/OnlinePlanningSearchResultsForWeeklyMonthlyGo"
DETAIL_PAGE = "/Planning/OnlinePlanning/OnlinePlanningOverview"
PAGINATION_URL = "/Planning/OnlinePlanning/OnlinePlanningSearchResults"


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class NorthgateAssureScraper(BaseScraper):
    """Scraper for Northgate Assure planning portals (Hyndburn and others).

    Uses httpx with session cookies and AJAX headers to interact with the
    ASP.NET MVC endpoints. The search form is serialised and POSTed to get
    paginated HTML result fragments.
    """

    DATE_FORMAT = "%d/%m/%Y"
    MAX_PAGES = 50

    def __init__(self, config: CouncilConfig):
        super().__init__(config)
        cfg = COUNCIL_CONFIG.get(config.authority_code, {})
        self._base_url = cfg.get("base_url", config.base_url)
        self._keywords = cfg.get("keywords", ["plan", "app", "house"])
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html, */*; q=0.01",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            follow_redirects=True,
            timeout=30,
        )
        self._session_ready = False

    async def _ensure_session(self) -> None:
        """GET the search page to establish ASP.NET session cookie and cache
        the form fields the server requires (ApplicationStatutes[N], etc)."""
        if self._session_ready:
            return
        resp = await self._client.get(self._base_url + SEARCH_PAGE)
        resp.raise_for_status()
        self._base_form = self._extract_form_fields(resp.text)
        self._session_ready = True

    @staticmethod
    def _extract_form_fields(html: str) -> Dict[str, str]:
        """Pull every input/select default value from the live search form.

        The Northgate Assure search server rejects POSTs (500) when fields
        like ApplicationStatutes[0..N].Selected are missing — they're built
        dynamically from the council's status configuration so we can't hard-
        code them.
        """
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form")
        if not form:
            return {}
        fields: Dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            if not name:
                continue
            input_type = (inp.get("type") or "").lower()
            value = inp.get("value", "")
            if input_type == "checkbox":
                # ASP.NET emits two inputs per checkbox: visible (type=checkbox)
                # and a hidden "false". Honour `checked` on the visible one;
                # the hidden one keeps the false so don't overwrite if checked.
                if inp.has_attr("checked"):
                    fields[name] = value if value else "true"
                elif name not in fields:
                    fields[name] = value or "false"
            elif input_type == "radio":
                if inp.has_attr("checked"):
                    fields[name] = value
                elif name not in fields:
                    fields.setdefault(name, "")
            else:
                # text, hidden, submit etc.
                fields[name] = value
        for sel in form.find_all("select"):
            name = sel.get("name", "")
            if not name:
                continue
            chosen = sel.find("option", selected=True) or sel.find("option")
            fields[name] = chosen.get("value", "") if chosen else ""
        for ta in form.find_all("textarea"):
            name = ta.get("name", "")
            if name:
                fields[name] = ta.get_text() or ""
        return fields

    def _ajax_headers(self) -> Dict[str, str]:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": self._base_url + SEARCH_PAGE,
        }

    def _build_search_form(
        self,
        search_input: str = "",
        status_option: str = "ReceivedAnyTime",
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        any_status: bool = True,
    ) -> Dict[str, str]:
        """Build form data starting from the live form fields (which include
        ApplicationStatutes[N], status checkboxes, etc) and overriding with
        the search-specific values. Falls back to the legacy hard-coded set
        if _ensure_session hasn't run yet."""
        form: Dict[str, str] = dict(getattr(self, "_base_form", {}) or {})
        # Sensible defaults (only set when not present in the scraped form)
        form.setdefault("SearchFor", "PlanningApplications")
        form.setdefault("DisplayTPOs", "False")
        form.setdefault("DisplayWorksToTrees", "False")
        form.setdefault("DisplayEnforcements", "False")
        form.setdefault("DisplayMapSearch", "False")
        form.setdefault("SortOptions", "SortedByMostRecent")
        # Search-specific overrides
        form["SearchInput"] = search_input
        form["AnyStatus"] = "true" if any_status else "false"
        form["StatusOptions"] = status_option
        if from_date and to_date:
            form["StatusOptions"] = "CustomDateRange"
            form["FromDate"] = from_date.strftime(self.DATE_FORMAT)
            form["ToDate"] = to_date.strftime(self.DATE_FORMAT)
        return form

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        """Search for planning applications in a date range.

        Northgate Assure has no "list everything in date range" endpoint, so
        we union three strategies:
         1. Keyword search (postcode prefixes + reference fragments) with
            custom date range — basic search requires ≥3 chars SearchInput.
         2. Monthly list for each month overlapping the range, both
            ValidatedThisMonth and DecidedThisMonth statuses.
         3. Weekly list as a final fallback if both above are empty.
        """
        await self._ensure_session()

        seen: set = set()
        merged: List[ApplicationSummary] = []

        for src in (
            await self._search_by_date_range(date_from, date_to),
            await self._search_monthly_list(date_from, date_to),
        ):
            for s in src:
                if s.uid in seen:
                    continue
                seen.add(s.uid)
                merged.append(s)

        if merged:
            return merged

        # Last-resort fallback if nothing turned up
        return await self._search_weekly_monthly(date_from, date_to)

    async def _search_monthly_list(
        self, date_from: date, date_to: date
    ) -> List[ApplicationSummary]:
        """Use the Monthly list view for each month in the range.

        The form requires `SelectedMonth` (e.g. "April 2026") plus
        `MonthlyListStatus` set to ValidatedThisMonth or DecidedThisMonth.
        """
        results: List[ApplicationSummary] = []
        seen: set = set()

        # Build month labels covering [date_from, date_to]
        months: List[str] = []
        cur = date(date_from.year, date_from.month, 1)
        end = date(date_to.year, date_to.month, 1)
        while cur <= end:
            months.append(cur.strftime("%B %Y"))
            year, month = cur.year, cur.month
            if month == 12:
                cur = date(year + 1, 1, 1)
            else:
                cur = date(year, month + 1, 1)

        for month_label in months:
            for status in ("ValidatedThisMonth", "DecidedThisMonth"):
                form = dict(self._base_form)
                form["SearchInput"] = ""
                form["IsMonthlyListSearch"] = "true"
                form["IsWeeklyListSearch"] = "false"
                form["SelectedMonth"] = month_label
                form["MonthlyListStatus"] = status
                form["StatusOptions"] = "ReceivedAnyTime"
                try:
                    resp = await self._client.post(
                        self._base_url + SEARCH_RESULTS,
                        data=form,
                        headers=self._ajax_headers(),
                    )
                    if resp.status_code != 200:
                        continue
                    for s in self._parse_search_results(resp.text):
                        if s.uid in seen:
                            continue
                        seen.add(s.uid)
                        results.append(s)
                except httpx.HTTPError:
                    continue
        return results

    async def _search_by_date_range(
        self, date_from: date, date_to: date
    ) -> List[ApplicationSummary]:
        """POST search form with custom date range and paginate results."""
        # The basic search requires SearchInput >= 3 chars.
        # We search with common keywords that match broadly.
        all_summaries: List[ApplicationSummary] = []

        for keyword in self._keywords:
            form = self._build_search_form(
                search_input=keyword,
                from_date=date_from,
                to_date=date_to,
            )

            try:
                resp = await self._client.post(
                    self._base_url + SEARCH_RESULTS,
                    data=form,
                    headers=self._ajax_headers(),
                )
                if resp.status_code != 200:
                    continue

                html = resp.text
                if "error" in html.lower() and "occurred" in html.lower():
                    continue

                page_summaries = self._parse_search_results(html)
                for s in page_summaries:
                    if s.uid not in {x.uid for x in all_summaries}:
                        all_summaries.append(s)

                # Paginate
                all_summaries = await self._paginate_results(
                    html, form, all_summaries
                )
            except httpx.HTTPError:
                continue

        return all_summaries

    async def _search_weekly_monthly(
        self, date_from: date, date_to: date
    ) -> List[ApplicationSummary]:
        """Use weekly/monthly list endpoint as fallback search strategy."""
        # Try weekly list first (works on most councils), then monthly
        for is_weekly in [True, False]:
            form = {
                "SearchFor": "PlanningApplications",
                "IsWeeklyListSearch": "true" if is_weekly else "false",
                "IsMonthlyListSearch": "false" if is_weekly else "true",
            }
            try:
                resp = await self._client.post(
                    self._base_url + WEEKLY_MONTHLY_RESULTS,
                    data=form,
                    headers=self._ajax_headers(),
                )
                if resp.status_code != 200:
                    continue
                results = self._parse_search_results(resp.text)
                if results:
                    results = await self._paginate_results(
                        resp.text, form, results
                    )
                    return results
            except httpx.HTTPError:
                continue
        return []

    async def _paginate_results(
        self,
        first_page_html: str,
        form: Dict[str, str],
        existing: List[ApplicationSummary],
    ) -> List[ApplicationSummary]:
        """Follow pagination links in search results."""
        seen_uids = {s.uid for s in existing}
        all_summaries = list(existing)
        current_html = first_page_html

        for page_num in range(1, self.MAX_PAGES):
            # Check for pagination controls
            if not self._has_next_page(current_html, page_num):
                break

            # Update pagination parameters in form
            paginated_form = dict(form)
            paginated_form["PagingParameters.CurrentPageIndex"] = str(page_num)
            paginated_form["IsPaginationClicked"] = "true"

            try:
                resp = await self._client.post(
                    self._base_url + SEARCH_RESULTS,
                    data=paginated_form,
                    headers=self._ajax_headers(),
                )
                if resp.status_code != 200:
                    break

                current_html = resp.text
                new_summaries = self._parse_search_results(current_html)
                if not new_summaries:
                    break

                added = False
                for s in new_summaries:
                    if s.uid not in seen_uids:
                        seen_uids.add(s.uid)
                        all_summaries.append(s)
                        added = True

                if not added:
                    break

            except httpx.HTTPError:
                break

        return all_summaries

    @staticmethod
    def _has_next_page(html: str, current_page: int) -> bool:
        """Check if pagination controls indicate more pages."""
        soup = BeautifulSoup(html, "html.parser")
        # Look for pagination links or "Next" button
        paging = soup.find(id="generalSearchPagination") or soup.find(
            class_="pagination"
        )
        if not paging:
            # Check for PagingClick function calls
            return bool(re.search(r"PagingClick\(", html))
        return True

    def _parse_search_results(self, html: str) -> List[ApplicationSummary]:
        """Extract application references and URLs from search results HTML.

        Northgate Assure results use various layouts:
        - Table rows with data-redirect-url attributes pointing to overview pages
        - Links with applicationNumber query parameter
        - Table cells containing reference numbers
        """
        if not html or len(html) < 50:
            return []

        soup = BeautifulSoup(html, "html.parser")
        summaries: List[ApplicationSummary] = []
        seen: set = set()

        # Pattern 1: Elements with data-redirect-url (primary Northgate Assure pattern)
        for el in soup.find_all(attrs={"data-redirect-url": True}):
            url = el["data-redirect-url"]
            ref = self._extract_ref_from_url(url)
            if ref and ref not in seen:
                seen.add(ref)
                abs_url = url if url.startswith("http") else self._base_url + url
                summaries.append(ApplicationSummary(uid=ref, url=abs_url))

        # Pattern 2: Links to OnlinePlanningOverview
        for link in soup.find_all("a", href=re.compile(r"OnlinePlanningOverview|applicationNumber")):
            href = link.get("href", "")
            ref = self._extract_ref_from_url(href)
            if not ref:
                ref = link.get_text(strip=True)
            if ref and ref not in seen:
                seen.add(ref)
                abs_url = href if href.startswith("http") else self._base_url + href
                summaries.append(ApplicationSummary(uid=ref, url=abs_url))

        # Pattern 3: Table rows with reference-like text in first column
        if not summaries:
            for tr in soup.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    first_text = tds[0].get_text(strip=True)
                    # Hyndburn refs look like 11/21/0134 or HYNDBURN/2024/0001
                    if re.match(r"\d{2}/\d{2}/\d{4}$", first_text) or re.match(
                        r"[A-Z]+/\d{4}/\d+$", first_text
                    ):
                        if first_text not in seen:
                            seen.add(first_text)
                            link = tr.find("a", href=True)
                            url = ""
                            if link:
                                href = link["href"]
                                url = (
                                    href
                                    if href.startswith("http")
                                    else self._base_url + href
                                )
                            else:
                                url = (
                                    f"{self._base_url}{DETAIL_PAGE}"
                                    f"?applicationNumber={quote_plus(first_text)}"
                                )
                            summaries.append(
                                ApplicationSummary(uid=first_text, url=url)
                            )

        # Pattern 4: SearchResultsToHighlight span elements
        if not summaries:
            for span in soup.find_all(class_="SearchResultsToHighlight"):
                text = span.get_text(strip=True)
                if re.match(r"\d{2}/\d{2}/\d{4}$", text) and text not in seen:
                    seen.add(text)
                    url = (
                        f"{self._base_url}{DETAIL_PAGE}"
                        f"?applicationNumber={quote_plus(text)}"
                    )
                    summaries.append(ApplicationSummary(uid=text, url=url))

        return summaries

    @staticmethod
    def _extract_ref_from_url(url: str) -> Optional[str]:
        """Extract applicationNumber from a Northgate Assure URL."""
        match = re.search(r"applicationNumber=([^&]+)", url)
        if match:
            ref = match.group(1)
            # URL-decode common patterns
            ref = ref.replace("%2F", "/").replace("%2f", "/")
            ref = ref.replace("%20", " ").replace("+", " ")
            return ref.strip()
        return None

    async def fetch_detail(self, application: ApplicationSummary) -> ApplicationDetail:
        """Fetch the detail/overview page for a single application."""
        await self._ensure_session()

        url = application.url
        if not url or "OnlinePlanningOverview" not in url:
            url = (
                f"{self._base_url}{DETAIL_PAGE}"
                f"?applicationNumber={quote_plus(application.uid)}"
            )

        try:
            resp = await self._client.get(
                url,
                headers={"Referer": self._base_url + SEARCH_PAGE},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return ApplicationDetail(
                reference=application.uid,
                address="",
                description="",
                url=url,
            )

        return self._parse_detail_page(resp.text, application.uid, url)

    def _parse_detail_page(
        self, html: str, uid: str, url: str
    ) -> ApplicationDetail:
        """Parse application details from the overview page.

        Northgate Assure overview pages use various layouts:
        - Definition lists (dt/dd)
        - Table rows (th/td or label/value)
        - Div-based label/value pairs with specific classes
        """
        soup = BeautifulSoup(html, "html.parser")
        data: Dict[str, str] = {}

        # Extract from table rows (th/td pattern)
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                label = th.get_text(strip=True).rstrip(":").strip().lower()
                value = td.get_text(separator=" ", strip=True)
                if value:
                    data[label] = value

        # Extract from dt/dd pairs
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                label = dt.get_text(strip=True).rstrip(":").strip().lower()
                value = dd.get_text(separator=" ", strip=True)
                if value:
                    data[label] = value

        # Extract from label-value div pairs (Northgate Assure pattern)
        for label_el in soup.find_all("label"):
            value_el = label_el.find_next_sibling()
            if value_el:
                label = label_el.get_text(strip=True).rstrip(":").strip().lower()
                value = value_el.get_text(separator=" ", strip=True)
                if value and label:
                    data[label] = value

        # Extract from span label + following text (GDS pattern)
        for span in soup.find_all("span", class_="govuk-summary-list__key"):
            value_div = span.find_next_sibling(class_="govuk-summary-list__value")
            if value_div:
                label = span.get_text(strip=True).rstrip(":").strip().lower()
                value = value_div.get_text(separator=" ", strip=True)
                if value:
                    data[label] = value

        reference = (
            data.get("application number")
            or data.get("application no")
            or data.get("reference")
            or data.get("ref no")
            or data.get("application reference")
            or uid
        )
        address = (
            data.get("site address")
            or data.get("address")
            or data.get("location")
            or data.get("site location")
            or ""
        )
        description = (
            data.get("proposal")
            or data.get("description")
            or data.get("development description")
            or data.get("description of proposal")
            or data.get("development")
            or ""
        )

        return ApplicationDetail(
            reference=reference,
            address=address,
            description=description,
            url=url,
            application_type=(
                data.get("application type")
                or data.get("type")
                or data.get("type of application")
            ),
            status=(
                data.get("status")
                or data.get("application status")
            ),
            decision=(
                data.get("decision")
                or data.get("decision type")
            ),
            date_received=_parse_date(
                data.get("date received")
                or data.get("received date")
                or data.get("date registered")
            ),
            date_validated=_parse_date(
                data.get("date validated")
                or data.get("validated date")
                or data.get("valid date")
                or data.get("registration date")
            ),
            ward=(
                data.get("ward")
                or data.get("electoral ward")
            ),
            parish=(
                data.get("parish")
                or data.get("parish council")
            ),
            applicant_name=(
                data.get("applicant")
                or data.get("applicant name")
            ),
            case_officer=(
                data.get("case officer")
                or data.get("officer")
                or data.get("planning officer")
            ),
            raw_data=data,
        )
