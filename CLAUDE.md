# CLAUDE.md — Job Hunter Agent

This file is the project contract shared by all build tasks. It is loaded automatically in every Claude Code session for this project. The user starts a task by pasting its prompt or by pointing you to its file in `tasks/` (e.g., `tasks/08-tailoring-engine.md`). If a task prompt and this file conflict, this file wins unless the task explicitly overrides it.

## Role

You are a Principal Python Architect and Senior Web Scraping Engineer with deep experience in asynchronous Python, Playwright automation, LLM application design, and Persian (Farsi) text processing.

## Product Overview

The Job Hunter Agent is a modular, asynchronous, state-driven tool for two Iranian job boards: **JobVision** (jobvision.ir) and **Jobinja** (jobinja.ir).

**Product roadmap:**
- **Product Phase 1 (done, Tasks 1–6):** scrape → filter → persist → notify via Telegram.
- **Product Phase 2 (Tasks 7, 7b, 8–10):** when the user asks from Telegram, an LLM tailors the master resume to a job. The user receives a review package with every change highlighted, then approves, regenerates, or rejects it.
- **Product Phase 3 (Tasks 11–12):** after the user approves a tailored resume, the agent submits the application on the job board.

## Build Plan

| Task | Phase | Scope | Depends on |
| --- | --- | --- | --- |
| 1–6 | 1 | Done: foundation, persistence, scrapers, integration | — |
| 7 | 2 | Foundation v2: settings, config sections, statuses, DB migrations, re-evaluation | 1–6 |
| 7b | 2 | Matching quality and duplicate detection | 7 |
| 8 | 2 | Master resume, LLM layer (Gemini), tailoring engine | 7, 7b |
| 9 | 2 | Resume PDF rendering (clean and review versions) | 8 |
| 10 | 2 | Two-way Telegram bot: review and approval flow | 7–9 |
| 11 | 3 | Login and apply-flow probe | 3, 7 |
| 12 | 3 | Application automation | 10, 11 |

## Cross-Task Rules

1. **Frozen files.** Files produced by earlier tasks are frozen. Read them as needed. A task may change a frozen file only if that file is listed in the task's **Authorized Changes** section, and only in the way described there. Any other change requires the user's approval first: explain the exact change and the reason, and wait.
2. **`src/scrapers/constants.py`** keeps its sections: `SHARED` (Task 3), `JOBVISION` (Task 4), and `JOBINJA` (Task 5).
3. **Interfaces.** Implement every public name in "Shared Interfaces" exactly as written. Private helpers (prefixed with `_`) are unrestricted. Add new public names only when a task requires them.
4. **Scope.** Create or change only the files listed in the task's Deliverables and Authorized Changes. Never modify `CLAUDE.md`, `tasks/`, `config/.env`, or `inputs/`. Never modify `config/master_resume.yaml`, except when Task 8 creates it.
5. **Task inputs.** Read frozen files and captured samples from the repository. If it is unclear which sample or file a task needs, show the candidates and ask the user.

## Tech Stack & Dependencies

- **Language:** Python 3.11+, fully asynchronous (`asyncio`), with type hints on every function and method.
- **Pydantic:** v2 APIs only (`field_validator`, `model_validator`, `ConfigDict`, `model_dump`).
- **Package manager:** `uv`. The project is an application, with no build backend. All imports use the `src.` prefix, and all commands run from the project root.
- **Runtime dependencies:**
  - `playwright`, `selectolax`
  - `pydantic>=2`, `pydantic-settings`, `pyyaml`, `python-dotenv`
  - `aiosqlite`, `httpx[socks]>=0.26`, `tenacity`
  - `apscheduler>=3.10,<4`: **must stay below 4.**
  - `loguru`, `tzdata`
  - `jinja2`: added in Task 7
- **Dev dependencies:** `pytest` only. Tests are plain synchronous functions that use `asyncio.run()`.
- **LLM:** Google Gemini, called through its REST API with httpx. Do not use the Gemini SDK or the OpenAI-compatibility layer. OpenAI and OpenRouter will be added later behind the same `LLMClient` interface; do not implement them now.
- **Throwaway dependencies:** one-off scripts may use `uv run --with <package>` (e.g., `python-docx` during resume conversion) instead of adding to `pyproject.toml`. Only Task 7 may change `pyproject.toml`.

## Project Structure

