"""The review bot: long polling, authorization, and the tailor / regenerate / approve / reject handlers.

Flow: a `✍️ تنظیم رزومه` tap (or `/tailor jv:123`) on a `NOTIFIED` job queues a
tailoring. One worker runs tailorings one at a time: tailor with the LLM, save
the record, render both PDFs, send the review package to the private review
chat, then move the job `NOTIFIED → TAILORED`. Under the package, `✅` approves
(`TAILORED → APPROVED`, then `on_approved` sends the clean PDF, the cover
letter, and the job link), `🔁` makes a new version, and `❌` rejects
(`TAILORED → SKIPPED`).

Rules:
- Only `TELEGRAM_ALLOWED_USER_IDS` may act; others get «⛔️ مجاز نیستید» and
  their numeric ID is logged.
- Every status change is a compare-and-set (`transition_status`), so double
  taps and restarts never repeat an action; a job that is already queued or
  running is never queued twice.
- Callbacks are answered at once (handlers only touch the database); sending
  and tailoring run as background tasks.
- Resumes, review packages, and cover letters go only to
  `Settings.review_chat_id`, never to the notification channel.
- Nothing logs resume content, prompts, or cover letters; only IDs and counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from loguru import logger
from playwright.async_api import async_playwright

from src.bot import messages
from src.bot.callbacks import decode_callback, parse_job_ref, parse_resume_ref
from src.bot.client import BotApiError, BotConflictError, TelegramBotClient
from src.database import Database
from src.duplicates import find_prior_activity
from src.llm.base import LLMError
from src.llm.factory import create_llm_client
from src.models import AppConfig, JobPost, JobStatus, Settings, TailoredResumeRecord
from src.resume.changes import ChangeSet
from src.resume.models import MasterResume, TailoringPlan, load_master_resume
from src.resume.render import render_tailored_resume
from src.resume.tailor import TailoringError, tailor_resume

ALLOWED_UPDATES = ("callback_query", "message")
SHUTDOWN_GRACE_S = 60.0

_ERROR_BACKOFF_S = (5.0, 60.0)
_RESTORE_TIMEOUT_S = 5.0
_CONFLICT_BACKOFF_S = (30.0, 300.0)

OriginKind = Literal["notification", "guard", "package", "none"]


# ── Tailoring backend ──────────────────────────────────────────────────────


@dataclass(slots=True)
class TailoringOutcome:
    """A saved tailored resume (with its ID and PDF paths) and what is needed to present it."""

    record: TailoredResumeRecord
    plan: TailoringPlan
    changes: ChangeSet
    warnings: list[str]
    clean_pdf: Path
    review_pdf: Path


class TailoringBackend(Protocol):
    """Generates and renders tailored resumes; `TailoringPipeline` in production, a fake in tests."""

    def load_master(self) -> MasterResume:
        """Load the master resume."""

    async def tailor(self, job: JobPost, version: int) -> TailoringOutcome:
        """Tailor the master resume to a job, save it as `version`, and render both PDFs."""

    async def render(self, record: TailoredResumeRecord) -> tuple[Path, Path]:
        """Render (again) the clean and review PDFs of a saved record."""


class TailoringPipeline:
    """The production backend: LLM tailoring, the database record, and headless Chromium rendering."""

    def __init__(self, settings: Settings, config: AppConfig, db: Database) -> None:
        """Store the settings, config, and open database."""
        self._settings = settings
        self._config = config
        self._db = db

    def load_master(self) -> MasterResume:
        """Load the master resume from `tailoring.master_resume_path` (re-read on every call)."""
        return load_master_resume(self._config.tailoring.master_resume_path)

    async def tailor(self, job: JobPost, version: int) -> TailoringOutcome:
        """Tailor, save the record as `version`, and render both PDFs.

        Raises:
            LLMError, TailoringError: If tailoring fails.
            ValueError, OSError: If the configuration or master resume is unusable.
        """
        master = self.load_master()
        async with create_llm_client(self._config, self._settings) as llm:
            result = await tailor_resume(job, master, llm, self._config)
        record = TailoredResumeRecord(
            platform=job.platform,
            external_id=job.external_id,
            version=version,
            language=result.language,
            llm_model=result.model,
            plan_json=result.plan.model_dump_json(),
            changes_json=result.changes.model_dump_json(),
        )
        resume_id = await self._db.save_tailored_resume(record)
        clean, review = await self._render_record(resume_id, master, result.warnings)
        saved = record.model_copy(
            update={"id": resume_id, "clean_pdf_path": str(clean), "review_pdf_path": str(review)}
        )
        return TailoringOutcome(
            record=saved,
            plan=result.plan,
            changes=result.changes,
            warnings=result.warnings,
            clean_pdf=clean,
            review_pdf=review,
        )

    async def render(self, record: TailoredResumeRecord) -> tuple[Path, Path]:
        """Render the PDFs of a saved record again (e.g. when the files were deleted)."""
        if record.id is None:
            raise ValueError("The tailored resume record has no ID")
        return await self._render_record(record.id, self.load_master(), None)

    async def _render_record(
        self, resume_id: int, master: MasterResume, warnings: list[str] | None
    ) -> tuple[Path, Path]:
        """Launch headless Chromium and render both PDFs of a saved record."""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                return await render_tailored_resume(
                    resume_id,
                    self._db,
                    master,
                    browser,
                    output_dir=self._config.tailoring.output_dir,
                    warnings=warnings,
                )
            finally:
                await browser.close()


# ── Service ────────────────────────────────────────────────────────────────


class BotClient(Protocol):
    """The Bot API calls the service needs (implemented by `TelegramBotClient`)."""

    async def get_updates(self, offset: int | None, allowed_updates: Sequence[str]) -> list[dict[str, Any]]:
        """Long-poll for updates."""

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Answer a callback query with an optional toast."""

    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send an HTML message."""

    async def send_document(
        self,
        chat_id: int | str,
        path: Path,
        *,
        filename: str | None = None,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload a document."""

    async def edit_message_reply_markup(
        self, chat_id: int | str, message_id: int, reply_markup: dict[str, Any] | None
    ) -> None:
        """Replace a message's inline keyboard."""


