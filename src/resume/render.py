"""Render a tailored resume to HTML and to PDF: a clean copy for employers and a review copy.

The clean copy is an ATS-friendly, single-column resume in the plan's
language (`lang` and `dir` follow `plan.language`). The review copy adds a
legend, `<mark>` around every text the `ChangeSet` lists as rephrased, a
warnings box, a list of removed items, and a "not for sending" footer.

Facts (titles, employers, dates, education, certifications, languages) come
from the master resume; selection, order, and wording come from the plan.
Persian output shows Jalali months in Persian digits (from the `*_jalali`
fields, or converted from the 15th of the Gregorian month); English output
shows months like "Mar 2023".

PDFs are printed by Chromium with the Vazirmatn fonts embedded as data URIs,
in an offline browser context.

CLI (no LLM call; launches its own headless Chromium):

    uv run python -m src.resume.render --resume-id N
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jinja2
from loguru import logger
from markupsafe import Markup, escape
from playwright.async_api import Browser, async_playwright
from pydantic import ValidationError

from src.database import Database
from src.models import Language, Settings, load_app_config
from src.resume.changes import ChangeSet
from src.resume.models import Link, MasterResume, TailoringPlan, load_master_resume

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _ROOT / "templates"
_FONTS_DIR = _ROOT / "assets" / "fonts"
_TEMPLATE = "resume.html.j2"
_STYLESHEET = "resume.css"
_FONTS: tuple[tuple[str, int], ...] = (("Vazirmatn-Regular.woff2", 400), ("Vazirmatn-Bold.woff2", 700))
_CONFIG_PATH = Path("config/config.yaml")
_DEFAULT_OUTPUT_DIR = Path("data/resumes")
_PDF_MARGIN = {"top": "15mm", "right": "15mm", "bottom": "15mm", "left": "15mm"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

# ── Jalali calendar ────────────────────────────────────────────────────────
#
# Kazimierz M. Borkowski's algorithm ("The Persian calendar for 3000 years",
# Earth, Moon, and Planets 74, 1996), as in the jalaali-js reference
# implementation: the Jalali years at which the leap-year pattern breaks.
_JALALI_BREAKS = (
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178,
)  # fmt: skip

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)  # fmt: skip
_ENGLISH_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ── Bidirectional text ─────────────────────────────────────────────────────
#
# A left-to-right run inside Persian text: Latin words, ASCII numbers, emails,
# URLs, and phone numbers, joined by spaces. Trailing punctuation stays outside
# the run, so a sentence's final period keeps its right-to-left position.
_LTR_RUN_RE = re.compile(
    r"[A-Za-z0-9@+][A-Za-z0-9@+#%&._:/,'()\-]*(?:[ \t]+[A-Za-z0-9@+][A-Za-z0-9@+#%&._:/,'()\-]*)*"
)
_LTR_TRAILING = ".,:;/-_'("
# A right-to-left run inside English text (e.g. an untranslated Persian name).
_RTL_CHARS = "\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff"
_RTL_RUN_RE = re.compile(rf"[{_RTL_CHARS}]+(?:[\s\u200c]+[{_RTL_CHARS}]+)*")

_LABELS: dict[Language, dict[str, str]] = {
    "fa": {
        "title": "رزومه {name}",
        "summary": "خلاصه",
        "experiences": "سوابق کاری",
        "projects": "پروژه‌ها",
        "skills": "مهارت‌ها",
        "education": "تحصیلات",
        "certifications": "گواهی‌ها",
        "languages": "زبان‌ها",
        "present": "تاکنون",
        "legend_title": "راهنمای نسخه بررسی",
        "legend_before": "متن‌های ",
        "legend_marked": "هایلایت‌شده",
        "legend_after": " نسبت به رزومه اصلی بازنویسی شده‌اند. موارد حذف‌شده در انتهای سند آمده‌اند.",
        "counts": "{rephrased} مورد بازنویسی‌شده · {omitted} مورد حذف‌شده",
        "reordered": "ترتیب تغییرکرده:",
        "reordered_bullets": "ترتیب بندها در {count} مورد",
        "list_separator": "، ",
        "warnings": "هشدارها",
        "removed": "حذف‌شده در این نسخه",
        "removed_bullets": "بندها",
        "footer": "نسخه بررسی — برای ارسال نیست",
    },
    "en": {
        "title": "{name} — Resume",
        "summary": "Summary",
        "experiences": "Experience",
        "projects": "Projects",
        "skills": "Skills",
        "education": "Education",
        "certifications": "Certifications",
        "languages": "Languages",
        "present": "Present",
        "legend_title": "Review legend",
        "legend_before": "",
        "legend_marked": "Highlighted text",
        "legend_after": " was rephrased from the master resume. Omitted items are listed at the end.",
        "counts": "{rephrased} rephrased · {omitted} omitted",
        "reordered": "Order changed:",
        "reordered_bullets": "bullet order in {count} item(s)",
        "list_separator": ", ",
        "warnings": "Warnings",
        "removed": "Removed in this version",
        "removed_bullets": "Bullets",
        "footer": "Review copy — not for sending",
    },
}


def _div(a: int, b: int) -> int:
    """Integer division truncated toward zero, as in the reference algorithm."""
    quotient = abs(a) // abs(b)
    return quotient if (a >= 0) == (b > 0) else -quotient


def _mod(a: int, b: int) -> int:
    """The remainder that matches `_div` (it has the sign of `a`)."""
    return a - _div(a, b) * b


def _jalali_year_info(jy: int) -> tuple[int, int, int]:
    """Return `(leap, gregorian_year, march_day)` for a Jalali year.

    Farvardin 1 of `jy` is March `march_day` of `gregorian_year`. `leap` is the
    number of years since the last leap year (0 means `jy` itself is a leap year).

    Raises:
        ValueError: If the year is outside the algorithm's range.
    """
    if not _JALALI_BREAKS[0] <= jy < _JALALI_BREAKS[-1]:
        raise ValueError(f"Jalali year {jy} is out of the supported range")
    gy = jy + 621
    leap_j = -14
    jp = _JALALI_BREAKS[0]
    jump = 0
    for break_year in _JALALI_BREAKS[1:]:
        jump = break_year - jp
        if jy < break_year:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = break_year
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march_day = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march_day


def gregorian_to_jalali(value: date) -> tuple[int, int, int]:
    """Convert a Gregorian date to a Jalali `(year, month, day)`; e.g. 2023-03-21 → (1402, 1, 1)."""
    jy = value.year - 621
    leap, gy, march_day = _jalali_year_info(jy)
    days = (value - date(gy, 3, march_day)).days
    if days >= 0:
        if days <= 185:
            return jy, 1 + days // 31, days % 31 + 1
        days -= 186
    else:
        jy -= 1
        days += 179
        if leap == 1:
            days += 1
    return jy, 7 + days // 30, days % 30 + 1


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """Convert a Jalali date to a Gregorian date; e.g. (1403, 1, 1) → 2024-03-20.

    Raises:
        ValueError: If the month or day is out of range.
    """
    if not 1 <= jm <= 12 or not 1 <= jd <= (31 if jm <= 6 else 30):
        raise ValueError(f"Invalid Jalali date {jy}-{jm:02d}-{jd:02d}")
    _, gy, march_day = _jalali_year_info(jy)
    return date(gy, 3, march_day) + timedelta(days=(jm - 1) * 31 - (jm // 7) * (jm - 7) + jd - 1)


def _persian_digits(text: str) -> str:
    """Replace ASCII digits with Persian digits."""
    return text.translate(_PERSIAN_DIGITS)


def _split_year_month(value: str) -> tuple[int, int]:
    """Split a validated `YYYY-MM` string."""
    year, month = value.split("-")
    return int(year), int(month)


def _format_month(gregorian: str | None, jalali: str | None, language: Language) -> str | None:
    """Format one month: Jalali in Persian digits for "fa", "Mar 2023" for "en"; None if unknown.

    A missing calendar is converted from the 15th of the month in the other one.
    """
    if language == "fa":
        if jalali is not None:
            jy, jm = _split_year_month(jalali)
        elif gregorian is not None:
            jy, jm, _ = gregorian_to_jalali(date(*_split_year_month(gregorian), 15))
        else:
            return None
        return f"{_JALALI_MONTHS[jm - 1]} {_persian_digits(str(jy))}"
    if gregorian is not None:
        gy, gm = _split_year_month(gregorian)
    elif jalali is not None:
        converted = jalali_to_gregorian(*_split_year_month(jalali), 15)
        gy, gm = converted.year, converted.month
    else:
        return None
    return f"{_ENGLISH_MONTHS[gm - 1]} {gy}"


def _format_range(
    start: tuple[str | None, str | None],
    end: tuple[str | None, str | None],
    language: Language,
    *,
    open_ended: bool,
) -> str | None:
    """Format a `start – end` range; a missing end reads "Present" when `open_ended` and a start exists."""
    labels = _LABELS[language]
    start_text = _format_month(*start, language)
    end_text = _format_month(*end, language)
    if end_text is None and open_ended and start_text is not None:
        end_text = labels["present"]
    if start_text and end_text:
        return f"{start_text} – {end_text}"
    return start_text or end_text


# ── HTML ───────────────────────────────────────────────────────────────────


def _bidi(text: str | None, language: Language) -> Markup:
    """Escape a text and isolate its opposite-direction runs in `<bdi>` elements.

    In Persian text, Latin terms, ASCII numbers, emails, URLs, and phone
    numbers become `<bdi dir="ltr">`; in English text, Persian runs become
    `<bdi dir="rtl">`.
    """
    if not text:
        return Markup("")
    pattern, direction = (_LTR_RUN_RE, "ltr") if language == "fa" else (_RTL_RUN_RE, "rtl")
    parts: list[str] = []
    position = 0
    for match in pattern.finditer(text):
        run = match.group()
        if direction == "ltr":
            run = run.rstrip(_LTR_TRAILING)
            if run.count("(") != run.count(")"):
                run = run.rstrip(")")
        if not run:
            continue
        start = match.start()
        parts.append(str(escape(text[position:start])))
        parts.append(f'<bdi dir="{direction}">{escape(run)}</bdi>')
        position = start + len(run)
    parts.append(str(escape(text[position:])))
    return Markup("".join(parts))


@lru_cache(maxsize=1)
def _environment() -> jinja2.Environment:
    """Return the Jinja2 environment for `templates/`, with autoescaping on."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["bidi"] = _bidi
    return environment


