"""Async SQLite persistence for job posts, tailored resumes, and application attempts.

Every job the scrapers see is stored once per `(platform, external_id)`:
matching jobs as `NEW`, non-matching ones as `IGNORED`, so later runs can skip
their detail pages via `job_exists`. Datetimes are stored as fixed-width ISO 8601
UTC text, which makes lexicographic order equal to chronological order.

Schema versions are tracked with `PRAGMA user_version` and applied by
`init_db` from the ordered `_MIGRATIONS` list, each in its own transaction.
Before an existing database is migrated, it is backed up next to itself as
`{db_path}.bak-v{version}`. A database whose version is newer than the code is
refused. Status changes made by the bot go through `transition_status`, an
atomic compare-and-set that enforces `can_transition`.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from loguru import logger

from src.models import (
    ApplicationRecord,
    JobPost,
    JobStatus,
    TailoredResumeRecord,
    can_transition,
)

# ── Schema migrations ──────────────────────────────────────────────────────
#
# Append new versions at the end; never edit a released migration. Each one
# runs in a single transaction together with its `PRAGMA user_version` update.
# Statements run one by one through `execute` (not `executescript`, which
# would commit the open transaction first).

# v1: the Phase 1 schema. `IF NOT EXISTS` makes it a no-op on a database
# created before versioning existed (user_version 0).
_SCHEMA_V1: tuple[str, ...] = (
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
)

# v2: tailored resume versions and application attempts.
_SCHEMA_V2: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tailored_resumes (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        language TEXT NOT NULL CHECK (language IN ('fa', 'en')),
        llm_model TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        changes_json TEXT NOT NULL,
        clean_pdf_path TEXT,
        review_pdf_path TEXT,
        created_at TEXT NOT NULL,
        approved_at TEXT,
        UNIQUE (platform, external_id, version),
        FOREIGN KEY (platform, external_id) REFERENCES jobs (platform, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tailored_resumes_job ON tailored_resumes(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        resume_id INTEGER NOT NULL REFERENCES tailored_resumes (id),
        attempted_at TEXT NOT NULL,
        submitted INTEGER NOT NULL CHECK (submitted IN (0, 1)),
        receipt_path TEXT,
        error TEXT,
        FOREIGN KEY (platform, external_id) REFERENCES jobs (platform, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(platform, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_applications_attempted_at ON applications(attempted_at)",
)


@dataclass(frozen=True, slots=True)
class _Migration:
    """One schema version: its number, a short description for the log, and its statements."""

    version: int
    description: str
    statements: tuple[str, ...]


_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(1, "jobs table (Phase 1 schema)", _SCHEMA_V1),
    _Migration(2, "tailored_resumes and applications tables", _SCHEMA_V2),
)

if [migration.version for migration in _MIGRATIONS] != list(range(1, len(_MIGRATIONS) + 1)):
    raise RuntimeError("_MIGRATIONS must be numbered 1, 2, 3, ... in order")

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

_SELECT_JOB_SQL = f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE platform = ? AND external_id = ?"

_RESUME_COLUMNS = (
    "id",
    "platform",
    "external_id",
    "version",
    "language",
    "llm_model",
    "plan_json",
    "changes_json",
    "clean_pdf_path",
    "review_pdf_path",
    "created_at",
    "approved_at",
)

_INSERT_RESUME_SQL = (
    f"INSERT INTO tailored_resumes ({', '.join(_RESUME_COLUMNS[1:])}) "
    f"VALUES ({', '.join('?' for _ in _RESUME_COLUMNS[1:])})"
)

_SELECT_RESUME_SQL = f"SELECT {', '.join(_RESUME_COLUMNS)} FROM tailored_resumes"

_INSERT_APPLICATION_SQL = (
    "INSERT INTO applications (platform, external_id, resume_id, attempted_at, submitted, receipt_path, error) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
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


def _row_to_resume(row: aiosqlite.Row) -> TailoredResumeRecord:
    """Convert a `tailored_resumes` row (selected with `_RESUME_COLUMNS`) into a record."""
    data = dict(zip(_RESUME_COLUMNS, row, strict=True))
    data["created_at"] = _from_iso(data["created_at"])
    if data["approved_at"] is not None:
        data["approved_at"] = _from_iso(data["approved_at"])
    return TailoredResumeRecord.model_validate(data)


def _backup_path(db_path: Path, version: int) -> Path:
    """Return `{db_path}.bak-v{version}`, the pre-migration backup location."""
    return db_path.with_name(f"{db_path.name}.bak-v{version}")


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
        """Create the parent directory, connect, and apply the WAL, busy-timeout, and foreign-key PRAGMAs."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        # Resumes and applications must belong to a stored job (and resume).
        await self._conn.execute("PRAGMA foreign_keys=ON;")
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
        """Apply pending schema migrations in order; safe to call on every run.

        A database that already has a `jobs` table is backed up to
        `{db_path}.bak-v{current_version}` with the SQLite backup API before
        the first pending migration. Each migration runs in its own
        transaction and sets `user_version` in the same transaction, so a
        failed migration leaves the database at the previous version.

        Raises:
            RuntimeError: If the database's schema version is newer than any
                migration this code knows (it was written by a newer version).
        """
        conn = self._require_conn()
        current = await self._schema_version(conn)
        latest = _MIGRATIONS[-1].version
        if current > latest:
            raise RuntimeError(
                f"Database {self._db_path} has schema version {current}, but this code supports "
                f"versions up to {latest}. It was written by a newer version of Job Hunter Agent; "
                "refusing to start. Update the code, or restore an older backup of the database"
            )
        pending = [migration for migration in _MIGRATIONS if migration.version > current]
        if not pending:
            logger.debug("Database schema is up to date (version {})", current)
            return

        if await self._has_table(conn, "jobs"):
            await self._backup(conn, current)
        for migration in pending:
            await self._apply_migration(conn, migration)
        logger.info("Database schema migrated from version {} to {}", current, latest)

    @staticmethod
    async def _schema_version(conn: aiosqlite.Connection) -> int:
        """Return the database's `PRAGMA user_version`."""
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    async def _has_table(conn: aiosqlite.Connection, name: str) -> bool:
        """Return True if a table with this name exists."""
        async with conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (name,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _backup(self, conn: aiosqlite.Connection, version: int) -> Path:
        """Copy the database to `{db_path}.bak-v{version}` with the SQLite backup API.

        The copy is written to a temporary file first and then renamed, so an
        interrupted backup never leaves a truncated file under the final name.
        """
        target_path = _backup_path(self._db_path, version)
        partial_path = target_path.with_name(f"{target_path.name}.partial")
        partial_path.unlink(missing_ok=True)
        async with aiosqlite.connect(partial_path) as target:
            await conn.backup(target)
        if target_path.exists():
            logger.warning("Replacing the existing backup {}", target_path)
        partial_path.replace(target_path)
        logger.info("Backed up the database (schema version {}) to {}", version, target_path)
        return target_path

    async def _apply_migration(self, conn: aiosqlite.Connection, migration: _Migration) -> None:
        """Run one migration and its `user_version` update in a single transaction.

        `BEGIN IMMEDIATE` takes the write lock first, and the version is read
        again inside the transaction, so a migration that another process has
        applied in the meantime is skipped instead of run twice.
        """
        await conn.execute("BEGIN IMMEDIATE")
        try:
            version = await self._schema_version(conn)
            if version >= migration.version:
                await conn.rollback()
                logger.info("Migration v{} was already applied by another connection; skipped", migration.version)
                return
            for statement in migration.statements:
                await conn.execute(statement)
            # PRAGMA values cannot be bound parameters; the version is an int from _MIGRATIONS.
            await conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        except BaseException:
            try:
                await conn.rollback()
            except Exception:
                logger.exception("Rollback of migration v{} failed", migration.version)
            logger.error("Migration v{} ({}) failed and was rolled back", migration.version, migration.description)
            raise
        await conn.commit()
        logger.info("Applied migration v{}: {}", migration.version, migration.description)

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

    # ── Jobs (Task 7) ──────────────────────────────────────────────────────

    async def get_job(self, platform: str, external_id: str) -> JobPost | None:
        """Return the stored job, or None if it is unknown."""
        conn = self._require_conn()
        async with conn.execute(_SELECT_JOB_SQL, (platform, external_id)) as cursor:
            row = await cursor.fetchone()
        return _row_to_job(row) if row is not None else None

    async def get_jobs_by_status(self, status: JobStatus, *, since: datetime | None = None) -> list[JobPost]:
        """Return jobs with this status, oldest first; with `since`, only jobs whose `created_at` >= `since`.

        `created_at` is when the job was first stored (the scrapers do not set
        it), so `since` selects jobs first seen in that window.
        """
        conn = self._require_conn()
        sql = f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE status = ?"
        params: list[str] = [JobStatus(status).value]
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(_to_iso(since))
        sql += " ORDER BY created_at, id"
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        jobs = [_row_to_job(row) for row in rows]
        logger.debug(
            "Found {} {} job(s){}", len(jobs), JobStatus(status).value, "" if since is None else f" since {_to_iso(since)}"
        )
        return jobs

    async def transition_status(
        self,
        platform: str,
        external_id: str,
        from_statuses: Collection[JobStatus],
        to: JobStatus,
    ) -> bool:
        """Atomically move a job to `to` if its current status is one of `from_statuses`.

        This is a compare-and-set: a single `UPDATE ... WHERE status IN (...)`
        statement, so of two concurrent calls for the same transition (double
        taps, two processes) exactly one succeeds. `updated_at` is refreshed.

        Returns:
            True if the status changed; False if the job's current status is
            not in `from_statuses` or the job is unknown.

        Raises:
            ValueError: If `from_statuses` is empty, or any `(from, to)` pair is
                not allowed by `can_transition`.
        """
        target = JobStatus(to)
        sources = tuple(dict.fromkeys(JobStatus(status) for status in from_statuses))
        if not sources:
            raise ValueError("from_statuses must not be empty")
        invalid = [source for source in sources if not can_transition(source, target)]
        if invalid:
            pairs = ", ".join(f"{source.value} -> {target.value}" for source in invalid)
            raise ValueError(f"Status transition not allowed: {pairs}")

        conn = self._require_conn()
        placeholders = ", ".join("?" for _ in sources)
        sql = (
            "UPDATE jobs SET status = ?, updated_at = ? "
            f"WHERE platform = ? AND external_id = ? AND status IN ({placeholders})"
        )
        params = (target.value, _to_iso(_utcnow()), platform, external_id, *(source.value for source in sources))
        async with conn.execute(sql, params) as cursor:
            changed = cursor.rowcount == 1
        await conn.commit()
        expected = "|".join(source.value for source in sources)
        if changed:
            logger.info("Job {}:{} {} -> {}", platform, external_id, expected, target.value)
        else:
            logger.info(
                "Job {}:{} not moved to {}: its status is not {} (already handled, or unknown job)",
                platform,
                external_id,
                target.value,
                expected,
            )
        return changed

    async def requeue_ignored(self, platform: str, external_id: str, matched_profiles: list[str]) -> bool:
        """Move an `IGNORED` job back to `NEW` with its newly matched profiles (compare-and-set).

        Returns:
            True if the job was `IGNORED` and is now `NEW`; False otherwise
            (other status, or unknown job), in which case nothing changes.

        Raises:
            ValueError: If `matched_profiles` is empty.
        """
        if not matched_profiles:
            raise ValueError("matched_profiles must not be empty when requeueing a job")
        conn = self._require_conn()
        async with conn.execute(
            "UPDATE jobs SET matched_profiles = ?, status = ?, updated_at = ? "
            "WHERE platform = ? AND external_id = ? AND status = ?",
            (
                json.dumps(matched_profiles, ensure_ascii=False),
                JobStatus.NEW.value,
                _to_iso(_utcnow()),
                platform,
                external_id,
                JobStatus.IGNORED.value,
            ),
        ) as cursor:
            requeued = cursor.rowcount == 1
        await conn.commit()
        if requeued:
            logger.debug("Job {}:{} IGNORED -> NEW", platform, external_id)
        else:
            logger.info("Job {}:{} is not IGNORED (or unknown); not requeued", platform, external_id)
        return requeued

    # ── Tailored resumes (Task 7) ──────────────────────────────────────────

    async def save_tailored_resume(self, record: TailoredResumeRecord) -> int:
        """Insert a tailored resume version and return its new ID.

        Raises:
            ValueError: If the record already has an ID (it was saved before).
            sqlite3.IntegrityError: If this version already exists for the job,
                or the job is not stored.
        """
        if record.id is not None:
            raise ValueError(f"Tailored resume record already has id {record.id}; it cannot be saved again")
        conn = self._require_conn()
        params = (
            record.platform,
            record.external_id,
            record.version,
            record.language,
            record.llm_model,
            record.plan_json,
            record.changes_json,
            record.clean_pdf_path,
            record.review_pdf_path,
            _to_iso(record.created_at),
            _to_iso(record.approved_at) if record.approved_at is not None else None,
        )
        async with conn.execute(_INSERT_RESUME_SQL, params) as cursor:
            resume_id = cursor.lastrowid
        await conn.commit()
        if resume_id is None:
            raise RuntimeError("SQLite did not report the ID of the inserted tailored resume")
        logger.info(
            "Saved tailored resume {} for job {}:{} (v{}, {}, plan {} chars, changes {} chars)",
            resume_id,
            record.platform,
            record.external_id,
            record.version,
            record.language,
            len(record.plan_json),
            len(record.changes_json),
        )
        return resume_id

    async def update_tailored_resume_paths(self, resume_id: int, clean_pdf_path: str, review_pdf_path: str) -> None:
        """Store the rendered PDF paths of a tailored resume.

        Raises:
            LookupError: If no tailored resume has this ID.
        """
        conn = self._require_conn()
        async with conn.execute(
            "UPDATE tailored_resumes SET clean_pdf_path = ?, review_pdf_path = ? WHERE id = ?",
            (str(clean_pdf_path), str(review_pdf_path), resume_id),
        ) as cursor:
            updated = cursor.rowcount == 1
        await conn.commit()
        if not updated:
            raise LookupError(f"Tailored resume {resume_id} not found")
        logger.debug("Stored PDF paths for tailored resume {}", resume_id)

    async def get_tailored_resume(self, resume_id: int) -> TailoredResumeRecord | None:
        """Return a tailored resume by ID, or None if it is unknown."""
        conn = self._require_conn()
        async with conn.execute(f"{_SELECT_RESUME_SQL} WHERE id = ?", (resume_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_resume(row) if row is not None else None

    async def get_latest_tailored_resume(self, platform: str, external_id: str) -> TailoredResumeRecord | None:
        """Return the job's tailored resume with the highest version, or None if there is none."""
        conn = self._require_conn()
        async with conn.execute(
            f"{_SELECT_RESUME_SQL} WHERE platform = ? AND external_id = ? ORDER BY version DESC LIMIT 1",
            (platform, external_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_resume(row) if row is not None else None

    async def count_tailored_resumes(self, platform: str, external_id: str) -> int:
        """Return how many tailored resume versions are stored for the job."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM tailored_resumes WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def mark_tailored_resume_approved(self, resume_id: int) -> None:
        """Set `approved_at` to now; a resume that is already approved keeps its first timestamp.

        Raises:
            LookupError: If no tailored resume has this ID.
        """
        conn = self._require_conn()
        async with conn.execute(
            "UPDATE tailored_resumes SET approved_at = ? WHERE id = ? AND approved_at IS NULL",
            (_to_iso(_utcnow()), resume_id),
        ) as cursor:
            updated = cursor.rowcount == 1
        await conn.commit()
        if updated:
            logger.info("Tailored resume {} approved", resume_id)
            return
        if await self.get_tailored_resume(resume_id) is None:
            raise LookupError(f"Tailored resume {resume_id} not found")
        logger.debug("Tailored resume {} was already approved; approved_at unchanged", resume_id)

    # ── Applications (Task 7) ──────────────────────────────────────────────

    async def record_application(self, record: ApplicationRecord) -> int:
        """Insert an application attempt (submitted or failed) and return its new ID.

        Raises:
            ValueError: If the record already has an ID (it was saved before).
            sqlite3.IntegrityError: If the job or the tailored resume is not stored.
        """
        if record.id is not None:
            raise ValueError(f"Application record already has id {record.id}; it cannot be saved again")
        conn = self._require_conn()
        params = (
            record.platform,
            record.external_id,
            record.resume_id,
            _to_iso(record.attempted_at),
            int(record.submitted),
            record.receipt_path,
            record.error,
        )
        async with conn.execute(_INSERT_APPLICATION_SQL, params) as cursor:
            application_id = cursor.lastrowid
        await conn.commit()
        if application_id is None:
            raise RuntimeError("SQLite did not report the ID of the inserted application")
        logger.info(
            "Recorded application {} for job {}:{} with resume {} (submitted={})",
            application_id,
            record.platform,
            record.external_id,
            record.resume_id,
            record.submitted,
        )
        return application_id

    async def count_submitted_applications_since(self, since: datetime) -> int:
        """Return how many submitted applications were attempted at or after `since`."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM applications WHERE submitted = 1 AND attempted_at >= ?",
            (_to_iso(since),),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def has_submitted_application(self, platform: str, external_id: str) -> bool:
        """Return True if an application for this job was ever submitted successfully."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT 1 FROM applications WHERE platform = ? AND external_id = ? AND submitted = 1 LIMIT 1",
            (platform, external_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None
        logger.info("Closed SQLite database at {}", self._db_path)
