import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.config import CouncilConfig
from src.core.models import Application, Council, ScrapeRun
from src.core.scraper import ApplicationDetail, BaseScraper
from src.scheduler.registry import ScraperRegistry

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 60
MIN_LOOKBACK_DAYS = 28


async def run_council_scrape(
    config,
    registry,
    session,
    lookback_days=DEFAULT_LOOKBACK_DAYS,
):
    """Run a single scrape job for one council. Streams results to DB as they arrive."""
    council = session.execute(
        select(Council).where(Council.authority_code == config.authority_code)
    ).scalar_one()

    now = datetime.now(timezone.utc)
    date_to = date.today()

    if council.last_successful_at:
        date_from = council.last_successful_at.date()
        # Always look back at least MIN_LOOKBACK_DAYS to catch late-registered
        # apps. When the caller explicitly asks for a longer window via
        # `lookback_days` (e.g. dashboard "Test Scrape" with days=90), honour
        # the larger of the two as the floor.
        floor_days = max(MIN_LOOKBACK_DAYS, lookback_days)
        earliest_allowed = date_to - timedelta(days=floor_days)
        if date_from > earliest_allowed:
            date_from = earliest_allowed
    else:
        date_from = date_to - timedelta(days=lookback_days)

    scrape_run = ScrapeRun(
        council_id=council.id,
        status="running",
        date_range_from=date_from,
        date_range_to=date_to,
    )
    session.add(scrape_run)
    session.commit()

    scraper = registry.create_scraper(config)
    apps_found = 0
    apps_inserted = 0
    apps_updated = 0
    apps_failed = 0

    try:
        if type(scraper).scrape is not BaseScraper.scrape:
            # Scraper overrides scrape() — it owns the full pipeline
            result = await scraper.scrape(date_from, date_to)
            if result.error:
                raise RuntimeError(result.error)
            apps_found = len(result.applications)
            logger.info("Scrape %s: scrape() returned %d applications", config.authority_code, apps_found)
            for detail in result.applications:
                try:
                    change_type = _upsert_application(session, council.id, detail)
                    if change_type == "inserted":
                        apps_inserted += 1
                    elif change_type == "updated":
                        apps_updated += 1
                    session.commit()
                except Exception as e:
                    apps_failed += 1
                    logger.warning("Error upserting %s/%s: %s", config.authority_code, detail.reference, e)
                    session.rollback()
        else:
            # Standard pipeline: gather_ids + per-app fetch_detail with streaming inserts
            summaries = await scraper.gather_ids(date_from, date_to)
            apps_found = len(summaries)
            logger.info("Scrape %s: found %d applications, fetching details...", config.authority_code, apps_found)

            for summary in summaries:
                try:
                    detail = await scraper.fetch_detail(summary)
                    change_type = _upsert_application(session, council.id, detail)
                    if change_type == "inserted":
                        apps_inserted += 1
                    elif change_type == "updated":
                        apps_updated += 1
                    session.commit()
                except Exception as e:
                    apps_failed += 1
                    logger.warning("Error fetching %s/%s: %s", config.authority_code, summary.uid, e)
                    session.rollback()

        scrape_run.status = "success"
        scrape_run.applications_found = apps_found
        scrape_run.applications_updated = apps_inserted + apps_updated
        if apps_found > 0:
            council.last_successful_at = now
        logger.info(
            "Scrape %s complete: found=%d inserted=%d updated=%d unchanged=%d failed=%d",
            config.authority_code, apps_found, apps_inserted, apps_updated,
            apps_found - apps_inserted - apps_updated - apps_failed, apps_failed,
        )
    except Exception as e:
        scrape_run.status = "failed"
        scrape_run.error_message = str(e)
        logger.warning("Scrape %s failed: %s", config.authority_code, e)

    scrape_run.completed_at = datetime.now(timezone.utc)
    council.last_scraped_at = now
    session.commit()


def _upsert_application(session, council_id, detail):
    """Insert or update an application. Returns 'inserted', 'updated', or 'unchanged'."""
    existing = session.execute(
        select(Application).where(
            Application.council_id == council_id,
            Application.reference == detail.reference,
        )
    ).scalar_one_or_none()

    if existing:
        changed = False
        for field in ("address", "description", "url", "application_type", "status",
                      "decision", "date_received", "date_validated", "ward", "parish",
                      "applicant_name", "case_officer"):
            new_val = getattr(detail, field, None)
            if new_val is not None and new_val != getattr(existing, field):
                setattr(existing, field, new_val)
                changed = True
        if detail.raw_data:
            existing.raw_data = detail.raw_data
            changed = True
        return "updated" if changed else "unchanged"
    else:
        app = Application(
            council_id=council_id,
            reference=detail.reference,
            url=detail.url,
            address=detail.address,
            description=detail.description,
            application_type=detail.application_type,
            status=detail.status,
            decision=detail.decision,
            date_received=detail.date_received,
            date_validated=detail.date_validated,
            ward=detail.ward,
            parish=detail.parish,
            applicant_name=detail.applicant_name,
            case_officer=detail.case_officer,
            raw_data=detail.raw_data,
        )
        session.add(app)
        return "inserted"
