"""Async SQLite persistence for job posts and their status state machine.

Every job the scrapers see is stored once per `(platform, external_id)`:
matching jobs as `NEW`, non-matching ones as `IGNORED`, so later runs can skip
their detail pages via `job_exists`. Datetimes are stored as fixed-width ISO 8601
UTC text, which makes lexicographic order equal to chronological order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from loguru import logger

from src.models import JobPost, JobStatus

_SCHEMA = """
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

_JOB_COLUMNS = (
    "platform",
    "external_id",
    "title",
    "company",
    "city",
    "work_mode",
    "salary",
    "url",
    "raw_description",
    "matched_profiles",
    "status",
    "created_at",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO jobs ({', '.join(_JOB_COLUMNS)}, updated_at) "
    f"VALUES ({', '.join('?' for _ in _JOB_COLUMNS)}, ?)"
)

_SELECT_PENDING_SQL = (
    f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE status = ? ORDER BY created_at, id"
)


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _to_iso(value: datetime) -> str:
    """Format a datetime as fixed-width ISO 8601 UTC text (naive values are assumed UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _from_iso(text: str) -> datetime:
    """Parse stored ISO 8601 text back into a timezone-aware UTC datetime."""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _row_to_job(row: aiosqlite.Row) -> JobPost:
    """Convert a `jobs` row (selected with `_JOB_COLUMNS`) back into a `JobPost`."""
    data = dict(zip(_JOB_COLUMNS, row, strict=True))
    data["matched_profiles"] = json.loads(data["matched_profiles"])
    data["created_at"] = _from_iso(data["created_at"])
    return JobPost.model_validate(data)


class Database:
    """Async SQLite store for job posts, used as an async context manager.

    Example:
        async with Database("data/jobs.db") as db:
            await db.init_db()
            await db.save_job(job)
    """

    def __init__(self, db_path: str | Path) -> None:
        """Remember the database path; the connection opens in `__aenter__`."""
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Database:
        """Create the parent directory, connect, and apply the WAL and busy-timeout PRAGMAs."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        logger.info("Connected to SQLite database at {}", self._db_path)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the connection."""
        await self.close()

    def _require_conn(self) -> aiosqlite.Connection:
        """Return the open connection, or raise if the context manager was not entered."""
        if self._conn is None:
            raise RuntimeError("Database is not connected; use 'async with Database(...) as db'")
        return self._conn

    async def init_db(self) -> None:
        """Create the `jobs` table and status index if missing; safe to call on every run."""
        conn = self._require_conn()
        await conn.executescript(_SCHEMA)
        await conn.commit()
        logger.debug("Database schema ensured")

    async def job_exists(self, platform: str, external_id: str) -> bool:
        """Return True if a job with this platform and external ID is already stored."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT 1 FROM jobs WHERE platform = ? AND external_id = ? LIMIT 1",
            (platform, external_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def save_job(self, job: JobPost) -> bool:
        """Insert a job (any status, e.g. `NEW` or `IGNORED`).

        Returns:
            True if a row was inserted, False if the job was already stored.
        """
        conn = self._require_conn()
        params = (
            job.platform,
            job.external_id,
            job.title,
            job.company,
            job.city,
            job.work_mode.value,
            job.salary,
            job.url,
            job.raw_description,
            json.dumps(job.matched_profiles, ensure_ascii=False),
            job.status.value,
            _to_iso(job.created_at),
            _to_iso(_utcnow()),
        )
        async with conn.execute(_INSERT_SQL, params) as cursor:
            inserted = cursor.rowcount == 1
        await conn.commit()
        if inserted:
            logger.debug("Saved {} job {}:{}", job.status.value, job.platform, job.external_id)
        else:
            logger.debug("Job {}:{} already stored; skipped", job.platform, job.external_id)
        return inserted

    async def get_pending_notifications(self) -> list[JobPost]:
        """Return all `NEW` jobs, oldest first (by `created_at`, then insertion order)."""
        conn = self._require_conn()
        async with conn.execute(_SELECT_PENDING_SQL, (JobStatus.NEW.value,)) as cursor:
            rows = await cursor.fetchall()
        jobs = [_row_to_job(row) for row in rows]
        logger.debug("Found {} job(s) pending notification", len(jobs))
        return jobs

    async def update_status(self, platform: str, external_id: str, status: JobStatus) -> None:
        """Set a job's status and refresh its `updated_at`; logs a warning if the job is unknown."""
        conn = self._require_conn()
        async with conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE platform = ? AND external_id = ?",
            (status.value, _to_iso(_utcnow()), platform, external_id),
        ) as cursor:
            updated = cursor.rowcount
        await conn.commit()
        if updated:
            logger.debug("Job {}:{} -> {}", platform, external_id, status.value)
        else:
            logger.warning("Cannot set status {}: job {}:{} not found", status.value, platform, external_id)

    async def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None
        logger.info("Closed SQLite database at {}", self._db_path)