@dataclass(frozen=True, slots=True)
class Origin:
    """The message whose buttons were tapped (its keyboard shows progress)."""

    chat_id: int | str
    message_id: int
    kind: OriginKind


@dataclass(frozen=True, slots=True)
class _TailorRequest:
    """One queued tailoring: a first version (`tailor`) or a new version (`regenerate`)."""

    kind: Literal["tailor", "regenerate"]
    platform: str
    external_id: str
    origin: Origin | None
    replaces: int | None = None  # the resume ID whose package was tapped (regenerate)

    @property
    def key(self) -> tuple[str, str]:
        """The job key used to avoid queuing the same job twice."""
        return self.platform, self.external_id


class _Decision(Enum):
    """The outcome of a tailoring request, shown as a toast (button) or a reply (`/tailor`)."""

    STARTED = "started"
    QUEUED = "queued"
    RESENT = "resent"
    GUARDED = "guarded"
    NOT_FOUND = "not_found"
    WRONG_STATUS = "wrong_status"


@dataclass(slots=True)
class _State:
    """Mutable runtime state of the service."""

    offset: int | None = None
    pending: set[tuple[str, str]] = field(default_factory=set)
    busy: bool = False
    queue: asyncio.Queue[_TailorRequest] | None = None
    worker: asyncio.Task[None] | None = None
    background: set[asyncio.Task[None]] = field(default_factory=set)