@lru_cache(maxsize=1)
def _stylesheet() -> Markup:
    """Return the resume CSS with the Vazirmatn fonts embedded as base64 data URIs."""
    faces: list[str] = []
    for filename, weight in _FONTS:
        encoded = base64.b64encode((_FONTS_DIR / filename).read_bytes()).decode("ascii")
        faces.append(
            "@font-face {\n"
            '  font-family: "Vazirmatn";\n'
            f'  src: url("data:font/woff2;base64,{encoded}") format("woff2");\n'
            f"  font-weight: {weight};\n"
            "  font-style: normal;\n"
            "  font-display: block;\n"
            "}"
        )
    css = (_TEMPLATES_DIR / _STYLESHEET).read_text(encoding="utf-8")
    return Markup("\n".join([*faces, css]))


def _web_link(url: str, label: str | None = None) -> dict[str, str] | None:
    """Return `{href, text}` for an http(s) URL (a scheme-less URL gets https), or None for other schemes."""
    candidate = url.strip()
    if "://" not in candidate and not candidate.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
        candidate = f"https://{candidate}"
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    display = f"{parts.netloc.removeprefix('www.')}{parts.path}".rstrip("/")
    return {"href": candidate, "text": display or (label or candidate)}


def _contact_items(master: MasterResume, language: Language) -> list[dict[str, str | None]]:
    """Return the contact header items: location, email, phone, and links."""
    contact = master.contact
    items: list[dict[str, str | None]] = []
    if contact.location is not None:
        items.append({"text": contact.location.text_for(language), "href": None})
    if contact.email:
        items.append({"text": contact.email, "href": f"mailto:{contact.email}"})
    if contact.phone:
        digits = re.sub(r"[^\d+]", "", contact.phone)
        items.append({"text": contact.phone, "href": f"tel:{digits}" if digits else None})
    for link in contact.links:
        web = _web_link(link.url, link.label)
        if web is not None:
            items.append(web)
    return items


