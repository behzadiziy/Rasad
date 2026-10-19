"""Tests for the review bot service, its Bot API client, and the bot entry points (fake client and backend)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from loguru import logger

import main
from src.bot import client as client_module
from src.bot import messages
from src.bot import service as service_module
from src.bot.client import BotApiError, BotConflictError, TelegramBotClient
from src.bot.service import BotService, TailoringOutcome, TailoringPipeline, load_offset, save_offset
from src.database import Database
from src.llm.base import LLMClient, LLMResult, LLMUsage, T
from src.models import AppConfig, JobPost, JobStatus, Settings, TailoredResumeRecord, WorkMode
from src.resume.changes import compute_changes
from src.resume.models import MasterResume, TailoringPlan, load_master_resume
from src.resume.tailor import TailoringError

_FIXTURE = Path(__file__).parent / "fixtures" / "resume" / "master_resume.yaml"
_REVIEW_CHAT = 111
_CHANNEL = -1001234567890
_OUTSIDER = 999
_NOTIFICATION_MESSAGE = 50
_PACKAGE_MESSAGE = 70
_GUARD_MESSAGE = 80
_TOKEN = "123456:SECRET-TOKEN"

_PLAN_DATA: dict[str, Any] = {
    "language": "en",
    "summary": "Software engineer with 5 years of experience building Python API services.",
    "experiences": [
        {
            "experience_id": "alpha-backend",
            "bullets": [
                {"bullet_id": "alpha-api", "text": "Designed the FastAPI payment API for 1,200 stores"},
                {"bullet_id": "alpha-cache", "text": "Cut API response time by 40% with Redis"},
            ],
        }
    ],
    "projects": [],
    "skill_ids": ["python", "fastapi", "redis"],
    "cover_letter": "Dear Omega Labs team, I am applying for the Python Backend Engineer role.",
    "relevance_notes": "Strong Python fit.",
}


# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeClient:
    """Records every Bot API call; `get_updates` replays scripted batches."""

    def __init__(self) -> None:
        """Start with no calls and no scripted updates."""
        self.answers: list[tuple[str, str | None]] = []
        self.messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.edits: list[tuple[int | str, int, dict[str, Any] | None]] = []
        self.batches: list[list[dict[str, Any]] | Exception] = []
        self.offsets: list[int | None] = []
        self.stop: asyncio.Event | None = None
        self.fail_answers: int = 0

    async def get_updates(self, offset: int | None, allowed_updates: Any) -> list[dict[str, Any]]:
        """Return the next scripted batch (or raise it); stop polling when none are left."""
        await asyncio.sleep(0)  # yield like a real long poll, so the loop never starves other tasks
        self.offsets.append(offset)
        assert tuple(allowed_updates) == ("callback_query", "message")
        if not self.batches:
            if self.stop is not None:
                self.stop.set()
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Record the toast."""
        if self.fail_answers:
            self.fail_answers -= 1
            raise RuntimeError("unexpected failure")
        self.answers.append((callback_query_id, text))

    async def send_message(self, chat_id: int | str, text: str, *, reply_markup: Any = None) -> dict[str, Any]:
        """Record the message."""
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 1000 + len(self.messages)}

    async def send_document(
        self, chat_id: int | str, path: Path, *, filename: str | None = None, caption: str | None = None,
        reply_markup: Any = None,
    ) -> dict[str, Any]:
        """Record the document."""
        self.documents.append(
            {"chat_id": chat_id, "path": path, "filename": filename, "caption": caption, "reply_markup": reply_markup}
        )
        return {"message_id": 2000 + len(self.documents)}

    async def edit_message_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: Any) -> None:
        """Record the keyboard edit."""
        self.edits.append((chat_id, message_id, reply_markup))

    def toasts(self) -> list[str | None]:
        """Return the toast texts in order."""
        return [text for _, text in self.answers]