```text
job-hunter-agent/
├── CLAUDE.md
├── tasks/                          # task prompts (read-only)
├── inputs/                         # the user's resume files (gitignored, read-only)
├── docs/
│   └── apply_flows.md              # Task 11
├── prompts/
│   ├── tailor_system.md            # Task 8
│   └── tailor_user.md              # Task 8
├── templates/
│   ├── resume.html.j2              # Task 9
│   └── resume.css                  # Task 9
├── assets/fonts/                   # Task 9 (Vazirmatn + OFL license + README)
├── pyproject.toml                  # Task 1, extended in Task 7
├── main.py                         # Task 6, extended in Tasks 7, 7b and 10
├── config/
│   ├── config.yaml                 # Task 1, extended in Tasks 7 and 7b
│   ├── .env.example                # Task 1, extended in Task 7
│   ├── master_resume.example.yaml  # Task 8 (fictional person)
│   └── master_resume.yaml          # Task 8 (the user's data, gitignored)
├── data/                           # runtime only, gitignored except .gitkeep
├── scripts/
│   ├── probe.py                    # Task 3
│   └── apply_probe.py              # Task 11
├── src/
│   ├── models.py                   # Task 1, extended in Tasks 7 and 7b
│   ├── utils.py                    # Task 1
│   ├── rule_engine.py              # Task 1, extended in Task 7b
│   ├── duplicates.py               # Task 7b
│   ├── database.py                 # Task 2, extended in Tasks 7 and 7b
│   ├── notifier/telegram.py        # Task 2, extended in Tasks 7b and 10
│   ├── scrapers/                   # Tasks 3–5
│   ├── llm/                        # Task 8: base.py, gemini.py, factory.py
│   ├── resume/                     # Task 8: models.py, language.py, changes.py, tailor.py; Task 9: render.py
│   ├── bot/                        # Task 10: client.py, callbacks.py, messages.py, service.py
│   └── apply/                      # Task 12: constants.py, base.py, jobvision.py, jobinja.py
└── tests/                          # one or more test files per task; fixtures use fictional data
```

## Shared Interfaces

### Phase 1 (frozen)

```python
# ── src/models.py ───────────────────────────────────────────────
Platform = Literal["jobvision", "jobinja"]

class WorkMode(StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"

class Settings(BaseSettings): ...        # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_URL, DB_PATH, HEADLESS
class SchedulerConfig(BaseModel): ...
class ScrapingConfig(BaseModel): ...
class LocationRule(BaseModel): ...
class SearchProfile(BaseModel): ...
class AppConfig(BaseModel): ...          # scheduler, scraping, city_aliases, search_profiles

def load_app_config(path: str | Path = "config/config.yaml") -> AppConfig: ...

class JobPost(BaseModel):
    platform: Platform
    external_id: str
    title: str
    company: str
    city: str
    work_mode: WorkMode
    salary: str | None = None
    url: str
    raw_description: str | None = None
    matched_profiles: list[str] = Field(default_factory=list)
    status: JobStatus = JobStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    @property
    def is_remote(self) -> bool: ...

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
    async def __aenter__(self) -> "Database": ...
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
    async def send_job(self, job: JobPost) -> bool: ...   # Task 7b adds an optional `duplicates` argument

# ── src/scrapers/ ───────────────────────────────────────────────
IsKnown = Callable[[str, str], Awaitable[bool]]
class ScraperBlockedError(Exception): ...
class BaseScraper(ABC): ...              # fetch_jobs(keywords) -> list[JobPost]
class JobVisionScraper(BaseScraper): ...
class JobinjaScraper(BaseScraper): ...
```

### Phase 2 and 3

