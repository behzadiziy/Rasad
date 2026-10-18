# Task 1 — Foundation: Setup, Config, Models, Normalization, Rule Engine

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Create the project skeleton and all pure logic: configuration loading and validation, data models, Persian text normalization, and the Rule Engine, all covered by unit tests. This task involves no network, no database, and no browser.

## Inputs

None beyond the contract.

## Deliverables

1. `pyproject.toml`
2. `config/.env.example`
3. `config/config.yaml`
4. `data/.gitkeep`
5. `src/__init__.py`
6. `src/models.py`
7. `src/utils.py`
8. `src/rule_engine.py`
9. `tests/__init__.py`
10. `tests/test_utils.py`
11. `tests/test_rule_engine.py`

## Specification

### 1. `pyproject.toml`

- Declare **all** runtime dependencies of the whole project now, as listed in the contract. This file is frozen after Task 1.
- Put `pytest` in the `dev` dependency group.
- Add `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `pythonpath = ["."]`.

### 2. Environment settings (`Settings`)

Use `pydantic-settings`, loading from `config/.env` via `SettingsConfigDict(env_file="config/.env", env_file_encoding="utf-8")`. Document every variable with a comment in `config/.env.example`.

| Variable | Type | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `str` | required | |
| `TELEGRAM_CHAT_ID` | `str` | required | `@channelname` or a numeric ID (e.g., `-100…`). The bot must be an admin of the channel. |
| `TELEGRAM_PROXY_URL` | `Optional[str]` | `None` | e.g., `socks5://127.0.0.1:1080`. Required on an Iranian server/IP where Telegram is filtered. |
| `DB_PATH` | `str` | `data/jobs.db` | |
| `HEADLESS` | `bool` | `True` | Overridden by the `--headful` CLI flag. |

### 3. YAML configuration (`AppConfig`)

Ship exactly this file as `config/config.yaml`:

```yaml
scheduler:
  interval_minutes: 30
  active_hours: "08:00-23:00"
  timezone: "Asia/Tehran"

scraping:
  enabled_platforms: ["jobvision", "jobinja"]
  max_pages_per_keyword: 2

city_aliases:
  mashhad: ["mashhad", "مشهد"]
  tehran: ["tehran", "تهران"]

search_profiles:
  - profile_name: "AI & Automation"
    keywords: ["AI", "هوش مصنوعی", "n8n", "Agentic", "LLM", "RAG"]
    exclude_keywords: ["کارآموز", "Intern"]
    location_rules:
      - city: "mashhad"
        remote_only: false   # Mashhad: accept on-site, hybrid, and remote
      - city: "ALL"
        remote_only: true    # Any other or unknown city: accept REMOTE only
```

Validation rules, enforced with Pydantic validators:
- `active_hours` must match `HH:MM-HH:MM`. Windows that cross midnight (e.g., `22:00-02:00`) are valid.
- `timezone` must be a valid IANA zone (check with `zoneinfo`).
- Each `location_rules[].city` must be either `"ALL"` or a key of `city_aliases`. Raise a clear validation error otherwise.
- At most one `"ALL"` rule per profile.
- `interval_minutes >= 1` and `max_pages_per_keyword >= 1`.
- `enabled_platforms` must be non-empty and contain only `jobvision` or `jobinja`.

`load_app_config()` reads the file with `yaml.safe_load` and validates it. On failure, it raises an error message that includes the file path.

### 4. `normalize_text` and `human_delay` (`src/utils.py`)

Implement `normalize_text` with a precompiled `str.translate` table plus regex, applying these steps in order:

1. Remove zero-width and bidi control characters, except ZWNJ: U+200B, U+200D, U+200E, U+200F, U+202A–U+202E, U+FEFF.
2. Replace ZWNJ (U+200C) with a regular space.
3. Convert Arabic Yeh (U+064A) and Alef Maksura (U+0649) to Persian Yeh (U+06CC). Convert Arabic Kaf (U+0643) to Keheh (U+06A9).
4. Remove Arabic diacritics (U+064B–U+065F, U+0670) and tatweel (U+0640).
5. Convert Persian digits (U+06F0–U+06F9) and Arabic-Indic digits (U+0660–U+0669) to ASCII `0–9`.
6. Apply `casefold()`.
7. Collapse all whitespace runs into a single space and strip.