class FakeBackend:
    """Saves a fixture plan as a new version and writes fake PDFs, without an LLM or a browser."""

    def __init__(self, db: Database, master: MasterResume, out_dir: Path) -> None:
        """Store the database, master resume, and output directory."""
        self.db = db
        self.master = master
        self.out_dir = out_dir
        self.calls: list[tuple[str, int]] = []
        self.renders: list[int | None] = []
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    def load_master(self) -> MasterResume:
        """Return the fixture master resume."""
        return self.master

    async def tailor(self, job: JobPost, version: int) -> TailoringOutcome:
        """Save version `version` of the fixture plan for the job."""
        self.calls.append((job.external_id, version))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        plan = TailoringPlan.model_validate(_PLAN_DATA)
        changes = compute_changes(plan, self.master)
        record = TailoredResumeRecord(
            platform=job.platform,
            external_id=job.external_id,
            version=version,
            language=plan.language,
            llm_model="fake-model",
            plan_json=plan.model_dump_json(),
            changes_json=changes.model_dump_json(),
        )
        resume_id = await self.db.save_tailored_resume(record)
        clean, review = self._write_pdfs(job.external_id, version)
        await self.db.update_tailored_resume_paths(resume_id, str(clean), str(review))
        paths = {"id": resume_id, "clean_pdf_path": str(clean), "review_pdf_path": str(review)}
        saved = record.model_copy(update=paths)
        return TailoringOutcome(saved, plan, changes, ["Term 'X' in the summary does not appear"], clean, review)

    async def render(self, record: TailoredResumeRecord) -> tuple[Path, Path]:
        """Write fake PDFs for a saved record."""
        self.renders.append(record.id)
        return self._write_pdfs(record.external_id, record.version)

    def _write_pdfs(self, external_id: str, version: int) -> tuple[Path, Path]:
        """Write the clean and review fake PDFs."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        clean = self.out_dir / f"{external_id}_v{version}.pdf"
        review = self.out_dir / f"{external_id}_v{version}_review.pdf"
        clean.write_bytes(b"%PDF-clean")
        review.write_bytes(b"%PDF-review")
        return clean, review


# ── Helpers ────────────────────────────────────────────────────────────────


def _settings(**overrides: Any) -> Settings:
    """Settings without reading config/.env: user 111 is allowed, so the review chat is 111."""
    data: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": _TOKEN,
        "TELEGRAM_CHAT_ID": str(_CHANNEL),
        "TELEGRAM_ALLOWED_USER_IDS": str(_REVIEW_CHAT),
        "TELEGRAM_REVIEW_CHAT_ID": None,
        "GEMINI_API_KEY": "fake-key",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)  # type: ignore[call-arg]


def _config(tmp_path: Path, *, max_versions: int = 3, llm: bool = True) -> AppConfig:
    """An in-memory app config whose bot state and resumes live under `tmp_path`."""
    data: dict[str, Any] = {
        "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
        "scraping": {"enabled_platforms": ["jobinja"], "max_pages_per_keyword": 1},
        "search_profiles": [{"profile_name": "P", "keywords": ["Python"]}],
        "tailoring": {
            "master_resume_path": str(_FIXTURE),
            "output_dir": str(tmp_path / "resumes"),
            "max_versions_per_job": max_versions,
        },
        "bot": {"state_path": str(tmp_path / "bot_state.json")},
    }
    if llm:
        data["llm"] = {"provider": "gemini", "model": "gemini-test-flash"}
    return AppConfig.model_validate(data)


def _job(external_id: str = "tER9", **overrides: Any) -> JobPost:
    """A stored job; NOTIFIED unless overridden."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": external_id,
        "title": "Python Backend Engineer",
        "company": "Omega Labs",
        "city": "Mashhad",
        "work_mode": WorkMode.REMOTE,
        "url": f"https://jobinja.ir/jobs/{external_id}",
        "status": JobStatus.NOTIFIED,
    }
    data.update(overrides)
    return JobPost(**data)


def _tap(
    data: str,
    *,
    user: int = _REVIEW_CHAT,
    update_id: int = 1,
    chat: int = _CHANNEL,
    message_id: int = _NOTIFICATION_MESSAGE,
) -> dict[str, Any]:
    """A callback_query update for a button on a message."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"q{update_id}",
            "from": {"id": user, "is_bot": False, "first_name": "T"},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "channel"}, "date": 1},
        },
    }


def _say(text: str, *, user: int = _REVIEW_CHAT, update_id: int = 1, chat_type: str = "private") -> dict[str, Any]:
    """A message update in a private chat."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "from": {"id": user, "is_bot": False, "first_name": "T"},
            "chat": {"id": user, "type": chat_type},
            "date": 1,
            "text": text,
        },
    }


@dataclass
class Ctx:
    """Everything a scenario needs."""

    db: Database
    client: FakeClient
    backend: FakeBackend
    service: BotService
    config: AppConfig

    async def settle(self) -> None:
        """Wait for queued and background work to finish."""
        await self.service.shutdown(grace=5)

    async def tap(self, data: str, **kwargs: Any) -> str | None:
        """Handle one tap and return its toast."""
        before = len(self.client.answers)
        await self.service.handle_update(_tap(data, **kwargs))
        assert len(self.client.answers) == before + 1
        return self.client.answers[-1][1]

    async def tailored(self, job: JobPost, versions: int = 1) -> list[TailoredResumeRecord]:
        """Store a TAILORED job with `versions` saved versions."""
        await self.db.save_job(job.model_copy(update={"status": JobStatus.TAILORED}))
        return [(await self.backend.tailor(job, version)).record for version in range(1, versions + 1)]


def _run(
    tmp_path: Path,
    scenario: Callable[[Ctx], Awaitable[Any]],
    *,
    max_versions: int = 3,
    settings: Settings | None = None,
) -> Any:
    """Run a scenario against a fresh database, fake client, and fake backend."""

    async def runner() -> Any:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            config = _config(tmp_path, max_versions=max_versions)
            client = FakeClient()
            backend = FakeBackend(db, load_master_resume(_FIXTURE), tmp_path / "pdf")
            service = BotService(
                settings=settings or _settings(),
                config=config,
                client=client,
                db=db,
                backend=backend,
                state_path=config.bot.state_path,
            )
            ctx = Ctx(db, client, backend, service, config)
            try:
                return await scenario(ctx)
            finally:
                await service.shutdown(grace=5)

    return asyncio.run(runner())


@pytest.fixture
def logs() -> Any:
    """Capture loguru messages."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(sink_id)


async def _status(ctx: Ctx, external_id: str = "tER9", platform: str = "jobinja") -> JobStatus:
    """Return a stored job's status."""
    job = await ctx.db.get_job(platform, external_id)
    assert job is not None
    return job.status


def _sent_chats(client: FakeClient) -> set[int | str]:
    """Every chat that received a message or a document."""
    return {item["chat_id"] for item in (*client.messages, *client.documents)}


# ── Authorization ──────────────────────────────────────────────────────────


