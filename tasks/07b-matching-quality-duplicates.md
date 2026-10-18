# Task 7b — Matching Quality and Duplicate Detection

## Goal

Fix two problems found in the first real run (303 jobs: 46 matched, 257 ignored):

1. **Noisy matches.** Jobs such as a WordPress support role or a C#/.NET backend role matched only because their description mentioned "AI" once.
2. **Duplicates.** The same posting appears on both sites (e.g., «ستاپ ترک» AI Engineer and «گروه حقوق آسیا» LLM/RAG AI Engineer). In Phases 2 and 3 this could lead to tailoring or applying twice for one position.

Use the real database (`data/jobs.db`) to check every rule against real data. Change it only through the commands below.

## Authorized Changes

- `src/models.py`:
  - synonym groups in `SearchProfile.keywords`
  - `SearchProfile.description_min_hits`
  - a `duplicates` config section
  - `JobPost.duplicate_group`
  - the `NEW → IGNORED` transition
- `src/rule_engine.py`: synonym groups and `description_min_hits`.
- `src/database.py`: migration v3 and the new methods.
- `src/notifier/telegram.py`: the optional `duplicates` argument of `send_job`.
- `main.py`: keyword flattening for search, duplicate grouping in the pipeline, and the extended `--reevaluate`.
- `config/config.yaml`: the new settings.

## Deliverables

1. `src/models.py`
2. `src/rule_engine.py`
3. `src/duplicates.py`
4. `src/database.py`
5. `src/notifier/telegram.py`
6. `main.py`
7. `config/config.yaml`
8. `tests/test_rule_engine_v2.py`
9. `tests/test_duplicates.py`
10. `tests/test_database_v3.py`
11. `tests/test_reevaluate_v2.py`

## Specification

### 1. Synonym groups and `description_min_hits`

- **Synonym groups:** `keywords` accepts strings and lists of strings. A nested list is one synonym group (e.g., `["AI", "هوش مصنوعی"]`). A plain string is a group of one. Existing configs stay valid.
- **Search keywords:** the scrapers search with the flattened, deduplicated list of all terms, as before.
- **Matching:**
  - A keyword from any group in the **title** passes the keyword check, as before.
  - Otherwise, the **description** must contain terms from at least `description_min_hits` **distinct groups**. Repeating one term, or using two synonyms from the same group, counts once.
- **`description_min_hits`:** defaults to 1, which keeps the current behavior. It must be at least 1 and no more than the number of groups.
- **Shipped config:** change the profile's keywords to:

```yaml
  keywords:
    - ["AI", "هوش مصنوعی"]
    - "n8n"
    - "Agentic"
    - "LLM"
    - "RAG"
  description_min_hits: 2
```

  Keep the existing exclusions.

### 2. Duplicate detection (`src/duplicates.py`)

All functions are pure.

**`company_keys`:**
1. Split the name on `|` and `｜`, and normalize each part with `normalize_text`.
2. Remove generic words: «شرکت»، «گروه»، «هلدینگ»، «موسسه»، «سازمان»، «co»، «company»، «group»، «inc»، «ltd».
3. Remove spaces.
4. Return the set of non-empty keys. For example, «ستاپ ترک | SetupTrack» → `{"ستاپترک", "setuptrack"}`.