def load_offset(path: Path) -> int | None:
    """Read the persisted update offset, or None if there is none (or the file is unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable bot state file {}", path)
        return None
    offset = data.get("offset") if isinstance(data, dict) else None
    return offset if isinstance(offset, int) and not isinstance(offset, bool) else None


def save_offset(path: Path, offset: int) -> None:
    """Persist the update offset atomically (write a temporary file, then rename it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    os.replace(tmp, path)


class BotService:
    """Authorizes and dispatches bot updates, and runs the review flow handlers."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: AppConfig,
        client: BotClient,
        db: Database,
        backend: TailoringBackend,
        state_path: Path,
    ) -> None:
        """Store the collaborators and load the persisted update offset.

        With no allowed users the bot only answers /start and /id (so a new
        user can learn their ID) and refuses everything else.
        """
        review_chat_id = settings.review_chat_id
        self._config = config
        self._client = client
        self._db = db
        self._backend = backend
        self._allowed = settings.allowed_user_ids
        # Settings validates the review chat as a positive numeric user ID; it
        # exists exactly when some user is allowed, so only they can reach it.
        self._review_chat_id = int(review_chat_id) if review_chat_id is not None else None
        self._max_versions = config.tailoring.max_versions_per_job
        self._state_path = state_path
        self._state = _State(offset=load_offset(state_path))
        self._callback_handlers: dict[str, Callable[[str, Origin | None], Awaitable[str | None]]] = {
            "t": self._on_tailor_button,
            "d": self._on_continue_button,
            "r": self._on_regenerate_button,
            "a": self._on_approve_button,
            "s": self._on_reject_button,
            "n": self._on_noop_button,
        }

    @property
    def offset(self) -> int | None:
        """The next update ID to request."""
        return self._state.offset

    @property
    def _review_chat(self) -> int:
        """The private chat for resumes (it exists whenever an authorized user can act)."""
        if self._review_chat_id is None:
            raise RuntimeError("No review chat: TELEGRAM_ALLOWED_USER_IDS is empty")
        return self._review_chat_id

    # ── Polling ─────────────────────────────────────────────────────────────

    async def poll(self, stop_event: asyncio.Event) -> None:
        """Long-poll and handle updates until `stop_event` is set (errors back off and retry)."""
        error_delay, conflict_delay = _ERROR_BACKOFF_S[0], _CONFLICT_BACKOFF_S[0]
        logger.info("Review bot polling started (offset {})", self._state.offset)
        while not stop_event.is_set():
            try:
                updates = await self._client.get_updates(self._state.offset, ALLOWED_UPDATES)
            except BotConflictError as exc:
                logger.error("Telegram getUpdates conflict: {}. Retrying in {:.0f}s", exc, conflict_delay)
                await _wait_or_stop(stop_event, conflict_delay)
                conflict_delay = min(conflict_delay * 2, _CONFLICT_BACKOFF_S[1])
                continue
            except BotApiError as exc:
                logger.warning("Telegram getUpdates failed ({}); retrying in {:.0f}s", exc, error_delay)
                await _wait_or_stop(stop_event, error_delay)
                error_delay = min(error_delay * 2, _ERROR_BACKOFF_S[1])
                continue
            except Exception:
                logger.exception("Unexpected error while polling Telegram; retrying in {:.0f}s", error_delay)
                await _wait_or_stop(stop_event, error_delay)
                error_delay = min(error_delay * 2, _ERROR_BACKOFF_S[1])
                continue
            error_delay, conflict_delay = _ERROR_BACKOFF_S[0], _CONFLICT_BACKOFF_S[0]
            for update in updates:
                await self.handle_update(update)
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._state.offset = update_id + 1
                    save_offset(self._state_path, self._state.offset)

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Dispatch one update; a failing handler is logged and never stops polling."""
        try:
            if isinstance(update.get("callback_query"), dict):
                await self._on_callback(update["callback_query"])
            elif isinstance(update.get("message"), dict):
                await self._on_message(update["message"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to handle update {}", update.get("update_id"))

    async def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        """Wait up to `grace` seconds for queued and in-flight work, then cancel what is left."""
        state = self._state
        waiters: set[asyncio.Task[Any]] = set(state.background)
        if state.worker is not None and (state.busy or state.pending):
            waiters.add(asyncio.create_task(self._drain(), name="bot-drain"))
        if waiters and grace > 0:
            logger.info("Waiting up to {:.0f}s for {} bot task(s) to finish", grace, len(waiters))
            _, not_done = await asyncio.wait(waiters, timeout=grace)
            if not_done:
                logger.warning("Cancelling {} unfinished bot task(s)", len(not_done))
        candidates = (*waiters, *state.background, state.worker)
        leftovers = [task for task in candidates if task is not None and not task.done()]
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
        state.worker = None
        logger.info("Review bot stopped")

    async def _drain(self) -> None:
        """Wait until the tailoring queue is empty and the worker is idle."""
        while self._state.busy or self._state.pending:
            await asyncio.sleep(0.2)

    # ── Updates ─────────────────────────────────────────────────────────────

    async def _on_callback(self, query: dict[str, Any]) -> None:
        """Authorize, decode, and handle a button tap, then answer it with a toast."""
        query_id = str(query.get("id", ""))
        sender = query.get("from") if isinstance(query.get("from"), dict) else {}
        user_id = sender.get("id")
        if not isinstance(user_id, int) or user_id not in self._allowed:
            logger.warning("Refused a button tap from unauthorized Telegram user {}", user_id)
            await self._answer(query_id, messages.UNAUTHORIZED_TOAST)
            return

        origin = _origin_of(query.get("message"))
        decoded = decode_callback(query.get("data") or "")
        handler = self._callback_handlers.get(decoded[0]) if decoded is not None else None
        if decoded is None or handler is None:
            logger.info("Stale or unknown button from user {}: {!r}", user_id, query.get("data"))
            await self._answer(query_id, messages.STALE_TOAST)
            return
        action, ref = decoded
        logger.info("Button {} for {} from user {}", action, ref, user_id)
        toast = await handler(ref, origin)
        await self._answer(query_id, toast)

    async def _on_message(self, message: dict[str, Any]) -> None:
        """Handle private commands: /start, /id, /tailor, and help."""
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if chat.get("type") != "private":
            return
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        text = message.get("text")
        if not isinstance(user_id, int) or chat_id is None or not isinstance(text, str):
            return
        command, _, argument = text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        authorized = user_id in self._allowed

        if command in ("/start", "/id"):
            logger.info("ID request from Telegram user {} (authorized: {})", user_id, authorized)
            await self._reply(chat_id, messages.id_reply(user_id, authorized=authorized))
            return
        if not authorized:
            logger.warning("Refused a message from unauthorized Telegram user {}", user_id)
            if command.startswith("/"):
                await self._reply(chat_id, messages.UNAUTHORIZED_TOAST)
            return
        if command == "/tailor":
            await self._on_tailor_command(chat_id, argument.strip())
            return
        await self._reply(chat_id, messages.HELP_MESSAGE)

    async def _on_tailor_command(self, chat_id: int | str, argument: str) -> None:
        """`/tailor <jv|jb>:<external_id>`: the same flow as the tailor button, answered with a message."""
        parsed = parse_job_ref(argument) if argument else None
        if parsed is None:
            await self._reply(chat_id, messages.TAILOR_USAGE)
            return
        decision, job = await self._request_tailoring(parsed[0], parsed[1], origin=None, guard=True)
        if decision is _Decision.NOT_FOUND or job is None:
            await self._reply(chat_id, messages.job_not_found_message(argument))
        elif decision is _Decision.WRONG_STATUS:
            await self._reply(chat_id, messages.wrong_status_message(job))
        elif decision in (_Decision.STARTED, _Decision.QUEUED):
            await self._reply(chat_id, messages.queued_message(job, queued=decision is _Decision.QUEUED))
        elif decision is _Decision.RESENT:
            await self._reply(chat_id, messages.RESENDING_TOAST)
        # GUARDED: the guard question goes to the review chat.

    # ── Button handlers (return the toast) ──────────────────────────────────

    async def _on_tailor_button(self, ref: str, origin: Origin | None) -> str:
        """`t`: tailor a `NOTIFIED` job (or resend the package of a `TAILORED` one), behind the duplicate guard."""
        parsed = parse_job_ref(ref)
        if parsed is None:
            return messages.STALE_TOAST
        notification = Origin(origin.chat_id, origin.message_id, "notification") if origin else None
        decision, _ = await self._request_tailoring(parsed[0], parsed[1], origin=notification, guard=True)
        return _toast(decision)

    async def _on_continue_button(self, ref: str, origin: Origin | None) -> str:
        """`d`: tailor despite a probable duplicate with prior activity (the status checks still apply)."""
        parsed = parse_job_ref(ref)
        if parsed is None:
            return messages.STALE_TOAST
        guard_origin = Origin(origin.chat_id, origin.message_id, "guard") if origin else None
        decision, _ = await self._request_tailoring(parsed[0], parsed[1], origin=guard_origin, guard=False)
        return _toast(decision)

    async def _on_regenerate_button(self, ref: str, origin: Origin | None) -> str:
        """`r`: queue a new version, only from the latest version and below `max_versions_per_job`."""
        resolved = await self._resolve_latest(ref)
        if resolved is None:
            return messages.STALE_TOAST
        job, record = resolved
        if job.status is not JobStatus.TAILORED:
            return messages.STALE_TOAST
        if (job.platform, job.external_id) in self._state.pending:
            return messages.QUEUED_TOAST
        if await self._db.count_tailored_resumes(job.platform, job.external_id) >= self._max_versions:
            return messages.max_versions_toast(self._max_versions)
        package = Origin(origin.chat_id, origin.message_id, "package") if origin else None
        queued = self._enqueue(_TailorRequest("regenerate", job.platform, job.external_id, package, record.id))
        return messages.QUEUED_TOAST if queued else messages.REGENERATING_TOAST

    async def _on_approve_button(self, ref: str, origin: Origin | None) -> str:
        """`a`: approve the latest version (`TAILORED → APPROVED`), then run `on_approved` in the background."""
        resolved = await self._resolve_latest(ref)
        if resolved is None:
            return messages.STALE_TOAST
        job, record = resolved
        if (job.platform, job.external_id) in self._state.pending:
            return messages.QUEUED_TOAST
        if not await self._db.transition_status(
            job.platform, job.external_id, {JobStatus.TAILORED}, JobStatus.APPROVED
        ):
            return messages.STALE_TOAST
        if record.id is not None:
            await self._db.mark_tailored_resume_approved(record.id)
        if origin is not None:
            self._spawn(self._edit(origin, messages.status_keyboard(messages.STATUS_APPROVED)), "edit-approved")
        approved = job.model_copy(update={"status": JobStatus.APPROVED})
        self._spawn(self.on_approved(approved, record), f"approved-{record.id}")
        return messages.APPROVED_TOAST

    async def _on_reject_button(self, ref: str, origin: Origin | None) -> str:
        """`s`: reject the job (`TAILORED → SKIPPED`) from its latest version's package."""
        resolved = await self._resolve_latest(ref)
        if resolved is None:
            return messages.STALE_TOAST
        job, _ = resolved
        if (job.platform, job.external_id) in self._state.pending:
            return messages.QUEUED_TOAST
        if not await self._db.transition_status(
            job.platform, job.external_id, {JobStatus.TAILORED}, JobStatus.SKIPPED
        ):
            return messages.STALE_TOAST
        if origin is not None:
            self._spawn(self._edit(origin, messages.status_keyboard(messages.STATUS_REJECTED)), "edit-rejected")
        return messages.REJECTED_TOAST

    async def _on_noop_button(self, ref: str, origin: Origin | None) -> str | None:
        """`n`: a status button does nothing; the guard's cancel button closes the question."""
        if ref == messages.NOOP_CANCEL and origin is not None:
            self._spawn(self._edit(origin, messages.status_keyboard(messages.STATUS_CANCELLED)), "edit-cancelled")
            return messages.CANCELLED_TOAST
        return None

    # ── Approval ────────────────────────────────────────────────────────────

    async def on_approved(self, job: JobPost, record: TailoredResumeRecord) -> None:
        """Deliver an approved resume to the review chat: the clean PDF, the cover letter, and the job link.

        The PDF is named `{Name}_Resume_{lang}.pdf` after the master resume's
        name. (Task 12 extends this step for automatic submission.)
        """
        master = self._backend.load_master()
        plan = TailoringPlan.model_validate_json(record.plan_json)
        clean = Path(record.clean_pdf_path) if record.clean_pdf_path else None
        if clean is None or not clean.is_file():
            clean, _ = await self._backend.render(record)
        filename = messages.clean_pdf_filename(master.contact.name.text_for(record.language), record.language)
        await self._client.send_document(
            self._review_chat, clean, filename=filename, caption=messages.approved_caption(job, record.version)
        )
        if plan.cover_letter and plan.cover_letter.strip():
            for chunk in messages.cover_letter_messages(plan.cover_letter):
                await self._client.send_message(self._review_chat, chunk)
        await self._client.send_message(self._review_chat, messages.manual_apply_message(job))
        logger.info("Delivered approved resume {} for job {}:{}", record.id, job.platform, job.external_id)

    # ── Tailoring ───────────────────────────────────────────────────────────

    async def _request_tailoring(
        self, platform: str, external_id: str, *, origin: Origin | None, guard: bool
    ) -> tuple[_Decision, JobPost | None]:
        """Decide what a tailor request does now: queue it, resend a package, ask the guard, or refuse."""
        job = await self._db.get_job(platform, external_id)
        if job is None:
            return _Decision.NOT_FOUND, None
        if (job.platform, job.external_id) in self._state.pending:
            return _Decision.QUEUED, job
        if job.status is JobStatus.TAILORED:
            latest = await self._db.get_latest_tailored_resume(job.platform, job.external_id)
            if latest is None:
                return _Decision.WRONG_STATUS, job
            self._spawn(self._resend_package(job, latest), f"resend-{latest.id}")
            return _Decision.RESENT, job
        if job.status is not JobStatus.NOTIFIED:
            return _Decision.WRONG_STATUS, job
        if guard:
            prior = await self._prior_activity(job)
            if prior is not None:
                logger.info(
                    "Duplicate guard for {}:{}: {}:{} is {}",
                    job.platform,
                    job.external_id,
                    prior.platform,
                    prior.external_id,
                    prior.status.value,
                )
                self._spawn(self._send_guard(job, prior), f"guard-{job.external_id}")
                return _Decision.GUARDED, job
        queued = self._enqueue(_TailorRequest("tailor", job.platform, job.external_id, origin))
        return (_Decision.QUEUED if queued else _Decision.STARTED), job

    def _enqueue(self, request: _TailorRequest) -> bool:
        """Queue a tailoring; return True if it waits behind other work."""
        state = self._state
        queued = state.busy or bool(state.pending)
        state.pending.add(request.key)
        if state.queue is None:
            state.queue = asyncio.Queue()
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._work(state.queue), name="bot-tailoring-worker")
        state.queue.put_nowait(request)
        return queued

    async def _work(self, queue: asyncio.Queue[_TailorRequest]) -> None:
        """The single tailoring worker: run queued requests one at a time."""
        while True:
            request = await queue.get()
            self._state.busy = True
            try:
                if request.kind == "tailor":
                    await self._run_tailor(request)
                else:
                    await self._run_regenerate(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Tailoring request for {}:{} failed", request.platform, request.external_id)
            finally:
                self._state.busy = False
                self._state.pending.discard(request.key)
                queue.task_done()

    async def _run_tailor(self, request: _TailorRequest) -> None:
        """Tailor a `NOTIFIED` job, send its review package, and move it to `TAILORED`."""
        job = await self._db.get_job(request.platform, request.external_id)
        if job is None:
            return
        if job.status is not JobStatus.NOTIFIED:
            logger.info("Skipping tailoring of {}:{}: status is now {}", job.platform, job.external_id, job.status)
            return
        count = await self._db.count_tailored_resumes(job.platform, job.external_id)
        if count >= self._max_versions:
            await self._send_failure(job, messages.max_versions_toast(self._max_versions))
            return
        await self._show_progress(request.origin, job, messages.STATUS_TAILORING)
        try:
            outcome = await self._backend.tailor(job, count + 1)
            await self._send_outcome(job, outcome)
        except asyncio.CancelledError:
            await self._restore_after_cancel(request, job)
            raise
        except Exception as exc:
            await self._report_failure(job, exc)
            await self._restore(request, job)
            return
        moved = await self._db.transition_status(
            job.platform, job.external_id, {JobStatus.NOTIFIED}, JobStatus.TAILORED
        )
        if not moved:
            logger.warning("Job {}:{} was not NOTIFIED any more after tailoring", job.platform, job.external_id)
        await self._show_progress(request.origin, job, messages.STATUS_SENT)

    async def _run_regenerate(self, request: _TailorRequest) -> None:
        """Make a new version of a `TAILORED` job's resume and send its package (`TAILORED → TAILORED`)."""
        job = await self._db.get_job(request.platform, request.external_id)
        latest = await self._db.get_latest_tailored_resume(request.platform, request.external_id)
        if job is None or latest is None or job.status is not JobStatus.TAILORED or latest.id != request.replaces:
            logger.info("Skipping regeneration of {}:{}: no longer current", request.platform, request.external_id)
            return
        count = await self._db.count_tailored_resumes(job.platform, job.external_id)
        if count >= self._max_versions:
            await self._send_failure(job, messages.max_versions_toast(self._max_versions))
            return
        await self._edit(request.origin, messages.status_keyboard(messages.STATUS_REGENERATING))
        try:
            outcome = await self._backend.tailor(job, count + 1)
            await self._send_outcome(job, outcome)
        except asyncio.CancelledError:
            await self._restore_after_cancel(request, job)
            raise
        except Exception as exc:
            await self._report_failure(job, exc)
            await self._restore(request, job)
            return
        if not await self._db.transition_status(
            job.platform, job.external_id, {JobStatus.TAILORED}, JobStatus.TAILORED
        ):
            logger.warning("Job {}:{} was not TAILORED any more after regeneration", job.platform, job.external_id)
        superseded = messages.STATUS_SUPERSEDED.format(version=outcome.record.version)
        await self._edit(request.origin, messages.status_keyboard(superseded))

    async def _send_outcome(self, job: JobPost, outcome: TailoringOutcome) -> None:
        """Send the review package of a fresh tailoring."""
        await self._send_package(
            job, outcome.record, outcome.plan, outcome.changes, outcome.warnings, outcome.review_pdf
        )

    async def _resend_package(self, job: JobPost, record: TailoredResumeRecord) -> None:
        """Send the latest version's review package again (rendering the PDFs again if they are gone)."""
        plan = TailoringPlan.model_validate_json(record.plan_json)
        changes = ChangeSet.model_validate_json(record.changes_json)
        review = Path(record.review_pdf_path) if record.review_pdf_path else None
        if review is None or not review.is_file():
            _, review = await self._backend.render(record)
        await self._send_package(job, record, plan, changes, [], review)

    async def _send_package(
        self,
        job: JobPost,
        record: TailoredResumeRecord,
        plan: TailoringPlan,
        changes: ChangeSet,
        warnings: Sequence[str],
        review_pdf: Path,
    ) -> None:
        """Send the review PDF with its caption, then the details, with the decision buttons on the last message."""
        if record.id is None:
            raise ValueError("The tailored resume record has no ID")
        duplicates = await self._group_members(job)
        caption = messages.review_caption(
            job,
            version=record.version,
            max_versions=self._max_versions,
            language=record.language,
            changes=changes,
            warnings_count=len(warnings),
            duplicates=duplicates,
        )
        await self._client.send_document(self._review_chat, review_pdf, filename=review_pdf.name, caption=caption)
        chunks = messages.review_details(job, plan, changes, warnings)
        keyboard = messages.review_keyboard(record.id, can_regenerate=record.version < self._max_versions)
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            await self._client.send_message(self._review_chat, chunk, reply_markup=keyboard if last else None)
        logger.info(
            "Sent review package for job {}:{} (resume {}, v{}, {} message(s))",
            job.platform,
            job.external_id,
            record.id,
            record.version,
            len(chunks) + 1,
        )

    async def _restore(self, request: _TailorRequest, job: JobPost) -> None:
        """Put back the buttons that were tapped, after a failed tailoring."""
        origin = request.origin
        if origin is None:
            return
        if origin.kind == "notification":
            await self._edit(origin, messages.job_keyboard(job))
        elif origin.kind == "guard":
            await self._edit(origin, messages.guard_keyboard(job))
        elif origin.kind == "package" and request.replaces is not None:
            await self._edit(origin, messages.review_keyboard(request.replaces, can_regenerate=True))

    async def _restore_after_cancel(self, request: _TailorRequest, job: JobPost) -> None:
        """Best effort on shutdown: put the buttons back (within a few seconds) so the job can be retried."""
        logger.warning("Tailoring of {}:{} was cancelled; its status is unchanged", job.platform, job.external_id)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._restore(request, job), timeout=_RESTORE_TIMEOUT_S)

    async def _show_progress(self, origin: Origin | None, job: JobPost, status: str) -> None:
        """Show a status on the tapped message: inside the notification keyboard, or as its only button."""
        if origin is None:
            return
        if origin.kind == "notification":
            await self._edit(origin, messages.job_keyboard(job, status=status))
        else:
            await self._edit(origin, messages.status_keyboard(status))

    async def _report_failure(self, job: JobPost, exc: Exception) -> None:
        """Tell the review chat why tailoring failed; log only the error type."""
        details: Sequence[str] = ()
        if isinstance(exc, TailoringError):
            reason, details = str(exc), exc.errors
            logger.warning("Tailoring failed for {}:{}: {} error(s)", job.platform, job.external_id, len(exc.errors))
        elif isinstance(exc, LLMError | ValueError | OSError | BotApiError):
            reason = str(exc)
            logger.warning("Tailoring failed for {}:{}: {}", job.platform, job.external_id, type(exc).__name__)
        else:
            reason = f"خطای غیرمنتظره ({type(exc).__name__})"
            logger.opt(exception=exc).error("Tailoring failed for {}:{}", job.platform, job.external_id)
        await self._send_failure(job, reason, details)

    async def _send_failure(self, job: JobPost, reason: str, details: Sequence[str] = ()) -> None:
        """Send a failure report to the review chat (a failure to send is only logged)."""
        try:
            for chunk in messages.failure_messages(job, reason, details):
                await self._client.send_message(self._review_chat, chunk)
        except BotApiError as exc:
            logger.error("Could not send the failure report for {}:{}: {}", job.platform, job.external_id, exc)

    # ── Duplicate guard ─────────────────────────────────────────────────────

    async def _group_members(self, job: JobPost) -> list[JobPost]:
        """Return the other postings in the job's duplicate group."""
        if not job.duplicate_group:
            return []
        members = await self._db.get_duplicate_group_members(job.duplicate_group)
        key = (job.platform, job.external_id)
        return [member for member in members if (member.platform, member.external_id) != key]

    async def _prior_activity(self, job: JobPost) -> JobPost | None:
        """Return a duplicate of the job that is `TAILORED` or later, or None."""
        if not job.duplicate_group:
            return None
        return find_prior_activity(job, await self._db.get_duplicate_group_members(job.duplicate_group))

    async def _send_guard(self, job: JobPost, prior: JobPost) -> None:
        """Ask in the review chat whether to tailor despite the duplicate's activity."""
        await self._client.send_message(
            self._review_chat, messages.guard_message(job, prior), reply_markup=messages.guard_keyboard(job)
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _resolve_latest(self, ref: str) -> tuple[JobPost, TailoredResumeRecord] | None:
        """Return the job and record for a resume ref if that record is its job's latest version."""
        resume_id = parse_resume_ref(ref)
        if resume_id is None:
            return None
        record = await self._db.get_tailored_resume(resume_id)
        if record is None:
            return None
        job = await self._db.get_job(record.platform, record.external_id)
        latest = await self._db.get_latest_tailored_resume(record.platform, record.external_id)
        if job is None or latest is None or latest.id != resume_id:
            return None
        return job, record

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        """Run a coroutine in the background; failures are logged, and shutdown waits for it."""

        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Bot background task {} failed", name)

        task = asyncio.create_task(guarded(), name=f"bot-{name}")
        self._state.background.add(task)
        task.add_done_callback(self._state.background.discard)

    async def _answer(self, query_id: str, text: str | None) -> None:
        """Answer a callback query; a late or failed answer is only logged."""
        if not query_id:
            return
        try:
            await self._client.answer_callback_query(query_id, text)
        except BotApiError as exc:
            logger.info("Could not answer callback query: {}", exc)

    async def _reply(self, chat_id: int | str, text: str) -> None:
        """Reply in a private chat; a failure is only logged."""
        try:
            await self._client.send_message(chat_id, text)
        except BotApiError as exc:
            logger.warning("Could not reply in chat {}: {}", chat_id, exc)

    async def _edit(self, origin: Origin | None, keyboard: dict[str, Any]) -> None:
        """Replace the tapped message's keyboard; a failure is only logged."""
        if origin is None:
            return
        try:
            await self._client.edit_message_reply_markup(origin.chat_id, origin.message_id, keyboard)
        except BotApiError as exc:
            logger.warning("Could not update the buttons of message {}: {}", origin.message_id, exc)


def _origin_of(message: Any) -> Origin | None:
    """Return the chat and message ID of the message that carried the tapped button."""
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    message_id = message.get("message_id")
    if not isinstance(chat, dict) or chat.get("id") is None or not isinstance(message_id, int):
        return None
    return Origin(chat["id"], message_id, "none")


def _toast(decision: _Decision) -> str:
    """The toast for a tailor-button decision."""
    return {
        _Decision.STARTED: messages.STARTED_TOAST,
        _Decision.QUEUED: messages.QUEUED_TOAST,
        _Decision.RESENT: messages.RESENDING_TOAST,
        _Decision.GUARDED: messages.GUARD_TOAST,
        _Decision.NOT_FOUND: messages.STALE_TOAST,
        _Decision.WRONG_STATUS: messages.STALE_TOAST,
    }[decision]


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Sleep for `delay` seconds, returning early when `stop_event` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=delay)


# ── Entry point ────────────────────────────────────────────────────────────


async def run_bot(settings: Settings, config: AppConfig, stop_event: asyncio.Event) -> None:
    """Run the review bot until `stop_event` is set.

    On stop: polling stops at once, in-flight work gets up to 60 s, and the
    rest is cancelled. If this task itself is cancelled (e.g. a second
    Ctrl+C), in-flight work is cancelled immediately. Without allowed users
    the bot only answers /start and /id.
    """
    async with (
        TelegramBotClient(settings, poll_timeout_seconds=config.bot.poll_timeout_seconds) as client,
        Database(settings.DB_PATH) as db,
    ):
        await db.init_db()
        service = BotService(
            settings=settings,
            config=config,
            client=client,
            db=db,
            backend=TailoringPipeline(settings, config, db),
            state_path=config.bot.state_path,
        )
        poller = asyncio.create_task(service.poll(stop_event), name="bot-poll")
        stopper = asyncio.create_task(stop_event.wait(), name="bot-stop")
        cancelled = False
        try:
            done, _ = await asyncio.wait({poller, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if poller in done and not poller.cancelled() and poller.exception() is not None:
                logger.opt(exception=poller.exception()).error("Review bot polling stopped unexpectedly")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            for task in (stopper, poller):
                task.cancel()
            await asyncio.gather(stopper, poller, return_exceptions=True)
            await service.shutdown(grace=0.0 if cancelled else SHUTDOWN_GRACE_S)
