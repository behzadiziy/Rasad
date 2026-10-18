# Task 6 — Integration: Orchestrator, CLI, and Daemon

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Wire every component into a working application, with single-run and daemon modes.

## Inputs

Frozen from Tasks 1–5:
- `src/models.py`, `src/utils.py`, `src/rule_engine.py`
- `src/database.py`, `src/notifier/telegram.py`
- `src/scrapers/base.py`, `src/scrapers/jobvision.py`, `src/scrapers/jobinja.py`
- `config/config.yaml`

If context is tight, their public signatures are enough, because they must match the Shared Interfaces.

## Deliverables

1. `main.py`

## Specification

### 1. Pipeline

`run_pipeline(settings: Settings, config: AppConfig, *, headful: bool, debug_capture: bool)` returns a per-platform run summary.

1. Open `Database(settings.DB_PATH)` and call `init_db()`.
2. Launch Playwright Chromium. It runs headless unless `--headful` is passed or `HEADLESS=false`. Launch a fresh browser for each run and close it at the end, to avoid memory growth in a long-running daemon.
3. Map platform names to scraper classes: `{"jobvision": JobVisionScraper, "jobinja": JobinjaScraper}`. For each enabled platform, run its scraper sequentially:
   - Use `async with Scraper(browser, config, db.job_exists, headful=..., debug_capture=...)`.
   - Pass the deduplicated union of all profiles' keywords.
   - Catch `ScraperBlockedError` and any unexpected exception per platform, log it with a traceback, and continue with the next platform.
4. Deduplicate the collected jobs by `(platform, external_id)`.
5. For each job, compute `matched = rule_engine.match_profiles(job)`.
   - If `matched` is non-empty, set `matched_profiles = matched` and `status = NEW`.
   - Otherwise set `status = IGNORED`.
   - Then call `save_job`.
6. Inside `async with TelegramNotifier(settings)`, send each job from `get_pending_notifications()`. On success, set its status to `NOTIFIED`; on failure, leave it `NEW` so the next run retries it. The notifier handles pacing internally.
7. Log a summary per platform: scraped / new / ignored / notified / failed.

### 2. CLI (argparse)

| Flag | Behavior |
| --- | --- |
| `--once` | Run the pipeline once and exit cleanly (suitable for cron or systemd timers). |
| `--daemon` | Run continuously on the configured schedule. |
| `--headful` | Show the browser window. Overrides `HEADLESS`. |
| `--debug-capture` | Save intercepted JSON and HTML under `data/debug/`. |

`--once` and `--daemon` form a required, mutually exclusive group.

### 3. Daemon mode

- **Scheduler:** APScheduler 3.x `AsyncIOScheduler(timezone=config.scheduler.timezone)`.
- **Trigger:** an interval trigger with:
  - `minutes=interval_minutes`
  - `next_run_time=now`, so the first run starts immediately
  - `max_instances=1` and `coalesce=True`, so runs never overlap
  - a sensible `misfire_grace_time`
- **Active hours:** each run first checks `active_hours` in the configured timezone, supporting windows that cross midnight. Outside the window, it skips the run with an INFO log.
- **Graceful shutdown:** handle SIGINT and SIGTERM via `loop.add_signal_handler`. On Windows, where that is unsupported, fall back to `KeyboardInterrupt` handling. Shutdown must:
  1. Stop the scheduler.
  2. Give the in-flight run up to 60 s to finish, then cancel it cleanly.
  3. Make sure the scrapers' context managers save `storage_state`.
  4. Close the browser, the database, and the HTTP client.

### 4. Logging & documentation

- **Logging:** configure loguru with console output plus a rotating file at `data/logs/job_hunter.log` (rotation 10 MB, retention 14 days).
- **Module docstring:** the docstring of `main.py` must contain these setup and run commands:

```
uv sync
uv run playwright install chromium
cp config/.env.example config/.env       # then fill in values
uv run python main.py --once --headful   # first run: pass Cloudflare, save browser state
uv run python main.py --daemon
uv run pytest
```

## Acceptance Criteria

1. `uv run pytest` still passes.
2. `uv run python main.py --once` logs every pipeline stage and the summary. Telegram receives messages for matching jobs only.
3. Running `--once` again immediately sends nothing new. The logs show that known jobs were skipped without visiting their detail pages.
4. With a broken proxy or token, jobs stay `NEW`. The next successful run sends them.
5. With `interval_minutes: 1`, `--daemon` runs immediately and then every minute. With `active_hours` set to exclude the current time, runs are skipped with an INFO log.
6. Ctrl+C during the daemon shuts down cleanly with no traceback, and the `data/browser_state/*.json` files are updated.
7. `--once --headful --debug-capture` shows the browser and writes artifacts under `data/debug/`.

## Self-Review Checklist

- Pipeline order matches §1; non-matching jobs are saved as `IGNORED`
- Per-platform failures are isolated; one blocked site doesn't stop the other
- Failed notifications stay `NEW` and are retried on the next run
- A fresh browser is launched per run and always closed
- `max_instances=1`, `coalesce=True`, an immediate first run, and timezone-aware active hours that support midnight-crossing windows
- Graceful shutdown on SIGINT/SIGTERM with a Windows fallback; `storage_state`, DB, and HTTP client all closed
- loguru console and rotating file configured; setup commands present in the module docstring
