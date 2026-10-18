"""Job Hunter Agent entry point: the scrape → filter → persist → notify pipeline.

Setup and run (from the project root):

    uv sync
    uv run playwright install chromium
    cp config/.env.example config/.env       # then fill in values
    uv run python main.py --once --headful   # first run: pass Cloudflare, save browser state
    uv run python main.py --daemon
    uv run pytest

Modes (one is required):
    --once            Run the pipeline once and exit (for cron or systemd timers).
    --daemon          Run on the schedule in `config/config.yaml` until SIGINT/SIGTERM.
    --reevaluate      Re-check stored jobs against the current profiles: requeue
                      IGNORED jobs that match now, demote unsent NEW jobs that
                      no longer match, report sent jobs that no longer match,
                      recompute duplicate groups, then send the NEW jobs.
                      Never launches a browser.
Options:
    --headful         Show the browser window (overrides HEADLESS in config/.env).
    --debug-capture   Save intercepted JSON and HTML under `data/debug/`.
    --since-days N    With --reevaluate: only jobs first seen in the last N days (default 30).
    --dry-run         With --reevaluate: print what would change and change nothing.

    uv run python main.py --reevaluate --dry-run    # preview after editing the profiles
    uv run python main.py --reevaluate              # apply the changes and notify

`--once` and `--reevaluate` exit codes: 0 = success; 1 = fatal error
(settings, config, database); 2 = finished with problems (a platform failed or
notifications are still pending); 130 = interrupted.

Logs go to the console (INFO) and to `data/logs/job_hunter.log` (DEBUG, rotated
at 10 MB, kept for 14 days).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from playwright.async_api import Browser, async_playwright
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from src.database import Database
from src.duplicates import assign_duplicate_groups
from src.models import AppConfig, JobPost, JobStatus, Platform, Settings, load_app_config
from src.notifier.telegram import TelegramNotifier
from src.rule_engine import RuleEngine
from src.scrapers.base import BaseScraper, IsKnown, ScraperBlockedError
from src.scrapers.jobinja import JobinjaScraper
from src.scrapers.jobvision import JobVisionScraper
from src.utils import normalize_text

_CONFIG_PATH = Path("config/config.yaml")
_LOG_FILE = Path("data/logs/job_hunter.log")

_SCRAPERS: dict[Platform, type[BaseScraper]] = {
    "jobvision": JobVisionScraper,
    "jobinja": JobinjaScraper,
}

# A first run with an empty database visits many detail pages (~6 s each), so
# a heartbeat shows that a long run is still making progress.
_HEARTBEAT_INTERVAL_S = 60.0
# After this many failed sends in a row (e.g. a broken proxy or token), the
# remaining jobs stay NEW for the next run instead of retrying each one.
_MAX_CONSECUTIVE_SEND_FAILURES = 3
_SHUTDOWN_GRACE_S = 60.0
_CANCEL_CLEANUP_TIMEOUT_S = 30.0
_SCHEDULER_JOB_ID = "job_hunter_pipeline"

_DEFAULT_SINCE_DAYS = 30
# Display width caps of the long columns in the --reevaluate --dry-run tables.
_TABLE_TITLE_MAX = 60
_TABLE_COMPANY_MAX = 40  # room for bilingual names such as "گروه حقوق آسیا | Asia Law Group"
_TABLE_COLUMN_MAX: dict[str, int] = {"Title": _TABLE_TITLE_MAX, "Company": _TABLE_COMPANY_MAX}

# The --reevaluate --dry-run section titles.
_SECTION_ADDED = "اضافه می‌شوند"
_SECTION_REMOVED = "حذف می‌شوند"
_SECTION_REPORT_ONLY = "دیگر تطبیق ندارند (فقط گزارش)"
_SECTION_GROUPS = "گروه‌های تکراری"

_EXIT_OK = 0
_EXIT_FATAL = 1
_EXIT_PARTIAL = 2
_EXIT_INTERRUPTED = 130


# ── Run summary and progress ───────────────────────────────────────────────


@dataclass(slots=True)
class PlatformSummary:
    """Counters for one platform in one pipeline run.

    Attributes:
        scraped: Jobs returned by the scraper, i.e. not yet in the database.
        known: Listings skipped as already stored, without visiting their detail page.
        new: Jobs saved as `NEW` (matched at least one profile).
        ignored: Jobs saved as `IGNORED` (matched no profile).
        duplicates: Jobs saved in a probable-duplicate group (any status).
        notified: Notifications delivered; those jobs are now `NOTIFIED`.
        failed: Notifications that failed; those jobs stay `NEW`.
        deferred: `NEW` jobs not attempted this run (notifier unavailable or
            too many consecutive failures); they stay `NEW`.
        error: The exception type that stopped this platform's scraper, if any.
    """

    scraped: int = 0
    known: int = 0
    new: int = 0
    ignored: int = 0
    duplicates: int = 0
    notified: int = 0
    failed: int = 0
    deferred: int = 0
    error: str | None = None

    def describe(self) -> str:
        """Return a one-line, human-readable form of the counters."""
        text = (
            f"scraped {self.scraped} | new {self.new} | ignored {self.ignored} | "
            f"notified {self.notified} | failed {self.failed} | known (skipped) {self.known}"
        )
        if self.duplicates:
            text += f" | probable duplicates {self.duplicates}"
        if self.deferred:
            text += f" | deferred {self.deferred}"
        if self.error:
            text += f" | ERROR {self.error}"
        return text

    @property
    def has_problems(self) -> bool:
        """Return True if the scraper failed or some notifications are still pending."""
        return bool(self.error or self.failed or self.deferred)


@dataclass(slots=True)
class _RunProgress:
    """What the running pipeline is doing, for the heartbeat log."""

    started: float
    stage: str = "starting"
    platform: Platform | None = None


def _format_duration(seconds: float) -> str:
    """Format seconds as `1h02m03s`, `4m12s`, or `8s`."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


