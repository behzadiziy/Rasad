"""Tests for the Task 7 database additions: versioned migrations and the new methods.

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
from src.models import ApplicationRecord, JobPost, JobStatus, TailoredResumeRecord, WorkMode

_LATEST_VERSION = 2

# The Phase 1 schema exactly as Task 2 created it, before versioning existed.
_PHASE1_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    city TEXT NOT NULL,
    work_mode TEXT NOT NULL,
    salary TEXT,
    url TEXT NOT NULL,
    raw_description TEXT,
    matched_profiles TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_PHASE1_ROWS = [
    (
        "jobinja", "j1", "مهندس هوش مصنوعی", "شرکت نمونه", "مشهد", "REMOTE", None,
        "https://jobinja.ir/jobs/j1", "توضیحات", '["AI & Automation"]', "NOTIFIED",
        "2026-01-01T08:00:00.000000+00:00", "2026-01-01T09:00:00.000000+00:00",
    ),
    (
        "jobvision", "v1", "Data Analyst", "Example Co", "Tehran", "ONSITE", "توافقی",
        "https://jobvision.ir/jobs/v1", None, "[]", "IGNORED",
        "2026-01-02T08:00:00.000000+00:00", "2026-01-02T08:00:00.000000+00:00",
    ),
]  # fmt: skip


def _job(**overrides: Any) -> JobPost:
    """Build a valid job post; keyword arguments override the defaults."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "1001",
        "title": "مهندس هوش مصنوعی",
        "company": "شرکت نمونه",
        "city": "مشهد",
        "work_mode": WorkMode.REMOTE,
        "url": "https://jobinja.ir/jobs/1001",
        "matched_profiles": ["AI & Automation"],
    }
    data.update(overrides)
    return JobPost(**data)


