"""Tests for resume rendering: HTML structure, review vs clean copies, bidi isolation, Jalali dates, and PDFs."""

import asyncio
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

from src.database import Database
from src.models import JobPost, TailoredResumeRecord, WorkMode
from src.resume import render
from src.resume.changes import compute_changes
from src.resume.models import MasterResume, TailoringPlan, load_master_resume
from src.resume.render import (
    gregorian_to_jalali,
    jalali_to_gregorian,
    render_resume_html,
    render_tailored_resume,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "resume" / "master_resume.yaml"


@pytest.fixture(scope="module")
def master() -> MasterResume:
    """The fictional master resume."""
    return load_master_resume(_FIXTURE)


def _plan(language: str = "en", **overrides: Any) -> TailoringPlan:
    """A plan with one rephrased bullet, one omitted bullet, and some omitted skills."""
    if language == "fa":
        data: dict[str, Any] = {
            "language": "fa",
            "summary": "مهندس نرم‌افزار با ۵ سال سابقه در Python و ساخت سرویس‌های API.",
            "experiences": [
                {
                    "experience_id": "alpha-backend",
                    "bullets": [
                        {"bullet_id": "alpha-cache", "text": "کاهش ۴۰٪ زمان پاسخ سرویس با Redis"},
                        {"bullet_id": "alpha-api", "text": "طراحی API پرداخت با FastAPI برای ۱٬۲۰۰ فروشگاه"},
                    ],
                },
                {"experience_id": "beta-developer", "bullets": []},
            ],
            "projects": [{"project_id": "gamma-bot", "bullets": []}],
            "skill_ids": ["python", "fastapi", "redis"],
            "cover_letter": None,
            "relevance_notes": "یادداشت",
        }
    else:
        data = {
            "language": "en",
            "summary": "Python engineer who builds API services.",
            "experiences": [
                {
                    "experience_id": "alpha-backend",
                    "bullets": [
                        {"bullet_id": "alpha-cache", "text": "Cut service response time by 40% with Redis"},
                        {"bullet_id": "alpha-api", "text": "Designed the payment API with FastAPI for 1,200 stores"},
                    ],
                },
                {"experience_id": "beta-developer", "bullets": []},
            ],
            "projects": [{"project_id": "gamma-bot", "bullets": []}],
            "skill_ids": ["python", "fastapi", "redis"],
            "cover_letter": None,
            "relevance_notes": "notes",
        }
    data.update(overrides)
    return TailoringPlan.model_validate(data)


def _html(master: MasterResume, plan: TailoringPlan, *, review: bool, warnings: list[str] | None = None) -> str:
    """Render a plan to HTML."""
    return render_resume_html(master, plan, compute_changes(plan, master), review=review, warnings=warnings)


# ── Document structure ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("language", "direction"), [("fa", "rtl"), ("en", "ltr")])
def test_lang_and_dir_follow_the_plan(master: MasterResume, language: str, direction: str) -> None:
    for review in (False, True):
        html = _html(master, _plan(language), review=review)
        assert f'<html lang="{language}" dir="{direction}">' in html


def test_persian_headings_and_english_headings(master: MasterResume) -> None:
    fa = _html(master, _plan("fa"), review=False)
    for heading in ("خلاصه", "سوابق کاری", "پروژه‌ها", "مهارت‌ها", "تحصیلات", "گواهی‌ها", "زبان‌ها"):
        assert f"<h2>{heading}</h2>" in fa
    en = _html(master, _plan("en"), review=False)
    for heading in ("Summary", "Experience", "Projects", "Skills", "Education", "Certifications", "Languages"):
        assert f"<h2>{heading}</h2>" in en


def test_single_column_real_text_without_tables_images_or_scripts(master: MasterResume) -> None:
    html = _html(master, _plan("en"), review=True)
    for forbidden in ("<table", "<img", "<script", "<link", "<iframe"):
        assert forbidden not in html
    assert '<h1>Negar Azmoon</h1>' in html


def test_fonts_are_embedded_as_data_uris(master: MasterResume) -> None:
    html = _html(master, _plan("en"), review=False)
    assert html.count('src: url("data:font/woff2;base64,') == 2
    assert "font-weight: 700" in html
    assert "url(http" not in html


def test_plan_selection_order_and_wording_with_master_facts(master: MasterResume) -> None:
    html = _html(master, _plan("en"), review=False)
    assert html.index("Cut service response time by 40% with Redis") < html.index("Designed the payment API")
    assert "Set up CI/CD with GitHub Actions" not in html  # omitted bullet
    assert "Delta Scripts" not in html  # omitted project
    assert "Senior Backend Developer" in html and "Alpha Systems, Mashhad" in html
    skills = re.search(r'<section class="skills">.*?</section>', html, re.S)
    assert skills is not None
    assert re.findall(r"<li>(.*?)</li>", skills.group()) == ["Python", "FastAPI", "Redis"]


def test_unknown_repeated_and_misplaced_plan_items_are_skipped(master: MasterResume) -> None:
    plan = _plan(
        "en",
        experiences=[
            {"experience_id": "ghost-corp", "bullets": [{"bullet_id": "ghost", "text": "GHOST-BULLET"}]},
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "beta-cpp", "text": "MISPLACED-BULLET"},
                    {"bullet_id": "alpha-api", "text": "Designed the payment API"},
                    {"bullet_id": "alpha-api", "text": "REPEATED-BULLET"},
                ],
            },
        ],
        skill_ids=["python", "python", "kubernetes"],
    )
    html = _html(master, plan, review=False)
    for skipped in ("GHOST-BULLET", "MISPLACED-BULLET", "REPEATED-BULLET", "kubernetes"):
        assert skipped not in html
    assert html.count("<li>Python</li>") == 1