async def _heartbeat(progress: _RunProgress, summaries: dict[Platform, PlatformSummary]) -> None:
    """Log an INFO line every `_HEARTBEAT_INTERVAL_S` while the run is in progress."""
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        detail = ""
        if progress.platform is not None:
            summary = summaries[progress.platform]
            detail = (
                f": {summary.known + summary.scraped} listing(s) checked, {summary.known} known, "
                f"{summary.scraped} new so far (each new job's detail page takes a few seconds)"
            )
        logger.info(
            "Run still in progress after {} (stage: {}){}",
            _format_duration(time.monotonic() - progress.started),
            progress.stage,
            detail,
        )


# ── Pipeline ───────────────────────────────────────────────────────────────


def _collect_keywords(config: AppConfig) -> list[str]:
    """Return the union of all profiles' search terms, in config order.

    Synonym groups are flattened: every term of every group is searched.
    Terms that are equal after `normalize_text` (e.g. "AI" and "ai") are
    searched once, using the first spelling.
    """
    keywords: dict[str, str] = {}
    for profile in config.search_profiles:
        for group in profile.keywords:
            for keyword in [group] if isinstance(group, str) else group:
                keywords.setdefault(normalize_text(keyword), keyword.strip())
    return list(keywords.values())


def _tracking_is_known(job_exists: IsKnown, summary: PlatformSummary) -> IsKnown:
    """Wrap `job_exists` so that the platform summary counts known and new listings."""

    async def is_known(platform: str, external_id: str) -> bool:
        """Return whether the job is stored, and count the answer."""
        known = await job_exists(platform, external_id)
        if known:
            summary.known += 1
        else:
            summary.scraped += 1
        return known

    return is_known


async def _scrape_platform(
    browser: Browser,
    platform: Platform,
    db: Database,
    config: AppConfig,
    keywords: list[str],
    summary: PlatformSummary,
    *,
    show_browser: bool,
    debug_capture: bool,
) -> list[JobPost]:
    """Run one platform's scraper; failures are logged and yield an empty list.

    The scraper's context manager saves `storage_state` on every exit,
    including errors and cancellation.
    """
    scraper_cls = _SCRAPERS[platform]
    is_known = _tracking_is_known(db.job_exists, summary)
    started = time.monotonic()
    logger.info("[{}] Scraping started", platform)
    try:
        async with scraper_cls(
            browser, config, is_known, headful=show_browser, debug_capture=debug_capture
        ) as scraper:
            jobs = await scraper.fetch_jobs(keywords)
    except ScraperBlockedError:
        summary.error = "ScraperBlockedError"
        summary.scraped = 0
        logger.exception("[{}] Blocked by Cloudflare; skipping this platform for this run", platform)
        return []
    except Exception as exc:
        summary.error = type(exc).__name__
        summary.scraped = 0
        logger.exception("[{}] Scraper failed; continuing with the next platform", platform)
        return []
    summary.scraped = len(jobs)
    logger.info(
        "[{}] Scraping finished in {}: {} new job(s); {} known job(s) skipped without visiting their detail page",
        platform,
        _format_duration(time.monotonic() - started),
        len(jobs),
        summary.known,
    )
    return jobs


async def _scrape_all(
    db: Database,
    config: AppConfig,
    keywords: list[str],
    summaries: dict[Platform, PlatformSummary],
    progress: _RunProgress,
    collected: list[JobPost],
    *,
    show_browser: bool,
    debug_capture: bool,
) -> None:
    """Launch a fresh Chromium, scrape each enabled platform in turn, and close the browser.

    Jobs of each finished platform are appended to `collected`, so they are
    still available if the run is cancelled while a later platform is scraped.
    A browser failure is logged and marks every platform that did not finish as failed.
    """
    finished: set[Platform] = set()
    try:
        async with async_playwright() as playwright:
            logger.info("Launching Chromium ({})", "headful" if show_browser else "headless")
            # Shutdown is handled here, not by Playwright: with its default
            # signal handling, Ctrl+C would close the browser before the
            # scrapers could save their storage_state.
            browser = await playwright.chromium.launch(
                headless=not show_browser,
                handle_sigint=False,
                handle_sigterm=False,
                handle_sighup=False,
            )
            try:
                for platform in config.scraping.enabled_platforms:
                    progress.stage = f"scraping {platform}"
                    progress.platform = platform
                    collected.extend(
                        await _scrape_platform(
                            browser,
                            platform,
                            db,
                            config,
                            keywords,
                            summaries[platform],
                            show_browser=show_browser,
                            debug_capture=debug_capture,
                        )
                    )
                    finished.add(platform)
            finally:
                progress.platform = None
                try:
                    await browser.close()
                    logger.info("Browser closed")
                except PlaywrightError as exc:
                    logger.warning("Failed to close the browser: {}", exc)
    except Exception as exc:
        logger.exception("Browser failure; scraping stopped for this run")
        for platform in config.scraping.enabled_platforms:
            if platform not in finished:
                summaries[platform].error = type(exc).__name__


async def _load_grouping_context(db: Database, since: datetime) -> list[JobPost]:
    """Return the jobs stored since `since`, plus every other member of their duplicate groups.

    Loading whole groups keeps each group's earliest member, which names the
    group, in view even when it was first seen before `since`.
    """
    jobs = await db.get_jobs_since(since)
    known = {(job.platform, job.external_id) for job in jobs}
    for group in sorted({job.duplicate_group for job in jobs if job.duplicate_group}):
        for member in await db.get_duplicate_group_members(group):
            key = (member.platform, member.external_id)
            if key not in known:
                known.add(key)
                jobs.append(member)
    return jobs


async def _group_new_jobs(db: Database, config: AppConfig, jobs: list[JobPost]) -> dict[tuple[str, str], str]:
    """Return the duplicate-group changes for this run's jobs and the stored jobs they may duplicate.

    Stored jobs first seen up to `duplicates.window_days` before the earliest
    new job are compared. A failure is logged, and the jobs are then saved
    without groups instead of being lost.
    """
    if not jobs:
        return {}
    since = min(job.created_at for job in jobs) - timedelta(days=config.duplicates.window_days)
    try:
        existing = await _load_grouping_context(db, since)
        return _changes_in_groups_of(jobs, assign_duplicate_groups(jobs, existing, config))
    except Exception:
        logger.exception("Duplicate grouping failed; this run's jobs are saved without duplicate groups")
        return {}