def _resume(**overrides: Any) -> TailoredResumeRecord:
    """Build a tailored resume record for a fictional person."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "1001",
        "version": 1,
        "language": "fa",
        "llm_model": "gemini-test-flash",
        "plan_json": json.dumps({"summary": "Sara Example, ML engineer"}),
        "changes_json": json.dumps({"changes": []}),
    }
    data.update(overrides)
    return TailoredResumeRecord(**data)


def _make_phase1_db(path: Path, *, user_version: int = 0) -> None:
    """Create a database with the Phase 1 schema and two rows, as a pre-Task 7 install would have."""
    with sqlite3.connect(path) as conn:
        conn.executescript(_PHASE1_SCHEMA)
        conn.executemany(
            "INSERT INTO jobs (platform, external_id, title, company, city, work_mode, salary, url, "
            "raw_description, matched_profiles, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _PHASE1_ROWS,
        )
        conn.execute(f"PRAGMA user_version = {user_version}")
    conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _rows(path: Path, table: str = "jobs") -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        conn.close()


def _schema(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def _init(path: Path) -> None:
    async def scenario() -> None:
        async with Database(path) as db:
            await db.init_db()

    asyncio.run(scenario())


# ── Migrations ───────────────────────────────────────────────────


def test_fresh_database_gets_latest_schema_without_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _init(db_path)

    assert _user_version(db_path) == _LATEST_VERSION
    names = {name for _, name, _ in _schema(db_path)}
    assert {
        "jobs",
        "idx_jobs_status",
        "tailored_resumes",
        "idx_tailored_resumes_job",
        "applications",
        "idx_applications_job",
        "idx_applications_attempted_at",
    } <= names
    assert list(tmp_path.glob("*.bak-v*")) == []


def test_v0_database_migrates_with_rows_preserved_and_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path)
    jobs_schema_before = [entry for entry in _schema(db_path) if entry[1] in ("jobs", "idx_jobs_status")]

    _init(db_path)

    assert _user_version(db_path) == _LATEST_VERSION
    assert [row[1:] for row in _rows(db_path)] == _PHASE1_ROWS
    assert [entry for entry in _schema(db_path) if entry[1] in ("jobs", "idx_jobs_status")] == jobs_schema_before
    assert _rows(db_path, "tailored_resumes") == []
    assert _rows(db_path, "applications") == []

    backup = tmp_path / "jobs.db.bak-v0"
    assert backup.exists()
    assert _user_version(backup) == 0
    assert [row[1:] for row in _rows(backup)] == _PHASE1_ROWS
    assert "tailored_resumes" not in {name for _, name, _ in _schema(backup)}
    assert not (tmp_path / "jobs.db.bak-v0.partial").exists()


def test_migrated_v0_database_works_with_existing_methods(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path)

    async def scenario() -> tuple[bool, JobPost | None, list[JobPost]]:
        async with Database(db_path) as db:
            await db.init_db()
            exists = await db.job_exists("jobinja", "j1")
            job = await db.get_job("jobinja", "j1")
            ignored = await db.get_jobs_by_status(JobStatus.IGNORED)
            return exists, job, ignored

    exists, job, ignored = asyncio.run(scenario())
    assert exists is True
    assert job is not None and job.status == JobStatus.NOTIFIED
    assert job.matched_profiles == ["AI & Automation"]
    assert [item.external_id for item in ignored] == ["v1"]


def test_running_migrations_twice_is_a_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path)
    _init(db_path)
    backup = tmp_path / "jobs.db.bak-v0"
    backup.unlink()
    schema_after_first = _schema(db_path)
    rows_after_first = _rows(db_path)

    _init(db_path)

    async def same_connection_twice() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await db.init_db()

    asyncio.run(same_connection_twice())
    assert _user_version(db_path) == _LATEST_VERSION
    assert _schema(db_path) == schema_after_first
    assert _rows(db_path) == rows_after_first
    assert list(tmp_path.glob("*.bak-v*")) == []


def test_partially_migrated_database_is_backed_up_at_its_version(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path, user_version=1)
    _init(db_path)
    assert _user_version(db_path) == _LATEST_VERSION
    assert (tmp_path / "jobs.db.bak-v1").exists()
    assert _user_version(tmp_path / "jobs.db.bak-v1") == 1


def test_existing_backup_is_replaced(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path)
    backup = tmp_path / "jobs.db.bak-v0"
    backup.write_bytes(b"stale")
    _init(db_path)
    assert [row[1:] for row in _rows(backup)] == _PHASE1_ROWS


def test_future_schema_version_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path, user_version=_LATEST_VERSION + 1)
    schema_before = _schema(db_path)

    with pytest.raises(RuntimeError, match=r"schema version 3.*supports versions up to 2"):
        _init(db_path)

    assert _user_version(db_path) == _LATEST_VERSION + 1
    assert _schema(db_path) == schema_before
    assert list(tmp_path.glob("*.bak-v*")) == []


def test_failed_migration_rolls_back_to_previous_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _make_phase1_db(db_path)
    broken_v2 = database._Migration(
        2,
        "broken",
        (
            "CREATE TABLE half_done (id INTEGER PRIMARY KEY)",
            "INSERT INTO jobs (no_such_column) VALUES (1)",
        ),
    )
    monkeypatch.setattr(database, "_MIGRATIONS", (database._MIGRATIONS[0], broken_v2))

    with pytest.raises(sqlite3.OperationalError):
        _init(db_path)

    assert _user_version(db_path) == 1
    assert "half_done" not in {name for _, name, _ in _schema(db_path)}
    assert [row[1:] for row in _rows(db_path)] == _PHASE1_ROWS

    monkeypatch.undo()
    _init(db_path)
    assert _user_version(db_path) == _LATEST_VERSION
    assert (tmp_path / "jobs.db.bak-v1").exists()


# ── Jobs ─────────────────────────────────────────────────────────


def test_get_job(tmp_path: Path) -> None:
    async def scenario() -> tuple[JobPost | None, JobPost | None]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(salary="توافقی", raw_description="متن"))
            return await db.get_job("jobinja", "1001"), await db.get_job("jobinja", "missing")

    found, missing = asyncio.run(scenario())
    assert found == _job(salary="توافقی", raw_description="متن", created_at=found.created_at)
    assert missing is None


def test_get_jobs_by_status_with_since(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    async def scenario() -> tuple[list[str], list[str], list[str]]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(external_id="old", status=JobStatus.IGNORED, created_at=now - timedelta(days=40)))
            await db.save_job(_job(external_id="edge", status=JobStatus.IGNORED, created_at=now - timedelta(days=30)))
            await db.save_job(_job(external_id="recent", status=JobStatus.IGNORED, created_at=now - timedelta(days=1)))
            await db.save_job(_job(external_id="new", status=JobStatus.NEW, created_at=now))
            every = await db.get_jobs_by_status(JobStatus.IGNORED)
            windowed = await db.get_jobs_by_status(JobStatus.IGNORED, since=now - timedelta(days=30))
            new = await db.get_jobs_by_status(JobStatus.NEW, since=now - timedelta(days=30))
            return (
                [job.external_id for job in every],
                [job.external_id for job in windowed],
                [job.external_id for job in new],
            )

    every, windowed, new = asyncio.run(scenario())
    assert every == ["old", "edge", "recent"]
    assert windowed == ["edge", "recent"]
    assert new == ["new"]


def test_transition_status_compare_and_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    moved_at = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

    async def scenario() -> tuple[bool, bool, bool, JobPost | None]:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job(status=JobStatus.NOTIFIED))
            monkeypatch.setattr(database, "_utcnow", lambda: moved_at)
            first = await db.transition_status("jobinja", "1001", [JobStatus.NOTIFIED], JobStatus.TAILORED)
            stale = await db.transition_status("jobinja", "1001", [JobStatus.NOTIFIED], JobStatus.TAILORED)
            unknown = await db.transition_status("jobinja", "missing", [JobStatus.NOTIFIED], JobStatus.TAILORED)
            return first, stale, unknown, await db.get_job("jobinja", "1001")

    first, stale, unknown, job = asyncio.run(scenario())
    assert (first, stale, unknown) == (True, False, False)
    assert job is not None and job.status == JobStatus.TAILORED
    conn = sqlite3.connect(db_path)
    try:
        updated_at = conn.execute("SELECT updated_at FROM jobs WHERE external_id = '1001'").fetchone()[0]
    finally:
        conn.close()
    assert datetime.fromisoformat(updated_at) == moved_at


def test_transition_status_accepts_any_listed_source(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool, JobStatus | None]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(status=JobStatus.TAILORED))
            regenerated = await db.transition_status("jobinja", "1001", {JobStatus.TAILORED}, JobStatus.TAILORED)
            skipped = await db.transition_status(
                "jobinja", "1001", (JobStatus.TAILORED, JobStatus.APPLY_FAILED), JobStatus.SKIPPED
            )
            job = await db.get_job("jobinja", "1001")
            return regenerated, skipped, job.status if job else None

    assert asyncio.run(scenario()) == (True, True, JobStatus.SKIPPED)


@pytest.mark.parametrize(
    ("from_statuses", "to"),
    [
        ([JobStatus.NOTIFIED], JobStatus.APPLIED),
        ([JobStatus.TAILORED, JobStatus.NEW], JobStatus.APPROVED),
        ([JobStatus.APPLIED], JobStatus.SKIPPED),
        ([], JobStatus.NOTIFIED),
    ],
)
def test_transition_status_rejects_disallowed_pairs(
    tmp_path: Path, from_statuses: list[JobStatus], to: JobStatus
) -> None:
    async def scenario() -> JobStatus | None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(status=JobStatus.NOTIFIED))
            with pytest.raises(ValueError):
                await db.transition_status("jobinja", "1001", from_statuses, to)
            job = await db.get_job("jobinja", "1001")
            return job.status if job else None

    assert asyncio.run(scenario()) == JobStatus.NOTIFIED


def test_transition_status_race_on_one_connection(tmp_path: Path) -> None:
    async def scenario() -> list[bool]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(status=JobStatus.TAILORED))
            return list(
                await asyncio.gather(
                    db.transition_status("jobinja", "1001", [JobStatus.TAILORED], JobStatus.APPROVED),
                    db.transition_status("jobinja", "1001", [JobStatus.TAILORED], JobStatus.APPROVED),
                )
            )

    assert sorted(asyncio.run(scenario())) == [False, True]


def test_transition_status_race_across_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"

    async def scenario() -> tuple[list[bool], JobStatus | None]:
        async with Database(db_path) as setup:
            await setup.init_db()
            await setup.save_job(_job(status=JobStatus.TAILORED))
        async with Database(db_path) as first, Database(db_path) as second:
            await first.init_db()
            await second.init_db()
            results = await asyncio.gather(
                first.transition_status("jobinja", "1001", [JobStatus.TAILORED], JobStatus.APPROVED),
                second.transition_status("jobinja", "1001", [JobStatus.TAILORED], JobStatus.SKIPPED),
            )
            job = await first.get_job("jobinja", "1001")
            return list(results), job.status if job else None

    results, status = asyncio.run(scenario())
    assert sorted(results) == [False, True]
    assert status == (JobStatus.APPROVED if results[0] else JobStatus.SKIPPED)


def test_requeue_ignored(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool, bool, JobPost | None, JobPost | None, list[str]]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(external_id="ign", status=JobStatus.IGNORED, matched_profiles=[]))
            await db.save_job(_job(external_id="sent", status=JobStatus.NOTIFIED))
            requeued = await db.requeue_ignored("jobinja", "ign", ["AI & Automation", "پروفایل دوم"])
            again = await db.requeue_ignored("jobinja", "ign", ["AI & Automation"])
            not_ignored = await db.requeue_ignored("jobinja", "sent", ["Other"])
            pending = [job.external_id for job in await db.get_pending_notifications()]
            return (
                requeued,
                again,
                not_ignored,
                await db.get_job("jobinja", "ign"),
                await db.get_job("jobinja", "sent"),
                pending,
            )

    requeued, again, not_ignored, ignored_job, sent_job, pending = asyncio.run(scenario())
    assert (requeued, again, not_ignored) == (True, False, False)
    assert ignored_job is not None
    assert ignored_job.status == JobStatus.NEW
    assert ignored_job.matched_profiles == ["AI & Automation", "پروفایل دوم"]
    assert sent_job is not None
    assert sent_job.status == JobStatus.NOTIFIED
    assert sent_job.matched_profiles == ["AI & Automation"]
    assert pending == ["ign"]


def test_requeue_ignored_requires_profiles(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(status=JobStatus.IGNORED, matched_profiles=[]))
            with pytest.raises(ValueError):
                await db.requeue_ignored("jobinja", "1001", [])
            job = await db.get_job("jobinja", "1001")
            assert job is not None and job.status == JobStatus.IGNORED

    asyncio.run(scenario())


# ── Tailored resumes ─────────────────────────────────────────────


def test_tailored_resume_lifecycle(tmp_path: Path) -> None:
    created = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)

    async def scenario() -> dict[str, Any]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job())
            await db.save_job(_job(external_id="other"))
            empty_count = await db.count_tailored_resumes("jobinja", "1001")
            no_latest = await db.get_latest_tailored_resume("jobinja", "1001")
            first_id = await db.save_tailored_resume(_resume(created_at=created))
            second_id = await db.save_tailored_resume(_resume(version=2, language="en"))
            await db.save_tailored_resume(_resume(external_id="other"))
            await db.update_tailored_resume_paths(first_id, "data/resumes/a.pdf", "data/resumes/a-review.pdf")
            return {
                "ids": (first_id, second_id),
                "empty_count": empty_count,
                "no_latest": no_latest,
                "first": await db.get_tailored_resume(first_id),
                "latest": await db.get_latest_tailored_resume("jobinja", "1001"),
                "count": await db.count_tailored_resumes("jobinja", "1001"),
                "missing": await db.get_tailored_resume(9999),
            }

    result = asyncio.run(scenario())
    first_id, second_id = result["ids"]
    assert first_id != second_id
    assert result["empty_count"] == 0
    assert result["no_latest"] is None
    first = result["first"]
    assert first == _resume(
        id=first_id,
        created_at=created,
        clean_pdf_path="data/resumes/a.pdf",
        review_pdf_path="data/resumes/a-review.pdf",
    )
    assert result["latest"].id == second_id
    assert result["latest"].version == 2
    assert result["latest"].language == "en"
    assert result["latest"].created_at.tzinfo is not None
    assert result["count"] == 2
    assert result["missing"] is None


def test_tailored_resume_unique_version_and_job_foreign_key(tmp_path: Path) -> None:
    async def scenario() -> int:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job())
            await db.save_tailored_resume(_resume())
            with pytest.raises(sqlite3.IntegrityError):
                await db.save_tailored_resume(_resume())
            with pytest.raises(sqlite3.IntegrityError):
                await db.save_tailored_resume(_resume(external_id="unknown-job"))
            with pytest.raises(ValueError):
                await db.save_tailored_resume(_resume(id=5, version=2))
            return await db.count_tailored_resumes("jobinja", "1001")

    assert asyncio.run(scenario()) == 1


def test_mark_tailored_resume_approved_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_approval = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    second_approval = datetime(2026, 6, 2, 9, 5, tzinfo=UTC)

    async def scenario() -> TailoredResumeRecord | None:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job())
            resume_id = await db.save_tailored_resume(_resume())
            monkeypatch.setattr(database, "_utcnow", lambda: first_approval)
            await db.mark_tailored_resume_approved(resume_id)
            monkeypatch.setattr(database, "_utcnow", lambda: second_approval)
            await db.mark_tailored_resume_approved(resume_id)
            with pytest.raises(LookupError):
                await db.mark_tailored_resume_approved(9999)
            with pytest.raises(LookupError):
                await db.update_tailored_resume_paths(9999, "a.pdf", "b.pdf")
            return await db.get_tailored_resume(resume_id)

    record = asyncio.run(scenario())
    assert record is not None
    assert record.approved_at == first_approval


# ── Applications ─────────────────────────────────────────────────


def test_applications(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    day = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)

    async def scenario() -> dict[str, Any]:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job())
            await db.save_job(_job(external_id="2002"))
            resume_id = await db.save_tailored_resume(_resume())
            other_resume = await db.save_tailored_resume(_resume(external_id="2002"))
            before = await db.has_submitted_application("jobinja", "1001")
            failed_id = await db.record_application(
                ApplicationRecord(
                    platform="jobinja",
                    external_id="1001",
                    resume_id=resume_id,
                    attempted_at=day - timedelta(hours=1),
                    submitted=False,
                    error="unexpected page state",
                )
            )
            after_failure = await db.has_submitted_application("jobinja", "1001")
            await db.record_application(
                ApplicationRecord(
                    platform="jobinja",
                    external_id="1001",
                    resume_id=resume_id,
                    attempted_at=day + timedelta(hours=2),
                    submitted=True,
                    receipt_path="data/applications/1001.png",
                )
            )
            await db.record_application(
                ApplicationRecord(
                    platform="jobinja",
                    external_id="2002",
                    resume_id=other_resume,
                    attempted_at=day - timedelta(days=1),
                    submitted=True,
                )
            )
            with pytest.raises(sqlite3.IntegrityError):
                await db.record_application(
                    ApplicationRecord(platform="jobinja", external_id="1001", resume_id=9999, submitted=True)
                )
            with pytest.raises(ValueError):
                await db.record_application(
                    ApplicationRecord(id=1, platform="jobinja", external_id="1001", resume_id=resume_id, submitted=True)
                )
            return {
                "failed_id": failed_id,
                "before": before,
                "after_failure": after_failure,
                "after_success": await db.has_submitted_application("jobinja", "1001"),
                "unknown": await db.has_submitted_application("jobvision", "1001"),
                "since_day": await db.count_submitted_applications_since(day),
                "since_two_days": await db.count_submitted_applications_since(day - timedelta(days=2)),
            }

    result = asyncio.run(scenario())
    assert result["failed_id"] >= 1
    assert (result["before"], result["after_failure"], result["after_success"]) == (False, False, True)
    assert result["unknown"] is False
    assert result["since_day"] == 1
    assert result["since_two_days"] == 2

    rows = _rows(db_path, "applications")
    assert [row[5] for row in rows] == [0, 1, 1]
    assert all(isinstance(row[5], int) for row in rows)
    assert rows[0][7] == "unexpected page state"