def test_unauthorized_callback_is_refused_logged_and_ignored(tmp_path: Path, logs: list[str]) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        for data in ("t:jb:tER9", "a:1", "s:1", "r:1", "d:jb:tER9"):
            assert await ctx.tap(data, user=_OUTSIDER) == messages.UNAUTHORIZED_TOAST
        await ctx.settle()
        assert ctx.backend.calls == []
        assert ctx.client.messages == [] and ctx.client.documents == [] and ctx.client.edits == []
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)
    assert any("WARNING" in line and str(_OUTSIDER) in line for line in logs)


def test_commands_from_unauthorized_users(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        await ctx.service.handle_update(_say("/tailor jb:tER9", user=_OUTSIDER))
        await ctx.service.handle_update(_say("/start", user=_OUTSIDER, update_id=2))
        await ctx.service.handle_update(_say("hello", user=_OUTSIDER, update_id=3))
        await ctx.service.handle_update(_say("/id", update_id=4))
        await ctx.settle()
        refusal, outsider_id, own_id = ctx.client.messages
        assert refusal == {"chat_id": _OUTSIDER, "text": messages.UNAUTHORIZED_TOAST, "reply_markup": None}
        assert "<code>999</code>" in outsider_id["text"] and "مجاز نیستید" in outsider_id["text"]
        assert own_id["chat_id"] == _REVIEW_CHAT and "✅" in own_id["text"]
        assert ctx.backend.calls == []
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)


