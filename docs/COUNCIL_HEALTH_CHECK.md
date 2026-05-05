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
| P18 | `extracted_value_not_label` | For each detail in P7, check that no extracted field's value equals a known field-label literally (`"Address"`, `"Reference"`, `"Proposal"`, `"Site Address"`, `"Status"`). | 0 fields have label-as-value. Catches scrapers like NorthDevon that pick up the wrapping `<h1>` element instead of the sibling `<h2>` value. |
| P19 | `form_method_matches`    | Compare the `<form method=>` attribute to the HTTP verb the scraper actually sends. | Verb must match. WestDunbartonshire's form is `method=get` — POSTing silently ignores the date filter and returns 100 random apps spanning all years. |
| P20 | `form_action_resolves`   | Compare the `<form action=>` URL to the URL the scraper posts/gets to. | Must match (after urljoin). Posting to the form page itself reshows the empty form on most sites; the action URL is usually a different endpoint (Rochford `dcdisplayinitialx.asp`, NorthDevon `/Search/Results`). |
| P21 | `encoding_decoded_clean` | Scan extracted strings for the Unicode replacement char `�` and for windows-1252 high-bytes (0x91–0x97) that would arrive as mojibake under utf-8 decode. | 0 occurrences. WestDunbartonshire serves `text/html; charset=windows-1252` and httpx's default utf-8 decode raised `UnicodeDecodeError` on smart-quote bytes. |
| P22 | `label_keys_no_colon`    | Inspect the keys of any `data` dict produced by `_extract_table_pairs` / `_extract_dl_pairs` helpers. | No key ends with `:`. WestDunbartonshire's helper kept the trailing colon (`'Reference Number:'`), so every downstream `data.get('Reference Number')` returned `''`. |
| P23 | `selector_uniqueness`    | For each selector that uses `:-soup-contains('X')`, assert no OTHER `<dt>/<label>/<span>` on the page contains `'X'` as substring. | 0 cross-matches. Rochford `dt:-soup-contains('Proposal')` also matched `'Address Of Proposal'`, mis-filling description with the address. |
| P24 | `selector_label_case`    | Scrape the visible labels from the detail page and compare against the strings inside `:-soup-contains(...)` selectors. | 0 case-only mismatches. Rochford detail labels were `'Address Of Proposal'` / `'Type Of Application'` (capital `'O'`); selectors said `'of'` and matched nothing. |
| P25 | `pagination_url_distinct`| Track every page URL the scraper visits during pagination. | All distinct. SwiftLG `select_one(StartIndex)` always returned the first link (page 2) so we re-walked the same pages 12× until something stopped us. |
| P26 | `pagination_endpoint_correct` | Some platforms paginate via a SEPARATE endpoint than the initial search. Check the scraper hits the right one for page 2+. | NorthgateAssure paginates via `/SearchResultsForPagination` (not `/OnlinePlanningSearchResults`). Re-using the search URL silently returns the first page each time. |
| P27 | `form_pairs_preserve_duplicates` | If the form contains ASP.NET checkbox+hidden pairs (visible `name=X value=true` plus hidden `name=X value=false`), the scraper must send BOTH values, not let Python's dict dedupe to one. | NorthgateAssure Advanced Search returns 0 with one `AnyStatus` value sent and 64 with two. Send form data as `[(k,v), …]` ordered pairs, not a dict. |
| P28 | `salesforce_quick_search_cap` | Salesforce Arcus `PR_SearchService.search` caps at 250 records and returns oldest-first. Check `thresholdHit=True` and drill the search by reference prefix. | If `thresholdHit=True` and the scraper doesn't drill, recent apps are silently missed. Reading: a single `PL/26` query returned only PL/26/0001..0250, missing everything after January. |
| P29 | `salesforce_ref_prefix_literal` | Salesforce reference matching is literal substring. A 1-digit prefix like `PL/26/5` matches zero rows because all refs are 4-digit (no `PL/26/5XXX`). | Use 2-digit prefix chunks (`PL/26/00`..`PL/26/09`) so each search bucket corresponds to a real ref-number range. |
| P30 | `salesforce_package_prefix` | Salesforce field names depend on the council's managed-package install: `arcusbuiltenv__Site_Address__c` vs `arcusbuilt__Portal_Site_Address__c`. | Selectors must accept both prefixes plus per-council custom fields. Eastleigh's records had Name set but every other field empty because the scraper only looked for `arcusbuiltenv__`. |

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

### "Form HTTP method mismatch"
- Pattern: the form has `<form method="get">` but the scraper POSTs.
  Some servers silently 200 the POST and return the unfiltered base
  list (e.g. WestDunbartonshire returned ~100 random apps spanning
  2006–2026 regardless of the date filter we sent).
