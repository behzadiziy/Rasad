"""Tests for the master resume schema, plan validation, prompts, and the tailoring flow (fake LLM client)."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from src.llm.base import LLMClient, LLMResult, LLMUsage, T
from src.models import AppConfig, JobPost, TailoredResumeRecord, WorkMode
from src.resume import tailor
from src.resume.models import LocalizedText, MasterResume, TailoringPlan, load_master_resume
from src.resume.tailor import TailoringError, _parse_job_ref, _render_prompts, tailor_resume, validate_plan

_FIXTURE = Path(__file__).parent / "fixtures" / "resume" / "master_resume.yaml"
_EXAMPLE = Path(__file__).parents[1] / "config" / "master_resume.example.yaml"


@pytest.fixture(scope="module")
def master() -> MasterResume:
    """The fictional master resume."""
    return load_master_resume(_FIXTURE)


def _job(**overrides: Any) -> JobPost:
    """An English job posting."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "5001",
        "title": "Python Backend Engineer",
        "company": "Omega Labs",
        "city": "Mashhad",
        "work_mode": WorkMode.REMOTE,
        "url": "https://jobinja.ir/jobs/5001",
        "raw_description": "We are hiring a backend engineer with Python, FastAPI, and Kubernetes experience.",
    }
    data.update(overrides)
    return JobPost(**data)


def _persian_job() -> JobPost:
    """A Persian job posting."""
    return _job(
        title="برنامه‌نویس بک‌اند Python",
        company="شرکت امگا",
        raw_description="به یک برنامه‌نویس بک‌اند مسلط به FastAPI نیازمندیم.",
    )


def _bullets(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    """Build plan bullets from (id, text) pairs."""
    return [{"bullet_id": bullet_id, "text": text} for bullet_id, text in pairs]


def _plan_data(**overrides: Any) -> dict[str, Any]:
    """A valid English plan."""
    data: dict[str, Any] = {
        "language": "en",
        "summary": "Software engineer with 5 years of experience building Python API services.",
        "experiences": [
            {
                "experience_id": "alpha-backend",
                "bullets": _bullets(
                    ("alpha-api", "Designed the FastAPI payment API for 1,200 stores"),
                    ("alpha-cache", "Cut API response time by 40% with Redis"),
                ),
            },
            {"experience_id": "beta-developer", "bullets": _bullets(("beta-dashboard", "Built an internal Django dashboard"))},
        ],
        "projects": [{"project_id": "gamma-bot", "bullets": _bullets(("gamma-node", "Implemented a Telegram bot with Node.js"))}],
        "skill_ids": ["python", "fastapi", "redis"],
        "cover_letter": (
            "Dear Hiring Manager,\n\nI am applying for the Python Backend Engineer role at Omega Labs. "
            "I designed a FastAPI payment API for 1,200 stores at Alpha Systems.\n\nSincerely,\nNegar Azmoon"
        ),
        "relevance_notes": "Strong Python fit; Kubernetes is not in the resume.",
    }
    data.update(overrides)
    return data


def _plan(**overrides: Any) -> TailoringPlan:
    """A valid English plan with overrides."""
    return TailoringPlan.model_validate(_plan_data(**overrides))


def _persian_plan(**overrides: Any) -> TailoringPlan:
    """A valid Persian plan with overrides."""
    data: dict[str, Any] = {
        "language": "fa",
        "summary": "مهندس نرم‌افزار با ۵ سال سابقه در Python و ساخت سرویس‌های API.",
        "experiences": [
            {
                "experience_id": "alpha-backend",
                "bullets": _bullets(
                    ("alpha-api", "طراحی API پرداخت با FastAPI برای ۱٬۲۰۰ فروشگاه"),
                    ("alpha-cache", "کاهش ۴۰ درصدی زمان پاسخ با Redis"),
                ),
            }
        ],
        "projects": [],
        "skill_ids": ["python", "fastapi"],
        "cover_letter": "با سلام، برای موقعیت برنامه‌نویس بک‌اند Python در شرکت امگا درخواست می‌دهم.",
        "relevance_notes": "تطابق خوب.",
    }
    data.update(overrides)
    return TailoringPlan.model_validate(data)


def _app_config(**tailoring: Any) -> AppConfig:
    """A minimal in-memory app config with the `llm` section."""
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobinja"], "max_pages_per_keyword": 1},
            "search_profiles": [{"profile_name": "P", "keywords": ["Python"]}],
            "llm": {"provider": "gemini", "model": "gemini-test-flash"},
            "tailoring": tailoring,
        }
    )


