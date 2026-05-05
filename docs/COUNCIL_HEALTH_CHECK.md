# Council Scraper Health Check — Test Specification

A health-check tool runs each enabled council scraper through a fixed
battery of probes and compares the results to what a real browser sees
on the council's public planning portal. Outputs a per-council pass/
fail report so we can spot regressions quickly and triage outright
breakage.

## Goals

1. **Detect silent breakage** — councils returning 0 or 1 apps when they
   should return many (the failure mode that prompted this spec).
2. **Detect partial pagination** — councils returning the first page
   only when the platform caps page size below the real total.
3. **Detect silent result caps** — councils returning ~50 apps when the
   real total is 500+ because the scraper isn't bisecting on a
   "Too many results" error.
4. **Detect detail-page breakage** — `gather_ids` returns N but every
   `fetch_detail` fails (Birmingham/Tamworth pattern), so the dashboard
   shows `found=N updated=0` with empty applications.
5. **Detect stale data** — references that look like internal IDs
   (Salesforce `a1MP200000…`) instead of council reference numbers
   (Eastleigh pattern).

## Test scenarios per council

For every enabled council, run these probes in order. Stop the council's
test on the first hard failure (HTTP 5xx, exception, completely empty
result that the platform shouldn't produce); otherwise capture the
metrics from each probe.

| ID  | Probe                    | Range / Action                                                  | Success criteria                                                                                       |
| --- | ------------------------ | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| P1  | `scrape_7d`              | `gather_ids` over the last 7 days                               | Returns ≥ 0 apps without raising. Most councils have ≥1 in 7d; flag if 0.                              |
| P2  | `scrape_30d`             | `gather_ids` over the last 30 days                              | Returns ≥ P1 count. **Must be > 0** for every council with population > 50,000.                        |
| P3  | `scrape_60d`             | `gather_ids` over the last 60 days                              | Returns ≥ P2 count. Detects "Too many results" silent caps if P3 ≤ P2 by a wide margin.                |
| P4  | `scrape_180d`            | `gather_ids` over the last 180 days                             | Returns ≥ P3 count. Confirms bisection works at scale.                                                 |
| P5  | `pagination_integrity`   | Count `data-redirect-url` / detail links across all paginated POSTs | Sum across pages == total declared on first page (e.g. "X Results" header).                            |
| P6  | `cap_handling`           | Run a deliberately wide range (365d) on platforms that cap (~500) | Result must NOT equal exactly the platform cap. If it does, bisection isn't engaging.                  |
| P7  | `fetch_detail_sample`    | `fetch_detail` on first 3 results from P2                       | All 3 must populate `address` AND `description` AND `reference != Salesforce-Id-pattern`.              |
| P8  | `frontend_match`         | Playwright: replay the user's own search at the same URL/dates  | Scraper count is within ±5% (or ±5 apps, whichever is greater) of the rendered result count.           |
| P9  | `reference_format`       | Inspect P2 result references                                    | None match Salesforce internal Id regex `^a[0-9A-Za-z]{14,18}$`. None are empty.                       |
| P10 | `lookback_monotone`      | Compare P1 ⊆ P2 ⊆ P3 ⊆ P4 by uid                                | A wider range must contain every uid from a narrower range.                                            |
| P11 | `date_range_respected`   | Compare every returned `date_received` (or `date_validated` fallback) against P2 range | 0 records may have a date outside `[date_from, date_to]`. Catches scrapers like ScillyIsles that return everything ever and rely on the worker to filter. |
| P12 | `description_label_stripped` | For each detail in P7, check that `description` doesn't start with a field-label prefix (`Proposal :`, `Description:`, `Application number:` etc) | 0 stripped-label leakage. Catches the ScillyIsles inline-label bug. |
| P13 | `date_parse_yield`       | Of the apps returned by P2, count how many have a non-null `date_received` OR `date_validated` | ≥ 80%. A high null-date rate usually means a date format the parser doesn't handle (e.g. Drupal's `Friday, 1 May, 2026`). |
| P14 | `idempotency`            | Run gather+detail twice back-to-back, diff the second pass against the first | Identical refs both runs; same parsed values. Catches non-deterministic ordering and detail-URL drift. |
| P15 | `detail_url_stability`   | Re-fetch a known-historical detail URL captured 24h+ ago (if available) | 200 OK or a redirect to a stable equivalent. Catches PlanningExplorer-style ephemeral `PARAM0=N` URLs that expire. |
| P16 | `field_label_vs_class`   | When a scraper extracts by CSS class (e.g. `field-name-field-date-received`), assert the visible label matches the assumed semantics | Mismatch raises a warning. Catches Scilly's `field-date-received` being labelled "Valid date". |
| P17 | `worker_insert_parity`   | Compare `len(gather_ids)` vs `last_run.applications_updated` from a real DB run | Discrepancy > 0 with no fetch errors logged means silent fetch_detail failures (Tamworth pattern: gather returns 15, every detail 500s, 0 inserted). |

