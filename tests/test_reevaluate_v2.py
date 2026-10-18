"""Tests for the Task 7b pipeline and `--reevaluate` changes in `main.py`.

Covers the pure `build_reevaluation_plan` (the four sections), the dry-run
output, the real run (requeue, demote, report only, regroup, notify), and
duplicate grouping in the scrape pipeline. Configs are built in memory; the
browser and the Telegram notifier are replaced by fakes, so nothing touches
the network.
"""

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import main
from src.database import Database
from src.models import AppConfig, JobPost, JobStatus, Platform, Settings, WorkMode
from src.rule_engine import RuleEngine

_NOW = datetime.now(UTC)
_SETUPTRACK_FA = "ستاپ ترک"
_SETUPTRACK = "ستاپ ترک | SetupTrack"
_ASIA_FA = "گروه حقوق آسیا"
_ASIA = "گروه حقوق آسیا | Asia Law Group"


def _config() -> AppConfig:
    """The shipped profile (synonym groups, a minimum of 2) and the default duplicates settings."""
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 1},
            "city_aliases": {"mashhad": ["mashhad", "مشهد"], "tehran": ["tehran", "تهران"]},
            "search_profiles": [
                {
                    "profile_name": "AI & Automation",
                    "keywords": [["AI", "هوش مصنوعی"], "n8n", "Agentic", "LLM", "RAG"],
                    "description_min_hits": 2,
                    "exclude_keywords": ["کارآموز", "Intern"],
                    "location_rules": [{"city": "mashhad", "remote_only": False}, {"city": "ALL", "remote_only": True}],
                }
            ],
        }
    )


def _job(
    external_id: str,
    title: str,
    status: JobStatus,
    *,
    platform: Platform = "jobinja",
    company: str = "شرکت نمونه",
    days_ago: float = 1,
    **overrides: Any,
) -> JobPost:
    data: dict[str, Any] = {
        "platform": platform,
        "external_id": external_id,
        "title": title,
        "company": company,
        "city": "مشهد",
        "work_mode": WorkMode.ONSITE,
        "url": f"https://{platform}.ir/jobs/{external_id}",
        "status": status,
        "matched_profiles": [] if status == JobStatus.IGNORED else ["AI & Automation"],
        "created_at": _NOW - timedelta(days=days_ago),
    }
    data.update(overrides)
    return JobPost(**data)


def _seed_jobs() -> list[JobPost]:
    """A window of first-run-like jobs (30 days) plus older context jobs."""
    return [
        # Matches now and is a probable duplicate of jb-setup.
        _job("ign-match", "AI Engineer (LLM)", JobStatus.IGNORED, platform="jobvision", company=_SETUPTRACK_FA, days_ago=3),
        _job("jb-setup", "مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)", JobStatus.NOTIFIED, company=_SETUPTRACK, days_ago=2),
        _job("ign-none", "Accountant", JobStatus.IGNORED, days_ago=4),
        # Noise from the first run: "AI" once in the description.
        _job("new-noise", "پشتیبان وردپرس", JobStatus.NEW, raw_description="آشنایی با ابزارهای AI", days_ago=1),
        _job("new-good", "LLM Engineer", JobStatus.NEW, days_ago=1),
        _job("sent-noise", "C#/.NET Developer", JobStatus.NOTIFIED, raw_description="آشنایی با AI", days_ago=6),
        _job("skip-noise", "Digital Marketing", JobStatus.SKIPPED, raw_description="AI marketing", days_ago=7),
        # Its duplicate was first seen before the window (context).
        _job("new-asia", "LLM/RAG) AI Engineer-مشهد)", JobStatus.NEW, company=_ASIA, days_ago=5),
        _job("old-asia", "AI Engineer (LLM & RAG)", JobStatus.NOTIFIED, platform="jobvision", company=_ASIA_FA, days_ago=40),
        # A pair entirely before the window: never regrouped by this re-evaluation.
        _job("ctx-a", "AI Artist Senior", JobStatus.NOTIFIED, company="پانافر", days_ago=50),
        _job("ctx-b", "AI Artist Senior", JobStatus.NOTIFIED, company="پانافر | Panafor", days_ago=52),
    ]  # fmt: skip


def _split(jobs: Sequence[JobPost], since_days: int = 30) -> tuple[list[JobPost], list[JobPost]]:
    since = _NOW - timedelta(days=since_days)
    return [job for job in jobs if job.created_at >= since], [job for job in jobs if job.created_at < since]


def _settings(db_path: Path) -> Settings:
    return Settings(
        _env_file=None, TELEGRAM_BOT_TOKEN="123:test-token", TELEGRAM_CHAT_ID="@test_channel", DB_PATH=str(db_path)
    )


def _ids(jobs: Sequence[JobPost]) -> list[str]:
    return [job.external_id for job in jobs]