- Detection: P19. Or: scrape says N but the same N appears across
  unrelated date ranges.
- Action: read `<form method=>` and use that verb.

### "Wrong endpoint URL — POSTing to the form page"
- Pattern: the search form's `action=` attribute points at a
  different URL than the page itself, but the scraper posts to the
  page URL. The server reshows the empty form (HTTP 200, no error)
  and the parser finds no result rows.
- Examples: Rochford form on `dcsearch_appx.asp` with action
  `dcdisplayinitialx.asp`; NorthDevon form action `/Search/Results`
  while the scraper hit `/Search/Standard`.
- Detection: P20. Or: response title equals the search-page title
  rather than a results-page title.
- Action: read the form's `action=` attribute and resolve it via
  urljoin.

### "windows-1252 page → utf-8 decode mojibake"
- Pattern: legacy ASP/IIS sites declare `Content-Type: text/html;
  charset=windows-1252` but httpx defaults to utf-8 — smart quotes
  (`0x91`–`0x94`) and currency bytes raise UnicodeDecodeError, OR
  the parser sees `�` in extracted strings.
- Detection: P21.
- Action: decode `resp.content.decode('windows-1252', errors='replace')`
  before handing the body to BeautifulSoup.

### "Trailing colon in label key"
- Pattern: a `_extract_table_pairs` helper reads `<th>Reference Number:</th><td>DC26/01</td>` and stores the key with the colon (`'Reference Number:'`), so every downstream `data.get('Reference Number')` returns the empty default.
- Detection: P22. Or: P7 fails for ALL rows with empty addresses/descriptions even though gather works.
- Action: `key.rstrip(':').strip()` on every extracted label.

### "soupsieve `:-soup-contains(...)` is substring, not exact"
- Pattern: `dt:-soup-contains('Proposal')` matches both `'Proposal:'` and `'Address Of Proposal:'`, so description ends up filled with the site address.
- Detection: P23. Programmatic check: enumerate all `<dt>` text on the page and grep for the selector substring; a match count > 1 means the selector is ambiguous.
- Action: walk the dl/dt/dd structure manually for exact-label matching.

### "CSS selector label case-mismatch"
- Pattern: scraper says `:-soup-contains('Address of Proposal')` but the council renders `'Address Of Proposal'` (capital `'O'`). soupsieve's contains is case-sensitive.
- Detection: P24.
- Action: list both casings in the selector list, or normalise via the dl-pair walker.

### "Pagination link selector picks the wrong link"
- Pattern: `select_one('a[href*=StartIndex]')` returns the FIRST anchor with that param — usually page 2 or "Previous". Each page has all paging links, so the scraper revisits the same pages and accumulates duplicates. The DB upsert dedupes by reference so apparent `found=720, inserted=0` (SwiftLG/Walsall before fix).
- Detection: P25. Or: gather count > 5× the platform's "X Results" header.
- Action: walk paging URLs deduped by URL set, OR enumerate `StartIndex=1, StartIndex=11, StartIndex=21, …` directly.

### "Pagination uses a different endpoint"
- Pattern: NorthgateAssure paginates via
  `/Planning/OnlinePlanning/SearchResultsForPagination` while the
  initial search posts to `/Planning/OnlinePlanning/OnlinePlanningSearchResults`.
- Detection: P26. Diagnostic: the page-2 POST returns the same first
  20 results and `_has_next_page` keeps reporting True forever.
- Action: read the form action with `data-url` for the paging link
  (e.g. `<div id="generalSearchPagination" data-url="...">`).

### "ASP.NET checkbox+hidden duplicates being dict-dedup'd"
- Pattern: the form contains `<input type=checkbox name=AnyStatus value=true>` plus `<input type=hidden name=AnyStatus value=false>` — the browser submits BOTH values. Our scraper builds a `dict` from the form fields and Python dedupes the duplicate keys; the model binder then reads only the second value (`false`) and treats the criterion as missing → 0 results.
- Detection: P27.
- Action: send form data as an ordered list of `(key, value)` pairs and `urlencode` it manually; httpx's `content=` accepts the body as bytes.

### "Salesforce Arcus quick-search 250-row cap"
- Pattern: `PR_SearchService.search` returns the oldest 250 records and sets `thresholdHit=True`; if the scraper doesn't drill, the most-recent ~half of the year's apps are silently missed. Reading at PL/26: cap returned PL/26/0001..0250, latest received-date 2026-01-02 — everything since January was lost.
- Detection: P28. The scraper should treat `thresholdHit=True` as a soft failure that triggers a more-specific search.
- Action: drill by 2-digit reference-prefix chunks (`PL/26/00`..`PL/26/09`).