# ── Master resume schema ───────────────────────────────────────────────────


def test_fixture_and_shipped_example_load(master: MasterResume) -> None:
    assert [item.id for item in master.experiences] == ["alpha-backend", "beta-developer"]
    example = load_master_resume(_EXAMPLE)
    assert example.contact.name.en == "Arman Shirazi"
    assert example.experiences[0].end is None


def test_localized_text_fallback_and_plain_string() -> None:
    both = LocalizedText.model_validate("PostgreSQL")
    assert (both.fa, both.en) == ("PostgreSQL", "PostgreSQL")
    english_only = LocalizedText(en="Developer")
    assert english_only.text_for("fa") == "Developer"
    assert english_only.text_for("en") == "Developer"
    persian_only = LocalizedText(fa=" توسعه‌دهنده ", en="  ")
    assert persian_only.en is None
    assert persian_only.text_for("en") == "توسعه‌دهنده"
    with pytest.raises(ValueError, match="at least one"):
        LocalizedText(fa="", en=None)


def _resume_data(master: MasterResume) -> dict[str, Any]:
    """The fixture as a plain dict, for building invalid variants."""
    return master.model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["skills"].append({"id": "alpha-api", "name": "X"}), "duplicates: \\['alpha-api'\\]"),
        (lambda d: d["skills"].append({"id": "Bad_ID", "name": "X"}), "kebab-case"),
        (lambda d: d["experiences"][0].update(start="2021/03"), "YYYY-MM"),
        (lambda d: d["experiences"][0].update(start="2021-13"), "YYYY-MM"),
        (lambda d: d["experiences"][1].update(end="2017-01"), "end date is before its start date"),
        (lambda d: d["experiences"][0].update(end_jalali="1402-01"), "end_jalali but no end"),
        (lambda d: d["experiences"][0].update(start_jalali="2021-03"), "Jalali year"),
        (lambda d: d["skills"].append({"id": "x", "name": {"fa": " "}}), "at least one"),
        (lambda d: d.update(hobbies=["chess"]), "Extra inputs are not permitted"),
    ],
)
def test_invalid_master_resume_is_rejected_without_echoing_values(
    master: MasterResume, tmp_path: Path, mutate: Any, message: str
) -> None:
    data = _resume_data(master)
    data["contact"]["email"] = "SECRET-EMAIL@example.com"
    mutate(data)
    path = tmp_path / "resume.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message) as excinfo:
        load_master_resume(path)
    assert str(path) in str(excinfo.value)
    assert "SECRET-EMAIL" not in str(excinfo.value)
    assert "نگار" not in str(excinfo.value)