# ── build_reevaluation_plan (pure) ───────────────────────────────


def test_plan_has_the_four_sections() -> None:
    config = _config()
    window, context = _split(_seed_jobs())
    snapshot = [job.model_copy(deep=True) for job in [*window, *context]]

    plan = main.build_reevaluation_plan(window, context, RuleEngine(config), config)

    assert [(job.external_id, matched) for job, matched in plan.added] == [("ign-match", ["AI & Automation"])]
    assert _ids(plan.removed) == ["new-noise"]
    assert _ids(plan.report_only) == ["sent-noise", "skip-noise"]
    assert plan.group_changes == {
        ("jobvision", "ign-match"): "jobvision:ign-match",
        ("jobinja", "jb-setup"): "jobvision:ign-match",
        ("jobinja", "new-asia"): "jobvision:old-asia",
        ("jobvision", "old-asia"): "jobvision:old-asia",
    }
    assert [(group, _ids(members)) for group, members in plan.groups] == [
        ("jobvision:old-asia", ["old-asia", "new-asia"]),
        ("jobvision:ign-match", ["ign-match", "jb-setup"]),
    ]
    assert [*window, *context] == snapshot  # nothing was modified


def test_plan_keeps_stored_groups_and_skips_unchanged_jobs() -> None:
    config = _config()
    group = "jobinja:1"
    first = _job("1", "AI Engineer (LLM)", JobStatus.NOTIFIED, company=_SETUPTRACK, duplicate_group=group, days_ago=3)
    second = _job("2", "AI Engineer LLM", JobStatus.NOTIFIED, company=_SETUPTRACK, duplicate_group=group)
    plan = main.build_reevaluation_plan([first, second], [], RuleEngine(config), config)
    assert plan.group_changes == {}
    assert [(group, _ids(members)) for group, members in plan.groups] == [("jobinja:1", ["1", "2"])]
    assert (plan.added, plan.removed, plan.report_only) == ([], [], [])


def test_plan_of_an_empty_window() -> None:
    config = _config()
    plan = main.build_reevaluation_plan([], [], RuleEngine(config), config)
    assert (plan.added, plan.removed, plan.report_only, plan.group_changes, plan.groups) == ([], [], [], {}, [])


def test_plan_reevaluation_is_unchanged_for_ignored_jobs() -> None:
    config = _config()
    window, _ = _split(_seed_jobs())
    assert [job.external_id for job, _ in main.plan_reevaluation(window, RuleEngine(config))] == ["ign-match"]


# ── Dry-run text ─────────────────────────────────────────────────


def test_formatted_plan_lists_each_section_in_order() -> None:
    config = _config()
    window, context = _split(_seed_jobs())
    text = main._format_reevaluation_plan(main.build_reevaluation_plan(window, context, RuleEngine(config), config))

    headings = ["اضافه می‌شوند", "حذف می‌شوند", "دیگر تطبیق ندارند (فقط گزارش)", "گروه‌های تکراری"]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    added, removed, report_only, groups = (
        text[start:end] for start, end in zip(positions, [*positions[1:], len(text)], strict=True)
    )

    assert "ign-match" in added and _SETUPTRACK_FA in added and "AI & Automation" in added
    assert "1 IGNORED job(s) would be requeued" in added
    assert "new-noise" in removed and "پشتیبان وردپرس" in removed and "new-good" not in removed
    assert "sent-noise" in report_only and "NOTIFIED" in report_only and "skip-noise" in report_only
    assert "C#/.NET Developer" in report_only
    assert "2 group(s); 4 job(s) would get a new group" in groups
    assert "jobvision:old-asia" in groups and _ASIA in groups and "joins" in groups
    assert "ctx-a" not in text and "ctx-b" not in text and "ign-none" not in text
    assert added.splitlines()[1].split() == ["Platform", "ID", "Title", "Company", "Matched", "profiles"]
    assert removed.splitlines()[1].split() == ["Platform", "ID", "Title", "Company"]
    assert report_only.splitlines()[1].split() == ["Platform", "ID", "Title", "Company", "Status"]
    assert groups.splitlines()[1].split() == ["Group", "Platform", "ID", "Title", "Company", "Change"]


def test_formatted_plan_explains_empty_sections() -> None:
    empty = main.ReevaluationPlan(added=[], removed=[], report_only=[], group_changes={}, groups=[])
    text = main._format_reevaluation_plan(empty)
    assert "No IGNORED job in the window matches the current profiles." in text
    assert "Every unsent NEW job in the window still matches." in text
    assert "Every job in the window that was already notified still matches." in text
    assert "No probable duplicates among the jobs in the window." in text


