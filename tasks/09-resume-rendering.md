# Task 9 — Resume PDF Rendering (Clean and Review Versions)

## Goal

Turn a saved tailoring result into two PDFs:
- a clean, ATS-friendly resume to send to employers
- a review copy that highlights everything that differs from the master resume

## Inputs

- Saved `TailoredResumeRecord`s from Task 8.

## Deliverables

1. `assets/fonts/` (Vazirmatn Regular and Bold, the OFL license, and a `README.md` with the source URL and version)
2. `templates/resume.css`
3. `templates/resume.html.j2`
4. `src/resume/render.py`
5. `tests/test_resume_render.py`

## Specification

### 1. Fonts

- Download Vazirmatn from its official repository releases (github.com/rastikerdar/vazirmatn). It covers both Persian and Latin.
- At render time, embed the fonts as base64 data URIs in the CSS, so rendering needs no file or network access.

### 2. Template

**Page setup:**
- Jinja2 with autoescaping on.
- `lang` and `dir` follow `plan.language`.
- A single column with real, selectable text. Do not use layout tables or images of text.

**Sections:**
- Standard headings in the target language, in Persian «خلاصه»، «سوابق کاری»، «پروژه‌ها»، «مهارت‌ها»، «تحصیلات»، «گواهی‌ها»، «زبان‌ها», and their English equivalents.
- A contact header.

**Dates:**
- Persian output shows Jalali month and year in Persian digits. Use the `*_jalali` fields when they are present. Otherwise convert from the 15th of the Gregorian month, with an in-house, tested algorithm that needs no new dependency.
- English output uses the form `Mar 2023`.

**Bidirectional text:** wrap emails, URLs, phone numbers, and Latin terms inside Persian text in `<bdi>` or `dir="ltr"` isolates, so mixed text is not scrambled.

### 3. Review mode

Review mode adds:
- `<mark>` around the summary and every rephrased bullet listed in the `ChangeSet`
- a legend at the top
- an end section «حذف‌شده در این نسخه» (or «Removed in this version»), listing the omitted items
- a warnings box, if there are any warnings
- the footer «نسخه بررسی — برای ارسال نیست» (or «Review copy — not for sending»)

Clean mode contains none of these.

### 4. PDF output

- **Rendering:** call `page.set_content(html)`, await `document.fonts.ready`, then `page.pdf(format="A4", print_background=True)` with margins of about 15 mm.
- **Output paths:**
  - `data/resumes/{platform}_{external_id}_v{version}_{lang}.pdf`
  - the same name ending in `_review.pdf` for the review copy
- **Public helper:** `render_tailored_resume(resume_id, db, master, browser) -> tuple[Path, Path]` loads the record, renders both PDFs, and calls `update_tailored_resume_paths`.
- **CLI:** `uv run python -m src.resume.render --resume-id N` makes no LLM call and launches its own headless Chromium.

### 5. Tests

Use the fictional fixture and a fixture plan to check:
- `dir` and `lang` attributes
- `<mark>` appears only in review mode
- `<script>` in the text is escaped
- bidirectional isolation of emails and URLs
- Jalali conversion of known dates (2023-03-21 → 1402-01-01, 2024-03-20 → 1403-01-01)
- Persian digits
- one real PDF: the file starts with `%PDF` and is not empty

## Acceptance Criteria

1. `uv run pytest` passes.
2. `uv run python -m src.resume.render --resume-id <id from Task 8>` produces both PDFs for the Persian record and for the English record.
3. The user opens the PDFs and checks:
   - right-to-left layout, the font, and Jalali dates in the Persian resume
   - that the highlights in the review copy match the change summary from Task 8
   - that the text can be selected in the clean copy

## Self-Review Checklist

- Fonts are embedded, and the license is included
- Clean and review modes are clearly separated
- Bidirectional text is isolated; Jalali conversion is tested
- No personal data appears in fixtures or logs