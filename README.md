# Rasad

An asynchronous job-search agent for two Iranian job boards, [JobVision](https://jobvision.ir) and [Jobinja](https://jobinja.ir). It scrapes new postings, filters them against your search profiles with a Persian-aware rule engine, stores them in SQLite, and sends the matches to a Telegram channel.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Scrape → filter → persist → notify via Telegram | ✅ Done (Tasks 1–6) |
| 2 | Tailor the master resume to a job with an LLM (Gemini), review and approve it from Telegram | Planned (Tasks 7–10) |
| 3 | Submit the approved application on the job board | Planned (Tasks 11–12) |

The task prompts live in [tasks/](tasks/).

## How it works

Each run goes through four steps:

1. **Scrape.** Playwright opens the search pages for every keyword in your profiles, one platform at a time. A job's detail page is visited only when the job is not already in the database.
2. **Filter.** The rule engine checks each job against every search profile:
   - at least one keyword appears as a whole word in the title or description;
   - no exclusion keyword appears in the title;
   - the city and work mode satisfy the profile's location rules.
3. **Persist.** Matching jobs are saved as `NEW`. Jobs that match no profile are saved as `IGNORED`, so they are not scraped again.
4. **Notify.** Every `NEW` job is sent to Telegram and becomes `NOTIFIED`. If a send fails, the job stays `NEW` and is retried on the next run.

Persian and English text are normalized the same way before matching. Messages and the database keep the original text.

## Requirements

- Linux (developed on Ubuntu 25.10)
- Python 3.11 or newer (the repository pins 3.12 in `.python-version`)
- [uv](https://docs.astral.sh/uv/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and a channel where the bot is an admin
- From Iran: a local HTTP or SOCKS proxy for PyPI, the Playwright download, and Telegram

## Installation

```bash
uv sync
uv run playwright install chromium
cp config/.env.example config/.env   # then fill in the values
uv run pytest
```

### Installing behind a proxy

From an Iranian connection, `files.pythonhosted.org` is often unreachable, and `uv sync` fails with `client error (Connect) … operation timed out`. Point uv at your local proxy through environment variables:

```bash
export HTTP_PROXY=http://127.0.0.1:10808
export HTTPS_PROXY=http://127.0.0.1:10808
export UV_HTTP_TIMEOUT=120
uv sync
```

Replace `10808` with your proxy's port. To keep these settings, add the lines to `~/.zshrc` or `~/.bashrc`.

Do not wrap uv in `proxychains4` while these variables are set. uv already connects to the proxy itself, so proxychains would route the proxy connection through the proxy again, and every download fails with `socket error or timeout`.

`uv run playwright install chromium` uses the same variables. Playwright may print `your OS is not officially supported` and download the Ubuntu 24.04 build; that is expected.

## Configuration

### Secrets: `config/.env`

Copy [config/.env.example](config/.env.example) to `config/.env`. This file is gitignored and must never be committed.

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | `@channelname` or a numeric ID such as `-1001234567890` |
| `TELEGRAM_PROXY_URL` | From Iran | Proxy for Telegram API calls, e.g. `socks5://127.0.0.1:10808` or `http://127.0.0.1:10808` |
| `DB_PATH` | No | SQLite file, default `data/jobs.db` |
| `HEADLESS` | No | `true` or `false`; `--headful` overrides it |

The Telegram notifier ignores `HTTP_PROXY` and `HTTPS_PROXY`. If Telegram is filtered on your connection, set `TELEGRAM_PROXY_URL` even when those variables are exported.

### Search rules: `config/config.yaml`

```yaml
scheduler:
  interval_minutes: 30          # how often --daemon runs the pipeline
  active_hours: "08:00-23:00"   # runs outside this window are skipped; may cross midnight
  timezone: "Asia/Tehran"

scraping:
  enabled_platforms: ["jobvision", "jobinja"]
  max_pages_per_keyword: 2

city_aliases:                   # canonical city key → spellings found in postings
  mashhad: ["mashhad", "مشهد"]
  tehran: ["tehran", "تهران"]

search_profiles:
  - profile_name: "AI & Automation"
    keywords: ["AI", "هوش مصنوعی", "n8n", "Agentic", "LLM", "RAG"]
    exclude_keywords: ["کارآموز", "Intern"]
    location_rules:
      - city: "mashhad"
        remote_only: false      # Mashhad: on-site, hybrid, and remote
      - city: "ALL"
        remote_only: true       # any other or unknown city: remote only
```

How the rules are applied:

- **Keywords** match whole words, case-insensitively. Spaces inside a keyword match any whitespace. A Latin keyword also matches its plural with a trailing `s` (`LLM` matches `LLMs`).
- **Location rules.** A rule for the job's own city takes precedence. If no rule names the job's city, the `ALL` rule decides. If there is no `ALL` rule either, the job is rejected. A profile with no location rules accepts every location.
- **Work mode** comes from structured fields and labels on the job page, never from the free-text description. When it cannot be determined, it is `ONSITE`.

## Usage

### First run

Both sites sit behind Cloudflare. Run once with a visible browser so you can pass a challenge by hand if one appears. The session is saved to `data/browser_state/` and reused by later headless runs.

```bash
uv run python main.py --once --headful
```

The first run with an empty database visits many detail pages (about 6 seconds each), so it takes a while. A heartbeat in the log shows that it is still making progress.

### Regular runs

```bash
uv run python main.py --once     # run the pipeline once, e.g. from cron or a systemd timer
uv run python main.py --daemon   # run every interval_minutes until Ctrl+C or SIGTERM
```

| Option | Description |
| --- | --- |
| `--once` | Run the pipeline once and exit |
| `--daemon` | Run on the schedule in `config/config.yaml` |
| `--headful` | Show the browser window |
| `--debug-capture` | Save intercepted JSON and HTML under `data/debug/` |

`--once` exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Fatal error in settings, config, or the database |
| 2 | Finished with problems: a platform failed, or some notifications are still pending |
| 130 | Interrupted |

### Logs

Logs go to the console at INFO level and to `data/logs/job_hunter.log` at DEBUG level. The file rotates at 10 MB and is kept for 14 days.

### Probing a site

When a site changes its pages or API, the probe opens a page in a visible browser and saves every JSON response and the HTML to `data/debug/{platform}/`:

```bash
uv run python -m scripts.probe --platform jobinja --keyword "هوش مصنوعی"
uv run python -m scripts.probe --platform jobvision --keyword AI --wait 20
uv run python -m scripts.probe --platform jobinja --url <job detail URL> --headless
```

## Project layout

```text
main.py                  entry point: --once and --daemon
config/                  config.yaml, .env.example (and your .env)
src/
  models.py              settings, config models, JobPost, job statuses
  rule_engine.py         Persian-aware keyword and location matching
  database.py            SQLite persistence (aiosqlite)
  notifier/telegram.py   Telegram delivery (httpx)
  scrapers/              Playwright scrapers for JobVision and Jobinja
  utils.py               text normalization and human-like delays
scripts/probe.py         live-site capture tool for building scrapers
tests/                   pytest suite (no network, no browser)
tasks/                   build task prompts
data/                    runtime files: database, logs, browser state (gitignored)
```

## Data and privacy

Everything under `data/` is created at run time and is gitignored: the database, logs, browser sessions, and debug captures. `config/.env` is gitignored as well. Phase 2 adds `inputs/` and `config/master_resume.yaml` for your resume; those must never be committed either.

## Development

The project is built task by task with Claude Code. [CLAUDE.md](CLAUDE.md) is the shared contract: interfaces, rules, and which files each task may change. The prompts are in [tasks/](tasks/), and [task-implement-flow.md](task-implement-flow.md) describes the workflow for each task (in Persian).

Run the tests:

```bash
uv run pytest
```

The tests are plain synchronous functions that use `asyncio.run()`. They need neither the network nor a browser.
