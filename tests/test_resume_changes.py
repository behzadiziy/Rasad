"""Tests for `compute_changes`: the change set derived by code from a plan and the master resume."""

from pathlib import Path
from typing import Any

import pytest

from src.resume.changes import ChangeSet, compute_changes
from src.resume.models import MasterResume, TailoringPlan, load_master_resume

_FIXTURE = Path(__file__).parent / "fixtures" / "resume" / "master_resume.yaml"


@pytest.fixture(scope="module")
def master() -> MasterResume:
    """The fictional master resume."""
    return load_master_resume(_FIXTURE)


def _identity_plan(master: MasterResume, language: str = "en", **overrides: Any) -> TailoringPlan:
    """A plan that keeps everything in master order with the master texts."""
    lang = language  # type: ignore[assignment]
    data: dict[str, Any] = {
        "language": language,
        "summary": master.summary.text_for(lang),
        "experiences": [
            {
                "experience_id": item.id,
                "bullets": [{"bullet_id": bullet.id, "text": bullet.text.text_for(lang)} for bullet in item.bullets],
            }
            for item in master.experiences
        ],
        "projects": [
            {
                "project_id": item.id,
                "bullets": [{"bullet_id": bullet.id, "text": bullet.text.text_for(lang)} for bullet in item.bullets],
            }
            for item in master.projects
        ],
        "skill_ids": [skill.id for skill in master.skills],
        "cover_letter": None,
        "relevance_notes": "notes",
    }
    data.update(overrides)
    return TailoringPlan.model_validate(data)


def test_identity_plan_has_no_changes(master: MasterResume) -> None:
    changes = compute_changes(_identity_plan(master), master)
    assert changes.summary.changed is False
    assert changes.summary.before == changes.summary.after == master.summary.en
    assert changes.rephrased_bullets == []
    assert changes.omitted_count == 0
    assert changes.rephrased_count == 0
    assert changes.reordered_sections == []
    assert changes.reordered_bullets == []
    assert changes.has_cover_letter is False


def test_whitespace_only_differences_are_not_changes(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        summary="  Software engineer with 5 years   of experience\nin Python and building API services. ",
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "alpha-api", "text": "Designed the payment API  with FastAPI for 1,200 stores "},
                    {"bullet_id": "alpha-cache", "text": "Cut response time by 40% with Redis"},
                    {"bullet_id": "alpha-ci", "text": "Set up CI/CD with GitHub Actions"},
                ],
            },
            {
                "experience_id": "beta-developer",
                "bullets": [
                    {"bullet_id": "beta-dashboard", "text": "Built an internal dashboard with Django"},
                    {"bullet_id": "beta-cpp", "text": "Optimized a C++ image processing module"},
                ],
            },
        ],
    )
    changes = compute_changes(plan, master)
    assert changes.summary.changed is False
    assert changes.rephrased_bullets == []


def test_rephrased_summary_and_bullets_record_before_and_after(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        summary="Python engineer building API services for 5 years.",
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "alpha-api", "text": "Built the FastAPI payment API used by 1,200 stores"},
                    {"bullet_id": "alpha-cache", "text": "Cut response time by 40% with Redis"},
                    {"bullet_id": "alpha-ci", "text": "Set up CI/CD with GitHub Actions"},
                ],
            },
            {
                "experience_id": "beta-developer",
                "bullets": [
                    {"bullet_id": "beta-dashboard", "text": "Built an internal dashboard with Django"},
                    {"bullet_id": "beta-cpp", "text": "Optimized a C++ image-processing module"},
                ],
            },
        ],
    )
    changes = compute_changes(plan, master)
    assert changes.summary.changed is True
    assert changes.summary.before == master.summary.en
    assert changes.summary.after == "Python engineer building API services for 5 years."
    assert [(c.parent_id, c.bullet_id) for c in changes.rephrased_bullets] == [
        ("alpha-backend", "alpha-api"),
        ("beta-developer", "beta-cpp"),
    ]
    first = changes.rephrased_bullets[0]
    assert first.before == "Designed the payment API with FastAPI for 1,200 stores"
    assert first.after == "Built the FastAPI payment API used by 1,200 stores"
    assert changes.rephrased_count == 3