# ── Review vs clean ────────────────────────────────────────────────────────


def test_mark_appears_only_in_review_mode(master: MasterResume) -> None:
    plan = _plan("en")
    clean = _html(master, plan, review=False)
    review = _html(master, plan, review=True)
    assert "<mark>" not in clean
    assert "<mark>Python engineer who builds API services.</mark>" in review
    assert "<mark>Cut service response time by 40% with Redis</mark>" in review
    assert "<mark>Designed the payment API with FastAPI for 1,200 stores</mark>" not in review  # unchanged
    # The summary, one bullet, and the legend's sample highlight.
    assert review.count("<mark>") == 3


def test_unchanged_summary_is_not_marked(master: MasterResume) -> None:
    plan = _plan("en", summary=master.summary.en)
    review = _html(master, plan, review=True)
    assert f"<mark>{master.summary.en}</mark>" not in review


def test_review_extras_are_absent_from_the_clean_copy(master: MasterResume) -> None:
    plan = _plan("en")
    warnings = ["Term 'Terraform' in the summary does not appear in the master resume"]
    clean = _html(master, plan, review=False, warnings=warnings)
    review = _html(master, plan, review=True, warnings=warnings)
    extras = (
        '<aside class="review-legend"',
        '<aside class="review-warnings"',
        "Removed in this version",
        "Review copy — not for sending",
        "@bottom-center",
    )
    for extra in extras:
        assert extra not in clean
        assert extra in review
    assert "Terraform" not in clean
    assert "Term &#39;Terraform&#39; in the summary" in review


def test_review_lists_removed_items_and_reorders(master: MasterResume) -> None:
    review = _html(master, _plan("en"), review=True)
    removed = review[review.index("Removed in this version") :]
    assert "<li>Developer — Beta Soft</li>" not in removed  # beta is included (without bullets)
    assert "Delta Scripts" in removed
    assert "Set up CI/CD with GitHub Actions (Senior Backend Developer — Alpha Systems)" in removed
    assert "Built an internal dashboard with Django (Developer — Beta Soft)" in removed
    assert '<ul class="inline-list"><li>Django</li><li>C++</li><li>AWS</li></ul>' in removed
    assert "Order changed: bullet order in 1 item(s)" in review
    assert "2 rephrased · 9 omitted" in review


def test_persian_review_texts(master: MasterResume) -> None:
    review = _html(master, _plan("fa"), review=True)
    assert "حذف‌شده در این نسخه" in review
    assert "نسخه بررسی — برای ارسال نیست" in review
    # The Persian plan keeps the master summary: only one bullet is rephrased.
    assert "۱ مورد بازنویسی‌شده · ۹ مورد حذف‌شده" in review
    assert "ترتیب تغییرکرده: ترتیب بندها در ۱ مورد" in review


def test_no_review_without_warnings_box(master: MasterResume) -> None:
    review = _html(master, _plan("en"), review=True, warnings=[])
    assert '<aside class="review-warnings"' not in review


# ── Escaping ───────────────────────────────────────────────────────────────


def test_script_in_text_is_escaped(master: MasterResume) -> None:
    plan = _plan(
        "en",
        summary="<script>alert('summary')</script>",
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": [{"bullet_id": "alpha-api", "text": '<img src=x onerror="alert(1)"> & <b>bold</b>'}],
            }
        ],
    )
    for review in (False, True):
        html = _html(master, plan, review=review, warnings=["<script>alert('warning')</script>"])
        assert "<script" not in html
        assert "&lt;script&gt;alert(&#39;summary&#39;)&lt;/script&gt;" in html
        assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in html
        assert "<b>bold" not in html and "<img" not in html