def test_malformed_yaml_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("contact: [unclosed\nsecret: value", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed YAML at line"):
        load_master_resume(path)
    with pytest.raises(FileNotFoundError):
        load_master_resume(tmp_path / "missing.yaml")


def test_master_helpers(master: MasterResume) -> None:
    assert master.bullet_owners()["gamma-users"] == "gamma-bot"
    assert master.date_years() == {"2021", "1399", "2018", "1397", "2014", "2020"}
    texts = list(master.localized_texts())
    assert "Negar Azmoon" in texts and "Cut response time by 40% with Redis" in texts
    assert not any("example.com" in text for text in texts)


# ── Validation: valid plans ────────────────────────────────────────────────


def test_valid_english_plan_has_no_problems(master: MasterResume) -> None:
    validation = validate_plan(_plan(), master, _job(), "en")
    assert validation.errors == []
    assert validation.warnings == []


def test_valid_persian_plan_has_no_problems(master: MasterResume) -> None:
    validation = validate_plan(_persian_plan(), master, _persian_job(), "fa")
    assert validation.errors == []
    assert validation.warnings == []


# ── Validation: errors ─────────────────────────────────────────────────────


def _errors(master: MasterResume, **overrides: Any) -> list[str]:
    """Validate an English plan with overrides and return its errors."""
    return validate_plan(_plan(**overrides), master, _job(), "en").errors


def test_unknown_ids(master: MasterResume) -> None:
    errors = _errors(
        master,
        experiences=[{"experience_id": "ghost-corp", "bullets": _bullets(("ghost-bullet", "Did things"))}],
        projects=[{"project_id": "ghost-project", "bullets": []}],
    )
    assert "Unknown experience ID 'ghost-corp': use only experience IDs from the master resume" in errors
    assert "Unknown bullet ID 'ghost-bullet' under experience 'ghost-corp'" in errors
    assert "Unknown project ID 'ghost-project': use only project IDs from the master resume" in errors


def test_bullet_under_wrong_parent(master: MasterResume) -> None:
    errors = _errors(
        master,
        experiences=[{"experience_id": "alpha-backend", "bullets": _bullets(("beta-cpp", "Optimized a C++ module"))}],
        projects=[{"project_id": "gamma-bot", "bullets": _bullets(("alpha-ci", "Set up CI/CD with GitHub Actions"))}],
    )
    assert any("'beta-cpp' belongs to 'beta-developer', not to experience 'alpha-backend'" in e for e in errors)
    assert any("'alpha-ci' belongs to 'alpha-backend', not to project 'gamma-bot'" in e for e in errors)


def test_duplicate_ids(master: MasterResume) -> None:
    errors = _errors(
        master,
        experiences=[
            {"experience_id": "alpha-backend", "bullets": _bullets(("alpha-api", "Designed the payment API"))},
            {"experience_id": "alpha-backend", "bullets": _bullets(("alpha-api", "Designed the payment API"))},
        ],
        projects=[
            {"project_id": "gamma-bot", "bullets": []},
            {"project_id": "gamma-bot", "bullets": []},
        ],
        skill_ids=["python", "python"],
    )
    assert "Duplicate experience ID 'alpha-backend': list each experience once" in errors
    assert "Duplicate project ID 'gamma-bot': list each project once" in errors
    assert "Duplicate bullet ID 'alpha-api': use each bullet once" in errors
    assert "Duplicate skill ID 'python': list each skill once" in errors


def test_unknown_skill_id(master: MasterResume) -> None:
    assert "Skill ID 'kubernetes' is not in the master resume" in _errors(master, skill_ids=["python", "kubernetes"])


def test_too_many_bullets_and_empty_bullet(master: MasterResume) -> None:
    six = _bullets(*[("alpha-api", "Designed the payment API")] * 6)
    errors = _errors(master, experiences=[{"experience_id": "alpha-backend", "bullets": six}])
    assert "Experience 'alpha-backend' has 6 bullets; the maximum is 5" in errors
    errors = _errors(master, experiences=[{"experience_id": "alpha-backend", "bullets": _bullets(("alpha-api", "  "))}])
    assert "Bullet 'alpha-api' has an empty text" in errors


def test_numbers_not_in_master_are_errors(master: MasterResume) -> None:
    errors = _errors(
        master,
        summary="Software engineer with 7 years of experience in Python.",
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": _bullets(("alpha-cache", "Cut response time by 45% with Redis for 1,200 stores")),
            }
        ],
    )
    assert "Number 7 in the summary does not appear in the master resume" in errors
    assert (
        "Number 45 in bullet alpha-cache of experience alpha-backend does not appear in the master resume" in errors
    )
    assert not any("1200" in error for error in errors)


def test_numbers_are_compared_after_normalization(master: MasterResume) -> None:
    # Persian digits, Arabic thousands separators, and years from dates are known.
    plan = _persian_plan(
        summary="مهندس نرم‌افزار با ۵ سال سابقه، از ۲۰۲۱ (۱۳۹۹) در آلفا؛ ۱۲۰۰ فروشگاه و ۰۴۰٪ بهبود.",
    )
    assert validate_plan(plan, master, _persian_job(), "fa").errors == []
    plan = _persian_plan(summary="مهندس نرم‌افزار با ۸ سال سابقه در Python.")
    assert validate_plan(plan, master, _persian_job(), "fa").errors == [
        "Number 8 in the summary does not appear in the master resume"
    ]


