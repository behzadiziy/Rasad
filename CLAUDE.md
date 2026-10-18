# CLAUDE.md — Job Hunter Agent

This file is the project contract shared by all six build tasks. It is loaded automatically in every Claude Code session for this project. The user starts a task by pasting its prompt or by pointing you to its file in `tasks/` (e.g., `tasks/03-scraper-infra-probe.md`). If a task prompt and this file conflict, this file wins unless the task explicitly overrides it.

## Role

You are a Principal Python Architect and Senior Web Scraping Engineer with deep experience in asynchronous Python, Playwright automation, and Persian (Farsi) text processing.

## Product Overview

The Job Hunter Agent is a modular, asynchronous, state-driven tool for two Iranian job boards: **JobVision** (jobvision.ir) and **Jobinja** (jobinja.ir). It:
- reads search profiles from a YAML file
- scrapes listings with Playwright
- filters them through a local, Persian-aware Rule Engine
- stores every job it sees in SQLite, tracked by a status state machine
- sends Telegram notifications with inline buttons

**Product roadmap:**
- **Product Phase 1 (MVP), built by Tasks 1–6:** scrape → filter → persist → notify.
- **Product Phase 2 (future):** LLM-based resume tailoring from the stored `raw_description` (status `TAILORED`).
- **Product Phase 3 (future):** auto-apply (status `APPLIED`).

Never implement Product Phase 2/3 logic, Telegram callback handling, or any LLM calls.

## Build Plan

| Task | Scope | Depends on |
| --- | --- | --- |
| 1 | Foundation: project setup, config, models, normalization, rule engine | — |
| 2 | Persistence & notification: SQLite database, Telegram notifier | 1 |
| 3 | Scraper infrastructure and live-site probe | 1 |
| 4 | JobVision scraper, built from real captured samples | 1, 3 |
| 5 | Jobinja scraper, built from real captured samples | 1, 3 |
| 6 | Integration: orchestrator, CLI, daemon | 1–5 |

## Cross-Task Rules

1. **Frozen files.** Files produced by earlier tasks already exist in the repository and are frozen. Read them as needed, but never edit them. If a change is truly necessary, stop, explain the exact change and the reason, and wait for the user's approval before editing.
2. **One exception: `src/scrapers/constants.py`.** It has three sections: `SHARED`, `JOBVISION`, and `JOBINJA`. Task 3 creates the file. Task 4 may edit only the `JOBVISION` section, and Task 5 only the `JOBINJA` section.
3. **Interfaces.** Implement every public name in "Shared Interfaces" exactly as written: names, parameters, and return types. Private helpers (prefixed with `_`) are unrestricted. Add new public names only when a task requires them.
4. **Scope.** Create only the files listed in the task's Deliverables. Never modify `CLAUDE.md`, anything in `tasks/`, or `config/.env`.
5. **Task inputs.**
   - Read frozen files from the repository; the user does not need to paste them.
   - For Tasks 4 and 5, read the samples from `data/debug/<platform>/` unless the user pastes them.
   - If it is unclear which capture is the job-list response, show the candidates (URL, size, structure) and ask the user.

## Tech Stack & Dependencies

- **Language:** Python 3.11+, fully asynchronous (`asyncio`), with type hints on every function and method.
- **Pydantic:** v2 APIs only (`field_validator`, `model_validator`, `ConfigDict`, `model_dump`). Never use v1 APIs (`validator`, `.dict()`).
- **Package manager:** `uv`, with all metadata in `pyproject.toml`. The project is an application, so no build backend is needed. All imports use the `src.` prefix, and all commands run from the project root.
- **Runtime dependencies** (lower-bound pins only, except where stated):
  - `playwright` (async API) and `selectolax`
  - `pydantic>=2`, `pydantic-settings`, `pyyaml`, `python-dotenv`
  - `aiosqlite`
  - `httpx[socks]>=0.26`: the `socks` extra is required for SOCKS5 proxies, and the `proxy=` argument requires 0.26+.
  - `tenacity`
  - `apscheduler>=3.10,<4`: **this must stay below 4.** The 4.x series is a pre-release with a different API and no `AsyncIOScheduler`.
  - `loguru`
  - `argparse` (stdlib)
- **Dev dependencies:** `pytest`, in a `[dependency-groups]` `dev` group. Tests are plain synchronous pytest functions, and async code is exercised with `asyncio.run()`. Do not use pytest plugins.
- `pyproject.toml` is frozen after Task 1. Never add dependencies yourself. If one seems necessary, ask the user.

## Project Structure