def test_formatted_groups_show_moves_between_groups() -> None:
    member = _job("2", "AI Engineer", JobStatus.NOTIFIED, duplicate_group="jobinja:9")
    plan = main.ReevaluationPlan(
        added=[],
        removed=[],
        report_only=[],
        group_changes={("jobinja", "2"): "jobinja:1"},
        groups=[("jobinja:1", [_job("1", "AI Engineer", JobStatus.NOTIFIED, duplicate_group="jobinja:1"), member])],
    )
    text = main._format_reevaluation_plan(plan)
    assert "moves from jobinja:9" in text
    assert "1 group(s); 1 job(s) would get a new group" in text


# ── The run ──────────────────────────────────────────────────────


class _RecordingNotifier:
    """Stands in for `TelegramNotifier`; records each job and the duplicates it was sent with."""

    sent: list[tuple[str, list[str]]] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __aenter__(self) -> "_RecordingNotifier":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send_job(self, job: JobPost, *, duplicates: Sequence[JobPost] = ()) -> bool:
        self.sent.append((job.external_id, [duplicate.external_id for duplicate in duplicates]))
        return True


class _ExplodingNotifier:
    """Fails the test if the notifier is created at all."""

    def __init__(self, settings: Settings) -> None:
        raise AssertionError("the notifier must not be created in a dry run")


def _no_browser() -> None:
    raise AssertionError("--reevaluate must never launch a browser")


def _seed(db_path: Path) -> None:
    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            for job in _seed_jobs():
                await db.save_job(job)

    asyncio.run(scenario())


def _rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    finally:
        conn.close()


def _stored(db_path: Path) -> dict[str, JobPost]:
    async def scenario() -> dict[str, JobPost]:
        async with Database(db_path) as db:
            return {job.external_id: job for job in await db.get_jobs_since(_NOW - timedelta(days=365))}

    return asyncio.run(scenario())


def test_dry_run_prints_four_sections_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    before = _rows(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(main, "TelegramNotifier", _ExplodingNotifier)

    exit_code = asyncio.run(main._run_reevaluate(_settings(db_path), _config(), since_days=30, dry_run=True))

    assert exit_code == 0
    assert _rows(db_path) == before
    out = capsys.readouterr().out
    for heading in ("اضافه می‌شوند", "حذف می‌شوند", "دیگر تطبیق ندارند (فقط گزارش)", "گروه‌های تکراری"):
        assert heading in out
    assert "new-noise" in out and "sent-noise" in out and "jobvision:old-asia" in out


def test_dry_run_summary_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(main, "TelegramNotifier", _ExplodingNotifier)

    summary = asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=30, dry_run=True))

    assert (summary.window_jobs, summary.checked, summary.candidates) == (8, 2, 1)
    assert (summary.demote_candidates, summary.report_only, summary.group_changes) == (1, 2, 4)
    assert (summary.requeued, summary.demoted, summary.regrouped, summary.notified) == (0, 0, 0, 0)


def test_reevaluate_applies_the_plan_then_notifies_with_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(_RecordingNotifier, "sent", [])
    monkeypatch.setattr(main, "TelegramNotifier", _RecordingNotifier)

    summary = asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=30, dry_run=False))

    assert (summary.requeued, summary.demoted, summary.regrouped, summary.notified) == (1, 1, 4, 3)
    assert not summary.has_problems
    assert _RecordingNotifier.sent == [
        ("new-asia", ["old-asia"]),
        ("ign-match", ["jb-setup"]),
        ("new-good", []),
    ]
    stored = _stored(db_path)
    assert (stored["new-noise"].status, stored["new-noise"].matched_profiles) == (JobStatus.IGNORED, [])
    assert stored["ign-match"].status == JobStatus.NOTIFIED
    assert stored["sent-noise"].status == JobStatus.NOTIFIED  # report only
    assert stored["skip-noise"].status == JobStatus.SKIPPED
    assert {key: job.duplicate_group for key, job in stored.items() if job.duplicate_group} == {
        "ign-match": "jobvision:ign-match",
        "jb-setup": "jobvision:ign-match",
        "new-asia": "jobvision:old-asia",
        "old-asia": "jobvision:old-asia",
    }


def test_second_reevaluation_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(_RecordingNotifier, "sent", [])
    monkeypatch.setattr(main, "TelegramNotifier", _RecordingNotifier)
    asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=30, dry_run=False))
    monkeypatch.setattr(_RecordingNotifier, "sent", [])

    summary = asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=30, dry_run=False))

    assert (summary.requeued, summary.demoted, summary.regrouped, summary.notified) == (0, 0, 0, 0)
    assert summary.report_only == 2
    assert _RecordingNotifier.sent == []


# ── Pipeline grouping ────────────────────────────────────────────