def _project_links(links: Sequence[Link]) -> list[dict[str, str]]:
    """Return the http(s) links of a project."""
    return [web for link in links if (web := _web_link(link.url, link.label)) is not None]


def _context(
    master: MasterResume,
    plan: TailoringPlan,
    changes: ChangeSet,
    *,
    review: bool,
    warnings: Sequence[str],
) -> dict[str, Any]:
    """Build the template context: master facts, plan selection and wording, and review extras."""
    language = plan.language
    labels = _LABELS[language]
    rephrased = {(change.parent_id, change.bullet_id) for change in changes.rephrased_bullets}
    owners = master.bullet_owners()
    skipped = 0

    def bullets(parent_id: str, plan_bullets: Sequence[Any]) -> list[dict[str, Any]]:
        """Return the plan's bullets that belong to this parent, each once."""
        nonlocal skipped
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for bullet in plan_bullets:
            if owners.get(bullet.bullet_id) != parent_id or bullet.bullet_id in seen:
                skipped += 1
                continue
            seen.add(bullet.bullet_id)
            result.append({"text": bullet.text, "marked": review and (parent_id, bullet.bullet_id) in rephrased})
        return result

    experiences_by_id = master.experiences_by_id()
    experiences: list[dict[str, Any]] = []
    for plan_experience in plan.experiences:
        experience = experiences_by_id.get(plan_experience.experience_id)
        if experience is None or any(item["id"] == experience.id for item in experiences):
            skipped += 1
            continue
        org = experience.company.text_for(language)
        if experience.location is not None:
            org = f"{org}{labels['list_separator']}{experience.location.text_for(language)}"
        experiences.append(
            {
                "id": experience.id,
                "title": experience.title.text_for(language),
                "org": org,
                "dates": _format_range(
                    (experience.start, experience.start_jalali),
                    (experience.end, experience.end_jalali),
                    language,
                    open_ended=True,
                ),
                "bullets": bullets(experience.id, plan_experience.bullets),
            }
        )

    projects_by_id = master.projects_by_id()
    projects: list[dict[str, Any]] = []
    for plan_project in plan.projects:
        project = projects_by_id.get(plan_project.project_id)
        if project is None or any(item["id"] == project.id for item in projects):
            skipped += 1
            continue
        projects.append(
            {
                "id": project.id,
                "name": project.name.text_for(language),
                "description": project.description.text_for(language) if project.description else None,
                "bullets": bullets(project.id, plan_project.bullets),
                "links": _project_links(project.links),
            }
        )

    skills_by_id = master.skills_by_id()
    skill_ids = [skill_id for skill_id in dict.fromkeys(plan.skill_ids) if skill_id in skills_by_id]
    skipped += len(plan.skill_ids) - len(skill_ids)
    if skipped:
        logger.warning("Skipped {} plan item(s) that are unknown, repeated, or misplaced in the master resume", skipped)

    education = []
    for item in master.education:
        degree = item.degree.text_for(language)
        if item.field is not None:
            degree = f"{degree}{labels['list_separator']}{item.field.text_for(language)}"
        education.append(
            {
                "degree": degree,
                "institution": item.institution.text_for(language),
                "dates": _format_range(
                    (item.start, item.start_jalali), (item.end, item.end_jalali), language, open_ended=True
                ),
            }
        )

    certifications = []
    for certification in master.certifications:
        web = _web_link(certification.url) if certification.url else None
        certifications.append(
            {
                "name": certification.name.text_for(language),
                "issuer": certification.issuer.text_for(language) if certification.issuer else None,
                "dates": _format_month(certification.date, certification.date_jalali, language),
                "href": web["href"] if web else None,
                "link_text": web["text"] if web else None,
            }
        )

    languages = [
        f"{item.name.text_for(language)} ({item.level.text_for(language)})"
        if item.level
        else item.name.text_for(language)
        for item in master.languages
    ]

    context: dict[str, Any] = {
        "lang": language,
        "dir": "rtl" if language == "fa" else "ltr",
        "title": labels["title"].format(name=master.contact.name.text_for(language)),
        "css": _stylesheet(),
        "labels": labels,
        "review": review,
        "name": master.contact.name.text_for(language),
        "contact": _contact_items(master, language),
        "summary": {"text": plan.summary, "marked": review and changes.summary.changed},
        "experiences": experiences,
        "projects": projects,
        "skills": [skills_by_id[skill_id].name.text_for(language) for skill_id in skill_ids],
        "education": education,
        "certifications": certifications,
        "languages": languages,
        "warnings": list(warnings),
        "legend_counts": "",
        "reordered": "",
        "removed": [],
    }
    if review:
        context.update(_review_context(master, changes, language))
    return context