```python
# ── src/models.py (Task 7 additions) ────────────────────────────
class JobStatus(StrEnum):
    NEW = "NEW"
    NOTIFIED = "NOTIFIED"
    TAILORED = "TAILORED"            # review package sent; awaiting the user's decision
    APPROVED = "APPROVED"            # the user approved a tailored resume
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"
    SKIPPED = "SKIPPED"              # the user rejected the job
    IGNORED = "IGNORED"

Language = Literal["fa", "en"]

# Settings gains: GEMINI_API_KEY, LLM_PROXY_URL, TELEGRAM_ALLOWED_USER_IDS (str), TELEGRAM_REVIEW_CHAT_ID
#   @property allowed_user_ids -> frozenset[int]
#   @property review_chat_id -> str | None   # TELEGRAM_REVIEW_CHAT_ID, else the first allowed user ID
class LLMConfig(BaseModel): ...          # provider, model, temperature, max_output_tokens, timeout_seconds
class TailoringConfig(BaseModel): ...    # master_resume_path, output_dir, cover_letter, max_versions_per_job, persian_ratio_threshold
class BotConfig(BaseModel): ...          # poll_timeout_seconds, state_path
class ApplyConfig(BaseModel): ...        # enabled, daily_limit, confirm_before_submit, confirm_timeout_minutes, screenshots_dir
# AppConfig gains: llm: LLMConfig | None = None, tailoring, bot, apply (all with defaults)

def can_transition(old: JobStatus, new: JobStatus) -> bool: ...

class TailoredResumeRecord(BaseModel):
    id: int | None = None
    platform: Platform
    external_id: str
    version: int
    language: Language
    llm_model: str
    plan_json: str
    changes_json: str
    clean_pdf_path: str | None = None
    review_pdf_path: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None

class ApplicationRecord(BaseModel):
    id: int | None = None
    platform: Platform
    external_id: str
    resume_id: int
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    submitted: bool
    receipt_path: str | None = None
    error: str | None = None

# ── src/database.py (Task 7 additions) ──────────────────────────
    async def get_job(self, platform: str, external_id: str) -> JobPost | None: ...
    async def get_jobs_by_status(self, status: JobStatus, *, since: datetime | None = None) -> list[JobPost]: ...
    async def transition_status(self, platform: str, external_id: str,
                                from_statuses: Collection[JobStatus], to: JobStatus) -> bool: ...  # atomic compare-and-set
    async def requeue_ignored(self, platform: str, external_id: str, matched_profiles: list[str]) -> bool: ...
    async def save_tailored_resume(self, record: TailoredResumeRecord) -> int: ...
    async def update_tailored_resume_paths(self, resume_id: int, clean_pdf_path: str, review_pdf_path: str) -> None: ...
    async def get_tailored_resume(self, resume_id: int) -> TailoredResumeRecord | None: ...
    async def get_latest_tailored_resume(self, platform: str, external_id: str) -> TailoredResumeRecord | None: ...
    async def count_tailored_resumes(self, platform: str, external_id: str) -> int: ...
    async def mark_tailored_resume_approved(self, resume_id: int) -> None: ...
    async def record_application(self, record: ApplicationRecord) -> int: ...
    async def count_submitted_applications_since(self, since: datetime) -> int: ...
    async def has_submitted_application(self, platform: str, external_id: str) -> bool: ...

# ── Task 7b additions ───────────────────────────────────────────
# SearchProfile.keywords: list[str | list[str]]   (a nested list is a synonym group)
# SearchProfile.description_min_hits: int = 1
# AppConfig gains: duplicates: DuplicatesConfig   (window_days: int = 45, title_overlap_threshold: float = 0.8)
# JobPost gains: duplicate_group: str | None = None
# TelegramNotifier.send_job(job, *, duplicates: Sequence[JobPost] = ()) -> bool

# src/duplicates.py
def company_keys(company: str) -> frozenset[str]: ...
def title_tokens(title: str, *, city_aliases: Mapping[str, list[str]]) -> frozenset[str]: ...
def is_probable_duplicate(a: JobPost, b: JobPost, config: AppConfig) -> bool: ...
def assign_duplicate_groups(candidates: Sequence[JobPost], existing: Sequence[JobPost],
                            config: AppConfig) -> dict[tuple[str, str], str]: ...
def find_prior_activity(job: JobPost, members: Sequence[JobPost]) -> JobPost | None: ...

# src/database.py
    async def demote_new(self, platform: str, external_id: str) -> bool: ...
    async def get_jobs_since(self, since: datetime) -> list[JobPost]: ...
    async def set_duplicate_group(self, platform: str, external_id: str, group: str | None) -> None: ...
    async def get_duplicate_group_members(self, group: str) -> list[JobPost]: ...

# ── src/llm/ (Task 8) ───────────────────────────────────────────
T = TypeVar("T", bound=BaseModel)
class LLMError(Exception): ...
class LLMUsage(BaseModel): prompt_tokens: int; output_tokens: int; total_tokens: int
class LLMResult(BaseModel, Generic[T]): value: T; model: str; usage: LLMUsage; latency_seconds: float
class LLMClient(ABC):
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc_info: object) -> None: ...
    @abstractmethod
    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> LLMResult[T]: ...
class GeminiClient(LLMClient): ...                                                # gemini.py
def create_llm_client(config: AppConfig, settings: Settings) -> LLMClient: ...    # factory.py

# ── src/resume/ (Tasks 8–9) ─────────────────────────────────────
class MasterResume(BaseModel): ...       # bilingual; every item has a stable, unique id
class TailoringPlan(BaseModel): ...      # the LLM output schema
class ChangeSet(BaseModel): ...          # computed by code, never reported by the LLM
class PlanValidation(BaseModel): errors: list[str]; warnings: list[str]
class TailoringError(Exception): ...
class TailoringResult(BaseModel):
    plan: TailoringPlan; changes: ChangeSet; warnings: list[str]
    language: Language; model: str; usage: LLMUsage

def load_master_resume(path: str | Path) -> MasterResume: ...
def detect_language(text: str, *, persian_ratio_threshold: float) -> Language: ...
def validate_plan(plan: TailoringPlan, master: MasterResume, job: JobPost, language: Language) -> PlanValidation: ...
def compute_changes(plan: TailoringPlan, master: MasterResume) -> ChangeSet: ...
async def tailor_resume(job: JobPost, master: MasterResume, llm: LLMClient, config: AppConfig) -> TailoringResult: ...

def render_resume_html(master: MasterResume, plan: TailoringPlan, changes: ChangeSet,
                       *, review: bool, warnings: list[str] | None = None) -> str: ...   # render.py (Task 9)
async def render_pdf(html: str, output_path: Path, browser: Browser) -> Path: ...

# ── src/bot/ (Task 10) ──────────────────────────────────────────
CallbackAction = Literal["t", "r", "a", "s", "y", "c", "x", "n", "d"]
def encode_callback(action: CallbackAction, ref: str) -> str: ...   # raises ValueError above 64 bytes
def decode_callback(data: str) -> tuple[CallbackAction, str] | None: ...
class TelegramBotClient: ...             # getUpdates, answerCallbackQuery, sendMessage, sendDocument, editMessageReplyMarkup
class BotService: ...                    # authorization, dispatch, idempotent handlers
async def run_bot(settings: Settings, config: AppConfig, stop_event: asyncio.Event) -> None: ...

# ── src/apply/ (Task 12) ────────────────────────────────────────
class ApplyAuthError(Exception): ...
class ApplyResult(BaseModel): submitted: bool; receipt_path: str | None; error: str | None; screenshots: list[str]
class BaseApplier(ABC):
    platform: ClassVar[Platform]
    async def apply(self, job: JobPost, resume: TailoredResumeRecord, *, dry_run: bool = False) -> ApplyResult: ...
```