async def _save_jobs(
    db: Database,
    rule_engine: RuleEngine,
    config: AppConfig,
    jobs: list[JobPost],
    summaries: dict[Platform, PlatformSummary],
) -> None:
    """Deduplicate jobs by key, match them against the profiles, group probable duplicates, and save them.

    Matching jobs are saved as NEW and the others as IGNORED. Before saving,
    this run's jobs are grouped with the stored jobs they probably duplicate
    (`assign_duplicate_groups`): new jobs are saved with their group, and
    stored jobs that joined a group are updated.
    """
    unique: dict[tuple[str, str], JobPost] = {}
    for job in jobs:
        unique.setdefault((job.platform, job.external_id), job)
    if len(unique) < len(jobs):
        logger.info("Dropped {} repeated listing(s) with the same platform and ID", len(jobs) - len(unique))

    evaluated: list[JobPost] = []
    for job in unique.values():
        matched = rule_engine.match_profiles(job)
        if matched:
            evaluated.append(job.model_copy(update={"matched_profiles": matched, "status": JobStatus.NEW}))
        else:
            evaluated.append(job.model_copy(update={"status": JobStatus.IGNORED}))

    groups = await _group_new_jobs(db, config, evaluated)

    saved_new = saved_ignored = saved_grouped = 0
    for job in evaluated:
        key = (job.platform, job.external_id)
        if key in groups:
            job = job.model_copy(update={"duplicate_group": groups[key]})
        if not await db.save_job(job):
            continue
        summary = summaries.setdefault(job.platform, PlatformSummary())
        if job.status == JobStatus.NEW:
            summary.new += 1
            saved_new += 1
            logger.info(
                "[{}] Match {}: {} | {} | profiles: {}",
                job.platform,
                job.external_id,
                job.title,
                job.company or "-",
                ", ".join(job.matched_profiles),
            )
        else:
            summary.ignored += 1
            saved_ignored += 1
        if job.duplicate_group is not None:
            summary.duplicates += 1
            saved_grouped += 1
            logger.log(
                "INFO" if job.status == JobStatus.NEW else "DEBUG",
                "[{}] Probable duplicate {} ({}): {} | {} | group {}",
                job.platform,
                job.external_id,
                job.status.value,
                job.title,
                job.company or "-",
                job.duplicate_group,
            )

    joined = 0
    for (platform, external_id), group in groups.items():
        if (platform, external_id) in unique:
            continue
        await db.set_duplicate_group(platform, external_id, group)
        joined += 1
        logger.info("[{}] Stored job {} joined duplicate group {}", platform, external_id, group)
    logger.info(
        "Saved {} job(s): {} NEW, {} IGNORED; {} in a duplicate group, {} stored job(s) joined a group",
        saved_new + saved_ignored,
        saved_new,
        saved_ignored,
        saved_grouped,
        joined,
    )


async def _other_group_members(db: Database, job: JobPost) -> list[JobPost]:
    """Return the other members of the job's duplicate group (any status); [] when it has no group."""
    if job.duplicate_group is None:
        return []
    members = await db.get_duplicate_group_members(job.duplicate_group)
    return [member for member in members if (member.platform, member.external_id) != (job.platform, job.external_id)]


async def _notify_pending(
    db: Database,
    settings: Settings,
    summaries: dict[Platform, PlatformSummary],
    progress: _RunProgress,
) -> None:
    """Send every NEW job; delivered jobs become NOTIFIED, the others stay NEW for the next run.

    A job in a duplicate group is sent with links to the group's other postings.
    """
    pending = await db.get_pending_notifications()
    if not pending:
        logger.info("No pending notifications")
        return
    logger.info("Sending {} pending notification(s)", len(pending))

    async with AsyncExitStack() as stack:
        try:
            notifier = await stack.enter_async_context(TelegramNotifier(settings))
        except ValueError as exc:
            logger.error("Cannot start the Telegram notifier: {}; {} job(s) stay NEW", exc, len(pending))
            for job in pending:
                summaries.setdefault(job.platform, PlatformSummary()).deferred += 1
            return

        consecutive_failures = 0
        for index, job in enumerate(pending, start=1):
            progress.stage = f"notifying ({index}/{len(pending)})"
            summary = summaries.setdefault(job.platform, PlatformSummary())
            duplicates = await _other_group_members(db, job)
            if await notifier.send_job(job, duplicates=duplicates):
                await db.update_status(job.platform, job.external_id, JobStatus.NOTIFIED)
                summary.notified += 1
                consecutive_failures = 0
                continue
            summary.failed += 1
            consecutive_failures += 1
            remaining = pending[index:]
            if consecutive_failures >= _MAX_CONSECUTIVE_SEND_FAILURES and remaining:
                for skipped in remaining:
                    summaries.setdefault(skipped.platform, PlatformSummary()).deferred += 1
                logger.warning(
                    "{} notifications failed in a row; stopping for this run. {} job(s) stay NEW "
                    "and will be retried on the next run",
                    consecutive_failures,
                    len(remaining),
                )
                break


def _log_summary(summaries: dict[Platform, PlatformSummary], elapsed: float, outcome: str) -> None:
    """Log the run outcome and one summary line per platform."""
    logger.info("Run {} in {}", outcome, _format_duration(elapsed))
    for platform, summary in summaries.items():
        logger.info("Summary [{}]: {}", platform, summary.describe())