def test_cover_letter_may_use_numbers_from_job_title_and_company(master: MasterResume) -> None:
    job = _job(title="Python Developer Level 3", company="Studio 21")
    letter = "Dear team at Studio 21, I am applying for Python Developer Level 3. I served 1,200 stores."
    assert validate_plan(_plan(cover_letter=letter), master, job, "en").errors == []
    summary_errors = validate_plan(_plan(summary="Level 3 engineer."), master, job, "en").errors
    assert summary_errors == ["Number 3 in the summary does not appear in the master resume"]


def test_empty_summary(master: MasterResume) -> None:
    assert "The summary is empty" in _errors(master, summary="   ")


def test_wrong_output_language(master: MasterResume) -> None:
    validation = validate_plan(_plan(), master, _persian_job(), "fa")
    assert "The plan's language is 'en', but the target language is 'fa'" in validation.errors
    assert "The text is written in English, but the target language is Persian (Farsi)" in validation.errors

    persian_text_marked_english = _persian_plan(language="en")
    errors = validate_plan(persian_text_marked_english, master, _job(), "en").errors
    assert "The text is written in Persian (Farsi), but the target language is English" in errors


def test_persian_output_dense_with_tool_names_is_still_persian(master: MasterResume) -> None:
    plan = _persian_plan(summary="توسعه API با FastAPI و Redis و Python.", cover_letter=None)
    assert validate_plan(plan, master, _persian_job(), "fa").errors == []


# ── Validation: warnings ───────────────────────────────────────────────────


def _warnings(master: MasterResume, *, job: JobPost | None = None, **overrides: Any) -> list[str]:
    """Validate an English plan with overrides and return its warnings."""
    return validate_plan(_plan(**overrides), master, job or _job(), "en").warnings


def test_persian_output_flags_every_unknown_latin_token(master: MasterResume) -> None:
    plan = _persian_plan(summary="مهندس نرم‌افزار با ۵ سال سابقه در Python، kubernetes و Terraform.")
    warnings = validate_plan(plan, master, _persian_job(), "fa").warnings
    assert warnings == [
        "Term 'kubernetes' in the summary does not appear in the master resume",
        "Term 'Terraform' in the summary does not appear in the master resume",
    ]


@pytest.mark.parametrize(
    "term",
    [
        "GCP",  # acronym
        "GraphQL",  # mixed case
        "iOS",  # mixed case starting lowercase
        "Vue.js",  # contains "."
        "C#",  # contains "#"
        "K8s",  # contains a digit
        "Kubernetes",  # capitalized, not at a sentence start
    ],
)
def test_english_output_flags_technical_terms(master: MasterResume, term: str) -> None:
    summary = f"Software engineer with 5 years of experience in Python and {term} services."
    assert _warnings(master, summary=summary) == [f"Term {term!r} in the summary does not appear in the master resume"]


@pytest.mark.parametrize(
    "summary",
    [
        "Kubernetes aside, I build Python API services.",  # capitalized at the start of the text
        "I build Python API services. Kubectl is next.",  # capitalized at a sentence start
        "I build Python services with kubernetes.",  # lowercase words are not term candidates in English
        "I build Python APIs, e.g. with FastAPI.",  # plural of a known acronym; prose abbreviation
        "I build C++ and Node.js services on AWS with GitHub Actions and CI/CD.",
        "Dear Hiring Manager, in March I'm Negar Azmoon from Mashhad.",
    ],
)
def test_english_output_does_not_flag_known_or_ordinary_words(master: MasterResume, summary: str) -> None:
    assert _warnings(master, summary=summary) == []


def test_compound_terms_need_every_part_known(master: MasterResume) -> None:
    assert _warnings(master, summary="Python services with FastAPI/Redis.") == []
    assert _warnings(master, summary="Python services with Redis/Memcached.") == [
        "Term 'Redis/Memcached' in the summary does not appear in the master resume"
    ]