def _review_context(master: MasterResume, changes: ChangeSet, language: Language) -> dict[str, Any]:
    """Build the legend counts, the reordered-sections line, and the removed-items groups."""
    labels = _LABELS[language]
    number = _persian_digits if language == "fa" else str
    counts = labels["counts"].format(
        rephrased=number(str(changes.rephrased_count)), omitted=number(str(changes.omitted_count))
    )
    reordered = [labels[section] for section in changes.reordered_sections]
    if changes.reordered_bullets:
        reordered.append(labels["reordered_bullets"].format(count=number(str(len(changes.reordered_bullets)))))

    parents: dict[str, str] = {}
    for experience in master.experiences:
        parents[experience.id] = (
            f"{experience.title.text_for(language)} — {experience.company.text_for(language)}"
        )
    for project in master.projects:
        parents[project.id] = project.name.text_for(language)

    removed = [
        {"heading": labels["experiences"], "entries": [item.label for item in changes.omitted_experiences], "inline": False},
        {"heading": labels["projects"], "entries": [item.label for item in changes.omitted_projects], "inline": False},
        {
            "heading": labels["removed_bullets"],
            "entries": [
                f"{item.label} ({parents.get(item.parent_id or '', item.parent_id or '-')})"
                for item in changes.omitted_bullets
            ],
            "inline": False,
        },
        {"heading": labels["skills"], "entries": [item.label for item in changes.omitted_skills], "inline": True},
    ]
    return {
        "legend_counts": counts,
        "reordered": labels["list_separator"].join(reordered),
        "removed": [group for group in removed if group["entries"]],
    }