## Things we already know break, and how the test should report them

These are the failure signatures we've debugged in the recent fix
commits. The health-check should detect each automatically.

### "Returned 0 because gather_ids is dead-code"
- Pattern: scraper has `gather_ids() return []` and a `scrape()` override.
- Detection: scraper class with `gather_ids` returning `[]` for any range.
- Probe: P2 returns 0; the worker's new `scrape()` path should fix this
  but flag the scraper for review.

### "Detail urljoin drops a path segment"
- Pattern: P5 OK, P7 fails with 404 on every detail.
- Detection: P7 fails for ≥ 50% of sample.
- Diagnostic: log the constructed detail URL vs the search-results URL.

### "Salesforce custom-package fields not mapped"
- Pattern: P9 shows `a1MP200000…` references; P7 shows empty fields.
- Detection: P9 reference regex match.
- Diagnostic: dump the raw record keys.

### "Idox / Northgate Assure silent results cap"
- Pattern: P3 ≈ P2 ≈ 500 (or platform-specific cap).
- Detection: P6 returns exactly 500 ± a few; or P3 equal to P2.
- Diagnostic: search the result HTML for `Too many results`.

### "ASP.NET checkbox+hidden duplicate fields stripped by dict()"
- Pattern: Northgate Assure councils return 0 from Advanced Search.
- Detection: P3 = 0 on a NorthgateAssure scraper.
- Diagnostic: capture the request body and verify `AnyStatus` / `Validated`
  appear at least twice (once `true`/once `false`).

### "Server-side WAF (Incapsula / Cloudflare / Azure WAF / IP block)"
- Pattern: First GET returns < 1KB or contains `Incapsula`, `Just a moment`,
  `Azure WAF`, `Access Denied`.
- Detection: log `len(response.text) < 1000` and a regex for those markers.
- Action: tag the council `waf_blocked` and skip remaining probes.

### "Pagination 429 from Buckinghamshire-style strict rate limits"
- Pattern: P5 gets 429 on a deep page, scrape fails entirely.
- Detection: HTTPStatusError on a paginated request after page 5+.
- Action: scraper should break-and-keep-what-we-have (already
  implemented for idox; verify per platform).

### "List page has no date column → scraper returns everything ever"
- Pattern: scraper walks all pages of a paginated list with no per-row
  date metadata, then trusts the worker to filter — but the worker
  upserts unconditionally, so DB grows by thousands per scrape.
- Detection: P11 sees records older than `date_from`. P2 ≫ what the
  council's known monthly volume should be.
- Diagnostic: list page row HTML — does it contain any date-shaped
  text (`/\d{1,2}\s+[A-Z][a-z]+\s+\d{4}/`)?
- Action: scraper must filter at gather_ids time, either via a column
  on the list, a year/month embedded in the reference, or by GETting
  detail pages early and discarding pre-`date_from` apps. The worker
  doesn't filter.

### "Detail server returns 500 for every PARAM (Tamworth)"
- Pattern: gather_ids returns N apps cleanly; every fetch_detail 500s.
  Dashboard shows `found=N updated=0`. Different from urljoin-404
  because the URL path is correct — the upstream council's detail
  endpoint is broken/misconfigured.
- Detection: P17 (worker_insert_parity) shows 100% gather but 0
  inserts; P7 (fetch_detail_sample) gets 500 from every URL.
- Action: distinguish from urljoin bugs by checking the URL renders
  in a browser. If the browser also 500s, it's upstream → disable
  the council with a `disabled_reason` and revisit periodically.

