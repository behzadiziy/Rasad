"""Tests for the Task 7b database changes: migration v3 and the duplicate and re-evaluation methods.

Every database lives under `tmp_path`; resume fixtures use a fictional person.
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import database
from src.database import Database
from src.models import JobPost, JobStatus, TailoredResumeRecord, WorkMode

_T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

_PHASE1_COLUMNS = [
    "id", "platform", "external_id", "title", "company", "city", "work_mode", "salary", "url",
    "raw_description", "matched_profiles", "status", "created_at", "updated_at",
]  # fmt: skip

# Two jobs of a v2 database (as Task 7 left it), with one tailored resume.
_V2_ROWS = [
    (
        "jobinja", "j1", "مهندس هوش مصنوعی", "شرکت نمونه", "مشهد", "REMOTE", None,
        "https://jobinja.ir/jobs/j1", "توضیحات", '["AI & Automation"]', "TAILORED",
        "2026-01-01T08:00:00.000000+00:00", "2026-01-01T09:00:00.000000+00:00",
    ),
    (
        "jobvision", "v1", "Data Analyst", "Example Co", "Tehran", "ONSITE", "توافقی",
        "https://jobvision.ir/jobs/v1", None, "[]", "IGNORED",
        "2026-01-02T08:00:00.000000+00:00", "2026-01-02T08:00:00.000000+00:00",
    ),
]  # fmt: skip


def _job(external_id: str, *, days: float = 0, **overrides: Any) -> JobPost:
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": external_id,
        "title": "مهندس هوش مصنوعی",
        "company": "شرکت نمونه",
        "city": "مشهد",
        "work_mode": WorkMode.REMOTE,
        "url": f"https://jobinja.ir/jobs/{external_id}",
        "matched_profiles": ["AI & Automation"],
        "created_at": _T0 + timedelta(days=days),
    }
    data.update(overrides)
    return JobPost(**data)


def _make_v2_db(path: Path) -> None:
    """Create a database at schema version 2 (migrations v1 and v2 only) with rows in every table."""
    with sqlite3.connect(path) as conn:
        for migration in database._MIGRATIONS[:2]:
            for statement in migration.statements:
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO jobs (platform, external_id, title, company, city, work_mode, salary, url, "
            "raw_description, matched_profiles, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _V2_ROWS,
        )
        conn.execute(
            "INSERT INTO tailored_resumes (platform, external_id, version, language, llm_model, plan_json, "
            "changes_json, created_at) VALUES ('jobinja', 'j1', 1, 'fa', 'gemini-test-flash', "
            "'{\"summary\": \"Sara Example\"}', '{\"changes\": []}', '2026-01-01T10:00:00.000000+00:00')"
        )
        conn.execute("PRAGMA user_version = 2")
    conn.close()


def _query(path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _init(path: Path) -> None:
    async def scenario() -> None:
        async with Database(path) as db:
            await db.init_db()

    asyncio.run(scenario())


# ── Migration v3 ─────────────────────────────────────────────────


def test_v2_database_migrates_to_v3_with_rows_preserved(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_v2_db(db_path)
    resumes_before = _query(db_path, "SELECT * FROM tailored_resumes")

    _init(db_path)

    assert _query(db_path, "PRAGMA user_version") == [(3,)]
    columns = [row[1] for row in _query(db_path, "PRAGMA table_info(jobs)")]
    assert columns == [*_PHASE1_COLUMNS, "duplicate_group"]
    assert [row[1:-1] for row in _query(db_path, "SELECT * FROM jobs ORDER BY id")] == _V2_ROWS
    assert _query(db_path, "SELECT duplicate_group FROM jobs") == [(None,), (None,)]
    assert _query(db_path, "SELECT * FROM tailored_resumes") == resumes_before
    indexes = {row[1] for row in _query(db_path, "PRAGMA index_list(jobs)")}
    assert {"idx_jobs_status", "idx_jobs_duplicate_group"} <= indexes
    assert _query(db_path, "PRAGMA index_info(idx_jobs_duplicate_group)")[0][2] == "duplicate_group"

    backup = tmp_path / "jobs.db.bak-v2"
    assert _query(backup, "PRAGMA user_version") == [(2,)]
    assert "duplicate_group" not in [row[1] for row in _query(backup, "PRAGMA table_info(jobs)")]
    assert [row[1:] for row in _query(backup, "SELECT * FROM jobs ORDER BY id")] == _V2_ROWS


def test_migrated_v2_rows_read_back_without_a_group(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_v2_db(db_path)

    async def scenario() -> JobPost | None:
        async with Database(db_path) as db:
            await db.init_db()
            return await db.get_job("jobinja", "j1")

    job = asyncio.run(scenario())
    assert job is not None
    assert (job.status, job.duplicate_group, job.matched_profiles) == (JobStatus.TAILORED, None, ["AI & Automation"])


def test_migration_v3_runs_once(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_v2_db(db_path)
    _init(db_path)
    (tmp_path / "jobs.db.bak-v2").unlink()
    _init(db_path)
    assert _query(db_path, "PRAGMA user_version") == [(3,)]
    assert list(tmp_path.glob("*.bak-v*")) == []


def test_fresh_database_has_the_column_and_index(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    _init(db_path)
    assert "duplicate_group" in [row[1] for row in _query(db_path, "PRAGMA table_info(jobs)")]
    assert "idx_jobs_duplicate_group" in {row[1] for row in _query(db_path, "PRAGMA index_list(jobs)")}


def test_v3_database_is_refused_by_v2_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _init(db_path)
    monkeypatch.setattr(database, "_MIGRATIONS", database._MIGRATIONS[:2])
    with pytest.raises(RuntimeError, match=r"schema version 3.*supports versions up to 2"):
        _init(db_path)


# ── Reading and writing duplicate_group ──────────────────────────


def test_save_job_and_every_read_include_the_group(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job("1", duplicate_group="jobinja:1"))
            await db.save_job(_job("2", days=1, duplicate_group="jobinja:1", status=JobStatus.IGNORED))
            await db.save_job(_job("3", days=2))
            return {
                "get": await db.get_job("jobinja", "1"),
                "pending": await db.get_pending_notifications(),
                "by_status": await db.get_jobs_by_status(JobStatus.IGNORED),
                "since": await db.get_jobs_since(_T0),
            }

    result = asyncio.run(scenario())
    assert result["get"].duplicate_group == "jobinja:1"
    assert [(job.external_id, job.duplicate_group) for job in result["pending"]] == [("1", "jobinja:1"), ("3", None)]
    assert [(job.external_id, job.duplicate_group) for job in result["by_status"]] == [("2", "jobinja:1")]
    assert [job.duplicate_group for job in result["since"]] == ["jobinja:1", "jobinja:1", None]


def test_set_duplicate_group_and_members(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job("late", days=5))
            await db.save_job(_job("early", days=1, status=JobStatus.NOTIFIED))
            await db.save_job(_job("other", days=2))
            await db.save_job(_job("jv", days=3, platform="jobvision", status=JobStatus.APPLIED))
            for platform, external_id in (("jobinja", "late"), ("jobinja", "early"), ("jobvision", "jv")):
                await db.set_duplicate_group(platform, external_id, "jobinja:early")
            await db.set_duplicate_group("jobinja", "other", "jobinja:other")
            members = await db.get_duplicate_group_members("jobinja:early")
            await db.set_duplicate_group("jobinja", "late", None)
            await db.set_duplicate_group("jobinja", "missing", "jobinja:early")  # logged, no error
            return {
                "members": members,
                "after_removal": await db.get_duplicate_group_members("jobinja:early"),
                "late": await db.get_job("jobinja", "late"),
                "unknown_group": await db.get_duplicate_group_members("jobinja:nope"),
            }

    result = asyncio.run(scenario())
    assert [(job.platform, job.external_id, job.status) for job in result["members"]] == [
        ("jobinja", "early", JobStatus.NOTIFIED),
        ("jobvision", "jv", JobStatus.APPLIED),
        ("jobinja", "late", JobStatus.NEW),
    ]
    assert [job.external_id for job in result["after_removal"]] == ["early", "jv"]
    assert result["late"].duplicate_group is None
    assert result["unknown_group"] == []


def test_set_duplicate_group_refreshes_updated_at(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job("1"))
            await db.set_duplicate_group("jobinja", "1", "jobinja:1")

    before = datetime.now(UTC)
    asyncio.run(scenario())
    (updated_at,) = _query(db_path, "SELECT updated_at FROM jobs")[0]
    assert datetime.fromisoformat(updated_at) >= before


@pytest.mark.parametrize("group", ["", "   "])
def test_set_duplicate_group_rejects_blank_ids(tmp_path: Path, group: str) -> None:
    async def scenario() -> None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.set_duplicate_group("jobinja", "1", group)

    with pytest.raises(ValueError, match="non-blank"):
        asyncio.run(scenario())


# ── get_jobs_since ───────────────────────────────────────────────


def test_get_jobs_since_returns_every_status_oldest_first(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[JobPost], list[JobPost]]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job("old", days=-10))
            await db.save_job(_job("c", days=3, status=JobStatus.APPLIED))
            await db.save_job(_job("a", days=0, status=JobStatus.IGNORED, matched_profiles=[]))
            await db.save_job(_job("b", days=1, status=JobStatus.NOTIFIED))
            return await db.get_jobs_since(_T0), await db.get_jobs_since(_T0 + timedelta(days=30))

    window, empty = asyncio.run(scenario())
    assert [(job.external_id, job.status) for job in window] == [
        ("a", JobStatus.IGNORED),
        ("b", JobStatus.NOTIFIED),
        ("c", JobStatus.APPLIED),
    ]
    assert empty == []


# ── demote_new ───────────────────────────────────────────────────


def test_demote_new_is_a_compare_and_set(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"

    async def scenario() -> dict[str, Any]:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job("new"))
            await db.save_job(_job("sent", status=JobStatus.NOTIFIED))
            first = await db.demote_new("jobinja", "new")
            second = await db.demote_new("jobinja", "new")
            return {
                "first": first,
                "second": second,
                "sent": await db.demote_new("jobinja", "sent"),
                "unknown": await db.demote_new("jobinja", "missing"),
                "new_job": await db.get_job("jobinja", "new"),
                "sent_job": await db.get_job("jobinja", "sent"),
            }

    result = asyncio.run(scenario())
    assert (result["first"], result["second"], result["sent"], result["unknown"]) == (True, False, False, False)
    assert (result["new_job"].status, result["new_job"].matched_profiles) == (JobStatus.IGNORED, [])
    assert (result["sent_job"].status, result["sent_job"].matched_profiles) == (JobStatus.NOTIFIED, ["AI & Automation"])
    assert _query(db_path, "SELECT matched_profiles FROM jobs WHERE external_id = 'new'") == [("[]",)]


def test_demote_new_races_with_a_notification(tmp_path: Path) -> None:
    """Two connections race: whichever runs first wins, and the other changes nothing."""
    db_path = tmp_path / "jobs.db"

    async def scenario() -> tuple[bool, bool, JobPost | None]:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job("race"))
        async with Database(db_path) as first, Database(db_path) as second:
            demoted, notified = await asyncio.gather(
                first.demote_new("jobinja", "race"),
                second.transition_status("jobinja", "race", [JobStatus.NEW], JobStatus.NOTIFIED),
            )
            return demoted, notified, await first.get_job("jobinja", "race")

    demoted, notified, job = asyncio.run(scenario())
    assert demoted != notified
    assert job is not None
    assert job.status == (JobStatus.IGNORED if demoted else JobStatus.NOTIFIED)


def test_demote_then_requeue_round_trip(tmp_path: Path) -> None:
    async def scenario() -> JobPost | None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job("1"))
            assert await db.demote_new("jobinja", "1")
            assert await db.requeue_ignored("jobinja", "1", ["Other"])
            return await db.get_job("jobinja", "1")

    job = asyncio.run(scenario())
    assert job is not None
    assert (job.status, job.matched_profiles) == (JobStatus.NEW, ["Other"])


def test_resume_records_still_work_after_v3(tmp_path: Path) -> None:
    async def scenario() -> int:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job("1", duplicate_group="jobinja:1"))
            return await db.save_tailored_resume(
                TailoredResumeRecord(
                    platform="jobinja",
                    external_id="1",
                    version=1,
                    language="fa",
                    llm_model="gemini-test-flash",
                    plan_json=json.dumps({"summary": "Sara Example"}),
                    changes_json=json.dumps({"changes": []}),
                )
            )

    assert asyncio.run(scenario()) == 1