def test_group_messages_are_ignored(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.service.handle_update(_say("/start", chat_type="group"))
        assert ctx.client.messages == []

    _run(tmp_path, scenario)


def test_without_allowed_users_only_id_commands_work(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        await ctx.service.handle_update(_say("/start"))
        assert await ctx.tap("t:jb:tER9", update_id=2) == messages.UNAUTHORIZED_TOAST
        await ctx.service.handle_update(_say("/tailor jb:tER9", update_id=3))
        await ctx.settle()
        id_reply, refusal = ctx.client.messages
        assert "<code>111</code>" in id_reply["text"] and "مجاز نیستید" in id_reply["text"]
        assert refusal["text"] == messages.UNAUTHORIZED_TOAST
        assert ctx.backend.calls == [] and ctx.client.documents == []

    _run(tmp_path, scenario, settings=_settings(TELEGRAM_ALLOWED_USER_IDS=""))


# ── Tailor ─────────────────────────────────────────────────────────────────


def test_tailor_button_sends_the_review_package_and_marks_tailored(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job = _job()
        await ctx.db.save_job(job)
        assert await ctx.tap("t:jb:tER9") == messages.STARTED_TOAST
        await ctx.settle()

        assert ctx.backend.calls == [("tER9", 1)]
        (document,) = ctx.client.documents
        assert document["chat_id"] == _REVIEW_CHAT
        assert document["path"].name == "tER9_v1_review.pdf"
        assert "نسخه ۱ از ۳" in document["caption"] and "Python Backend Engineer" in document["caption"]
        assert all(message["chat_id"] == _REVIEW_CHAT for message in ctx.client.messages)
        record = await ctx.db.get_latest_tailored_resume("jobinja", "tER9")
        assert record is not None
        assert ctx.client.messages[-1]["reply_markup"] == messages.review_keyboard(record.id, can_regenerate=True)
        assert "Dear Omega Labs team" in "".join(message["text"] for message in ctx.client.messages)

        progress, done = ctx.client.edits
        tailoring = messages.job_keyboard(job, status=messages.STATUS_TAILORING)
        assert progress == (_CHANNEL, _NOTIFICATION_MESSAGE, tailoring)
        assert done == (_CHANNEL, _NOTIFICATION_MESSAGE, messages.job_keyboard(job, status=messages.STATUS_SENT))
        assert await _status(ctx) is JobStatus.TAILORED

    _run(tmp_path, scenario)


def test_double_tap_on_tailor_runs_exactly_once(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        ctx.backend.gate = asyncio.Event()
        first = await ctx.tap("t:jb:tER9", update_id=1)
        second = await ctx.tap("t:jb:tER9", update_id=2)
        ctx.backend.gate.set()
        await ctx.settle()
        assert (first, second) == (messages.STARTED_TOAST, messages.QUEUED_TOAST)
        assert ctx.backend.calls == [("tER9", 1)]
        assert len(ctx.client.documents) == 1
        assert await ctx.db.count_tailored_resumes("jobinja", "tER9") == 1

    _run(tmp_path, scenario)


def test_tailorings_run_one_at_a_time_and_others_are_queued(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job("A1"))
        await ctx.db.save_job(_job("B2"))
        ctx.backend.gate = asyncio.Event()
        assert await ctx.tap("t:jb:A1", update_id=1) == messages.STARTED_TOAST
        assert await ctx.tap("t:jb:B2", update_id=2) == messages.QUEUED_TOAST
        await asyncio.sleep(0.05)
        assert ctx.backend.calls == [("A1", 1)]  # B2 waits for A1
        ctx.backend.gate.set()
        await ctx.settle()
        assert ctx.backend.calls == [("A1", 1), ("B2", 1)]
        assert await _status(ctx, "A1") is JobStatus.TAILORED and await _status(ctx, "B2") is JobStatus.TAILORED

    _run(tmp_path, scenario)


def test_failed_tailoring_reports_restores_the_keyboard_and_keeps_the_status(tmp_path: Path, logs: list[str]) -> None:
    async def scenario(ctx: Ctx) -> None:
        job = _job()
        await ctx.db.save_job(job)
        ctx.backend.error = TailoringError(
            "The plan still has 1 error(s)", ["Skill ID 'kubernetes' is not in the master resume"]
        )
        assert await ctx.tap("t:jb:tER9") == messages.STARTED_TOAST
        await ctx.settle()
        (report,) = ctx.client.messages
        assert report["chat_id"] == _REVIEW_CHAT
        assert "ناموفق" in report["text"] and "Skill ID 'kubernetes'" in report["text"]
        assert ctx.client.documents == []
        assert ctx.client.edits[-1] == (_CHANNEL, _NOTIFICATION_MESSAGE, messages.job_keyboard(job))
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)
    assert not any("kubernetes" in line for line in logs)


def test_tailor_on_a_tailored_job_resends_the_latest_package(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (record,) = await ctx.tailored(_job())
        assert await ctx.tap("t:jb:tER9") == messages.RESENDING_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [("tER9", 1)]  # only the setup call
        (document,) = ctx.client.documents
        assert str(document["path"]) == record.review_pdf_path
        assert ctx.client.messages[-1]["reply_markup"] == messages.review_keyboard(record.id, can_regenerate=True)

    _run(tmp_path, scenario)


def test_tailor_command(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        await ctx.db.save_job(_job("IG1", status=JobStatus.IGNORED))
        await ctx.service.handle_update(_say("/tailor jb:tER9"))
        await ctx.settle()
        assert ctx.client.messages[0]["text"].startswith(messages.STARTED_TOAST)
        assert await _status(ctx) is JobStatus.TAILORED
        ctx.client.messages.clear()
        commands = ("/tailor", "/tailor nonsense", "/tailor jb:nope", "/tailor jb:IG1", "/help")
        for update_id, text in enumerate(commands, 2):
            await ctx.service.handle_update(_say(text, update_id=update_id))
        usage, usage_again, missing, wrong_status, help_text = (message["text"] for message in ctx.client.messages)
        assert usage == usage_again == messages.TAILOR_USAGE
        assert "jb:nope" in missing
        assert "نادیده‌گرفته‌شده" in wrong_status
        assert help_text == messages.HELP_MESSAGE

    _run(tmp_path, scenario)


# ── Stale buttons ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "data", ["zz", "a:abc", "a:999", "s:999", "r:999", "t:jb:missing", "t:xx:1", "y:1", "c:1", "x:1", ""]
)
def test_unknown_or_malformed_buttons_are_stale(tmp_path: Path, data: str) -> None:
    async def scenario(ctx: Ctx) -> None:
        await ctx.db.save_job(_job())
        assert await ctx.tap(data) == messages.STALE_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [] and ctx.client.edits == []
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)


def test_buttons_of_older_versions_and_finished_jobs_are_stale(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        first, second = await ctx.tailored(_job(), versions=2)
        for data in (f"a:{first.id}", f"s:{first.id}", f"r:{first.id}"):
            assert await ctx.tap(data, chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE) == messages.STALE_TOAST
        assert await _status(ctx) is JobStatus.TAILORED
        toast = await ctx.tap(f"s:{second.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        assert toast == messages.REJECTED_TOAST
        for data in (f"a:{second.id}", f"s:{second.id}", f"r:{second.id}", "t:jb:tER9"):
            assert await ctx.tap(data, chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE) == messages.STALE_TOAST
        await ctx.settle()
        assert await _status(ctx) is JobStatus.SKIPPED
        assert ctx.client.documents == []

    _run(tmp_path, scenario)


# ── Regenerate and the version limit ───────────────────────────────────────


def test_regenerate_until_the_maximum_version(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (first,) = await ctx.tailored(_job())
        toast = await ctx.tap(f"r:{first.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        assert toast == messages.REGENERATING_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [("tER9", 1), ("tER9", 2)]
        second = await ctx.db.get_latest_tailored_resume("jobinja", "tER9")
        assert second is not None and second.version == 2
        assert "نسخه ۲ از ۲" in ctx.client.documents[-1]["caption"]
        # The last allowed version has no regenerate button.
        assert ctx.client.messages[-1]["reply_markup"] == messages.review_keyboard(second.id, can_regenerate=False)
        regenerating, superseded = ctx.client.edits
        assert regenerating == (_REVIEW_CHAT, _PACKAGE_MESSAGE, messages.status_keyboard(messages.STATUS_REGENERATING))
        assert superseded == (
            _REVIEW_CHAT,
            _PACKAGE_MESSAGE,
            messages.status_keyboard(messages.STATUS_SUPERSEDED.format(version=2)),
        )
        assert await _status(ctx) is JobStatus.TAILORED

        toast = await ctx.tap(f"r:{second.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        assert toast == messages.max_versions_toast(2)
        await ctx.settle()
        assert len(ctx.backend.calls) == 2

    _run(tmp_path, scenario, max_versions=2)


def test_tailor_refuses_beyond_the_maximum_version(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job = _job()
        await ctx.db.save_job(job)
        await ctx.backend.tailor(job, 1)  # an earlier version whose package was never delivered
        ctx.backend.calls.clear()
        assert await ctx.tap("t:jb:tER9") == messages.STARTED_TOAST
        await ctx.settle()
        assert ctx.backend.calls == []
        assert "سقف" in ctx.client.messages[-1]["text"]
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario, max_versions=1)


# ── Approve and reject ─────────────────────────────────────────────────────


def test_approval_sends_files_only_to_the_review_chat(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job = _job()
        (record,) = await ctx.tailored(job)
        toast = await ctx.tap(f"a:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        assert toast == messages.APPROVED_TOAST
        await ctx.settle()

        assert await _status(ctx) is JobStatus.APPROVED
        approved = await ctx.db.get_tailored_resume(record.id)
        assert approved is not None and approved.approved_at is not None
        (document,) = ctx.client.documents
        assert document["chat_id"] == _REVIEW_CHAT
        assert str(document["path"]) == record.clean_pdf_path
        assert document["filename"] == "Negar_Azmoon_Resume_en.pdf"
        cover_letter, link = ctx.client.messages
        assert "Dear Omega Labs team" in cover_letter["text"]
        assert 'href="https://jobinja.ir/jobs/tER9"' in link["text"]
        assert _sent_chats(ctx.client) == {_REVIEW_CHAT}
        assert str(_CHANNEL) not in {str(chat) for chat in _sent_chats(ctx.client)}
        approved_keyboard = messages.status_keyboard(messages.STATUS_APPROVED)
        assert ctx.client.edits == [(_REVIEW_CHAT, _PACKAGE_MESSAGE, approved_keyboard)]

    _run(tmp_path, scenario)


def test_double_tap_on_approve_acts_once(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (record,) = await ctx.tailored(_job())
        first = await ctx.tap(f"a:{record.id}", update_id=1, chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        second = await ctx.tap(f"a:{record.id}", update_id=2, chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        await ctx.settle()
        assert (first, second) == (messages.APPROVED_TOAST, messages.STALE_TOAST)
        assert len(ctx.client.documents) == 1

    _run(tmp_path, scenario)


def test_approval_renders_the_clean_pdf_again_when_it_is_missing(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (record,) = await ctx.tailored(_job())
        assert record.clean_pdf_path is not None
        Path(record.clean_pdf_path).unlink()
        await ctx.tap(f"a:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        await ctx.settle()
        assert ctx.backend.renders == [record.id]
        assert len(ctx.client.documents) == 1

    _run(tmp_path, scenario)


def test_reject_marks_skipped(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (record,) = await ctx.tailored(_job())
        toast = await ctx.tap(f"s:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        assert toast == messages.REJECTED_TOAST
        await ctx.settle()
        assert await _status(ctx) is JobStatus.SKIPPED
        rejected_keyboard = messages.status_keyboard(messages.STATUS_REJECTED)
        assert ctx.client.edits == [(_REVIEW_CHAT, _PACKAGE_MESSAGE, rejected_keyboard)]
        assert ctx.client.documents == [] and ctx.client.messages == []

    _run(tmp_path, scenario)


def test_approve_and_reject_wait_while_a_new_version_is_being_made(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        (record,) = await ctx.tailored(_job())
        ctx.backend.gate = asyncio.Event()
        await ctx.tap(f"r:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE)
        await asyncio.sleep(0.05)
        assert await ctx.tap(f"a:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE) == messages.QUEUED_TOAST
        assert await ctx.tap(f"s:{record.id}", chat=_REVIEW_CHAT, message_id=_PACKAGE_MESSAGE) == messages.QUEUED_TOAST
        ctx.backend.gate.set()
        await ctx.settle()
        assert await _status(ctx) is JobStatus.TAILORED

    _run(tmp_path, scenario)


# ── No-op buttons ──────────────────────────────────────────────────────────


def test_noop_buttons(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        assert await ctx.tap("n:status") is None
        assert await ctx.tap("n:cancel", chat=_REVIEW_CHAT, message_id=_GUARD_MESSAGE) == messages.CANCELLED_TOAST
        await ctx.settle()
        assert ctx.client.edits == [(_REVIEW_CHAT, _GUARD_MESSAGE, messages.status_keyboard(messages.STATUS_CANCELLED))]

    _run(tmp_path, scenario)


# ── Duplicate guard ────────────────────────────────────────────────────────

_GROUP = "jobvision:1522572"


def _pair(other_status: JobStatus) -> tuple[JobPost, JobPost]:
    """A NOTIFIED Jobinja job and its JobVision duplicate with `other_status`."""
    job = _job(duplicate_group=_GROUP)
    other = _job(
        "1522572",
        platform="jobvision",
        title="AI Engineer (LLM & RAG)",
        url="https://jobvision.ir/jobs/1522572",
        status=other_status,
        duplicate_group=_GROUP,
    )
    return job, other


@pytest.mark.parametrize("status", [JobStatus.TAILORED, JobStatus.APPROVED, JobStatus.APPLY_FAILED, JobStatus.APPLIED])
def test_guard_triggers_when_a_duplicate_has_activity(tmp_path: Path, status: JobStatus) -> None:
    async def scenario(ctx: Ctx) -> None:
        job, other = _pair(status)
        await ctx.db.save_job(job)
        await ctx.db.save_job(other)
        assert await ctx.tap("t:jb:tER9") == messages.GUARD_TOAST
        await ctx.settle()
        assert ctx.backend.calls == []
        (question,) = ctx.client.messages
        assert question["chat_id"] == _REVIEW_CHAT
        expected = "قبلاً برای آگهی مشابه «AI Engineer (LLM &amp; RAG)» در JobVision اقدام کرده‌اید"
        assert question["text"].startswith(expected)
        assert question["reply_markup"] == messages.guard_keyboard(job)
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)


def test_guard_also_applies_to_the_tailor_command(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job, other = _pair(JobStatus.TAILORED)
        await ctx.db.save_job(job)
        await ctx.db.save_job(other)
        await ctx.service.handle_update(_say("/tailor jb:tER9"))
        await ctx.settle()
        assert ctx.backend.calls == []
        assert [message["reply_markup"] for message in ctx.client.messages] == [messages.guard_keyboard(job)]

    _run(tmp_path, scenario)


def test_continue_proceeds_with_tailoring_despite_the_duplicate(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job, other = _pair(JobStatus.APPROVED)
        await ctx.db.save_job(job)
        await ctx.db.save_job(other)
        assert await ctx.tap("d:jb:tER9", chat=_REVIEW_CHAT, message_id=_GUARD_MESSAGE) == messages.STARTED_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [("tER9", 1)]
        assert await _status(ctx) is JobStatus.TAILORED
        # The package caption mentions the other posting.
        assert '<a href="https://jobvision.ir/jobs/1522572">JobVision</a>' in ctx.client.documents[0]["caption"]
        assert ctx.client.edits[-1] == (_REVIEW_CHAT, _GUARD_MESSAGE, messages.status_keyboard(messages.STATUS_SENT))
        # The status checks still apply: a second `d` on the now TAILORED job only resends.
        assert await ctx.tap("d:jb:tER9", chat=_REVIEW_CHAT, message_id=_GUARD_MESSAGE) == messages.RESENDING_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [("tER9", 1)]

    _run(tmp_path, scenario)


@pytest.mark.parametrize("other_status", [None, JobStatus.NOTIFIED, JobStatus.SKIPPED, JobStatus.IGNORED])
def test_no_guard_without_a_group_or_without_activity(tmp_path: Path, other_status: JobStatus | None) -> None:
    async def scenario(ctx: Ctx) -> None:
        if other_status is None:
            await ctx.db.save_job(_job())
        else:
            job, other = _pair(other_status)
            await ctx.db.save_job(job)
            await ctx.db.save_job(other)
        assert await ctx.tap("t:jb:tER9") == messages.STARTED_TOAST
        await ctx.settle()
        assert ctx.backend.calls == [("tER9", 1)]

    _run(tmp_path, scenario)


# ── Polling and the offset ─────────────────────────────────────────────────


def test_offset_is_persisted_atomically_and_reloaded(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        ctx.client.stop = asyncio.Event()
        ctx.client.batches = [[_say("/id", update_id=41), _tap("n:status", update_id=42)]]
        await ctx.service.poll(ctx.client.stop)
        assert ctx.client.offsets == [None, 43]
        assert ctx.service.offset == 43

    _run(tmp_path, scenario)
    state = tmp_path / "bot_state.json"
    assert json.loads(state.read_text(encoding="utf-8")) == {"offset": 43}
    assert not list(tmp_path.glob("*.tmp"))
    assert load_offset(state) == 43

    def reload_check(ctx: Ctx) -> Awaitable[None]:
        async def check() -> None:
            assert ctx.service.offset == 43

        return check()

    _run(tmp_path, reload_check)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"offset": "7"}', '{"offset": true}'])
def test_unreadable_state_files_are_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bot_state.json"
    path.write_text(content, encoding="utf-8")
    assert load_offset(path) is None
    assert load_offset(tmp_path / "missing.json") is None
    save_offset(tmp_path / "nested" / "state.json", 5)
    assert load_offset(tmp_path / "nested" / "state.json") == 5


def test_polling_survives_conflicts_errors_and_failing_handlers(
    tmp_path: Path, logs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def no_wait(stop_event: asyncio.Event, delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(service_module, "_wait_or_stop", no_wait)

    async def scenario(ctx: Ctx) -> None:
        ctx.client.stop = asyncio.Event()
        ctx.client.fail_answers = 1
        ctx.client.batches = [
            BotConflictError("getUpdates", 409, "Conflict: terminated by other getUpdates request"),
            BotApiError("getUpdates", 0, "network error (ConnectError)"),
            [_tap("n:status", update_id=5), _tap("n:status", update_id=6)],
        ]
        await ctx.service.poll(ctx.client.stop)
        assert ctx.service.offset == 7
        assert ctx.client.toasts() == [None]  # the first answer failed; polling went on

    _run(tmp_path, scenario)
    assert waits == [30.0, 5.0]
    assert any("ERROR" in line and "conflict" in line.lower() for line in logs)
    assert any("Failed to handle update 5" in line for line in logs)


# ── Client (httpx.MockTransport) ───────────────────────────────────────────


def _client_run(
    handler: Callable[[httpx.Request], httpx.Response], call: Callable[[TelegramBotClient], Awaitable[Any]]
) -> Any:
    """Run one client call against a mock transport."""

    async def scenario() -> Any:
        async with TelegramBotClient(
            _settings(), poll_timeout_seconds=25, min_send_interval=0, transport=httpx.MockTransport(handler)
        ) as client:
            return await call(client)

    return asyncio.run(scenario())


def _ok(result: Any) -> httpx.Response:
    """A successful Bot API response."""
    return httpx.Response(200, json={"ok": True, "result": result})


def test_client_get_updates_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok([{"update_id": 1}, "junk"])

    updates = _client_run(handler, lambda client: client.get_updates(17, ["callback_query", "message"]))
    assert updates == [{"update_id": 1}]
    (request,) = seen
    assert request.url.path == f"/bot{_TOKEN}/getUpdates"
    expected = {"timeout": 25, "allowed_updates": ["callback_query", "message"], "offset": 17}
    assert json.loads(request.content) == expected
    assert request.extensions["timeout"]["read"] == 40.0  # longer than the 25 s poll


def test_client_conflict_raises_bot_conflict_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": (
                    "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"
                ),
            },
        )

    with pytest.raises(BotConflictError, match="another bot instance is polling getUpdates, or a webhook is set"):
        _client_run(handler, lambda client: client.get_updates(None, ["message"]))


def test_client_get_updates_network_error_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to reach {request.url}")

    with pytest.raises(BotApiError) as excinfo:
        _client_run(handler, lambda client: client.get_updates(None, ["message"]))
    assert "SECRET-TOKEN" not in str(excinfo.value)


def test_client_send_document_is_a_multipart_upload(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"message_id": 9})

    keyboard = messages.review_keyboard(3, can_regenerate=True)
    result = _client_run(
        handler,
        lambda client: client.send_document(
            _REVIEW_CHAT, pdf, filename="Negar_Resume_en.pdf", caption="<b>رزومه</b>", reply_markup=keyboard
        ),
    )
    assert result == {"message_id": 9}
    (request,) = seen
    assert request.url.path.endswith("/sendDocument")
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="chat_id"\r\n\r\n111' in body
    assert b'name="parse_mode"\r\n\r\nHTML' in body
    assert 'name="caption"\r\n\r\n<b>رزومه</b>'.encode() in body
    assert b'filename="Negar_Resume_en.pdf"' in body and b"%PDF-1.7 test" in body
    assert f'name="reply_markup"\r\n\r\n{json.dumps(keyboard, ensure_ascii=False)}'.encode() in body


def test_client_send_message_and_answer_payloads() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, **json.loads(request.content)})
        return _ok(True if request.url.path.endswith("answerCallbackQuery") else {"message_id": 1})

    async def calls(client: TelegramBotClient) -> None:
        await client.send_message(_REVIEW_CHAT, "<b>hi</b>", reply_markup=messages.status_keyboard("x"))
        await client.answer_callback_query("q1", "ت" * 300)
        await client.answer_callback_query("q2")

    _client_run(handler, calls)
    message, long_toast, silent = seen
    assert message["parse_mode"] == "HTML" and message["link_preview_options"] == {"is_disabled": True}
    assert message["reply_markup"] == messages.status_keyboard("x")
    assert len(long_toast["text"]) == 200
    assert "text" not in silent


def test_client_edit_ignores_not_modified_but_raises_other_errors() -> None:
    descriptions = iter(
        [
            "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
            "Bad Request: message to edit not found",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": next(descriptions)})

    async def calls(client: TelegramBotClient) -> None:
        await client.edit_message_reply_markup(_CHANNEL, 5, None)
        with pytest.raises(BotApiError, match="message to edit not found"):
            await client.edit_message_reply_markup(_CHANNEL, 5, None)

    _client_run(handler, calls)


def test_client_retries_429_after_retry_after(monkeypatch: pytest.MonkeyPatch, logs: list[str]) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module, "_sleep", fake_sleep)
    responses = [
        httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}},
        ),
        httpx.Response(502, json={"ok": False, "description": "Bad Gateway"}),
        _ok({"message_id": 4}),
    ]
    result = _client_run(lambda request: responses.pop(0), lambda client: client.send_message(_REVIEW_CHAT, "hi"))
    assert result == {"message_id": 4}
    assert sleeps[0] == 3.0 and len(sleeps) == 2
    assert not any("SECRET-TOKEN" in line for line in logs)


# ── Entry points ───────────────────────────────────────────────────────────


def test_bot_disabled_reasons(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert main._bot_disabled_reason(_settings(), config) is None
    no_users = _settings(TELEGRAM_ALLOWED_USER_IDS="")
    assert "TELEGRAM_ALLOWED_USER_IDS" in (main._bot_disabled_reason(no_users, config) or "")
    assert "'llm'" in (main._bot_disabled_reason(_settings(), _config(tmp_path, llm=False)) or "")
    assert "GEMINI_API_KEY" in (main._bot_disabled_reason(_settings(GEMINI_API_KEY=None), config) or "")


def test_bot_mode_starts_without_allowed_users_so_start_can_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[str]
) -> None:
    started: list[Settings] = []

    async def fake_run_bot(settings: Settings, config: AppConfig, stop_event: asyncio.Event) -> None:
        started.append(settings)

    monkeypatch.setattr(main, "run_bot", fake_run_bot)
    exit_code = asyncio.run(main._run_bot_only(_settings(TELEGRAM_ALLOWED_USER_IDS=""), _config(tmp_path)))
    assert exit_code == 0 and len(started) == 1
    assert any("only answers /start and /id" in line for line in logs)


def test_bot_mode_warns_when_tailoring_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[str]
) -> None:
    async def fake_run_bot(settings: Settings, config: AppConfig, stop_event: asyncio.Event) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_bot", fake_run_bot)
    exit_code = asyncio.run(main._run_bot_only(_settings(GEMINI_API_KEY=None), _config(tmp_path)))
    assert exit_code == 1  # run_bot failed
    assert any("tailoring will fail" in line and "GEMINI_API_KEY" in line for line in logs)


def test_bot_flag_parses_and_excludes_other_modes() -> None:
    assert main._parse_args(["--bot"]).bot is True
    with pytest.raises(SystemExit):
        main._parse_args(["--bot", "--daemon"])


# ── Production pipeline (fake LLM, real rendering) ─────────────────────────


class _FakeLLM(LLMClient):
    """Returns the fixture plan."""

    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        """Return the fixture plan as the structured answer."""
        value = schema.model_validate(_PLAN_DATA)
        return LLMResult[schema](
            value=value,
            model="fake-llm",
            usage=LLMUsage(prompt_tokens=1, output_tokens=1, total_tokens=2),
            latency_seconds=0,
        )


def test_tailoring_pipeline_saves_and_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "create_llm_client", lambda config, settings: _FakeLLM())

    async def scenario() -> TailoringOutcome:
        async with Database(tmp_path / "jobs.db") as db:
            await db.init_db()
            job = _job(raw_description="We need a Python backend engineer.")
            await db.save_job(job)
            pipeline = TailoringPipeline(_settings(), _config(tmp_path), db)
            outcome = await pipeline.tailor(job, 1)
            stored = await db.get_tailored_resume(outcome.record.id or 0)
            assert stored is not None
            assert stored.clean_pdf_path == str(outcome.clean_pdf)
            assert stored.review_pdf_path == str(outcome.review_pdf)
            return outcome

    outcome = asyncio.run(scenario())
    assert outcome.record.version == 1 and outcome.record.llm_model == "fake-llm"
    assert outcome.clean_pdf.read_bytes().startswith(b"%PDF")
    assert outcome.review_pdf.read_bytes().startswith(b"%PDF")
    assert outcome.clean_pdf.parent == tmp_path / "resumes"


# ── Shutdown ───────────────────────────────────────────────────────────────


def test_cancelled_tailoring_restores_the_buttons(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx) -> None:
        job = _job()
        await ctx.db.save_job(job)
        ctx.backend.gate = asyncio.Event()  # never released: the tailoring is still running at shutdown
        await ctx.tap("t:jb:tER9")
        await asyncio.sleep(0.05)
        await ctx.service.shutdown(grace=0.1)
        assert ctx.client.edits[-1] == (_CHANNEL, _NOTIFICATION_MESSAGE, messages.job_keyboard(job))
        assert await _status(ctx) is JobStatus.NOTIFIED

    _run(tmp_path, scenario)


class _ContextClient(FakeClient):
    """A fake client that is also an async context manager, standing in for `TelegramBotClient`."""

    instances: list["_ContextClient"] = []
    next_batches: list[list[dict[str, Any]] | Exception] = []
    next_stop: asyncio.Event | None = None

    def __init__(self, settings: Settings, *, poll_timeout_seconds: int) -> None:
        """Take the scripted updates and stop event, and record the instance for inspection."""
        super().__init__()
        self.poll_timeout_seconds = poll_timeout_seconds
        self.batches = list(_ContextClient.next_batches)
        self.stop = _ContextClient.next_stop
        _ContextClient.instances.append(self)

    async def __aenter__(self) -> "_ContextClient":
        """Nothing to open."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Nothing to close."""


def test_run_bot_handles_updates_and_stops_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ContextClient.instances = []
    monkeypatch.setattr(service_module, "TelegramBotClient", _ContextClient)
    backends: list[FakeBackend] = []

    def make_backend(settings: Settings, config: AppConfig, db: Database) -> FakeBackend:
        backend = FakeBackend(db, load_master_resume(_FIXTURE), tmp_path / "pdf")
        backends.append(backend)
        return backend

    monkeypatch.setattr(service_module, "TailoringPipeline", make_backend)
    settings = _settings(DB_PATH=str(tmp_path / "jobs.db"))
    config = _config(tmp_path)

    async def scenario() -> None:
        async with Database(settings.DB_PATH) as db:
            await db.init_db()
            await db.save_job(_job())
        stop = asyncio.Event()
        _ContextClient.next_stop = stop  # the fake sets it when the script is done
        _ContextClient.next_batches = [[_say("/id", update_id=8), _tap("t:jb:tER9", update_id=9)]]
        await asyncio.wait_for(service_module.run_bot(settings, config, stop), timeout=10)
        client = _ContextClient.instances[0]
        assert client.poll_timeout_seconds == config.bot.poll_timeout_seconds
        assert backends[0].calls == [("tER9", 1)]  # in-flight tailoring finished during shutdown
        assert len(client.documents) == 1

    asyncio.run(scenario())
    assert load_offset(config.bot.state_path) == 10


def test_run_bot_cancels_in_flight_work_after_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ContextClient.instances = []
    monkeypatch.setattr(service_module, "TelegramBotClient", _ContextClient)
    monkeypatch.setattr(service_module, "SHUTDOWN_GRACE_S", 0.2)
    backends: list[FakeBackend] = []

    def make_backend(settings: Settings, config: AppConfig, db: Database) -> FakeBackend:
        backend = FakeBackend(db, load_master_resume(_FIXTURE), tmp_path / "pdf")
        backend.gate = asyncio.Event()  # the tailoring never finishes on its own
        backends.append(backend)
        return backend

    monkeypatch.setattr(service_module, "TailoringPipeline", make_backend)
    settings = _settings(DB_PATH=str(tmp_path / "jobs.db"))

    async def scenario() -> None:
        async with Database(settings.DB_PATH) as db:
            await db.init_db()
            await db.save_job(_job())
        stop = asyncio.Event()
        _ContextClient.next_stop = stop
        _ContextClient.next_batches = [[_tap("t:jb:tER9", update_id=1)]]
        await asyncio.wait_for(service_module.run_bot(settings, _config(tmp_path), stop), timeout=10)
        client = _ContextClient.instances[0]
        assert backends[0].calls == [("tER9", 1)]
        assert client.documents == []
        assert client.edits[-1] == (_CHANNEL, _NOTIFICATION_MESSAGE, messages.job_keyboard(_job()))
        async with Database(settings.DB_PATH) as db:
            job = await db.get_job("jobinja", "tER9")
            assert job is not None and job.status is JobStatus.NOTIFIED

    asyncio.run(scenario())
