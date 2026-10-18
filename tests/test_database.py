"""Tests for the async SQLite `Database` (exercised with `asyncio.run`)."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src import database
from src.database import Database
from src.models import JobPost, JobStatus, WorkMode


def _job(**overrides: Any) -> JobPost:
    """Build a valid job post; keyword arguments override the defaults."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "1001",
        "title": "مهندس هوش مصنوعی",
        "company": "شرکت نمونه",
        "city": "مشهد",
        "work_mode": WorkMode.REMOTE,
        "salary": "توافقی",
        "url": "https://jobinja.ir/jobs/1001",
        "raw_description": "توضیحات آگهی",
        "matched_profiles": ["AI & Automation"],
    }
    data.update(overrides)
    return JobPost(**data)


def _read_column(db_path: Path, column: str, external_id: str) -> Any:
    """Read one column of a stored job with the stdlib driver."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f"SELECT {column} FROM jobs WHERE external_id = ?", (external_id,)).fetchone()
    assert row is not None
    return row[0]


def test_connect_creates_parent_dir_and_enables_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "jobs.db"

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()

    asyncio.run(scenario())
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        index_names = {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}
    assert "idx_jobs_status" in index_names


def test_save_new_returns_true_and_duplicate_returns_false(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            first = await db.save_job(_job())
            second = await db.save_job(_job(title="عنوان دیگر"))
            return first, second

    assert asyncio.run(scenario()) == (True, False)


def test_ignored_job_is_persisted_and_exists(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool, bool, list[JobPost]]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            saved = await db.save_job(_job(status=JobStatus.IGNORED, matched_profiles=[]))
            exists = await db.job_exists("jobinja", "1001")
            other_platform = await db.job_exists("jobvision", "1001")
            return saved, exists, other_platform, await db.get_pending_notifications()

    saved, exists, other_platform, pending = asyncio.run(scenario())
    assert saved is True
    assert exists is True
    assert other_platform is False
    assert pending == []
    assert _read_column(tmp_path / "jobs.db", "status", "1001") == "IGNORED"


def test_pending_returns_only_new_jobs_oldest_first(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def scenario() -> list[JobPost]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.save_job(_job(external_id="newest", created_at=base + timedelta(hours=2)))
            await db.save_job(_job(external_id="ignored", created_at=base, status=JobStatus.IGNORED))
            await db.save_job(_job(external_id="oldest", created_at=base))
            await db.save_job(_job(external_id="notified", created_at=base, status=JobStatus.NOTIFIED))
            await db.save_job(_job(external_id="middle", created_at=base + timedelta(hours=1)))
            return await db.get_pending_notifications()

    pending = asyncio.run(scenario())
    assert [job.external_id for job in pending] == ["oldest", "middle", "newest"]
    assert all(job.status == JobStatus.NEW for job in pending)


def test_update_status_removes_from_pending_and_sets_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobs.db"
    saved_at = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    notified_at = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)

    async def scenario() -> tuple[list[str], list[str]]:
        async with Database(db_path) as db:
            await db.init_db()
            monkeypatch.setattr(database, "_utcnow", lambda: saved_at)
            await db.save_job(_job(external_id="a"))
            await db.save_job(_job(external_id="b"))
            before = [job.external_id for job in await db.get_pending_notifications()]
            monkeypatch.setattr(database, "_utcnow", lambda: notified_at)
            await db.update_status("jobinja", "a", JobStatus.NOTIFIED)
            await db.update_status("jobinja", "missing", JobStatus.NOTIFIED)
            after = [job.external_id for job in await db.get_pending_notifications()]
            return before, after

    before, after = asyncio.run(scenario())
    assert before == ["a", "b"]
    assert after == ["b"]
    assert _read_column(db_path, "status", "a") == "NOTIFIED"
    assert datetime.fromisoformat(_read_column(db_path, "updated_at", "a")) == notified_at
    assert datetime.fromisoformat(_read_column(db_path, "updated_at", "b")) == saved_at


def test_round_trip_preserves_profiles_and_aware_created_at(tmp_path: Path) -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    created_at = datetime(2026, 3, 15, 10, 45, 30, 123456, tzinfo=tehran)
    original = _job(
        matched_profiles=["AI & Automation", "پروفایل دوم"],
        created_at=created_at,
        salary=None,
        raw_description=None,
    )

    async def scenario() -> list[JobPost]:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            await db.init_db()  # idempotent
            await db.save_job(original)
            return await db.get_pending_notifications()

    (loaded,) = asyncio.run(scenario())
    assert loaded.matched_profiles == ["AI & Automation", "پروفایل دوم"]
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)
    assert loaded.created_at == created_at
    assert loaded == original
    assert _read_column(tmp_path / "jobs.db", "created_at", "1001") == "2026-03-15T07:15:30.123456+00:00"


def test_methods_require_context_manager(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(Database(tmp_path / "jobs.db").init_db())