```text
job-hunter-agent/
├── CLAUDE.md                       # this file
├── tasks/                          # task prompts 01–06 (read-only)
├── pyproject.toml                  # Task 1
├── main.py                         # Task 6
├── config/
│   ├── config.yaml                 # Task 1
│   └── .env.example                # Task 1
├── data/
│   └── .gitkeep                    # Task 1 (runtime: jobs.db, browser_state/, debug/, logs/)
├── scripts/
│   └── probe.py                    # Task 3
├── src/
│   ├── __init__.py                 # Task 1
│   ├── models.py                   # Task 1
│   ├── utils.py                    # Task 1
│   ├── rule_engine.py              # Task 1
│   ├── database.py                 # Task 2
│   ├── notifier/
│   │   ├── __init__.py             # Task 2
│   │   └── telegram.py             # Task 2
│   └── scrapers/
│       ├── __init__.py             # Task 3
│       ├── constants.py            # Task 3 (JOBVISION section: Task 4, JOBINJA section: Task 5)
│       ├── base.py                 # Task 3
│       ├── jobvision.py            # Task 4
│       └── jobinja.py              # Task 5
└── tests/
    ├── __init__.py                 # Task 1
    ├── test_utils.py               # Task 1
    ├── test_rule_engine.py         # Task 1
    ├── test_database.py            # Task 2
    ├── test_scraper_helpers.py     # Task 3
    ├── test_jobvision_parser.py    # Task 4
    ├── test_jobinja_parser.py      # Task 5
    └── fixtures/
        ├── jobvision/              # Task 4
        └── jobinja/                # Task 5
```

## Shared Interfaces

```python
# ── src/models.py ───────────────────────────────────────────────
Platform = Literal["jobvision", "jobinja"]

class JobStatus(StrEnum):
    NEW = "NEW"
    NOTIFIED = "NOTIFIED"
    TAILORED = "TAILORED"
    APPLIED = "APPLIED"
    IGNORED = "IGNORED"

class WorkMode(StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"

class Settings(BaseSettings): ...        # fields defined in Task 1
class SchedulerConfig(BaseModel): ...    # interval_minutes, active_hours, timezone
class ScrapingConfig(BaseModel): ...     # enabled_platforms, max_pages_per_keyword
class LocationRule(BaseModel): ...       # city, remote_only
class SearchProfile(BaseModel): ...      # profile_name, keywords, exclude_keywords, location_rules
class AppConfig(BaseModel): ...          # scheduler, scraping, city_aliases, search_profiles

def load_app_config(path: str | Path = "config/config.yaml") -> AppConfig: ...

class JobPost(BaseModel):
    platform: Platform
    external_id: str
    title: str
    company: str
    city: str                            # raw location text; may be empty
    work_mode: WorkMode
    salary: str | None = None
    url: str
    raw_description: str | None = None   # stored for Product Phase 2
    matched_profiles: list[str] = Field(default_factory=list)
    status: JobStatus = JobStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_remote(self) -> bool: ...     # work_mode == WorkMode.REMOTE

# ── src/utils.py ────────────────────────────────────────────────
def normalize_text(text: str) -> str: ...
async def human_delay(min_s: float = 1.5, max_s: float = 3.5) -> None: ...

# ── src/rule_engine.py ──────────────────────────────────────────
class RuleEngine:
    def __init__(self, config: AppConfig) -> None: ...
    def matches(self, job: JobPost, profile: SearchProfile) -> bool: ...
    def match_profiles(self, job: JobPost) -> list[str]: ...

# ── src/database.py ─────────────────────────────────────────────
class Database:
    def __init__(self, db_path: str | Path) -> None: ...
    async def __aenter__(self) -> "Database": ...    # connects and applies PRAGMAs
    async def __aexit__(self, *exc_info: object) -> None: ...
    async def init_db(self) -> None: ...
    async def job_exists(self, platform: str, external_id: str) -> bool: ...
    async def save_job(self, job: JobPost) -> bool: ...
    async def get_pending_notifications(self) -> list[JobPost]: ...
    async def update_status(self, platform: str, external_id: str, status: JobStatus) -> None: ...
    async def close(self) -> None: ...

# ── src/notifier/telegram.py ────────────────────────────────────
class TelegramNotifier:
    def __init__(self, settings: Settings) -> None: ...
    async def __aenter__(self) -> "TelegramNotifier": ...
    async def __aexit__(self, *exc_info: object) -> None: ...
    async def send_job(self, job: JobPost) -> bool: ...  # never raises on API/network failure

# ── src/scrapers/base.py ────────────────────────────────────────
IsKnown = Callable[[str, str], Awaitable[bool]]

class ScraperBlockedError(Exception): ...

async def create_browser_context(browser: Browser, platform: Platform) -> BrowserContext: ...
async def save_storage_state(context: BrowserContext, platform: Platform) -> None: ...
async def wait_for_cloudflare(page: Page, platform: Platform, *, headful: bool) -> None: ...
def save_debug_artifact(platform: Platform, name: str, content: str | bytes, ext: str) -> Path: ...
def extract_json_ld_job_posting(html: str) -> dict[str, Any] | None: ...
def detect_work_mode(text: str) -> WorkMode: ...

class BaseScraper(ABC):
    platform: ClassVar[Platform]
    def __init__(self, browser: Browser, config: AppConfig, is_known: IsKnown,
                 *, headful: bool = False, debug_capture: bool = False) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc_info: object) -> None: ...
    @abstractmethod
    async def fetch_jobs(self, keywords: list[str]) -> list[JobPost]: ...

# ── src/scrapers/jobvision.py ───────────────────────────────────
class JobVisionScraper(BaseScraper): ...
def parse_search_payload(payload: Any) -> list[JobPost]: ...
def parse_detail_html(html: str) -> str | None: ...   # returns the description as plain text

# ── src/scrapers/jobinja.py ─────────────────────────────────────
class JobinjaScraper(BaseScraper): ...
def extract_external_id(url: str) -> str | None: ...
def parse_search_html(html: str) -> list[JobPost]: ...
def parse_detail_html(html: str) -> str | None: ...   # returns the description as plain text
```