**Anonymous names** (e.g., JobVision's «سازمانی فعال در حوزه …») return an empty set and never match. Find the anonymous patterns in the real database and mark them `# Source: data/jobs.db`.

**`title_tokens`:** normalize the title and split it on anything that is not a letter or a digit. Then drop these noise tokens:
- city aliases from config
- «دورکاری»، «دور»، «کاری»، «remote»
- «آقا»، «خانم»
- «فوری»، «استخدام»، «تمام»، «وقت»، «پاره»
- tokens of one character

**`is_probable_duplicate(a, b, config)`:** returns true only when all three conditions hold:
1. The company key sets intersect.
2. Both title token sets have at least 2 tokens, and the overlap coefficient `|A ∩ B| / min(|A|, |B|)` is at least `duplicates.title_overlap_threshold` (default 0.8). The overlap coefficient handles bilingual titles, where one title contains a translation of the other.
3. The two jobs' `created_at` values are within `duplicates.window_days` (default 45) of each other.

This covers both cross-platform duplicates and reposts on the same platform.

**`assign_duplicate_groups`:**
- Uses union-find over the candidates plus the existing jobs.
- The group ID is `"{platform}:{external_id}"` of the earliest member, so it is stable.
- Returns only the jobs whose group changed.

**`find_prior_activity(job, members)`:** returns the member other than `job` with the most advanced status among `TAILORED`, `APPROVED`, `APPLY_FAILED`, and `APPLIED`, or `None`.

**Tests** use real postings from the first run (public data).

These pairs must match:
- «AI Engineer (LLM)» (JobVision, «ستاپ ترک») ↔ «مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)» (Jobinja, «ستاپ ترک | SetupTrack»)
- «AI Engineer (LLM & RAG)» (JobVision, «گروه حقوق آسیا») ↔ «LLM/RAG) AI Engineer-مشهد)» (Jobinja, «گروه حقوق آسیا | Asia Law Group»)

These must NOT match:
- «Data Engineer» ↔ «AI Engineer (LLM & RAG)» at «گروه حقوق آسیا»
- «AI Engineer» ↔ «مهندس ارشد نرم‌افزار» at «شرکت آریا اندیش سورنا»
- two different jobs at an anonymous company
- a one-token title
- otherwise matching jobs outside the time window

### 3. Database (migration v3)

- Add `ALTER TABLE jobs ADD COLUMN duplicate_group TEXT`, with an index on it.
- `save_job` and every job read include the new column.
- New methods:
  - `demote_new`: atomically sets `NEW → IGNORED` and clears `matched_profiles`
  - `get_jobs_since`
  - `set_duplicate_group`
  - `get_duplicate_group_members`

### 4. Pipeline (`main.py`)

1. **Grouping:** after rule evaluation and before saving, call `assign_duplicate_groups` for this run's jobs against `get_jobs_since(now − window_days)`. Save the new jobs with their group, and call `set_duplicate_group` for existing jobs that joined a group.
2. **Notifications:** pass the other group members (any status) to `send_job(job, duplicates=...)`.
3. **Notifier:** when `duplicates` is not empty, add the line «🔁 احتمالاً تکراری», followed by links to the other postings. Label each link with the platform name, and escape each URL for the HTML attribute.

### 5. Extended `--reevaluate`

Within `--since-days`, re-evaluation does four things:
- `IGNORED` jobs that match now → `NEW` (as before).
- `NEW` jobs that no longer match → `IGNORED`, via `demote_new`.
- `NOTIFIED` and later jobs that no longer match → reported only, never changed.
- Duplicate groups are recomputed for all jobs in the window.

`--dry-run` changes nothing and prints four sections, each listing platform, ID, title, and company:
- «اضافه می‌شوند» (added)
- «حذف می‌شوند» (removed)
- «دیگر تطبیق ندارند (فقط گزارش)» (no longer match, report only)
- «گروه‌های تکراری» (duplicate groups)

Without `--dry-run`, apply the changes, then run the notification stage.

### 6. Tests

Cover:
- synonym groups and distinct-group counting
- title matches bypassing the minimum
- validation errors for `description_min_hits`
- every duplicate case from §2
- migration v3 on a v2 database, with rows preserved
- `demote_new` as a compare-and-set
- the notification text with duplicates, including escaping and links
- the four re-evaluation sections, as a pure function

## Acceptance Criteria

1. `uv run pytest` passes.
2. `uv run python main.py --reevaluate --dry-run` runs on a copy of the real database (as in Task 7), then on the real database. In the output:
   - The noisy matches from the first run (the WordPress, marketing, and C#/.NET roles) appear under «حذف می‌شوند» or «دیگر تطبیق ندارند». If some still match, report which keyword