### "Date format the parser doesn't recognise"
- Pattern: P13 (date_parse_yield) below threshold. Apps insert OK but
  every record has `date_received=NULL` and `date_validated=NULL`.
- Common offenders we hit:
  - Drupal long form: `Friday, 1 May, 2026` (weekday + commas)
  - Idox: `Wed 16 Apr 2026` (short weekday, no commas)
  - PlanningRegister: `04/22/2026 00:00:00` (US ordering)
  - Salesforce ISO: `2026-04-24T00:00:00.000Z`
- Action: the scraper's `_parse_date` should try all variants used by
  any UK council; a single helper module avoids per-platform drift.

### "Field CSS class disagrees with visible label"
- Pattern: scraper picks fields by CSS class (e.g.
  `field-name-field-date-received`) but the council renders a
  different label ("Valid date") so the value is going into the
  wrong column.
- Detection: P16 dumps `<label>: <value>` pairs and compares the
  visible label to the column we're mapping into.
- Action: be explicit about which date semantic the council exposes;
  if only one date is published, populate both `date_received` and
  `date_validated` so existing dashboards keep working.

### "Description starts with the field label"
- Pattern: stored description literally begins with `Proposal :` or
  `Description: ` or `Application number: ` because the field-item
  div was extracted with the inline label intact.
- Detection: P12 string-prefix check.
- Action: strip a leading `^[A-Z][a-z ]+:\s*` before storing, OR
  prefer the field-item span over the wrapping div.

