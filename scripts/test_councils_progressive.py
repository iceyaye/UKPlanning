#!/usr/bin/env python3
"""Progressive council scraper test.

Tests each council one at a time with escalating date ranges:
  Phase 1: Last 7 days (1 week)
  Phase 2: Last 30 days (1 month) — only councils with 0 results from Phase 1
  Phase 3: 01/01/2026 to 30/01/2026 — only councils still with 0 results

Outputs: council_progressive_report.json
"""
import asyncio
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

from src.core.config import load_all_councils
from src.scheduler.registry import ScraperRegistry

CONFIG_DIR = Path(__file__).parent.parent / "src" / "config" / "councils"
REPORT_PATH = Path(__file__).parent.parent / "council_progressive_report.json"


async def close_scraper(scraper):
    if hasattr(scraper, "_client") and hasattr(scraper._client, "_client"):
        try:
            await scraper._client._client.aclose()
        except Exception:
            pass


async def test_gather(config, registry, date_from, date_to):
    """Test gather_ids for a single council. Returns (count, error_or_None)."""
    try:
        scraper = registry.create_scraper(config)
    except KeyError as e:
        return (-1, f"Unsupported platform: {e}")

    try:
        ids = await asyncio.wait_for(
            scraper.gather_ids(date_from, date_to),
            timeout=60
        )
        return (len(ids), None)
    except asyncio.TimeoutError:
        return (-1, "Timeout (60s)")
    except Exception as e:
        return (-1, str(e)[:300])
    finally:
        await close_scraper(scraper)


