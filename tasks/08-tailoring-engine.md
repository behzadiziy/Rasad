# Task 8 — Master Resume, LLM Layer (Gemini), Tailoring Engine

## Goal

Build the core of Product Phase 2:
- a validated bilingual master resume
- a provider-agnostic LLM layer with a Gemini implementation
- a tailoring engine that selects, reorders, and rephrases the master resume for a job, never invents content, and computes the exact changes

## Inputs

- The user's resume files in `inputs/`. Word (`.docx`) is preferred. Both a Persian and an English version may be present.
- `GEMINI_API_KEY` (and `LLM_PROXY_URL`, if needed) set in `config/.env` by the user.

## Deliverables

1. `src/llm/__init__.py`
2. `src/llm/base.py`
3. `src/llm/gemini.py`
4. `src/llm/factory.py`
5. `src/resume/__init__.py`
6. `src/resume/models.py`
7. `src/resume/language.py`
8. `src/resume/changes.py`
9. `src/resume/tailor.py`
10. `prompts/tailor_system.md`
11. `prompts/tailor_user.md`
12. `config/master_resume.example.yaml`
13. `config/master_resume.yaml` (converted from the user's files; never committed)
14. `tests/fixtures/resume/master_resume.yaml` (a fictional person)
15. `tests/test_llm_gemini.py`
16. `tests/test_resume_language.py`
17. `tests/test_tailor_validation.py`
18. `tests/test_resume_changes.py`

## Specification

### 1. Gemini client (`src/llm/gemini.py`)

**Before writing the client,** verify these details against the official docs and cite the URLs in comments:
- the `generateContent` endpoint
- the request field for a JSON Schema response (`responseJsonSchema` or `responseSchema`)
- how thought parts are marked
- the 429 error format, including the retry delay
- the usage metadata fields

**Client setup:**
- `httpx.AsyncClient(proxy=settings.LLM_PROXY_URL, trust_env=False, timeout=config.llm.timeout_seconds)`.
- Send the API key only in the `x-goog-api-key` header.
- Set the `httpx` and `httpcore` loggers to WARNING.

**Request:**
- `systemInstruction` and one user message.
- `generationConfig` with `temperature`, `maxOutputTokens`, `responseMimeType: "application/json"`, and the JSON Schema from `schema.model_json_schema()`.
- If the API rejects some JSON Schema keywords, adapt the schema in a tested helper.

**Response:**
1. Join the text of the first candidate's non-thought parts.
2. If `finishReason` is anything other than STOP, raise `LLMError` naming the reason.
3. Parse the text with `schema.model_validate_json`. Invalid JSON raises `LLMError`.

**Retries** (tenacity, at most 4 attempts):
- Network errors, 5xx, and 429 are retried. For 429, use the retry delay from the error details when present; otherwise use exponential backoff with jitter.
- Other 4xx errors immediately raise `LLMError` with the API's error message, never the request.

**Logging:** INFO lines contain only the model, latency, and token counts.

### 2. Factory (`src/llm/factory.py`)

`create_llm_client` returns a `GeminiClient` for `provider: gemini`. If the `llm` section or `GEMINI_API_KEY` is missing, it raises a clear configuration error.

### 3. Master resume schema (`src/resume/models.py`)

- **Localized text:** `LocalizedText {fa: str | None, en: str | None}`, with at least one value set.
- **Sections:**
  - **contact:** name, email, phone, location, links
  - **summary**
  - **experiences:** `id`, company, title, location, `start`/`end` as Gregorian `YYYY-MM` (`end=None` means current), optional `start_jalali`/`end_jalali` as `YYYY-MM`, and bullets (`id`, text)
  - **projects:** `id`, name, description, bullets, links
  - **skills:** `id`, name exactly as written (e.g., `PostgreSQL`), category
  - **education:** `id`, institution, degree, field, dates
  - **certifications:** `id`, name, issuer, date
  - **languages:** `id`, name, level
- **IDs:** kebab-case, and unique across the whole document (enforced by a validator).
- **Helper:** `text_for(lang)` returns the text in the requested language, falling back to the other one.

### 4. One-time resume conversion

You do this conversion yourself in this session; it is not code.

1. Extract text from the files in `inputs/` with a throwaway script:
   - `.docx`: `uv run --with python-docx`
   - PDF: `uv run --with pypdf`. Warn the user if the extracted Persian text looks garbled.
2. Write `config/master_resume.yaml`:
   - Preserve the user's facts exactly, and generate stable IDs.
   - Fill in both languages. Where a section exists in only one language, translate it and add a `# translated — review` comment.
   - When the source shows Jalali dates, fill in both the Gregorian and the Jalali fields.
3. Validate the file with `load_master_resume`.
4. **Stop and ask the user to review the file before continuing.** Never quote its contents beyond what is needed to ask questions.
5. Write `config/master_resume.example.yaml` with a fictional person, using the same structure.

### 5. Language detection (`src/resume/language.py`)

Compare the share of Persian letters among all letters in the job title and description against `persian_ratio_threshold`. If there are no letters at all, return `"fa"`.

### 6. `TailoringPlan` (the LLM output schema)

The plan contains these fields:
- `language`
- `summary`
- `experiences`: `[{experience_id, bullets: [{bullet_id, text}]}]`, in display order, with at most 5 bullets per experience
- `projects`: in the same form
- `skill_ids`: in display order
- `cover_letter`: `str | None`
- `relevance_notes`: shown to the user only, never sent to the employer

Every field has a description that guides the model.

### 7. Prompts

Both prompts live in `prompts/`, are loaded at runtime, and are rendered with Jinja2.

**System prompt:**
- Role: an expert resume writer for ATS screening and for Iranian and international employers.
- Hard rules:
  - Only select, reorder, and rephrase.
  - Rephrase to mirror the job's wording only when the underlying fact is identical.
  - Never add facts, numbers, tools, employers, titles, dates, or degrees.
- Write everything in the target language.
- Bullets have at most 25 words.
- The cover letter is 120–180 words, addressed to the company, and makes no claim that is not in the master resume.
- Return only the schema.

**User prompt:**
- the target language
- the job's title, company, city, and description (with the description capped at 6,000 characters)
- the master resume as JSON, containing only the IDs and the target-language text

### 8. Validation and the tailoring flow (`src/resume/tailor.py`)

`validate_plan` sorts problems into errors and warnings.

**Errors:**
- unknown IDs
- a bullet placed under the wrong experience
- duplicate IDs
- skill IDs that are not in the master resume
- numbers in the output text that do not appear in the master resume (compared after `normalize_text`)
- an empty summary
- output whose detected language is not the target language

**Warnings:** technical terms in the output that appear nowhere in the master resume. These are:
- all Latin tokens, when the output is Persian
- in English output: acronyms (two or more capitals), mixed-case words, tokens containing digits or `+#./`, and capitalized words that do not start a sentence

The company name and job title from the posting are allowed in the cover letter.

**`tailor_resume` flow:**
1. Call the LLM and validate the result.
2. If there are any errors or warnings, retry once with the list of problems appended to the user prompt.
3. If errors remain, raise `TailoringError`.
4. Remaining warnings go into `TailoringResult.warnings`.

### 9. ChangeSet (`src/resume/changes.py`)

`compute_changes` is pure code and records:
- the summary before and after
- rephrased bullets: the master text in the target language, and the new text
- omitted experiences, bullets, projects, and skills
- sections whose order changed
- whether a cover letter is present

### 10. CLI

`uv run python -m src.resume.tailor --job <platform>:<external_id> [--save]`

- Prints the change summary, warnings, and token usage to the terminal.
- `--save` stores a `TailoredResumeRecord` with `version = count + 1` and empty PDF paths, then prints its ID.
- It never changes the job's status.

### 11. Tests

Use only the fictional fixture, plus a fake `LLMClient`. Cover:
- **Gemini client** (via `httpx.MockTransport`):
  - the key is in the header and not in the URL
  - thought parts are skipped
  - each finish reason
  - the 429 retry delay
  - non-JSON responses
- **Validation:** every error and warning rule
- **The tailoring flow:** a retry that fixes the problems, and a retry that still fails
- **`compute_changes`**
- **Language detection**

## Acceptance Criteria

1. `uv run pytest` passes.
2. The user has reviewed `config/master_resume.yaml`.
3. The user runs `--save` (this calls the API) on one Persian job and one English job. Give a command that lists `NOTIFIED` job IDs. For each run, the language is correct, there are no errors, and the change summary makes sense.

## Self-Review Checklist

- The API key is sent only in a header; prompts and responses are never logged
- Gemini details are verified against the docs, with the URLs cited
- Every validation rule is tested; changes are computed by code
- `master_resume.yaml` is gitignored, and fixtures use only fictional data