async def run_pipeline(
    settings: Settings,
    config: AppConfig,
    *,
    headful: bool,
    debug_capture: bool,
) -> dict[Platform, PlatformSummary]:
    """Run one scrape → filter → persist → notify cycle and return a per-platform summary.

    Steps:
    1. Open the database and ensure its schema.
    2. Launch a fresh Chromium (headless unless `headful` or `HEADLESS=false`)
       and run each enabled platform's scraper in turn; per-platform failures
       are logged and do not stop the other platforms.
    3. Deduplicate jobs by `(platform, external_id)`, match them against the
       profiles, group probable duplicates with the stored jobs, and save
       them as `NEW` (matched) or `IGNORED`.
    4. Send every `NEW` job to Telegram, with links to its probable
       duplicates; delivered jobs become `NOTIFIED`.

    If the run is cancelled while scraping, the jobs of platforms that already
    finished are still saved before the cancellation propagates.

    Args:
        settings: Environment settings (database path, Telegram, `HEADLESS`).
        config: The application config.
        headful: Show the browser window, overriding `HEADLESS`.
        debug_capture: Save intercepted JSON and HTML under `data/debug/`.

    Returns:
        A `PlatformSummary` per platform: every enabled platform, plus any
        other platform that had pending notifications.
    """
    show_browser = headful or not settings.HEADLESS
    summaries: dict[Platform, PlatformSummary] = {
        platform: PlatformSummary() for platform in config.scraping.enabled_platforms
    }
    progress = _RunProgress(started=time.monotonic())
    keywords = _collect_keywords(config)
    logger.info(
        "Run started: platforms {}, {} keyword(s) {}, up to {} page(s) per keyword",
        config.scraping.enabled_platforms,
        len(keywords),
        keywords,
        config.scraping.max_pages_per_keyword,
    )
    heartbeat = asyncio.create_task(_heartbeat(progress, summaries), name="run-heartbeat")
    outcome = "failed"
    try:
        rule_engine = RuleEngine(config)
        logger.info("Stage 1/4: opening the database")
        progress.stage = "opening the database"
        async with Database(settings.DB_PATH) as db:
            await db.init_db()

            logger.info("Stage 2/4: scraping")
            collected: list[JobPost] = []
            try:
                await _scrape_all(
                    db,
                    config,
                    keywords,
                    summaries,
                    progress,
                    collected,
                    show_browser=show_browser,
                    debug_capture=debug_capture,
                )
            except asyncio.CancelledError:
                if collected:
                    logger.warning(
                        "Run cancelled while scraping; saving {} job(s) from finished platforms", len(collected)
                    )
                    await _save_jobs(db, rule_engine, config, collected, summaries)
                raise

            logger.info("Stage 3/4: filtering and saving {} job(s)", len(collected))
            progress.stage = "filtering and saving"
            await _save_jobs(db, rule_engine, config, collected, summaries)

            logger.info("Stage 4/4: notifications")
            progress.stage = "notifying"
            await _notify_pending(db, settings, summaries, progress)
        outcome = "finished"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        heartbeat.cancel()
        await asyncio.wait({heartbeat})
        _log_summary(summaries, time.monotonic() - progress.started, outcome)
    return summaries


# ── --once ─────────────────────────────────────────────────────────────────


def _add_signal_handler(sig: signal.Signals, callback: Callable[[], object]) -> Callable[[], None]:
    """Install a loop signal handler; return a function that removes it (a no-op if unsupported)."""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(sig, callback)
    except (NotImplementedError, RuntimeError):
        logger.debug("loop.add_signal_handler({}) is not supported here", sig.name)
        return lambda: None
    return lambda: loop.remove_signal_handler(sig)


async def _run_once(settings: Settings, config: AppConfig, *, headful: bool, debug_capture: bool) -> int:
    """Run the pipeline once and return the process exit code.

    Ctrl+C (turned into a cancellation by `asyncio.run`) and SIGTERM both
    cancel the run; the context managers then save `storage_state` and close
    the browser, database, and HTTP client.
    """
    task = asyncio.current_task()
    remove_handler = _add_signal_handler(signal.SIGTERM, task.cancel) if task is not None else (lambda: None)
    try:
        summaries = await run_pipeline(settings, config, headful=headful, debug_capture=debug_capture)
    except asyncio.CancelledError:
        if task is not None:
            task.uncancel()
        logger.warning("Run interrupted; cleanup finished")
        return _EXIT_INTERRUPTED
    except Exception:
        logger.exception("Run failed")
        return _EXIT_FATAL
    finally:
        remove_handler()
    if any(summary.has_problems for summary in summaries.values()):
        return _EXIT_PARTIAL
    return _EXIT_OK


# ── --daemon ───────────────────────────────────────────────────────────────


def _parse_active_hours(value: str) -> tuple[dt_time, dt_time]:
    """Split a validated `HH:MM-HH:MM` string into start and end times."""
    start, end = value.split("-")
    return dt_time.fromisoformat(start), dt_time.fromisoformat(end)


def _within_active_hours(active_hours: str, now: datetime) -> bool:
    """Return True if `now` (already in the configured time zone) is inside the window.

    The start is inclusive and the end exclusive. A window whose end is before
    its start crosses midnight: "22:00-02:00" covers 22:00 to 01:59.
    """
    start, end = _parse_active_hours(active_hours)
    current = now.time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