## Domain Rules

- `normalize_text` is used only for matching. The database and notifications always keep the original text.
- Jobs that match no profile are persisted with status `IGNORED`, so `job_exists` prevents re-fetching their detail pages on later runs.
- When the work mode cannot be determined, it defaults to `WorkMode.ONSITE`, logged at DEBUG. This is deliberately conservative: such jobs will not pass remote-only rules.
- Never infer the work mode from free-text job descriptions. Negations such as «امکان دورکاری وجود ندارد» cause false positives. Use structured fields, badges, or short labels only.
- All datetimes are timezone-aware and stored in UTC.
- Scrapers visit a detail page only after `is_known(platform, external_id)` returns `False`. On detail pages, prefer JSON-LD `JobPosting` data and fall back to DOM selectors.
- Scrapers process keywords and pages sequentially (concurrency 1) and call `human_delay()` between navigations.
- Never solve or bypass CAPTCHAs programmatically. In headful mode the user solves challenges manually.

## Code Quality Requirements

- Write complete, production-grade code. Do not use `# TODO`, placeholder `pass`, `...` bodies, or "implement here" comments. The only exception is unknown site-specific values in `constants.py` (see "Handling Unknowns").
- Add docstrings to every module, class, and public function.
- Use loguru logging at key lifecycle stages.
- Never use a bare `except:`. Catch specific exceptions. At platform and run boundaries, use `except Exception` together with `logger.exception`.
- Use parameterized SQL only.

## Handling Unknowns

- Never present guesses about URLs, API routes, JSON fields, or CSS selectors as facts.
- Every site-specific value lives in `src/scrapers/constants.py`, with one of two comments:
  - `# Source: <sample id or file path>` when the value is derived from a real sample
  - `# VERIFY: <what it is and how to confirm it>` when it is not
- Code that uses these constants must be complete and working, so that fixing a broken scraper requires editing `constants.py` only.
- If any other requirement is ambiguous, choose the most conservative reasonable interpretation, implement it, and list it under "Assumptions to Verify". If the ambiguity would change the design, ask the user before implementing.

## Commands & Safety

**Setup (once):** `uv sync` and `uv run playwright install chromium`.

**Tests:** after writing code, run `uv run pytest`. Fix failures in your task's own files before finishing.

**What you may run yourself:** headless smoke runs that only read public pages, such as `uv run python -m src.scrapers.jobvision --keyword AI --pages 1`.

**What the user must run:**
- Anything that needs a visible browser or manual interaction: the probe in its default headful mode, and any `--headful` run. Ask the user to run it and share the output.
- Anything that sends real Telegram messages: `uv run python -m src.notifier.telegram` and `main.py --once`. Ask the user before running these.
- `main.py --daemon`. Never start it yourself; it runs indefinitely.

**Secrets and data:**
- Never read, print, or edit `config/.env`, because it holds secrets. Use `config/.env.example` for reference.
- Never commit `config/.env` or anything under `data/` except `.gitkeep`.

## Task Workflow

1. Read the task prompt and the frozen files it depends on.
2. Write the Deliverables directly to disk, in the listed order. Test fixtures are trimmed copies of the real samples.
3. Run the tests and every acceptance command you are allowed to run. Fix any failures.
4. Finish with a **Self-Review** message with four parts:
   - **Checklist:** each item from the task's Self-Review Checklist, marked ✅ or ⚠️, with the file where it is implemented.
   - **Assumptions to Verify:** one line per `# VERIFY` constant and per interpretation you chose.
   - **Contract Change Requests:** approved changes to frozen files that you made, or "None".
   - **How to Verify:** the commands you ran with their results, and the commands the user must run manually.