def test_persian_text_with_markup_is_escaped_inside_isolates(master: MasterResume) -> None:
    plan = _plan("fa", summary="مهندس <script>x</script> با Python")
    html = _html(master, plan, review=False)
    assert "<script>" not in html
    assert '<bdi dir="ltr">Python</bdi>' in html
    assert "&lt;" in html


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,<script>x</script>", "ftp://example.com/file", "mailto:a@b.c"],
)
def test_non_web_links_get_no_href(url: str) -> None:
    assert render._web_link(url) is None


def test_web_links_are_normalized() -> None:
    assert render._web_link("https://www.github.com/example/") == {
        "href": "https://www.github.com/example/",
        "text": "github.com/example",
    }
    assert render._web_link("github.com/example") == {"href": "https://github.com/example", "text": "github.com/example"}


# ── Bidirectional isolation ────────────────────────────────────────────────


def test_contact_email_url_and_phone_are_isolated(master: MasterResume) -> None:
    html = _html(master, _plan("fa"), review=False)
    assert (
        '<bdi dir="ltr"><a href="mailto:negar.azmoon@example.com">negar.azmoon@example.com</a></bdi>' in html
    )
    assert '<bdi dir="ltr"><a href="https://github.com/example-negar">github.com/example-negar</a></bdi>' in html
    assert '<bdi dir="ltr"><a href="tel:+989001112222">+98 900 111 2222</a></bdi>' in html


def test_latin_terms_in_persian_text_are_isolated(master: MasterResume) -> None:
    html = _html(master, _plan("fa"), review=False)
    assert 'کاهش <bdi dir="ltr">۴۰٪</bdi>' not in html  # Persian digits stay in the RTL flow
    assert 'زمان پاسخ سرویس با <bdi dir="ltr">Redis</bdi>' in html
    assert 'طراحی <bdi dir="ltr">API</bdi> پرداخت با <bdi dir="ltr">FastAPI</bdi>' in html


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "ایمیل من ali@example.com است.",
            'ایمیل من <bdi dir="ltr">ali@example.com</bdi> است.',
        ),
        (
            "سایت https://example.com/path را ببینید.",
            'سایت <bdi dir="ltr">https://example.com/path</bdi> را ببینید.',
        ),
        ("کار با Django REST Framework.", 'کار با <bdi dir="ltr">Django REST Framework</bdi>.'),
        ("کاهش 40% زمان", 'کاهش <bdi dir="ltr">40%</bdi> زمان'),
        ("بیش از 120,000 سند", 'بیش از <bdi dir="ltr">120,000</bdi> سند'),
        ("مهندس (Agentic AI)", 'مهندس (<bdi dir="ltr">Agentic AI</bdi>)'),
        ("تماس: +98 900 000 0000", 'تماس: <bdi dir="ltr">+98 900 000 0000</bdi>'),
        ("فقط فارسی", "فقط فارسی"),
    ],
)
def test_bidi_filter_in_persian_text(text: str, expected: str) -> None:
    assert str(render._bidi(text, "fa")) == expected


def test_bidi_filter_isolates_persian_in_english_text() -> None:
    assert str(render._bidi("Worked at شرکت نمونه in Tehran", "en")) == 'Worked at <bdi dir="rtl">شرکت نمونه</bdi> in Tehran'
    assert str(render._bidi("a < b", "en")) == "a &lt; b"
    assert str(render._bidi(None, "en")) == ""


# ── Dates ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gregorian", "jalali"),
    [
        ("2023-03-21", (1402, 1, 1)),
        ("2024-03-20", (1403, 1, 1)),
        ("2025-03-21", (1404, 1, 1)),
        ("2025-03-20", (1403, 12, 30)),  # 1403 is a leap year
        ("2024-03-19", (1402, 12, 29)),  # 1402 is not
        ("2021-03-20", (1399, 12, 30)),  # 1399 is a leap year
        ("2000-01-01", (1378, 10, 11)),
        ("1979-02-11", (1357, 11, 22)),
        ("2023-09-23", (1402, 7, 1)),
        ("2023-09-22", (1402, 6, 31)),
    ],
)
def test_gregorian_to_jalali_known_dates(gregorian: str, jalali: tuple[int, int, int]) -> None:
    assert gregorian_to_jalali(date.fromisoformat(gregorian)) == jalali
    assert jalali_to_gregorian(*jalali) == date.fromisoformat(gregorian)


def test_jalali_round_trip_over_four_decades() -> None:
    day = date(1990, 1, 1)
    previous = gregorian_to_jalali(day - timedelta(days=1))
    while day < date(2031, 1, 1):
        jalali = gregorian_to_jalali(day)
        assert jalali_to_gregorian(*jalali) == day
        assert jalali > previous  # strictly increasing, no gaps or repeats in order
        previous = jalali
        day += timedelta(days=1)