def _scraped(platform: Platform, external_id: str, title: str, company: str) -> JobPost:
    """A job as a scraper returns it: NEW, no profiles, no group, created now."""
    return JobPost(
        platform=platform,
        external_id=external_id,
        title=title,
        company=company,
        city="تهران، تهران",
        work_mode=WorkMode.REMOTE,
        url=f"https://{platform}.ir/jobs/{external_id}",
    )


def test_save_jobs_groups_new_jobs_with_stored_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    config = _config()
    stored = _job("1535897", "AI Engineer (LLM)", JobStatus.NOTIFIED, platform="jobvision", company=_SETUPTRACK_FA)
    scraped = [
        _scraped("jobinja", "tNhZ", "مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)", _SETUPTRACK),
        _scraped("jobinja", "a1", "Data Engineer Python", _ASIA),
        _scraped("jobvision", "a2", "Data Engineer (Python)", _ASIA_FA),
        _scraped("jobinja", "x", "Accountant", "شرکت نمونه"),
    ]
    monkeypatch.setattr(_RecordingNotifier, "sent", [])
    monkeypatch.setattr(main, "TelegramNotifier", _RecordingNotifier)

    async def scenario() -> dict[Platform, main.PlatformSummary]:
        summaries: dict[Platform, main.PlatformSummary] = {}
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(stored)
            await main._save_jobs(db, RuleEngine(config), config, scraped, summaries)
            progress = main._RunProgress(started=0.0)
            await main._notify_pending(db, _settings(db_path), summaries, progress)
        return summaries

    summaries = asyncio.run(scenario())
    jobs = _stored(db_path)
    assert jobs["1535897"].duplicate_group == "jobvision:1535897"  # the stored job joined the group
    assert (jobs["tNhZ"].status, jobs["tNhZ"].duplicate_group) == (JobStatus.NOTIFIED, "jobvision:1535897")
    # Two new IGNORED postings of one position in the same run: grouped too.
    assert {jobs["a1"].duplicate_group, jobs["a2"].duplicate_group} == {"jobinja:a1"}
    assert jobs["a1"].status == JobStatus.IGNORED
    assert jobs["x"].duplicate_group is None
    assert (summaries["jobinja"].new, summaries["jobinja"].ignored, summaries["jobinja"].duplicates) == (1, 2, 2)
    assert summaries["jobvision"].duplicates == 1
    assert _RecordingNotifier.sent == [("tNhZ", ["1535897"])]


def test_save_jobs_ignores_stored_jobs_outside_the_window(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    config = _config()
    stored = _job(
        "1535897", "AI Engineer (LLM)", JobStatus.NOTIFIED, platform="jobvision", company=_SETUPTRACK_FA, days_ago=46
    )

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(stored)
            await main._save_jobs(
                db, RuleEngine(config), config, [_scraped("jobinja", "tNhZ", "AI Engineer (LLM)", _SETUPTRACK)], {}
            )

    asyncio.run(scenario())
    assert {job.duplicate_group for job in _stored(db_path).values()} == {None}


def test_save_jobs_keeps_the_name_of_a_stored_group_whose_anchor_is_old(tmp_path: Path) -> None:
    """The group's earliest member is outside the window but still names the group."""
    db_path = tmp_path / "jobs.db"
    config = _config()
    anchor = _job("v1", "AI Engineer (LLM)", JobStatus.APPLIED, platform="jobvision", company=_SETUPTRACK_FA,
                  days_ago=80, duplicate_group="jobvision:v1")  # fmt: skip
    member = _job("j1", "AI Engineer (LLM)", JobStatus.NOTIFIED, company=_SETUPTRACK, days_ago=40,
                  duplicate_group="jobvision:v1")  # fmt: skip

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(anchor)
            await db.save_job(member)
            await main._save_jobs(
                db, RuleEngine(config), config, [_scraped("jobinja", "j2", "AI Engineer LLM", _SETUPTRACK)], {}
            )

    asyncio.run(scenario())
    assert {key: job.duplicate_group for key, job in _stored(db_path).items()} == {
        "v1": "jobvision:v1",
        "j1": "jobvision:v1",
        "j2": "jobvision:v1",
    }


def test_save_jobs_still_saves_when_grouping_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    config = _config()

    def broken(*args: object) -> dict[tuple[str, str], str]:
        raise RuntimeError("grouping bug")

    monkeypatch.setattr(main, "assign_duplicate_groups", broken)

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await main._save_jobs(
                db, RuleEngine(config), config, [_scraped("jobinja", "1", "AI Engineer (LLM)", _SETUPTRACK)], {}
            )

    asyncio.run(scenario())
    jobs = _stored(db_path)
    assert (jobs["1"].status, jobs["1"].duplicate_group) == (JobStatus.NEW, None)


def test_parse_args_help_mentions_the_extended_reevaluate(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main._parse_args(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "demote unsent NEW jobs" in help_text