### "Salesforce reference search is literal substring"
- Pattern: refs are 4-digit sequences (e.g. PL/26/0508). A 1-digit prefix like `PL/26/5` matches ZERO rows because no ref starts `PL/26/5XXX`. A 2-digit prefix `PL/26/05` matches the 100-record `PL/26/05XX` bucket.
- Detection: P29.
- Action: build search terms by appending the leading 2 digits, not 1 digit.

### "Salesforce managed-package prefix variation"
- Pattern: different councils install different versions of the
  Arcus managed package: `arcusbuiltenv__Site_Address__c` vs
  `arcusbuilt__Portal_Site_Address__c`, with related-record dicts
  like `arcusbuilt__Location__r.arcusgazetteer__Address__c` for
  some councils (Eastleigh).
- Detection: P30. Diagnostic: dump `record.keys()` for one record
  and grep for `__c` suffixes — any prefix not in the scraper's
  field-name table is a miss.
- Action: list both `arcusbuiltenv__` and `arcusbuilt__` prefixes
  in every field accessor; check related-record dicts for address
  and case-officer.

### "GET-only redirect strips path segment via urljoin"
- Pattern: `urljoin("https://x/swift/apas/run", "WPHAPPDETAIL?...")`
  produces `https://x/swift/apas/WPHAPPDETAIL?...` — `run` is
  treated as a file, not a directory, so it's REPLACED by the
  relative href. Detail URLs then 404 silently per app.
- Detection: P7 fails 100% with 404.
- Action: append `/` to the base before urljoin: `base.rstrip('/') + '/'`.

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
| `planning_register`   | Disclaimer cookie must be carried. Three search-path variants exist: GET `/Search/Standard` (most councils, with `AcknowledgeLetterDateFrom` etc); POST `/Search/Results` (NorthDevon, SouthOxfordshire, Whitehorse — `DateReceivedFrom` etc + `__RequestVerificationToken`); POST `/Search/List` (Leicestershire — `SearchParams.DateReceivedFrom` + `AdvancedSearch=True`). Retry on 502/503/504 (Pantheon-style transient gateway errors). |
| `salesforce_direct`   | Two managed-package prefixes: `arcusbuiltenv__` (Anglesey/Carmarthenshire/Wiltshire) and `arcusbuilt__` (Eastleigh, no `env`). Reference is `Name`, not `Id`. Address may live in a related-record dict (`Location__r`). |
| `scillyisles`         | Drupal 7. List has no date column — must filter by year-prefix in reference, then per-app detail GET. Pantheon WAF blocks Python TLS — uses curl subprocess. Dates are `Friday, 1 May, 2026`. CSS `field-date-received` is labelled "Valid date" in the UI. |
| `tascomi`             | The `getReceivedWeeklyList` form needs `week=YYYY-MM-DD` (Monday of an ISO week) POSTed. A bare GET reshows the empty form on most councils — Stoke/Gloucestershire/Denbighshire showed 0 apps until we enumerated every Monday in the date range. |
| `swiftlg`             | Slow Oracle backend (Walsall: ~28s/POST, exceeded the 30s default httpx timeout). Pagination links list every page number with the same `?StartIndex=N` shape; `select_one` always picked page 2 → infinite reuse. base_url often lacks a trailing slash so `urljoin` drops the last path segment from detail URLs. |
| `westdunbarton`       | Classic ASP. Form is `method=get` with action `dcdisplayinitialx.asp` (NOT the search page itself). POSTing or hitting the wrong URL silently returns 100 random apps spanning all years. Page is windows-1252 encoded. Two-column tables keep trailing colons on labels. |
| `rochford`            | Astun GIS template portal. Search is GET to `DevelopmentControl.aspx` with all hidden form fields including a per-session `history` token — POSTing reshows the empty form. Result rows put the reference inside `<dd class="last">… <strong>26/00323/DOC</strong> received on <strong>21/04/2026</strong>` so a regex over the whole text concatenates date with ref. Detail labels have capital letters: `'Address Of Proposal'`, `'Type Of Application'`. |
| `salesforce` (Arcus)  | `PR_SearchService.search` quick-search caps at 250 records oldest-first and sets `thresholdHit=True`. Drill by 2-digit reference prefix (`PL/26/00`..`PL/26/09`) to keep each bucket under the cap. Reference matching is literal substring; 1-digit prefixes return zero. |
| `salesforce_direct`   | Two managed-package prefixes co-exist: `arcusbuiltenv__` (Anglesey/Carmarthenshire/Wiltshire) and `arcusbuilt__` (Eastleigh). Per-council custom fields like `Portal_Site_Address__c` are common; address can also live in a related-record dict (`Location__r.arcusgazetteer__Address__c`). Reference is `Name`, never `Id`. |
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