async def run():
    configs = load_all_councils(CONFIG_DIR)
    registry = ScraperRegistry()

    # Filter to enabled + supported
    enabled = []
    for c in configs:
        yml_path = CONFIG_DIR / f"{c.authority_code}.yml"
        data = yaml.safe_load(yml_path.read_text())
        if data.get("enabled") is not False and c.platform in registry.list_platforms():
            enabled.append(c)

    total = len(enabled)
    print(f"Testing {total} enabled & supported councils\n")

    # Phase definitions
    today = date.today()
    phases = [
        ("1_week", today - timedelta(days=7), today),
        ("1_month", today - timedelta(days=30), today),
        ("jan_2026", date(2026, 1, 1), date(2026, 1, 30)),
    ]

    # Results dict keyed by authority_code
    results = {}
    for c in enabled:
        results[c.authority_code] = {
            "authority_code": c.authority_code,
            "name": c.name,
            "platform": c.platform,
            "base_url": c.base_url,
            "phases": {},
            "final_status": None,
            "final_count": 0,
        }

    # ── Phase 1: 1 week ──
    phase_name, df, dt = phases[0]
    print(f"{'='*70}")
    print(f"PHASE 1: {phase_name} ({df} to {dt})")
    print(f"{'='*70}")
    to_test = list(enabled)
    phase_start = time.time()

    for i, config in enumerate(to_test):
        code = config.authority_code
        sys.stdout.write(f"  [{i+1}/{len(to_test)}] {config.name:40s} ... ")
        sys.stdout.flush()

        count, error = await test_gather(config, registry, df, dt)
        results[code]["phases"][phase_name] = {
            "count": count, "error": error,
            "date_from": str(df), "date_to": str(dt),
        }

        if count > 0:
            results[code]["final_status"] = "HAS_RESULTS"
            results[code]["final_count"] = count
            print(f"OK ({count} apps)")
        elif count == 0:
            print(f"EMPTY (0 apps)")
        else:
            results[code]["final_status"] = "ERROR"
            print(f"ERROR: {error[:60]}")

    phase1_time = time.time() - phase_start
    has_results_p1 = sum(1 for r in results.values() if r["final_status"] == "HAS_RESULTS")
    empty_p1 = sum(1 for r in results.values() if r["final_status"] is None)
    error_p1 = sum(1 for r in results.values() if r["final_status"] == "ERROR")
    print(f"\nPhase 1 done in {phase1_time:.0f}s: {has_results_p1} OK, {empty_p1} empty, {error_p1} errors")

    # ── Phase 2: 1 month — only empty councils ──
    phase_name, df, dt = phases[1]
    retry_empty = [c for c in enabled if results[c.authority_code]["final_status"] is None]
    if retry_empty:
        print(f"\n{'='*70}")
        print(f"PHASE 2: {phase_name} ({df} to {dt}) — retrying {len(retry_empty)} empty councils")
        print(f"{'='*70}")
        phase_start = time.time()

        for i, config in enumerate(retry_empty):
            code = config.authority_code
            sys.stdout.write(f"  [{i+1}/{len(retry_empty)}] {config.name:40s} ... ")
            sys.stdout.flush()

            count, error = await test_gather(config, registry, df, dt)
            results[code]["phases"][phase_name] = {
                "count": count, "error": error,
                "date_from": str(df), "date_to": str(dt),
            }

            if count > 0:
                results[code]["final_status"] = "HAS_RESULTS"
                results[code]["final_count"] = count
                print(f"OK ({count} apps)")
            elif count == 0:
                print(f"EMPTY (0 apps)")
            else:
                results[code]["final_status"] = "ERROR"
                print(f"ERROR: {error[:60]}")

        phase2_time = time.time() - phase_start
        has_results_p2 = sum(1 for c in retry_empty if results[c.authority_code]["final_status"] == "HAS_RESULTS")
        print(f"\nPhase 2 done in {phase2_time:.0f}s: {has_results_p2} newly found results")

    # ── Phase 3: Jan 2026 — only still-empty councils ──
    phase_name, df, dt = phases[2]
    retry_empty2 = [c for c in enabled if results[c.authority_code]["final_status"] is None]
    if retry_empty2:
        print(f"\n{'='*70}")
        print(f"PHASE 3: {phase_name} ({df} to {dt}) — retrying {len(retry_empty2)} still-empty councils")
        print(f"{'='*70}")
        phase_start = time.time()

        for i, config in enumerate(retry_empty2):
            code = config.authority_code
            sys.stdout.write(f"  [{i+1}/{len(retry_empty2)}] {config.name:40s} ... ")
            sys.stdout.flush()

            count, error = await test_gather(config, registry, df, dt)
            results[code]["phases"][phase_name] = {
                "count": count, "error": error,
                "date_from": str(df), "date_to": str(dt),
            }

            if count > 0:
                results[code]["final_status"] = "HAS_RESULTS"
                results[code]["final_count"] = count
                print(f"OK ({count} apps)")
            elif count == 0:
                results[code]["final_status"] = "EMPTY_ALL_PHASES"
                print(f"EMPTY (still 0 — possible dead scraper)")
            else:
                results[code]["final_status"] = "ERROR"
                print(f"ERROR: {error[:60]}")

        phase3_time = time.time() - phase_start
        has_results_p3 = sum(1 for c in retry_empty2 if results[c.authority_code]["final_status"] == "HAS_RESULTS")
        print(f"\nPhase 3 done in {phase3_time:.0f}s: {has_results_p3} newly found results")

    # Mark any remaining None as empty across all
    for r in results.values():
        if r["final_status"] is None:
            r["final_status"] = "EMPTY_ALL_PHASES"

    # ── Final Summary ──
    all_results = list(results.values())
    has_results = [r for r in all_results if r["final_status"] == "HAS_RESULTS"]
    empty_all = [r for r in all_results if r["final_status"] == "EMPTY_ALL_PHASES"]
    errors = [r for r in all_results if r["final_status"] == "ERROR"]

    print(f"\n{'='*70}")
    print(f"FINAL REPORT")
    print(f"{'='*70}")
    print(f"  Total tested:       {len(all_results)}")
    print(f"  HAS RESULTS:        {len(has_results)}")
    print(f"  EMPTY (all phases): {len(empty_all)}")
    print(f"  ERRORS:             {len(errors)}")

    if empty_all:
        print(f"\n--- Empty across ALL phases ({len(empty_all)}) — investigate these ---")
        by_platform = {}
        for r in sorted(empty_all, key=lambda x: x["platform"]):
            by_platform.setdefault(r["platform"], []).append(r)
        for platform, councils in sorted(by_platform.items()):
            print(f"\n  [{platform}]")
            for r in councils:
                print(f"    {r['name']:40s} {r['base_url']}")

    if errors:
        print(f"\n--- Errors ({len(errors)}) ---")
        for r in sorted(errors, key=lambda x: x["authority_code"]):
            # Find the first error
            err_msg = "unknown"
            for phase_data in r["phases"].values():
                if phase_data.get("error"):
                    err_msg = phase_data["error"][:80]
                    break
            print(f"  {r['name']:40s} [{r['platform']}] {err_msg}")

    # Platform breakdown for successful
    print(f"\n--- Results by platform ---")
    platform_stats = {}
    for r in all_results:
        p = r["platform"]
        if p not in platform_stats:
            platform_stats[p] = {"total": 0, "ok": 0, "empty": 0, "error": 0}
        platform_stats[p]["total"] += 1
        if r["final_status"] == "HAS_RESULTS":
            platform_stats[p]["ok"] += 1
        elif r["final_status"] == "EMPTY_ALL_PHASES":
            platform_stats[p]["empty"] += 1
        else:
            platform_stats[p]["error"] += 1

    for p, stats in sorted(platform_stats.items(), key=lambda x: -x[1]["total"]):
        print(f"  {p:25s} {stats['ok']:3d}/{stats['total']:3d} OK  |  {stats['empty']} empty  |  {stats['error']} errors")

    # Save full report
    with open(REPORT_PATH, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull report saved: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