class _Daemon:
    """Runs the pipeline on the configured interval until SIGINT or SIGTERM.

    Runs never overlap (`max_instances=1`, `coalesce=True`). A run that is
    longer than the interval makes the scheduler skip the slots it overlaps;
    the next run starts at the first slot after it finishes.
    """

    def __init__(self, settings: Settings, config: AppConfig, *, headful: bool, debug_capture: bool) -> None:
        """Store the run parameters and create the scheduler (started in `run`)."""
        self._settings = settings
        self._config = config
        self._headful = headful
        self._debug_capture = debug_capture
        self._tz = ZoneInfo(config.scheduler.timezone)
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        self._stop = asyncio.Event()
        self._force = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._run_started: float | None = None

    async def run(self) -> int:
        """Start the scheduler, wait for a stop request, shut down, and return the exit code."""
        loop = asyncio.get_running_loop()
        installed = self._install_signal_handlers(loop)
        scheduler_config = self._config.scheduler
        interval_s = scheduler_config.interval_minutes * 60
        self._scheduler.add_listener(self._on_max_instances, EVENT_JOB_MAX_INSTANCES)
        self._scheduler.add_job(
            self._scheduled_run,
            trigger=IntervalTrigger(minutes=scheduler_config.interval_minutes, timezone=self._tz),
            id=_SCHEDULER_JOB_ID,
            name="job hunter pipeline",
            next_run_time=datetime.now(self._tz),
            max_instances=1,
            coalesce=True,
            # A slot delayed by less than one interval (e.g. after the machine
            # wakes from sleep) still runs; missed slots are coalesced into one.
            misfire_grace_time=interval_s,
        )
        self._scheduler.start()
        logger.info(
            "Daemon started: every {} min, active hours {} ({}). Press Ctrl+C to stop",
            scheduler_config.interval_minutes,
            scheduler_config.active_hours,
            scheduler_config.timezone,
        )
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            # Fallback where loop.add_signal_handler is unsupported (Windows):
            # asyncio.run() turns the first Ctrl+C into a cancellation of this task.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            logger.info("Interrupt received; shutting down")
        try:
            await self._shutdown()
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
        return _EXIT_OK

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> list[signal.Signals]:
        """Handle SIGINT and SIGTERM in the loop; return the signals that were installed."""
        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):
                logger.debug("loop.add_signal_handler is not supported; falling back to KeyboardInterrupt")
                break
            installed.append(sig)
        return installed

    def _on_signal(self, sig: signal.Signals) -> None:
        """First signal: graceful shutdown. Second signal: cancel the current run at once."""
        if not self._stop.is_set():
            logger.info(
                "Received {}; shutting down (press Ctrl+C again to cancel the current run immediately)",
                sig.name,
            )
            self._stop.set()
        elif not self._force.is_set():
            logger.warning("Received {} again; cancelling the current run now", sig.name)
            self._force.set()

    def _on_max_instances(self, event: JobSubmissionEvent) -> None:
        """Explain a slot skipped because the previous run is still going."""
        elapsed = time.monotonic() - self._run_started if self._run_started is not None else 0.0
        logger.info(
            "The current run is still in progress after {} (longer than the {}-min interval); "
            "this slot is skipped so runs never overlap",
            _format_duration(elapsed),
            self._config.scheduler.interval_minutes,
        )

    async def _scheduled_run(self) -> None:
        """Scheduler job: check active hours, then run the pipeline as a shielded task.

        The scheduler's shutdown cancels this wrapper, not the run itself;
        `_shutdown` then gives the run its grace period.
        """
        scheduler_config = self._config.scheduler
        now = datetime.now(self._tz)
        if not _within_active_hours(scheduler_config.active_hours, now):
            logger.info(
                "Outside active hours {} ({:%H:%M} {}); skipping this run",
                scheduler_config.active_hours,
                now,
                scheduler_config.timezone,
            )
            self._log_next_run()
            return
        task = asyncio.create_task(self._guarded_run(), name="pipeline-run")
        self._run_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            logger.debug("Scheduler stopped; the in-flight run is handed over to shutdown")

    async def _guarded_run(self) -> None:
        """Run the pipeline once; a failed run is logged and the daemon keeps going."""
        self._run_started = time.monotonic()
        try:
            await run_pipeline(
                self._settings, self._config, headful=self._headful, debug_capture=self._debug_capture
            )
        except asyncio.CancelledError:
            logger.warning("Run cancelled during shutdown")
            raise
        except Exception:
            logger.exception("Scheduled run failed; the daemon keeps running")
        finally:
            self._run_started = None
        self._log_next_run()

    def _log_next_run(self) -> None:
        """Log when the scheduler will start the next run."""
        if self._stop.is_set() or not self._scheduler.running:
            return
        job = self._scheduler.get_job(_SCHEDULER_JOB_ID)
        if job is not None and job.next_run_time is not None:
            logger.info("Next run at {:%Y-%m-%d %H:%M:%S %Z}", job.next_run_time.astimezone(self._tz))

    async def _shutdown(self) -> None:
        """Stop the scheduler, let the current run finish (up to 60 s), then cancel it.

        Cancelling the run unwinds its context managers: the scrapers save
        `storage_state`, and the browser, database, and HTTP client are closed.
        """
        logger.info("Stopping the scheduler")
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        task = self._run_task
        if task is not None and not task.done():
            logger.info("Waiting up to {:.0f}s for the current run to finish", _SHUTDOWN_GRACE_S)
            force = asyncio.create_task(self._force.wait(), name="force-shutdown")
            try:
                await asyncio.wait({task, force}, timeout=_SHUTDOWN_GRACE_S, return_when=asyncio.FIRST_COMPLETED)
            finally:
                force.cancel()
            if not task.done():
                logger.warning("Cancelling the current run; scrapers save their browser state while closing")
                task.cancel()
                await asyncio.wait({task}, timeout=_CANCEL_CLEANUP_TIMEOUT_S)
                if not task.done():
                    logger.error("The run did not finish cleaning up within {:.0f}s", _CANCEL_CLEANUP_TIMEOUT_S)
        logger.info("Daemon stopped")


async def _run_daemon(settings: Settings, config: AppConfig, *, headful: bool, debug_capture: bool) -> int:
    """Run the daemon until it is stopped and return the process exit code."""
    return await _Daemon(settings, config, headful=headful, debug_capture=debug_capture).run()


# ── --reevaluate ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class _ReevaluationSummary:
    """Counters for one `--reevaluate` run.

    Attributes:
        window_jobs: Jobs (any status) first seen in the window.
        checked: `IGNORED` jobs in the window that were re-checked.
        candidates: Jobs among them that match at least one profile now.
        requeued: Candidates moved back to `NEW` (always 0 in a dry run).
        demote_candidates: Unsent `NEW` jobs in the window that no longer match.
        demoted: Those moved to `IGNORED` (always 0 in a dry run).
        report_only: Jobs at `NOTIFIED` or later that no longer match; never changed.
        group_changes: Jobs whose duplicate group changes.
        regrouped: Group changes written (always 0 in a dry run).
        notified: Notifications delivered by the notification stage.
        failed: Notifications that failed; those jobs stay `NEW`.
        deferred: `NEW` jobs not attempted (notifier unavailable or too many
            consecutive failures); they stay `NEW`.
    """

    window_jobs: int = 0
    checked: int = 0
    candidates: int = 0
    requeued: int = 0
    demote_candidates: int = 0
    demoted: int = 0
    report_only: int = 0
    group_changes: int = 0
    regrouped: int = 0
    notified: int = 0
    failed: int = 0
    deferred: int = 0

    @property
    def has_problems(self) -> bool:
        """Return True if some notifications are still pending."""
        return bool(self.failed or self.deferred)