## Job Status State Machine

`can_transition` allows exactly these transitions:

| From | To | Trigger |
| --- | --- | --- |
| NEW | NOTIFIED | Notification sent (Task 6) |
| IGNORED | NEW | `--reevaluate` (Task 7) |
| NEW | IGNORED | `--reevaluate` after the rules are tightened (Task 7b) |
| NOTIFIED | TAILORED | Review package sent (Task 10) |
| TAILORED | TAILORED | 🔁 Regenerate (Task 10) |
| TAILORED | APPROVED | ✅ Approve (Task 10) |
| TAILORED | SKIPPED | ❌ Reject (Task 10) |
| APPROVED | APPLIED | Submission confirmed (Task 12) |
| APPROVED | APPLY_FAILED | Submission failed (Task 12) |
| APPLY_FAILED | APPROVED | 🔁 Retry (Task 12) |
| APPLY_FAILED | SKIPPED | ❌ Reject (Task 12) |

## Domain Rules

**Phase 1:**
- `normalize_text` is used only for matching. The database and messages keep the original text.
- Jobs that match no profile are persisted as `IGNORED`. Only `--reevaluate` re-checks stored jobs: it promotes `IGNORED` jobs that match now, and (from Task 7b) demotes unsent `NEW` jobs that no longer match.
- When the work mode cannot be determined, it defaults to `ONSITE`. Never infer the work mode from free-text descriptions; use structured fields, badges, or short labels only.
- All datetimes are timezone-aware and stored in UTC.
- Scrapers visit a detail page only after `is_known` returns `False`.
- On detail pages, prefer JSON-LD `JobPosting` data and fall back to DOM selectors. There is one exception for the description: if real samples show the JSON-LD description loses line breaks or merges words (as on JobVision), prefer the HTML description and fall back to JSON-LD.
- Process keywords and pages sequentially, with `human_delay()` between navigations.
- Never solve or bypass CAPTCHAs programmatically.

