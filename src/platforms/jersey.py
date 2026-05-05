"""Jersey planning scraper.

Crown dependency at gov.je. ASP.NET form with date dropdowns behind Cloudflare.
Requires Playwright to bypass Cloudflare and submit the ASP.NET form.
"""
import re
from datetime import date, datetime
from typing import List, Optional

from playwright.async_api import async_playwright

from src.core.config import CouncilConfig
from src.core.scraper import ApplicationDetail, ApplicationSummary, BaseScraper, ScrapeResult

SEARCH_URL = "https://www.gov.je/citizen/Planning/Pages/Planning.aspx"


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ["%d/%m/%Y", "%d %b %Y", "%d %B %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


class JerseyScraper(BaseScraper):

    def __init__(self, config: CouncilConfig):
        super().__init__(config)

    async def gather_ids(self, date_from: date, date_to: date) -> List[ApplicationSummary]:
        return []

    async def fetch_detail(self, app: ApplicationSummary) -> ApplicationDetail:
        return ApplicationDetail(reference=app.uid, address="", description="", url=app.url)

    async def scrape(self, date_from: date, date_to: date) -> ScrapeResult:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                page = await ctx.new_page()

                await page.goto(SEARCH_URL, timeout=30000)
                await page.wait_for_timeout(3000)

                # Fill date dropdowns
                prefix = "ctl00$PlaceHolderMain$PlanningRegisterSearchForm$"
                dates = {
                    "fromDay": str(date_from.day),
                    "fromMonth": str(date_from.month),
                    "fromYear": str(date_from.year),
                    "toDay": str(date_to.day),
                    "toMonth": str(date_to.month),
                    "toYear": str(date_to.year),
                }
                await page.evaluate(f"""(d) => {{
                    const set = (name, val) => {{
                        const el = document.querySelector('[name="' + name + '"]');
                        if (el) {{ el.value = val; el.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                    }};
                    set('{prefix}ddlFromDay', d.fromDay);
                    set('{prefix}ddlFromMonth', d.fromMonth);
                    set('{prefix}ddlFromYear', d.fromYear);
                    set('{prefix}ddlToDay', d.toDay);
                    set('{prefix}ddlToMonth', d.toMonth);
                    set('{prefix}ddlToYear', d.toYear);
                    document.querySelector('[name="{prefix}btnPlanningApplicationSearchSubmit"]').click();
                }}""", dates)

                await page.wait_for_timeout(10000)

                # Results live in <li><h2><a>REF</a></h2><dl><dt>Property:</dt><dd>ADDR</dd>...</dl></li>
                data = await page.evaluate("""() => {
                    const links = document.querySelectorAll('a[href*="PlanningApplicationDetail"]');
                    const results = [];
                    const seen = new Set();
                    for (const a of links) {
                        const ref = a.innerText.trim();
                        if (!ref || !ref.match(/[A-Z]+\\/\\d{4}/)) continue;
                        if (seen.has(ref)) continue;
                        seen.add(ref);
                        const container = a.closest('li') || a.closest('tr') || a.parentElement;
                        const fields = {};
                        if (container) {
                            const dl = container.querySelector('dl');
                            if (dl) {
                                const dts = dl.querySelectorAll('dt');
                                const dds = dl.querySelectorAll('dd');
                                for (let i = 0; i < dts.length && i < dds.length; i++) {
                                    const k = dts[i].innerText.replace(':','').trim().toLowerCase();
                                    fields[k] = dds[i].innerText.trim();
                                }
                            }
                        }
                        results.push({
                            ref,
                            href: a.href,
                            address: fields['property'] || fields['address'] || '',
                            proposal: fields['description'] || fields['proposal'] || '',
                            status: fields['status'] || '',
                            type: fields['type'] || '',
                        });
                    }
                    return results;
                }""")

                details = [
                    ApplicationDetail(
                        reference=d["ref"],
                        address=d.get("address", ""),
                        description=d.get("proposal", ""),
                        url=d.get("href") or SEARCH_URL,
                        status=d.get("status") or None,
                        application_type=d.get("type") or None,
                    )
                    for d in data
                ]

                await browser.close()
            return ScrapeResult(date_from=date_from, date_to=date_to, applications=details)
        except Exception as e:
            return ScrapeResult(date_from=date_from, date_to=date_to, error=str(e))