@pytest.mark.parametrize(("month", "day"), [(0, 1), (13, 1), (7, 31), (1, 32), (1, 0)])
def test_invalid_jalali_dates_are_rejected(month: int, day: int) -> None:
    with pytest.raises(ValueError):
        jalali_to_gregorian(1402, month, day)


@pytest.mark.parametrize(
    ("gregorian", "jalali", "language", "expected"),
    [
        ("2023-03", None, "fa", "اسفند ۱۴۰۱"),  # converted from 2023-03-15
        ("2023-04", None, "fa", "فروردین ۱۴۰۲"),
        ("2023-03", "1402-01", "fa", "فروردین ۱۴۰۲"),  # the stored Jalali month wins
        ("2023-03", None, "en", "Mar 2023"),
        (None, "1402-01", "en", "Apr 2023"),  # converted from 1402-01-15
        (None, None, "en", None),
    ],
)
def test_format_month(gregorian: str | None, jalali: str | None, language: str, expected: str | None) -> None:
    assert render._format_month(gregorian, jalali, language) == expected  # type: ignore[arg-type]


def test_persian_dates_use_jalali_months_and_persian_digits(master: MasterResume) -> None:
    html = _html(master, _plan("fa"), review=False)
    dates = re.findall(r'<span class="dates">(.*?)</span>', html)
    assert dates == [
        "اسفند ۱۳۹۹ – تاکنون",  # stored start_jalali, no end
        "خرداد ۱۳۹۷ – بهمن ۱۳۹۹",  # stored Jalali fields
        "شهریور ۱۳۹۳ – خرداد ۱۳۹۷",  # converted from Gregorian (education)
        "آبان ۱۳۹۹",  # converted from Gregorian (certification)
    ]
    assert not any(re.search(r"[0-9]", value) for value in dates)


def test_english_dates(master: MasterResume) -> None:
    html = _html(master, _plan("en"), review=False)
    dates = re.findall(r'<span class="dates">(.*?)</span>', html)
    assert dates == ["Mar 2021 – Present", "Jun 2018 – Feb 2021", "Sep 2014 – Jun 2018", "Nov 2020"]


# ── PDF ────────────────────────────────────────────────────────────────────


def test_output_paths_are_sanitized(tmp_path: Path) -> None:
    clean, review = render._output_paths(tmp_path, "jobinja", "tER9/../x", 2, "fa")
    assert clean == tmp_path / "jobinja_tER9-x_v2_fa.pdf"
    assert review == tmp_path / "jobinja_tER9-x_v2_fa_review.pdf"


def test_render_tailored_resume_writes_real_pdfs_and_stores_paths(master: MasterResume, tmp_path: Path) -> None:
    plan = _plan("fa")
    changes = compute_changes(plan, master)

    async def scenario() -> tuple[Path, Path, TailoredResumeRecord | None]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(
                JobPost(
                    platform="jobinja",
                    external_id="tER9",
                    title="برنامه‌نویس Python",
                    company="شرکت نمونه",
                    city="مشهد",
                    work_mode=WorkMode.REMOTE,
                    url="https://jobinja.ir/jobs/tER9",
                )
            )
            resume_id = await db.save_tailored_resume(
                TailoredResumeRecord(
                    platform="jobinja",
                    external_id="tER9",
                    version=1,
                    language="fa",
                    llm_model="fake-model",
                    plan_json=plan.model_dump_json(),
                    changes_json=changes.model_dump_json(),
                )
            )
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    clean, review = await render_tailored_resume(
                        resume_id, db, master, browser, output_dir=tmp_path / "resumes", warnings=["w"]
                    )
                finally:
                    await browser.close()
            return clean, review, await db.get_tailored_resume(resume_id)

    clean, review, record = asyncio.run(scenario())
    assert clean == tmp_path / "resumes" / "jobinja_tER9_v1_fa.pdf"
    assert review == tmp_path / "resumes" / "jobinja_tER9_v1_fa_review.pdf"
    for path in (clean, review):
        data = path.read_bytes()
        assert data.startswith(b"%PDF")
        assert len(data) > 5000
    assert not list((tmp_path / "resumes").glob("*.tmp"))
    assert record is not None
    assert (record.clean_pdf_path, record.review_pdf_path) == (str(clean), str(review))


def test_render_tailored_resume_unknown_id(master: MasterResume, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await render_tailored_resume(999, db, master, None)  # type: ignore[arg-type]

    with pytest.raises(LookupError, match="999"):
        asyncio.run(scenario())