def plan_reevaluation(jobs: Sequence[JobPost], engine: RuleEngine) -> list[tuple[JobPost, list[str]]]:
    """Return the `IGNORED` jobs that match at least one profile now, with the matched profile names.

    A pure function: it reads nothing and changes nothing. Jobs with any other
    status are skipped, and the input order is kept.
    """
    plan: list[tuple[JobPost, list[str]]] = []
    for job in jobs:
        if job.status != JobStatus.IGNORED:
            continue
        matched = engine.match_profiles(job)
        if matched:
            plan.append((job, matched))
    return plan


@dataclass(frozen=True, slots=True)
class ReevaluationPlan:
    """What `--reevaluate` changes, as computed by `build_reevaluation_plan`.

    Attributes:
        added: `IGNORED` jobs that match now, with their matched profiles; they become `NEW`.
        removed: Unsent `NEW` jobs that no longer match; they become `IGNORED`.
        report_only: Jobs at `NOTIFIED` or later that no longer match; reported, never changed.
        group_changes: The new duplicate group of each job whose group changes,
            keyed by `(platform, external_id)`.
        groups: The duplicate groups of the window's jobs once the changes are
            applied, as (group ID, members oldest first), oldest group first.
    """

    added: list[tuple[JobPost, list[str]]]
    removed: list[JobPost]
    report_only: list[JobPost]
    group_changes: dict[tuple[str, str], str]
    groups: list[tuple[str, list[JobPost]]]


def _oldest_first(job: JobPost) -> tuple[datetime, str, str]:
    """Sort key: oldest `created_at` first, ties broken by platform and external ID."""
    return job.created_at, job.platform, job.external_id


def _changes_in_groups_of(
    jobs: Sequence[JobPost], changes: dict[tuple[str, str], str]
) -> dict[tuple[str, str], str]:
    """Keep the group changes that land in a group containing one of `jobs` (once the changes apply).

    The other changes concern only stored jobs outside the scope of the run,
    for example after `duplicates` settings were edited; they are left to a
    `--reevaluate` whose window covers those jobs.
    """
    groups = {changes.get((job.platform, job.external_id), job.duplicate_group) for job in jobs}
    return {key: group for key, group in changes.items() if group in groups}


def _groups_after(
    window_jobs: Sequence[JobPost], context_jobs: Sequence[JobPost], changes: dict[tuple[str, str], str]
) -> list[tuple[str, list[JobPost]]]:
    """Return the groups with a window job once `changes` apply, members oldest first, oldest group first."""
    members: dict[str, dict[tuple[str, str], JobPost]] = defaultdict(dict)
    for job in [*window_jobs, *context_jobs]:
        key = (job.platform, job.external_id)
        group = changes.get(key, job.duplicate_group)
        if group is not None:
            members[group].setdefault(key, job)
    window_groups = {changes.get((job.platform, job.external_id), job.duplicate_group) for job in window_jobs}
    groups = [
        (group, sorted(members[group].values(), key=_oldest_first))
        for group in window_groups
        if group is not None and len(members[group]) >= 2
    ]
    return sorted(groups, key=lambda item: _oldest_first(item[1][0]))


def build_reevaluation_plan(
    window_jobs: Sequence[JobPost],
    context_jobs: Sequence[JobPost],
    engine: RuleEngine,
    config: AppConfig,
) -> ReevaluationPlan:
    """Compute the four `--reevaluate` sections; a pure function that reads and changes nothing.

    Args:
        window_jobs: Jobs first seen within `--since-days`, any status. Only
            their statuses are re-evaluated.
        context_jobs: Other stored jobs the window's jobs may duplicate: those
            first seen up to `duplicates.window_days` before the window, and
            the other members of their groups. They can join the window's
            groups, but their statuses are never re-evaluated.
        engine: A rule engine built from the current profiles.
        config: The current config; its `duplicates` section drives grouping.
    """
    added = plan_reevaluation(window_jobs, engine)
    removed: list[JobPost] = []
    report_only: list[JobPost] = []
    for job in window_jobs:
        if job.status == JobStatus.IGNORED or engine.match_profiles(job):
            continue
        if job.status == JobStatus.NEW:
            removed.append(job)
        else:
            report_only.append(job)
    group_changes = _changes_in_groups_of(window_jobs, assign_duplicate_groups(window_jobs, context_jobs, config))
    return ReevaluationPlan(
        added=added,
        removed=removed,
        report_only=report_only,
        group_changes=group_changes,
        groups=_groups_after(window_jobs, context_jobs, group_changes),
    )


