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
Options:
    --headful         Show the browser window (overrides HEADLESS in config/.env).
    --debug-capture   Save intercepted JSON and HTML under `data/debug/`.

`--once` exit codes: 0 = success; 1 = fatal error (settings, config, database);
2 = finished with problems (a platform failed or notifications are still pending);
130 = interrupted.

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
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
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
    """Return the union of all profiles' keywords, in config order.

    Keywords that are equal after `normalize_text` (e.g. "AI" and "ai") are
    searched once, using the first spelling.
    """
    keywords: dict[str, str] = {}
    for profile in config.search_profiles:
        for keyword in profile.keywords:
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


async def _save_jobs(
    db: Database,
    rule_engine: RuleEngine,
    jobs: list[JobPost],
    summaries: dict[Platform, PlatformSummary],
) -> None:
    """Deduplicate jobs, match them against the profiles, and save them as NEW or IGNORED."""
    unique: dict[tuple[str, str], JobPost] = {}
    for job in jobs:
        unique.setdefault((job.platform, job.external_id), job)
    if len(unique) < len(jobs):
        logger.info("Dropped {} duplicate job(s)", len(jobs) - len(unique))

    saved_new = saved_ignored = 0
    for job in unique.values():
        matched = rule_engine.match_profiles(job)
        if matched:
            job = job.model_copy(update={"matched_profiles": matched, "status": JobStatus.NEW})
        else:
            job = job.model_copy(update={"status": JobStatus.IGNORED})
        if not await db.save_job(job):
            continue
        summary = summaries.setdefault(job.platform, PlatformSummary())
        if matched:
            summary.new += 1
            saved_new += 1
            logger.info(
                "[{}] Match {}: {} | {} | profiles: {}",
                job.platform,
                job.external_id,
                job.title,
                job.company or "-",
                ", ".join(matched),
            )
        else:
            summary.ignored += 1
            saved_ignored += 1
    logger.info("Saved {} job(s): {} NEW, {} IGNORED", saved_new + saved_ignored, saved_new, saved_ignored)


async def _notify_pending(
    db: Database,
    settings: Settings,
    summaries: dict[Platform, PlatformSummary],
    progress: _RunProgress,
) -> None:
    """Send every NEW job; delivered jobs become NOTIFIED, the others stay NEW for the next run."""
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
            if await notifier.send_job(job):
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
       profiles, and save them as `NEW` (matched) or `IGNORED`.
    4. Send every `NEW` job to Telegram; delivered jobs become `NOTIFIED`.

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
                    await _save_jobs(db, rule_engine, collected, summaries)
                raise

            logger.info("Stage 3/4: filtering and saving {} job(s)", len(collected))
            progress.stage = "filtering and saving"
            await _save_jobs(db, rule_engine, collected, summaries)

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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Job Hunter Agent: scrape JobVision and Jobinja, filter, store, and notify via Telegram.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run the pipeline once and exit")
    mode.add_argument("--daemon", action="store_true", help="run continuously on the configured schedule")
    parser.add_argument("--headful", action="store_true", help="show the browser window (overrides HEADLESS)")
    parser.add_argument(
        "--debug-capture", action="store_true", help="save intercepted JSON and HTML under data/debug/"
    )
    return parser.parse_args(argv)


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

    runner = _run_daemon if args.daemon else _run_once
    try:
        return asyncio.run(runner(settings, config, headful=args.headful, debug_capture=args.debug_capture))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return _EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
