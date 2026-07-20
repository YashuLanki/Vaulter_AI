"""
pipeline/scheduler.py
----------------------
Vaulter AI Stage 2 — Background Scheduler

Keeps all data pipelines running automatically:
  - Web sources (CBRE, Marcus & Millichap, GlobeSt) — each on its own frequency
  - Outlook email — every 30 minutes
  - Property intelligence (all properties from Project Master CSV) — daily at 6 AM

Called by:  python main.py schedule
"""

import logging
import sys
from datetime import datetime as _dt, timedelta as _timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import LOG_DIR, SCHEDULER_TIMEZONE, LOG_LEVEL, RUN_SCHEDULED_SCRAPING

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ─── Logging ──────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [SCHEDULER] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Jobs
# ══════════════════════════════════════════════════════════════════

def job_scrape(source_name: str):
    """Scrape one configured web source."""
    log.info(f"Scheduled scrape: {source_name}")
    try:
        from pipeline.web_scraper import scrape_all
        scrape_all(target_name=source_name)
    except Exception as e:
        log.error(f"Scrape job failed for '{source_name}': {e}")


def job_email():
    """Pull new Outlook emails."""
    log.info("Scheduled email check")
    try:
        from pipeline.email_reader import process_all_emails
        process_all_emails()
    except ValueError as e:
        log.warning(f"Outlook not authorized — run 'python main.py auth' first. ({e})")
    except Exception as e:
        log.error(f"Email job failed: {e}")


def job_property_scrape():
    """Scrape news and market data for all properties in the Project Master CSV."""
    log.info("Scheduled property intelligence scrape")
    try:
        from pipeline.property_scraper import scrape_all_properties
        scrape_all_properties()
    except FileNotFoundError as e:
        log.warning(str(e))
    except Exception as e:
        log.error(f"Property scrape job failed: {e}")


# ══════════════════════════════════════════════════════════════════
# Scheduler
# ══════════════════════════════════════════════════════════════════

def start_scheduler():
    scheduler = BlockingScheduler(timezone=SCHEDULER_TIMEZONE)

    # ── Web sources — each at its own configured frequency ────────
    # Gated behind RUN_SCHEDULED_SCRAPING: only one designated team machine
    # needs this on (see config.py) -- web/property scraping hits the same
    # public pages regardless of who runs it. Email is NOT gated -- it's
    # correctly per-person, never duplicated.
    if RUN_SCHEDULED_SCRAPING:
        # Loaded via load_web_sources() (not the raw config.WEB_SOURCES
        # constant) so a CSV override in data/web_sources/ is scheduled
        # correctly -- previously this always used config.WEB_SOURCES while
        # scrape_all() itself preferred a CSV if present, so a CSV-only
        # source was never scheduled, and a config-only source (when a CSV
        # existed) silently no-op'd every time it fired.
        from pipeline.web_scraper import load_web_sources
        sources, source_label = load_web_sources()
        log.info(f"Web sources: {source_label} ({len(sources)} sources)")

        for source in sources:
            scheduler.add_job(
                job_scrape,
                trigger=IntervalTrigger(hours=source["frequency_hours"]),
                args=[source["name"]],
                id=f"scrape_{source['name'].replace(' ', '_')}",
                name=f"Scrape: {source['name']}",
                next_run_time=_dt.now() + _timedelta(seconds=30),   # run immediately on startup
                replace_existing=True,
            )
            log.info(f"Scheduled '{source['name']}' — every {source['frequency_hours']}h")
    else:
        log.info("Web/property scraping is OFF on this machine (RUN_SCHEDULED_SCRAPING=false) "
                  "-- another team machine handles that.")

    # ── Outlook email — every 30 minutes ─────────────────────────
    scheduler.add_job(
        job_email,
        trigger=IntervalTrigger(minutes=30),
        id="check_email",
        name="Email: Outlook Pull",
        next_run_time=_dt.now() + _timedelta(seconds=30),
        replace_existing=True,
    )
    log.info("Scheduled Outlook email — every 30 min")

    # ── Property intelligence — daily at 6:00 AM ─────────────────
    if RUN_SCHEDULED_SCRAPING:
        scheduler.add_job(
            job_property_scrape,
            trigger=CronTrigger(hour=6, minute=0),
            id="property_scrape",
            name="Property Intelligence Scrape",
            replace_existing=True,
        )
        log.info("Scheduled property intelligence scrape — daily at 6:00 AM")

    log.info("=" * 60)
    log.info("  Vaulter AI Stage 2 Scheduler STARTED")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 60)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
        scheduler.shutdown()