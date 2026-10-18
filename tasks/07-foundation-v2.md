# Task 7 — Foundation v2: Settings, Config, Statuses, DB Migrations, Re-evaluation

## Goal

Prepare the frozen foundation for Product Phases 2 and 3 in one controlled step:
- new settings and config sections
- the extended job state machine
- versioned database migrations with the new tables
- a `--reevaluate` command that re-checks `IGNORED` jobs against the current rules

## Authorized Changes

- `pyproject.toml`: add `jinja2`.
- `.gitignore`: add `config/master_resume.yaml` and `inputs/`.
- `config/.env.example`, `config/config.yaml`: add the new variables and sections.
- `src/models.py`, `src/database.py`: additions only. Existing names keep their exact behavior.
- `main.py`: add the `--reevaluate` mode only.

## Deliverables

1. `pyproject.toml`
2. `.gitignore`
3. `config/.env.example`
4. `config/config.yaml`
5. `src/models.py`
6. `src/database.py`
7. `main.py`
8. `tests/test_models_v2.py`
9. `tests/test_database_v2.py`
10. `tests/test_reevaluate.py`

## Specification

### 1. New settings

| Variable | Type | Default | Notes |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | `Optional[str]` | `None` | Required only for Phase 2 features. |
| `LLM_PROXY_URL` | `Optional[str]` | `None` | Proxy for LLM API calls. A blank value becomes `None`. |
| `TELEGRAM_ALLOWED_USER_IDS` | `str` | `""` | Comma-separated numeric Telegram user IDs. |
| `TELEGRAM_REVIEW_CHAT_ID` | `Optional[str]` | `None` | Private chat for resumes. Defaults at runtime to the first allowed user ID. |

- Declare `TELEGRAM_ALLOWED_USER_IDS` as a plain `str`. Validate that every item is a positive integer, and expose it through the `allowed_user_ids` property. Do NOT declare it as `list[int]`: pydantic-settings JSON-decodes list fields from the environment and rejects comma-separated values.
- Implement the `review_chat_id` property as described in the contract.

### 2. New config sections

Add these sections to `config/config.yaml`:

```yaml
llm:
  provider: "gemini"
  model: "<current Gemini Flash model>"
  temperature: 0.3
  max_output_tokens: 8192
  timeout_seconds: 120

tailoring:
  master_resume_path: "config/master_resume.yaml"
  output_dir: "data/resumes"
  cover_letter: true
  max_versions_per_job: 3
  persian_ratio_threshold: 0.3

bot:
  poll_timeout_seconds: 30
  state_path: "data/bot_state.json"

apply:
  enabled: false
  daily_limit: 10
  confirm_before_submit: false
  confirm_timeout_minutes: 30
  screenshots_dir: "data/applications"
```

- **`llm.model`:** check https://ai.google.dev/gemini-api/docs/models and choose a current, generally available Flash-class model. Write the docs URL and the date checked in a YAML comment.
- **Optional `llm`:** in `AppConfig`, `llm` is optional (`None` disables Phase 2 features, with a clear message). The other sections default to the values above.
- **Validation:**
  - `provider: Literal["gemini"]`, with a comment showing where OpenAI and OpenRouter will be added
  - `0 ≤ temperature ≤ 2`
  - positive integers and durations
  - `0 < persian_ratio_threshold < 1`

### 3. Statuses and records

- Extend `JobStatus` as the contract defines.
- Implement `can_transition` exactly from the state machine table in `CLAUDE.md`.
- Add `TailoredResumeRecord` and `ApplicationRecord`.

### 4. Database migrations

- **Versioning:** track the schema version with `PRAGMA user_version`. Keep an ordered list of migrations. `init_db` applies the pending ones in order, each in its own transaction, and sets `user_version` after each one succeeds.
- **v1:** the existing schema, idempotent. On an existing database at version 0 it changes nothing.
- **v2:** two new tables:
  - `tailored_resumes`: the `TailoredResumeRecord` fields, with `UNIQUE(platform, external_id, version)` and an index on `(platform, external_id)`.
  - `applications`: the `ApplicationRecord` fields, with `submitted` stored as an INTEGER 0/1, and indexes on `(platform, external_id)` and `attempted_at`.
- **Backup:** before migrating a database that already has a `jobs` table, back it up to `{db_path}.bak-v{current_version}` using the SQLite backup API.
- **Unknown version:** if `user_version` is higher than any version the code knows, refuse to start with a clear error.

### 5. New database methods

Implement the new methods exactly as the contract lists them.
- `transition_status` raises `ValueError` if any `(from, to)` pair is not allowed by `can_transition`. It returns `False` when the job's current status is not in `from_statuses`, so it behaves as a compare-and-set.
- `requeue_ignored` updates `matched_profiles`, `status=NEW`, and `updated_at` only when the job's current status is `IGNORED`.

### 6. `--reevaluate`

- **CLI:** the required group becomes `--once | --daemon | --reevaluate`. Two new options apply only to `--reevaluate`: `--since-days N` (default 30) and `--dry-run`.
- **Pure function:** `plan_reevaluation(jobs, engine) -> list[tuple[JobPost, list[str]]]` returns the `IGNORED` jobs that match at least one profile now.
- **`--dry-run`:** prints a table (platform, ID, title, matched profiles) and changes nothing.
- **Without `--dry-run`:** calls `requeue_ignored` for each match, then runs the same notification stage as `--once`. It never launches a browser.
- **Summary:** log the counts of jobs checked, requeued, notified, and failed.

### 7. Tests

- **Settings:** parsing of allowed IDs (valid values, spaces, empty, invalid), and the `review_chat_id` fallback.
- **Config:** section defaults and validation errors.
- **State machine:** every allowed and every disallowed transition.
- **Migrations:**
  - a v0 database created with the old schema migrates with its rows preserved
  - the backup file is created
  - running the migration twice is a no-op
  - a database with a future version is refused
- **Database methods:** every new method, including the compare-and-set race (two transitions, only one succeeds).
- **Re-evaluation:** `plan_reevaluation` with an in-memory config.

## Acceptance Criteria

1. `uv run pytest` passes.
2. On a copy of the real database:

```bash
   cp data/jobs.db /tmp/jobs-copy.db
   DB_PATH=/tmp/jobs-copy.db uv run python main.py --reevaluate --dry-run
```

   The migration runs, the backup is created, `user_version` is 2, the row counts are unchanged, and candidates are printed with nothing sent.
3. The same dry run on the real database succeeds.

## Self-Review Checklist

- Existing behavior is unchanged; the Phase 1 tests still pass
- The allowed-IDs field is declared as a string, with a parsed property
- Migrations are versioned and transactional, with a backup taken first and future versions refused
- `transition_status` is an atomic compare-and-set
- `--reevaluate` never launches a browser, and `--dry-run` changes nothing