`human_delay` sleeps for `random.uniform(min_s, max_s)` seconds.

### 5. Rule Engine (`src/rule_engine.py`)

`RuleEngine` precompiles all regex patterns once, at construction.
- `match_profiles(job)` returns the names of **all** matching profiles, in config order.
- A profile matches only if all three checks below pass.
- Normalize the job text and the keywords with the same `normalize_text` function.

#### 5.1 Keyword matching (word boundary, not substring)

- Build each pattern as `(?<!\w)` + the escaped normalized keyword + `(?!\w)`, with internal spaces in a keyword replaced by `\s+`. Python's Unicode-aware `\w` makes this boundary work for both Latin and Persian text.
- Latin keywords may also match a trailing plural `s` (e.g., `LLM` matches `LLMs`).
- Required behavior: `AI` matches `Senior AI Engineer` and `(AI)`, but NOT `Maintenance` or `Email`. `RAG` does NOT match `Storage`.
- A job passes this check if at least one keyword appears in the normalized `title` or `raw_description`.

#### 5.2 Exclusions

Reject the job if any `exclude_keywords` entry appears in the normalized `title`, using the same boundary logic.

#### 5.3 Location rules

1. **Resolve the job's canonical cities.** A `city_aliases` key applies if any of its normalized aliases appears in the normalized `job.city` as a whole word. This means `مشهد، خراسان رضوی` resolves to `mashhad`. A job may resolve to zero, one, or several cities.
2. **City-specific rules.** These are all rules except `"ALL"`. Collect the city-specific rules whose city is among the job's resolved cities. If any exist, accept the job if at least one of them accepts it:
   - `remote_only: false` accepts any work mode.
   - `remote_only: true` accepts only `REMOTE`.
3. **Fallback to `"ALL"`.** If no city-specific rule applies (including an empty or unknown city), apply the `"ALL"` rule if one exists. If there is no `"ALL"` rule, reject.
4. **No rules.** An empty `location_rules` list means no location restriction.

Expected outcomes with the default config:

| City | Work mode | Result |
| --- | --- | --- |
| مشهد | ONSITE | ✅ accept |
| Mashhad | HYBRID | ✅ accept |
| تهران | ONSITE | ❌ reject |
| تهران | HYBRID | ❌ reject |
| تهران | REMOTE | ✅ accept |
| (empty) | REMOTE | ✅ accept |
| (empty) | ONSITE | ❌ reject |
| تهران، مشهد | ONSITE | ✅ accept |

### 6. Tests

Build configs and jobs in memory; tests must not read `config/config.yaml`.

- **`tests/test_utils.py`:** one or more cases for each of the 7 normalization steps.
- **`tests/test_rule_engine.py`:**
  - every keyword-boundary case from §5.1, plus `LLM` → `LLMs`
  - Persian keyword matching across ZWNJ and Arabic-Yeh variants (e.g., `هوش‌مصنوعي`)
  - an exclusion keyword rejecting a job
  - every row of the location table, as a parametrized test
  - a job that matches two profiles returning both names, in config order

## Acceptance Criteria

1. `uv sync` completes without errors.
2. `uv run pytest` passes with no failures.
3. `uv run python -c "from src.models import load_app_config; print(load_app_config())"` prints the validated default config.
4. Adding `city: "isfahan"` (not in `city_aliases`) to a location rule makes `load_app_config()` fail with a clear validation error.

## Self-Review Checklist

- All runtime and dev dependencies declared; APScheduler pinned `<4`; `httpx[socks]` used
- `Settings` reads `config/.env`, and `.env.example` documents every variable
- Every config validation rule from §3 enforced
- The 7 normalization steps from §4 implemented, in order
- Word-boundary keyword matching, with keywords and text normalized by the same function
- Location rule evaluation order, multi-city handling, and unknown-city handling
- Every keyword-boundary case and every row of the location table covered by tests