def render_resume_html(
    master: MasterResume,
    plan: TailoringPlan,
    changes: ChangeSet,
    *,
    review: bool,
    warnings: list[str] | None = None,
) -> str:
    """Render the resume as a self-contained HTML document.

    Args:
        master: The master resume (the source of facts and dates).
        plan: The tailoring plan (selection, order, wording, and language).
        changes: The change set computed from the plan.
        review: True for the review copy (highlights, legend, warnings,
            removed items, footer); False for the clean copy.
        warnings: Validation warnings to show in the review copy.
    """
    shown_warnings = (warnings or []) if review else []
    context = _context(master, plan, changes, review=review, warnings=shown_warnings)
    return _environment().get_template(_TEMPLATE).render(**context)


# ── PDF ────────────────────────────────────────────────────────────────────


async def render_pdf(html: str, output_path: Path, browser: Browser) -> Path:
    """Print HTML to an A4 PDF with Chromium and return the path.

    The page runs in an offline browser context, so it cannot load anything
    from the network; the fonts are already embedded. The file is written to
    a temporary name and renamed into place.

    Raises:
        RuntimeError: If the Vazirmatn font did not load.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp")
    context = await browser.new_context(offline=True)
    try:
        page = await context.new_page()
        await page.set_content(html, wait_until="load")
        await page.evaluate("() => document.fonts.ready.then(() => true)")
        if not await page.evaluate("() => document.fonts.check('700 12px Vazirmatn')"):
            raise RuntimeError("The Vazirmatn font did not load; check assets/fonts/")
        await page.pdf(path=str(tmp_path), format="A4", print_background=True, margin=_PDF_MARGIN)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        await context.close()
    os.replace(tmp_path, output_path)
    logger.info("Rendered {} ({} bytes)", output_path, output_path.stat().st_size)
    return output_path


def _output_paths(
    output_dir: Path, platform: str, external_id: str, version: int, language: Language
) -> tuple[Path, Path]:
    """Return the clean and review PDF paths: `{platform}_{external_id}_v{version}_{lang}[_review].pdf`."""
    safe_id = _SAFE_NAME_RE.sub("-", external_id).strip("-") or "job"
    stem = f"{platform}_{safe_id}_v{version}_{language}"
    return output_dir / f"{stem}.pdf", output_dir / f"{stem}_review.pdf"


async def render_tailored_resume(
    resume_id: int,
    db: Database,
    master: MasterResume,
    browser: Browser,
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    warnings: list[str] | None = None,
) -> tuple[Path, Path]:
    """Render both PDFs of a saved tailored resume and store their paths in the database.

    Args:
        resume_id: The `tailored_resumes` row ID.
        db: An open database.
        master: The master resume the plan was made from.
        browser: A running Chromium browser.
        output_dir: Where to write the PDFs (`tailoring.output_dir`).
        warnings: Tailoring warnings to show in the review copy (they are not
            stored with the record).

    Returns:
        The clean and review PDF paths.

    Raises:
        LookupError: If no tailored resume has this ID.
    """
    record = await db.get_tailored_resume(resume_id)
    if record is None:
        raise LookupError(f"Tailored resume {resume_id} not found")
    plan = TailoringPlan.model_validate_json(record.plan_json)
    changes = ChangeSet.model_validate_json(record.changes_json)
    clean_path, review_path = _output_paths(
        output_dir, record.platform, record.external_id, record.version, plan.language
    )
    await render_pdf(render_resume_html(master, plan, changes, review=False), clean_path, browser)
    await render_pdf(
        render_resume_html(master, plan, changes, review=True, warnings=warnings), review_path, browser
    )
    await db.update_tailored_resume_paths(resume_id, str(clean_path), str(review_path))
    logger.info("Rendered tailored resume {} (job {}:{})", resume_id, record.platform, record.external_id)
    return clean_path, review_path


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m src.resume.render",
        description="Render the clean and review PDFs of a saved tailored resume (no LLM call).",
    )
    parser.add_argument("--resume-id", required=True, type=int, help="the tailored resume ID printed by --save")
    return parser.parse_args(argv)


async def _run(resume_id: int) -> int:
    """Load the settings, config, master resume, and record; render both PDFs; return the exit code."""
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        logger.error("Invalid settings in config/.env ({})", fields)
        return 1
    try:
        config = load_app_config(_CONFIG_PATH)
        master = load_master_resume(config.tailoring.master_resume_path)
    except (OSError, ValueError) as exc:
        logger.error("{}", exc)
        return 1

    async with Database(settings.DB_PATH) as db:
        await db.init_db()
        if await db.get_tailored_resume(resume_id) is None:
            logger.error("Tailored resume {} not found", resume_id)
            return 1
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                clean, review = await render_tailored_resume(
                    resume_id, db, master, browser, output_dir=config.tailoring.output_dir
                )
            except LookupError as exc:
                logger.error("{}", exc)
                return 1
            finally:
                await browser.close()
    print(f"Clean PDF:  {clean}")
    print(f"Review PDF: {review}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = _parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    try:
        return asyncio.run(_run(args.resume_id))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
