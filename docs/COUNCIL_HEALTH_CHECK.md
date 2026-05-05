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
    "lookback_monotone":    {"violations": 0}
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