def test_cover_letter_may_name_the_company_and_title(master: MasterResume) -> None:
    job = _job(title="Senior MLOps Engineer", company="Zephyr AI")
    letter = "Dear Zephyr AI team, I am applying for the Senior MLOps Engineer role with my Python experience."
    assert _warnings(master, job=job, cover_letter=letter) == []
    warnings = _warnings(master, job=job, summary="Python engineer interested in MLOps.", cover_letter=None)
    assert warnings == ["Term 'MLOps' in the summary does not appear in the master resume"]
    # Another company's name is not allowed in this job's cover letter.
    other = "Dear Omega Labs team, I am applying with my Python experience."
    assert _warnings(master, job=job, cover_letter=other) == [
        "Term 'Omega' in the cover letter does not appear in the master resume",
        "Term 'Labs' in the cover letter does not appear in the master resume",
    ]


def test_each_term_is_reported_once_per_text(master: MasterResume) -> None:
    summary = "Python with Terraform, more Terraform, and TERRAFORM."
    assert _warnings(master, summary=summary) == ["Term 'Terraform' in the summary does not appear in the master resume"]


# ── Prompts ────────────────────────────────────────────────────────────────


def test_prompts_contain_job_and_target_language_master_without_contact_details(master: MasterResume) -> None:
    system, user = _render_prompts(_job(), master, "en", cover_letter=True)
    assert "in English" in system
    assert "120 to 180 words" in system
    assert "at most 25 words" in system
    assert "Title: Python Backend Engineer" in user
    assert "Company: Omega Labs" in user
    master_json = json.loads(user.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert master_json["experiences"][0] == {
        "id": "alpha-backend",
        "title": "Senior Backend Developer",
        "company": "Alpha Systems",
        "bullets": [
            {"id": "alpha-api", "text": "Designed the payment API with FastAPI for 1,200 stores"},
            {"id": "alpha-cache", "text": "Cut response time by 40% with Redis"},
            {"id": "alpha-ci", "text": "Set up CI/CD with GitHub Actions"},
        ],
    }
    for private in ("negar.azmoon@example.com", "+98 900", "github.com", "2021-03", "1399"):
        assert private not in user
    assert "Problems in your previous answer" not in user


def test_persian_prompt_uses_persian_text_and_lists_problems(master: MasterResume) -> None:
    system, user = _render_prompts(_persian_job(), master, "fa", cover_letter=False, problems=["Problem one"])
    assert "in Persian (Farsi)" in system
    assert "Set `cover_letter` to null." in system
    assert "سامانه‌های آلفا" in user
    assert "## Problems in your previous answer" in user
    assert "- Problem one" in user


def test_long_description_is_capped(master: MasterResume) -> None:
    job = _job(raw_description="A" * 7000 + "TAIL-MARKER")
    _, user = _render_prompts(job, master, "en", cover_letter=True)
    assert "A" * 6000 in user
    assert "A" * 6001 not in user
    assert "TAIL-MARKER" not in user
    assert "[…truncated]" in user


# ── Tailoring flow (fake LLM) ──────────────────────────────────────────────


class _FakeLLM(LLMClient):
    """Returns queued plans and records the prompts it receives."""

    def __init__(self, plans: list[TailoringPlan]) -> None:
        """Queue the plans to return, in order."""
        self.plans = list(plans)
        self.calls: list[tuple[str, str]] = []

    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        """Return the next queued plan."""
        self.calls.append((system, user))
        value = self.plans.pop(0)
        usage = LLMUsage(prompt_tokens=100, output_tokens=10, total_tokens=110)
        return LLMResult[schema](value=value, model=f"fake-{len(self.calls)}", usage=usage, latency_seconds=0.1)


def _tailor(llm: _FakeLLM, master: MasterResume, job: JobPost | None = None, **tailoring: Any) -> Any:
    """Run `tailor_resume` with the fake client."""
    return asyncio.run(tailor_resume(job or _job(), master, llm, _app_config(**tailoring)))


def test_clean_plan_needs_one_call(master: MasterResume) -> None:
    llm = _FakeLLM([_plan()])
    result = _tailor(llm, master)
    assert len(llm.calls) == 1
    assert result.language == "en"
    assert result.warnings == []
    assert result.model == "fake-1"
    assert result.usage == LLMUsage(prompt_tokens=100, output_tokens=10, total_tokens=110)
    assert result.changes.has_cover_letter is True
    record = TailoredResumeRecord(
        platform="jobinja",
        external_id="5001",
        version=1,
        language=result.language,
        llm_model=result.model,
        plan_json=result.plan.model_dump_json(),
        changes_json=result.changes.model_dump_json(),
    )
    assert TailoringPlan.model_validate_json(record.plan_json) == result.plan


def test_retry_fixes_the_problems(master: MasterResume) -> None:
    bad = _plan(skill_ids=["python", "kubernetes"], summary="Python engineer with 9 years and Terraform skills.")
    llm = _FakeLLM([bad, _plan()])
    result = _tailor(llm, master)
    assert len(llm.calls) == 2
    first_user, retry_user = llm.calls[0][1], llm.calls[1][1]
    assert "Problems in your previous answer" not in first_user
    assert "- Skill ID 'kubernetes' is not in the master resume" in retry_user
    assert "- Number 9 in the summary does not appear in the master resume" in retry_user
    assert "- Term 'Terraform' in the summary does not appear in the master resume" in retry_user
    assert llm.calls[0][0] == llm.calls[1][0]
    assert result.plan == _plan()
    assert result.model == "fake-2"
    assert result.usage == LLMUsage(prompt_tokens=200, output_tokens=20, total_tokens=220)
    assert result.warnings == []


def test_warnings_alone_trigger_one_retry_and_remaining_warnings_are_returned(master: MasterResume) -> None:
    flagged = _plan(summary="Python engineer with 5 years and Terraform skills.")
    llm = _FakeLLM([flagged, flagged])
    result = _tailor(llm, master)
    assert len(llm.calls) == 2
    assert result.warnings == ["Term 'Terraform' in the summary does not appear in the master resume"]


def test_retry_that_still_fails_raises_tailoring_error(master: MasterResume) -> None:
    bad = _plan(skill_ids=["kubernetes"])
    llm = _FakeLLM([bad, bad])
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{message}")
    try:
        with pytest.raises(TailoringError) as excinfo:
            _tailor(llm, master)
    finally:
        logger.remove(sink_id)
    assert len(llm.calls) == 2
    assert excinfo.value.errors == ["Skill ID 'kubernetes' is not in the master resume"]
    assert "1 error(s)" in str(excinfo.value)
    assert "kubernetes" not in str(excinfo.value)
    logged = "".join(lines)
    for private in ("kubernetes", "Software engineer", "Negar", "Omega Labs"):
        assert private not in logged


def test_persian_job_is_tailored_in_persian(master: MasterResume) -> None:
    llm = _FakeLLM([_persian_plan()])
    result = _tailor(llm, master, job=_persian_job())
    assert result.language == "fa"
    assert "in Persian (Farsi)" in llm.calls[0][0]
    assert result.changes.language == "fa"


def test_cover_letter_is_dropped_when_disabled(master: MasterResume) -> None:
    llm = _FakeLLM([_plan()])
    result = _tailor(llm, master, cover_letter=False)
    assert result.plan.cover_letter is None
    assert result.changes.has_cover_letter is False
    assert "Set `cover_letter` to null." in llm.calls[0][0]


# ── CLI helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jobinja:123", ("jobinja", "123")),
        ("jobvision:abc-9", ("jobvision", "abc-9")),
        ("jv:42", ("jobvision", "42")),
        ("JB: 7 ", ("jobinja", "7")),
    ],
)
def test_parse_job_ref(value: str, expected: tuple[str, str]) -> None:
    assert _parse_job_ref(value) == expected


@pytest.mark.parametrize("value", ["linkedin:1", "jobinja:", "123", ":5"])
def test_parse_job_ref_rejects_invalid(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_job_ref(value)


def test_print_changes_shows_summary_warnings_and_tokens(master: MasterResume, capsys: pytest.CaptureFixture[str]) -> None:
    result = _tailor(_FakeLLM([_plan()]), master)
    result = result.model_copy(update={"warnings": ["Term 'X' in the summary does not appear in the master resume"]})
    tailor._print_changes(_job(), result)
    out = capsys.readouterr().out
    assert "Summary: rephrased" in out
    assert "Rephrased bullets (3):" in out
    assert "Omitted skills (3):" in out
    assert "Warnings (1):" in out
    assert "Tokens: prompt 100, output 10, total 110" in out