def test_omitted_items_are_labeled_in_target_language(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        language="fa",
        summary=master.summary.fa,
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": [{"bullet_id": "alpha-cache", "text": "کاهش ۴۰٪ زمان پاسخ با Redis"}],
            }
        ],
        projects=[],
        skill_ids=["python", "redis"],
    )
    changes = compute_changes(plan, master)
    assert [(item.id, item.label) for item in changes.omitted_experiences] == [
        ("beta-developer", "توسعه‌دهنده — نرم‌افزار بتا")
    ]
    # Only bullets of included experiences are listed; beta's bullets go with beta.
    assert [(item.parent_id, item.id) for item in changes.omitted_bullets] == [
        ("alpha-backend", "alpha-api"),
        ("alpha-backend", "alpha-ci"),
    ]
    assert changes.omitted_bullets[0].label == "طراحی API پرداخت با FastAPI برای ۱٬۲۰۰ فروشگاه"
    assert [(item.id, item.label) for item in changes.omitted_projects] == [
        ("gamma-bot", "ربات گاما"),
        ("delta-scripts", "اسکریپت‌های دلتا"),
    ]
    assert [item.id for item in changes.omitted_skills] == ["fastapi", "django", "cpp", "aws"]
    assert changes.omitted_skills[3].label == "AWS"
    assert changes.omitted_count == 1 + 2 + 2 + 4


def test_persian_plan_compares_with_english_fallback_when_persian_is_missing(master: MasterResume) -> None:
    plan = _identity_plan(master, language="fa", summary=master.summary.fa)
    changes = compute_changes(plan, master)
    # beta-dashboard has only English text, so the Persian plan's text equals the fallback.
    assert changes.rephrased_bullets == []

    translated = _identity_plan(
        master,
        language="fa",
        summary=master.summary.fa,
        experiences=[
            {"experience_id": "alpha-backend", "bullets": []},
            {
                "experience_id": "beta-developer",
                "bullets": [{"bullet_id": "beta-dashboard", "text": "ساخت داشبورد داخلی با Django"}],
            },
        ],
    )
    (change,) = compute_changes(translated, master).rephrased_bullets
    assert change.before == "Built an internal dashboard with Django"
    assert change.after == "ساخت داشبورد داخلی با Django"


def test_reordered_sections_and_bullets(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        experiences=[
            {"experience_id": "beta-developer", "bullets": []},
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "alpha-ci", "text": "Set up CI/CD with GitHub Actions"},
                    {"bullet_id": "alpha-api", "text": "Designed the payment API with FastAPI for 1,200 stores"},
                ],
            },
        ],
        projects=[
            {"project_id": "delta-scripts", "bullets": []},
            {"project_id": "gamma-bot", "bullets": [{"bullet_id": "gamma-node", "text": "Implemented a Telegram bot with Node.js"}]},
        ],
        skill_ids=["redis", "python"],
    )
    changes = compute_changes(plan, master)
    assert changes.reordered_sections == ["experiences", "projects", "skills"]
    assert changes.reordered_bullets == ["alpha-backend"]


def test_selecting_a_subset_in_master_order_is_not_a_reorder(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        experiences=[
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "alpha-api", "text": "Designed the payment API with FastAPI for 1,200 stores"},
                    {"bullet_id": "alpha-ci", "text": "Set up CI/CD with GitHub Actions"},
                ],
            }
        ],
        skill_ids=["python", "django", "aws"],
    )
    changes = compute_changes(plan, master)
    assert changes.reordered_sections == []
    assert changes.reordered_bullets == []


@pytest.mark.parametrize(("cover_letter", "expected"), [("Dear team, ...", True), ("   ", False), (None, False)])
def test_cover_letter_presence(master: MasterResume, cover_letter: str | None, expected: bool) -> None:
    changes = compute_changes(_identity_plan(master, cover_letter=cover_letter), master)
    assert changes.has_cover_letter is expected


def test_unknown_and_repeated_ids_are_ignored(master: MasterResume) -> None:
    plan = _identity_plan(
        master,
        experiences=[
            {"experience_id": "ghost-company", "bullets": [{"bullet_id": "ghost-bullet", "text": "Invented"}]},
            {
                "experience_id": "alpha-backend",
                "bullets": [
                    {"bullet_id": "alpha-api", "text": "Designed the payment API with FastAPI for 1,200 stores"},
                    {"bullet_id": "beta-cpp", "text": "Misplaced bullet"},
                    {"bullet_id": "alpha-api", "text": "Repeated"},
                ],
            },
            {"experience_id": "alpha-backend", "bullets": []},
        ],
        skill_ids=["python", "python", "kubernetes"],
    )
    changes = compute_changes(plan, master)
    assert changes.rephrased_bullets == []
    assert [item.id for item in changes.omitted_experiences] == ["beta-developer"]
    assert [item.id for item in changes.omitted_bullets] == ["alpha-cache", "alpha-ci"]
    assert "python" not in [item.id for item in changes.omitted_skills]


def test_change_set_round_trips_through_json(master: MasterResume) -> None:
    plan = _identity_plan(master, summary="Changed summary", skill_ids=["aws", "python"], cover_letter="Letter")
    changes = compute_changes(plan, master)
    assert ChangeSet.model_validate_json(changes.model_dump_json()) == changes