def _display_width(text: str) -> int:
    """Return the terminal width of `text`: zero-width marks and joiners (such as ZWNJ) count 0, wide characters 2."""
    width = 0
    for char in text:
        if unicodedata.category(char) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _fit(text: str, width: int) -> str:
    """Truncate `text` to `width` display columns (adding "…") and pad it with spaces to exactly `width`."""
    text = " ".join(text.split())
    if _display_width(text) > width:
        while text and _display_width(text) > width - 1:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text + " " * (width - _display_width(text))


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render rows as a plain-text table with aligned columns; long titles and company names are truncated."""
    widths = [
        max([_display_width(header)] + [_display_width(" ".join(row[index].split())) for row in rows])
        for index, header in enumerate(headers)
    ]
    for index, header in enumerate(headers):
        if header in _TABLE_COLUMN_MAX:
            widths[index] = min(widths[index], _TABLE_COLUMN_MAX[header])
    lines = [
        "  ".join(_fit(cell, width) for cell, width in zip(headers, widths, strict=True)).rstrip(),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(_fit(cell, width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows
    )
    return "\n".join(lines)


def _format_reevaluation_table(plan: Sequence[tuple[JobPost, list[str]]]) -> str:
    """Render `plan_reevaluation` candidates as a plain-text table: platform, ID, title, matched profiles."""
    rows = [(job.platform, job.external_id, job.title, ", ".join(matched)) for job, matched in plan]
    return _format_table(("Platform", "ID", "Title", "Matched profiles"), rows)


def _job_cells(job: JobPost) -> tuple[str, str, str, str]:
    """Return the platform, ID, title, and company cells of a job (company "-" when blank)."""
    return job.platform, job.external_id, job.title, job.company.strip() or "-"


def _format_section(heading: str, table: str | None, empty: str) -> str:
    """Render one dry-run section: its heading, then the table, or `empty` when there is nothing to list."""
    return f"{heading}\n{table if table is not None else empty}"


def _format_reevaluation_plan(plan: ReevaluationPlan) -> str:
    """Render the four `--reevaluate --dry-run` sections as plain text.

    Each section lists platform, ID, title, and company, plus its own detail
    column: the matched profiles, the kept status, or the group and its change.
    """
    added = _format_section(
        f"{_SECTION_ADDED} (added): {len(plan.added)} IGNORED job(s) would be requeued as NEW and notified",
        _format_table(
            ("Platform", "ID", "Title", "Company", "Matched profiles"),
            [(*_job_cells(job), ", ".join(matched)) for job, matched in plan.added],
        )
        if plan.added
        else None,
        "No IGNORED job in the window matches the current profiles.",
    )
    removed = _format_section(
        f"{_SECTION_REMOVED} (removed): {len(plan.removed)} unsent NEW job(s) would become IGNORED",
        _format_table(("Platform", "ID", "Title", "Company"), [_job_cells(job) for job in plan.removed])
        if plan.removed
        else None,
        "Every unsent NEW job in the window still matches.",
    )
    report_only = _format_section(
        f"{_SECTION_REPORT_ONLY} (no longer match, report only): {len(plan.report_only)} job(s) "
        "already notified or later; their status is not changed",
        _format_table(
            ("Platform", "ID", "Title", "Company", "Status"),
            [(*_job_cells(job), job.status.value) for job in plan.report_only],
        )
        if plan.report_only
        else None,
        "Every job in the window that was already notified still matches.",
    )
    rows: list[tuple[str, ...]] = []
    for group, members in plan.groups:
        for member in members:
            key = (member.platform, member.external_id)
            if key not in plan.group_changes:
                change = ""
            elif member.duplicate_group is None:
                change = "joins"
            else:
                change = f"moves from {member.duplicate_group}"
            rows.append((group, *_job_cells(member), change))
    groups = _format_section(
        f"{_SECTION_GROUPS} (duplicate groups): {len(plan.groups)} group(s); "
        f"{len(plan.group_changes)} job(s) would get a new group",
        _format_table(("Group", "Platform", "ID", "Title", "Company", "Change"), rows) if rows else None,
        "No probable duplicates among the jobs in the window.",
    )
    return "\n\n".join((added, removed, report_only, groups))


async def _apply_reevaluation(db: Database, plan: ReevaluationPlan, summary: _ReevaluationSummary) -> None:
    """Write a re-evaluation plan: requeue, demote, log the report-only jobs, and update duplicate groups.

    Status changes are compare-and-set, so a job whose status changed since
    the plan was computed is left alone.
    """
    for job, matched in plan.added:
        if await db.requeue_ignored(job.platform, job.external_id, matched):
            summary.requeued += 1
            logger.info(
                "[{}] Requeued {}: {} | {} | profiles: {}",
                job.platform,
                job.external_id,
                job.title,
                job.company or "-",
                ", ".join(matched),
            )
    for job in plan.removed:
        if await db.demote_new(job.platform, job.external_id):
            summary.demoted += 1
            logger.info(
                "[{}] Demoted {} to IGNORED (no longer matches): {} | {}",
                job.platform,
                job.external_id,
                job.title,
                job.company or "-",
            )
    for job in plan.report_only:
        logger.info(
            "[{}] {} no longer matches; its status {} is kept (report only): {} | {}",
            job.platform,
            job.external_id,
            job.status.value,
            job.title,
            job.company or "-",
        )
    for (platform, external_id), group in plan.group_changes.items():
        await db.set_duplicate_group(platform, external_id, group)
        summary.regrouped += 1
        logger.info("[{}] Job {} -> duplicate group {}", platform, external_id, group)


async def _reevaluate(
    settings: Settings,
    config: AppConfig,
    *,
    since_days: int,
    dry_run: bool,
) -> _ReevaluationSummary:
    """Re-check the jobs first seen in the last `since_days` days against the current profiles.

    Steps:
    1. Open the database and apply pending migrations (this happens in a dry
       run too; migrations never change job rows).
    2. Load the jobs in the window (any status) and, for duplicate grouping,
       the jobs first seen up to `duplicates.window_days` earlier plus the
       other members of their groups. Run `build_reevaluation_plan`.
    3. Dry run: print the four sections and stop; nothing is changed or sent.
    4. Otherwise: requeue `IGNORED` jobs that match now (`requeue_ignored`),
       demote unsent `NEW` jobs that no longer match (`demote_new`), log the
       report-only jobs, write the duplicate-group changes, then run the same
       notification stage as `--once`, which sends every `NEW` job.

    Never launches a browser.
    """
    started = time.monotonic()
    since = datetime.now(UTC) - timedelta(days=since_days)
    summary = _ReevaluationSummary()
    outcome = "failed"
    logger.info(
        "Re-evaluation started{}: jobs first seen in the last {} day(s) (since {:%Y-%m-%d %H:%M} UTC)",
        " (dry run)" if dry_run else "",
        since_days,
        since,
    )
    try:
        rule_engine = RuleEngine(config)
        async with Database(settings.DB_PATH) as db:
            await db.init_db()
            stored = await _load_grouping_context(db, since - timedelta(days=config.duplicates.window_days))
            window_jobs = [job for job in stored if job.created_at >= since]
            context_jobs = [job for job in stored if job.created_at < since]
            plan = build_reevaluation_plan(window_jobs, context_jobs, rule_engine, config)
            summary.window_jobs = len(window_jobs)
            summary.checked = sum(1 for job in window_jobs if job.status == JobStatus.IGNORED)
            summary.candidates = len(plan.added)
            summary.demote_candidates = len(plan.removed)
            summary.report_only = len(plan.report_only)
            summary.group_changes = len(plan.group_changes)
            logger.info(
                "{} job(s) in the window ({} older job(s) for duplicate grouping): {} IGNORED job(s) match now, "
                "{} NEW job(s) and {} notified-or-later job(s) no longer match, {} job(s) change duplicate group",
                len(window_jobs),
                len(context_jobs),
                len(plan.added),
                len(plan.removed),
                len(plan.report_only),
                len(plan.group_changes),
            )

            if dry_run:
                print(f"\n{_format_reevaluation_plan(plan)}\n")
                outcome = "finished (dry run: nothing changed, nothing sent)"
                return summary

            await _apply_reevaluation(db, plan, summary)

            progress = _RunProgress(started=started, stage="notifying")
            platform_summaries: dict[Platform, PlatformSummary] = {}
            await _notify_pending(db, settings, platform_summaries, progress)
            summary.notified = sum(item.notified for item in platform_summaries.values())
            summary.failed = sum(item.failed for item in platform_summaries.values())
            summary.deferred = sum(item.deferred for item in platform_summaries.values())
        outcome = "finished"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        text = (
            f"in window {summary.window_jobs} | IGNORED checked {summary.checked} | requeued {summary.requeued} | "
            f"demoted {summary.demoted} | report only {summary.report_only} | regrouped {summary.regrouped} | "
            f"notified {summary.notified} | failed {summary.failed}"
        )
        if dry_run:
            text += (
                f" | would requeue {summary.candidates} | would demote {summary.demote_candidates} | "
                f"would regroup {summary.group_changes}"
            )
        if summary.deferred:
            text += f" | deferred {summary.deferred}"
        logger.info("Re-evaluation {} in {}: {}", outcome, _format_duration(time.monotonic() - started), text)
    return summary


async def _run_reevaluate(settings: Settings, config: AppConfig, *, since_days: int, dry_run: bool) -> int:
    """Run `--reevaluate` and return the process exit code (the same codes as `--once`)."""
    task = asyncio.current_task()
    remove_handler = _add_signal_handler(signal.SIGTERM, task.cancel) if task is not None else (lambda: None)
    try:
        summary = await _reevaluate(settings, config, since_days=since_days, dry_run=dry_run)
    except asyncio.CancelledError:
        if task is not None:
            task.uncancel()
        logger.warning("Re-evaluation interrupted; cleanup finished")
        return _EXIT_INTERRUPTED
    except Exception:
        logger.exception("Re-evaluation failed")
        return _EXIT_FATAL
    finally:
        remove_handler()
    return _EXIT_PARTIAL if summary.has_problems else _EXIT_OK


# ── Logging and CLI ────────────────────────────────────────────────────────


class _InterceptHandler(logging.Handler):
    """Route stdlib `logging` records (APScheduler, asyncio, ...) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one record to loguru, keeping its level and caller."""
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _configure_logging() -> None:
    """Log to the console (INFO) and a rotating file (DEBUG), and route stdlib logging into loguru.

    `diagnose=False` keeps variable values (such as the Telegram URL with the
    bot token) out of tracebacks.
    """
    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        _LOG_FILE,
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    # Request URLs contain the bot token (see src/notifier/telegram.py).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Per-tick "Running job"/"executed successfully" lines are noise; warnings
    # (skipped or missed runs) still come through.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _positive_int(value: str) -> int:
    """argparse type: a positive integer."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return number


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Job Hunter Agent: scrape JobVision and Jobinja, filter, store, and notify via Telegram.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run the pipeline once and exit")
    mode.add_argument("--daemon", action="store_true", help="run continuously on the configured schedule")
    mode.add_argument(
        "--reevaluate",
        action="store_true",
        help=(
            "re-check stored jobs against the current profiles: requeue IGNORED jobs that match now, "
            "demote unsent NEW jobs that no longer match, regroup duplicates, and notify"
        ),
    )
    parser.add_argument("--headful", action="store_true", help="show the browser window (overrides HEADLESS)")
    parser.add_argument(
        "--debug-capture", action="store_true", help="save intercepted JSON and HTML under data/debug/"
    )
    parser.add_argument(
        "--since-days",
        type=_positive_int,
        default=None,
        metavar="N",
        help=f"with --reevaluate: only jobs first seen in the last N days (default {_DEFAULT_SINCE_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="with --reevaluate: print what would change and change nothing"
    )
    args = parser.parse_args(argv)
    if not args.reevaluate:
        if args.since_days is not None:
            parser.error("--since-days requires --reevaluate")
        if args.dry_run:
            parser.error("--dry-run requires --reevaluate")
    if args.since_days is None:
        args.since_days = _DEFAULT_SINCE_DAYS
    return args


def _load_settings() -> Settings | None:
    """Load `config/.env`; log the problems and return None if it is invalid.

    Only field names and messages are logged, never the input values, which
    may include the bot token.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        logger.error(
            "Invalid settings in config/.env ({}). Copy config/.env.example to config/.env and fill it in",
            problems,
        )
        return None


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load settings and config, run the chosen mode, and return the exit code."""
    args = _parse_args(argv)
    _configure_logging()
    settings = _load_settings()
    if settings is None:
        return _EXIT_FATAL
    try:
        config = load_app_config(_CONFIG_PATH)
    except (OSError, ValueError) as exc:
        logger.error("Cannot load {}: {}", _CONFIG_PATH, exc)
        return _EXIT_FATAL

    try:
        if args.reevaluate:
            return asyncio.run(
                _run_reevaluate(settings, config, since_days=args.since_days, dry_run=args.dry_run)
            )
        runner = _run_daemon if args.daemon else _run_once
        return asyncio.run(runner(settings, config, headful=args.headful, debug_capture=args.debug_capture))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return _EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