### "Stale records that never get touched"
- Pattern: the DB has rows for the council from before the scraper
  was fixed (e.g. ScillyIsles' 1000+ everything-ever records, or
  Eastleigh's three Salesforce-Id stubs). New scrapes write new rows
  with the correct format but the old ones persist.
- Detection: count rows with no recent `last_seen_at` update vs
  total. Or: count rows whose `reference` matches a known
  bad-pattern regex (Salesforce-Id, all-numeric where council uses
  alphanumeric).
- Action: write a one-off cleanup migration; not a scraper bug per
  se but the health-check should surface it.

## Output format

Emit one JSON line per council so the report can be diffed and consumed
by tooling later:

```json
{
  "code": "peakdistrict",
  "platform": "northgate_assure",
  "ts": "2026-05-05T13:00:00Z",
  "probes": {
    "scrape_7d":   {"count":  6, "elapsed_ms":  2300},
    "scrape_30d":  {"count": 44, "elapsed_ms":  8500},
    "scrape_60d":  {"count":140, "elapsed_ms": 21000},
    "scrape_180d": {"count":524, "elapsed_ms": 78000},
    "pagination_integrity": {"declared": 64, "captured": 44, "diff_pct": -31.25},
    "cap_handling":         {"hit_cap": false, "result": 524},
    "fetch_detail_sample":  {"sampled": 3, "ok": 3, "empty_addr": 0, "empty_desc": 0},
    "frontend_match":       {"scraper": 44, "browser": 64, "diff_pct": -31.25},
    "reference_format":     {"ok": 44, "salesforce_id_like": 0, "empty": 0},
    "lookback_monotone":    {"violations": 0},
    "date_range_respected": {"out_of_range": 0},
    "description_label_stripped": {"leaks": 0},
    "date_parse_yield":     {"with_date_pct": 100.0},
    "idempotency":          {"second_pass_diff": 0},
    "detail_url_stability": {"sampled": 5, "still_ok": 5},
    "field_label_vs_class": {"warnings": []},
    "worker_insert_parity": {"gather": 44, "inserted": 44, "diff": 0}
  },
  "verdict": "PARTIAL",
  "notes": [
    "pagination_integrity: server reports 64 results but only 44 unique app refs across all pages — investigate"
  ]
}
```

`verdict` is one of:
- `OK` — all probes pass.
- `PARTIAL` — at least one soft check (pagination_integrity / frontend_match diff) failed but no hard failures.
- `FAIL` — at least one hard failure (P2=0 for a town council, or P7=0% success).
- `SKIP` — council disabled in config, or WAF-blocked.

## Per-platform quirks the test should know about

The health-check should ship with a small registry of platform
fingerprints so its diagnostics are sharper. None of these need to
*pass*; they're hints when something fails.

| Platform              | Quirk                                                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `idox`                | Cap "Too many results found" at ~500. Form action redirects to a per-instance hostname after first GET. CSRF token in `input[name="_csrf"]`. Some sites only expose `applicationValidated` (no `applicationReceived`) — date-field fallback required. |
| `planning_explorer`   | Detail-link `urljoin` base must be the StdResults page URL, NOT `config.base_url` (config typically lacks `/Generic/`). Whitespace inside `href="StdDetails.aspx?…PARAM0=\n\t\t…"` is collapsed by httpx but worth flagging. |
| `northgate`           | New-style portal. `/Search/Advanced` → `/Search/Results`. Disclaimer flow is sticky via cookies.                   |
| `northgate_assure`    | Form must be sent as ordered `(key, value)` pairs — ASP.NET checkbox+hidden duplicates (e.g. `AnyStatus=true&AnyStatus=false`) get dedup'd by Python dict and the model binder rejects the search. Pagination uses a separate URL `/SearchResultsForPagination` with `PagingParameters.PageSize/CurrentPageIndex/TotalRecords`. Advanced Search is the only endpoint that lists all apps in a date range. |
| `planning_register`   | Disclaimer cookie must be carried. `/Search/Standard` 500s on certain date-param names per council — try `AcknowledgeLetterDateFrom`, `DateReceivedFrom`, `DateValidFrom`, `DateDeterminedFrom` in turn. Retry on 502/503/504 (Pantheon-style transient gateway errors). |
| `salesforce_direct`   | Two managed-package prefixes: `arcusbuiltenv__` (Anglesey/Carmarthenshire/Wiltshire) and `arcusbuilt__` (Eastleigh, no `env`). Reference is `Name`, not `Id`. Address may live in a related-record dict (`Location__r`). |
| `scillyisles`         | Drupal 7. List has no date column — must filter by year-prefix in reference, then per-app detail GET. Pantheon WAF blocks Python TLS — uses curl subprocess. Dates are `Friday, 1 May, 2026`. CSS `field-date-received` is labelled "Valid date" in the UI. |
| `tascomi`             | Weekly received-list endpoint. Some sites don't accept date-range queries — must walk the most-recent N weeks and filter client-side. |
| `kensington` (custom) | SolidJS SPA. Requires Playwright. Date filter is client-side after fetching all DOM-rendered cards. |
| `boston` (custom)     | Granicus form behind Cloudflare. Headless Playwright is detected and re-challenged. Currently disabled. |
| `jersey` (custom)     | ASP.NET form with date-dropdowns. Cookie banner overlays results table — extract via `<dl>/<dt>/<dd>` pairs in the result `<li>`, not generic table rows. |
| `tandridge` (custom)  | Three-phase ASP.NET ViewState postback. The "Search" button has `name=None` — submit by clicking the input directly. |

## Cadence and coverage

- Run nightly on all enabled councils. Compare today's verdict vs
  yesterday's: any new `FAIL` or new `PARTIAL` is the signal to triage.
- For platform-level fixes (e.g. NorthgateAssure changes), run the
  health-check ad-hoc against every council using that platform before
  merging.
- A separate "smoke" subset (one council per platform) runs hourly so
  we catch site-wide WAF changes faster than nightly.

## Implementation notes for whoever writes the script

- Use `asyncio.gather` with a per-domain semaphore (`max_concurrent=2`)
  so multi-council runs respect the same rate limits the live scrapers do.
- Per-council total timeout: 5 minutes. Most councils complete in < 60 s;
  Manchester / Chiltern / Peak District (heavy bisection) are the
  outliers and warrant the headroom.
- For P8 (frontend_match), use Playwright headed-by-default so any
  hidden CAPTCHA / Cloudflare challenge is observable; only the actual
  HTTP request the page makes is what we compare against.
- Do **not** persist anything to the production DB — the health check
  is read-only.
- Persist a JSON report per run to `reports/health-check/YYYY-MM-DD.jsonl`.
- Emit non-zero exit code if any council's verdict regressed vs the
  previous run, so CI / cron can alert.

## Out of scope

- Detecting newly-launched councils on existing platforms (separate
  discovery problem).
- Detecting URL changes on a council's site (covered by P8 picking up
  a 404).
- Validating the *content* of fetched data (no spell-checking proposals).