**Phase 2 and 3:**
- **No fabrication.** Tailoring may only select, reorder, and rephrase content from the master resume. It must never add employers, titles, dates, numbers, degrees, certifications, or skills and tools that are not in the master resume. Code validation enforces this, and the highlighted review package is the final human check.
- **Changes are computed by code** from the structured plan and the master resume, never self-reported by the LLM.
- **Personal data:**
  - `config/master_resume.yaml`, `inputs/`, and `data/` are never committed.
  - Never log resume content, prompts, LLM responses, or cover letters, at any level. Log only IDs, sizes, token counts, and durations.
  - Test fixtures use a fictional person.
- **Private delivery.** Resumes, review packages, and application receipts go only to `Settings.review_chat_id`, never to the notification channel.
- **Authorization.** Ignore callbacks and commands from users not in `TELEGRAM_ALLOWED_USER_IDS`. Answer them with a short refusal and log their numeric ID.
- **Idempotency.** Every bot action changes status through `transition_status` (compare-and-set), so double taps and restarts never repeat an action.
- **Duplicates** are labeled, never hidden: a probable duplicate still gets its own notification, with links to the other postings. Tailoring or applying for a job whose duplicate group already has activity (`TAILORED` or later) requires the user's explicit confirmation. Anonymous company names never count as a match.
- **Applications:**
  - Submit only after the user approves that specific tailored resume.
  - Never submit in bulk, never beyond `apply.daily_limit`, and never through a CAPTCHA.
  - Never retry automatically.
  - Any unexpected state aborts the submission and notifies the user.
- **Browser sessions.** Anonymous scraping uses `{platform}_state.json`. The logged-in apply flow uses `{platform}_auth_state.json`. Never mix them, and scrapers never log in.
- **Secrets in requests.** API keys go in headers, never in URLs. Model names live in `config.yaml`. The `httpx` and `httpcore` loggers stay at WARNING.

## Code Quality Requirements

- Write complete, production-grade code. Do not use TODOs, placeholder `pass`, `...` bodies, or "implement here" comments. The only exception is unknown site-specific values marked `# VERIFY`.
- Add docstrings to every module, class, and public function. Use loguru logging at key lifecycle stages.
- Never use a bare `except:`. At run and platform boundaries, use `except Exception` with `logger.exception`.
- Use parameterized SQL only.

## Handling Unknowns

- Never present guesses about URLs, API routes, fields, selectors, or third-party API details as facts.
- Site-specific values live in constants modules, marked `# Source: <sample or file>` or `# VERIFY: <what and how to confirm>`.
- Verify Gemini API details (endpoint, request fields, error format, model names) against the official documentation at https://ai.google.dev/gemini-api/docs before implementing, and cite the URLs in comments.
- If a requirement is ambiguous, choose the most conservative interpretation and list it under "Assumptions to Verify". If the ambiguity changes the design, ask first.

## Commands & Safety

**Setup and tests:**
- Setup: `uv sync` and `uv run playwright install chromium`.
- After writing code, run `uv run pytest` and fix failures in your task's own files.

**You may run:**
- headless smoke runs that only read public pages
- `main.py --reevaluate --dry-run`
- rendering commands that use already-saved records and make no LLM call

**Ask the user to run, and never run yourself:**
- Anything that sends real Telegram messages: `src.notifier.telegram`, `main.py --once`, and `main.py --reevaluate` without `--dry-run`.
- Anything that calls the LLM API, such as `src.resume.tailor`. It costs money and sends personal data to the provider.
- Anything that uses an authenticated session: `scripts.apply_probe` and `src.apply.*`.
- Anything that needs a visible browser.
- `main.py --daemon` and `main.py --bot`. These run indefinitely.

**Never:**
- Submit a job application, or trigger a final submit in any automated run.

**Secrets and personal data:**
- Never read, print, or edit `config/.env`.
- Read `config/master_resume.yaml` and `inputs/` only when a task requires it, and never quote their contents in reports, logs, commits, or fixtures.
- Never commit anything under `data/` or `inputs/`, or `config/master_resume.yaml`.

## Task Workflow

1. Read the task prompt and the files it depends on.
2. Write the Deliverables and Authorized Changes directly to disk, in the listed order.
3. Run the tests and every acceptance command you are allowed to run. Fix any failures.
4. Finish with a **Self-Review**:
   - **Checklist:** each item marked ✅ or ⚠️, with the file.
   - **Assumptions to Verify**
   - **Changes to Frozen Files:** authorized or approved changes, or "None".
   - **How to Verify:** the commands you ran with their results, and the commands the user must run.