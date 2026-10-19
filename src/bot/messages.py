"""Texts and keyboards of the review bot (Persian UI), within Telegram's size limits.

Every user-supplied value is HTML-escaped (`parse_mode=HTML`). Lengths are
measured in UTF-16 code units of the raw HTML, which is never shorter than
what Telegram counts "after entities parsing", so the limits always hold:
1,024 characters for captions and 4,096 for messages. Long texts are split
before escaping, so a split never cuts an HTML entity or tag.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from src.bot.callbacks import encode_callback, job_ref
from src.models import JobPost, JobStatus, Language, Platform
from src.resume.changes import ChangeSet
from src.resume.models import TailoringPlan

MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# ── Toasts (answerCallbackQuery, at most 200 characters) ──
UNAUTHORIZED_TOAST = "⛔️ مجاز نیستید"
QUEUED_TOAST = "⏳ در صف"
STALE_TOAST = "این دکمه دیگر معتبر نیست"
STARTED_TOAST = "⏳ تنظیم رزومه شروع شد"
REGENERATING_TOAST = "🔁 تولید نسخه جدید شروع شد"
RESENDING_TOAST = "📄 بسته بررسی دوباره ارسال می‌شود"
GUARD_TOAST = "⚠️ برای آگهی مشابهی اقدام کرده‌اید؛ پیام خصوصی را ببینید"
APPROVED_TOAST = "✅ تأیید شد"
REJECTED_TOAST = "❌ رد شد"
CANCELLED_TOAST = "✖️ لغو شد"

# ── Buttons and status labels ──
# The same text as the notifier's URL button (src/notifier/telegram.py).
VIEW_BUTTON_TEXT = "🔗 مشاهده آگهی در سایت"
TAILOR_BUTTON_TEXT = "✍️ تنظیم رزومه"
APPROVE_BUTTON_TEXT = "✅ تأیید و ارسال"
REGENERATE_BUTTON_TEXT = "🔁 تولید دوباره"
REJECT_BUTTON_TEXT = "❌ رد"
CONTINUE_BUTTON_TEXT = "✍️ ادامه"
CANCEL_BUTTON_TEXT = "✖️ لغو"
STATUS_TAILORING = "⏳ در حال تنظیم رزومه…"
STATUS_SENT = "📄 رزومه برای بررسی ارسال شد"
STATUS_REGENERATING = "🔁 در حال تولید نسخه جدید…"
STATUS_SUPERSEDED = "🔁 جایگزین شد با نسخه {version}"
STATUS_APPROVED = "✅ تأیید شد"
STATUS_REJECTED = "❌ رد شد"
STATUS_CANCELLED = "✖️ لغو شد"
STATUS_CONTINUED = "✍️ ادامه داده شد"

# `n` refs: a status button that does nothing, and the guard's cancel button.
NOOP_STATUS = "status"
NOOP_CANCEL = "cancel"

_TITLE_MAX = 200
_COMPANY_MAX = 100
_ELLIPSIS = "…"
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FILENAME_UNSAFE_RE = re.compile(r"[^\w-]+")

_PLATFORM_NAMES: dict[Platform, str] = {"jobvision": "JobVision", "jobinja": "Jobinja"}
_LANGUAGE_NAMES: dict[Language, str] = {"fa": "فارسی", "en": "انگلیسی"}
_STATUS_NAMES: dict[JobStatus, str] = {
    JobStatus.NEW: "جدید",
    JobStatus.NOTIFIED: "اعلان‌شده",
    JobStatus.TAILORED: "در انتظار بررسی رزومه",
    JobStatus.APPROVED: "رزومه تأییدشده",
    JobStatus.APPLIED: "درخواست ارسال‌شده",
    JobStatus.APPLY_FAILED: "ارسال ناموفق",
    JobStatus.SKIPPED: "ردشده",
    JobStatus.IGNORED: "نادیده‌گرفته‌شده",
}


# ── Length helpers ─────────────────────────────────────────────────────────


def telegram_length(text: str) -> int:
    """Return the length in UTF-16 code units (how Telegram counts characters, emoji included)."""
    return len(text.encode("utf-16-le")) // 2


def _escape(text: str) -> str:
    """Escape text for an HTML message body (quotes need no escaping outside attributes)."""
    return html.escape(text, quote=False)


def _truncate(text: str, limit: int) -> str:
    """Shorten raw text to at most `limit` characters, ending with an ellipsis when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + _ELLIPSIS


def _persian_number(value: int) -> str:
    """Format an integer with Persian digits."""
    return str(value).translate(_PERSIAN_DIGITS)


def _split_raw(text: str, limit: int) -> list[str]:
    """Split raw text into pieces whose escaped form is at most `limit` long, breaking at spaces when possible."""
    pieces: list[str] = []
    current = ""
    for word in re.split(r"(\s+)", text):
        candidate = current + word
        if telegram_length(_escape(candidate)) <= limit:
            current = candidate
            continue
        if current.strip():
            pieces.append(current.strip())
        current = word.lstrip()
        while telegram_length(_escape(current)) > limit:
            # A single word longer than the limit: cut it by characters.
            cut = len(current)
            while cut > 1 and telegram_length(_escape(current[:cut])) > limit:
                cut //= 2
            pieces.append(current[:cut])
            current = current[cut:]
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _text_lines(raw: str, *, prefix: str = "", limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Turn raw (possibly multi-line) text into escaped HTML lines, each within `limit`.

    `prefix` (already HTML) starts the first line only.
    """
    budget = limit - telegram_length(prefix)
    lines: list[str] = []
    for raw_line in raw.splitlines() or [""]:
        if not raw_line.strip():
            lines.append("")
            continue
        lines.extend(_escape(piece) for piece in _split_raw(raw_line, budget))
    if lines:
        lines[0] = prefix + lines[0]
    return lines


def chunk_lines(lines: Sequence[str], limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Pack HTML lines (each within `limit`) into as few messages as possible, each within `limit`."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        length = telegram_length(line)
        if length > limit:
            raise ValueError(f"A single line is {length} characters; split it first")
        extra = length + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current).strip("\n"))
            current, size = [line], length
            continue
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current).strip("\n"))
    return [chunk for chunk in chunks if chunk.strip()]


# ── Keyboards ──────────────────────────────────────────────────────────────


def _button(text: str, action: Any, ref: str) -> dict[str, str]:
    """Build one callback button."""
    return {"text": text, "callback_data": encode_callback(action, ref)}


def status_button(text: str) -> dict[str, str]:
    """A no-op button that only shows a status."""
    return _button(text, "n", NOOP_STATUS)


def status_keyboard(text: str) -> dict[str, Any]:
    """A keyboard with a single no-op status button."""
    return {"inline_keyboard": [[status_button(text)]]}


def job_keyboard(job: JobPost, *, status: str | None = None) -> dict[str, Any]:
    """The notification keyboard: the URL button plus either the tailor button or a status button.

    It matches the notifier's keyboard (`status=None`), so the bot can restore it.
    """
    if status:
        second = status_button(status)
    else:
        second = _button(TAILOR_BUTTON_TEXT, "t", job_ref(job.platform, job.external_id))
    return {"inline_keyboard": [[{"text": VIEW_BUTTON_TEXT, "url": job.url}, second]]}


def review_keyboard(resume_id: int, *, can_regenerate: bool) -> dict[str, Any]:
    """The buttons under a review package: approve, regenerate (while allowed), and reject."""
    ref = str(resume_id)
    row = [_button(APPROVE_BUTTON_TEXT, "a", ref)]
    if can_regenerate:
        row.append(_button(REGENERATE_BUTTON_TEXT, "r", ref))
    row.append(_button(REJECT_BUTTON_TEXT, "s", ref))
    return {"inline_keyboard": [row]}


def guard_keyboard(job: JobPost) -> dict[str, Any]:
    """The duplicate guard's buttons: continue anyway (`d`) or cancel (`n`)."""
    return {
        "inline_keyboard": [
            [
                _button(CONTINUE_BUTTON_TEXT, "d", job_ref(job.platform, job.external_id)),
                _button(CANCEL_BUTTON_TEXT, "n", NOOP_CANCEL),
            ]
        ]
    }


# ── Review package ─────────────────────────────────────────────────────────


def _job_heading(job: JobPost, title_max: int, company_max: int) -> list[str]:
    """The title and company lines of a job."""
    lines = [f"💼 <b>{_escape(_truncate(job.title, title_max))}</b>"]
    if job.company.strip():
        lines.append(f"🏛 {_escape(_truncate(job.company, company_max))}")
    return lines


def _web_url(url: str) -> str | None:
    """Return the URL if it is http(s), else None."""
    return url if urlsplit(url.strip()).scheme.lower() in ("http", "https") else None


def _posting_link(job: JobPost) -> str:
    """A link to a posting labeled with its platform name (plain text if the URL is not http(s))."""
    name = _escape(_PLATFORM_NAMES[job.platform])
    url = _web_url(job.url)
    return f'<a href="{html.escape(url, quote=True)}">{name}</a>' if url else name


def review_caption(
    job: JobPost,
    *,
    version: int,
    max_versions: int,
    language: Language,
    changes: ChangeSet,
    warnings_count: int,
    duplicates: Sequence[JobPost] = (),
) -> str:
    """The review PDF's caption: job, language, version `n/max`, change counts, and other postings.

    Always within 1,024 characters: the title and company are shortened, and
    the duplicate links become a count when they do not fit.
    """
    title_max, company_max = _TITLE_MAX, _COMPANY_MAX
    while True:
        lines = [
            f"📄 <b>رزومه برای بررسی</b> — نسخه {_persian_number(version)} از {_persian_number(max_versions)}",
            *_job_heading(job, title_max, company_max),
            f"🌐 زبان: {_LANGUAGE_NAMES[language]}",
            (
                f"✏️ بازنویسی: {_persian_number(changes.rephrased_count)} · "
                f"🗑 حذف: {_persian_number(changes.omitted_count)} · "
                f"⚠️ هشدار: {_persian_number(warnings_count)}"
            ),
        ]
        caption = "\n".join(lines)
        if telegram_length(caption) <= MAX_CAPTION_LENGTH or title_max == 0:
            break
        title_max, company_max = title_max // 2, company_max // 2

    key = (job.platform, job.external_id)
    others = [other for other in duplicates if (other.platform, other.external_id) != key]
    if others:
        links = "، ".join(_posting_link(other) for other in others)
        full = f"{caption}\n🔁 آگهی مشابه: {links}"
        short = f"{caption}\n🔁 {_persian_number(len(others))} آگهی مشابه"
        if telegram_length(full) <= MAX_CAPTION_LENGTH:
            caption = full
        elif telegram_length(short) <= MAX_CAPTION_LENGTH:
            caption = short
    return caption


def review_details(
    job: JobPost,
    plan: TailoringPlan,
    changes: ChangeSet,
    warnings: Sequence[str],
) -> list[str]:
    """The messages after the review PDF: rephrasings (قبل ← بعد), omissions, warnings, notes, and the cover letter.

    Returns one or more HTML messages, each within 4,096 characters.
    """
    lines: list[str] = [f"📋 <b>جزئیات تغییرات</b>: {_escape(_truncate(job.title, _TITLE_MAX))}", ""]

    lines.append("✏️ <b>بازنویسی‌ها</b> (قبل ← بعد)")
    rephrased = [("خلاصه", changes.summary.before, changes.summary.after)] if changes.summary.changed else []
    rephrased += [("", change.before, change.after) for change in changes.rephrased_bullets]
    if not rephrased:
        lines.append("— موردی نیست")
    for label, before, after in rephrased:
        if label:
            lines.append(f"▫️ <i>{_escape(label)}</i>")
        lines.extend(_text_lines(before, prefix="• قبل: "))
        lines.extend(_text_lines(after, prefix="  ← بعد: "))

    lines += ["", "🗑 <b>حذف‌شده‌ها</b>"]
    omitted_groups = (
        ("سوابق کاری", [item.label for item in changes.omitted_experiences]),
        ("پروژه‌ها", [item.label for item in changes.omitted_projects]),
        ("بندها", [item.label for item in changes.omitted_bullets]),
    )
    any_omitted = False
    for heading, labels in omitted_groups:
        if labels:
            any_omitted = True
            lines.append(f"▫️ <i>{heading}</i>")
            for label in labels:
                lines.extend(_text_lines(label, prefix="• "))
    if changes.omitted_skills:
        any_omitted = True
        skills = "، ".join(item.label for item in changes.omitted_skills)
        lines.extend(_text_lines(skills, prefix="▫️ <i>مهارت‌ها</i>: "))
    if not any_omitted:
        lines.append("— موردی نیست")

    if warnings:
        lines += ["", "⚠️ <b>هشدارها</b>"]
        for warning in warnings:
            lines.extend(_text_lines(warning, prefix="• "))

    if plan.relevance_notes.strip():
        lines += ["", "🧭 <b>یادداشت تطبیق</b> (فقط برای شما)"]
        lines.extend(_text_lines(plan.relevance_notes))

    if plan.cover_letter and plan.cover_letter.strip():
        lines += ["", "✉️ <b>نامه پوششی</b>"]
        lines.extend(_text_lines(plan.cover_letter))
    return chunk_lines(lines)


# ── Other messages ─────────────────────────────────────────────────────────


def guard_message(job: JobPost, prior: JobPost) -> str:
    """Ask whether to tailor although another posting of the same position already has activity."""
    title = _escape(_truncate(prior.title, _TITLE_MAX))
    platform = _PLATFORM_NAMES[prior.platform]
    status = _STATUS_NAMES.get(prior.status, prior.status.value)
    return (
        f"قبلاً برای آگهی مشابه «{title}» در {platform} اقدام کرده‌اید (وضعیت: {status}). ادامه می‌دهید؟\n"
        f"آگهی فعلی: {_escape(_truncate(job.title, _TITLE_MAX))} ({_PLATFORM_NAMES[job.platform]})"
    )


def failure_messages(job: JobPost, reason: str, details: Sequence[str] = ()) -> list[str]:
    """Report a failed tailoring, with the remaining validation errors when there are any."""
    lines = [f"❌ تنظیم رزومه برای «{_escape(_truncate(job.title, _TITLE_MAX))}» ناموفق بود."]
    lines.extend(_text_lines(reason, prefix="دلیل: "))
    for detail in details:
        lines.extend(_text_lines(detail, prefix="• "))
    lines.append("وضعیت آگهی تغییر نکرد؛ می‌توانید دوباره امتحان کنید.")
    return chunk_lines(lines)


def approved_caption(job: JobPost, version: int) -> str:
    """The caption of the clean PDF sent after approval."""
    return "\n".join(
        [f"✅ <b>رزومه نهایی</b> — نسخه {_persian_number(version)}", *_job_heading(job, _TITLE_MAX, _COMPANY_MAX)]
    )


def cover_letter_messages(cover_letter: str) -> list[str]:
    """The cover letter as one or more messages."""
    return chunk_lines(["✉️ <b>نامه پوششی</b>", *_text_lines(cover_letter)])


def manual_apply_message(job: JobPost) -> str:
    """Point the user to the posting to apply manually."""
    url = _web_url(job.url)
    title = _escape(_truncate(job.title, _TITLE_MAX))
    link = f'<a href="{html.escape(url, quote=True)}">{title}</a>' if url else title
    return f"🔗 لطفاً از طریق لینک آگهی، درخواست را به‌صورت دستی ارسال کنید:\n{link}"


def clean_pdf_filename(name: str, language: Language) -> str:
    """Return `{Name}_Resume_{lang}.pdf`, with the name made safe for a file name."""
    safe = _FILENAME_UNSAFE_RE.sub("_", name.strip()).strip("_") or "Resume"
    return f"{safe}_Resume_{language}.pdf"


def id_reply(user_id: int, *, authorized: bool) -> str:
    """Answer /start and /id with the sender's numeric ID and whether they are authorized."""
    status = (
        "✅ شما مجاز هستید."
        if authorized
        else "⛔️ مجاز نیستید. برای دسترسی، این شناسه را در TELEGRAM_ALLOWED_USER_IDS در config/.env بگذارید."
    )
    return f"🆔 شناسه عددی شما: <code>{user_id}</code>\n{status}"


TAILOR_USAGE = "استفاده: <code>/tailor jv:1234567</code> یا <code>/tailor jb:tER9</code>"
HELP_MESSAGE = (
    "دستورها:\n"
    "/id — نمایش شناسه عددی شما\n"
    "/tailor &lt;jv|jb&gt;:&lt;شناسه آگهی&gt; — تنظیم رزومه برای یک آگهی اعلان‌شده"
)


def job_not_found_message(ref: str) -> str:
    """The /tailor reply for an unknown job."""
    return f"آگهی <code>{_escape(ref)}</code> در پایگاه داده نیست."


def wrong_status_message(job: JobPost) -> str:
    """The /tailor reply for a job whose status does not allow tailoring."""
    status = _STATUS_NAMES.get(job.status, job.status.value)
    return (
        f"آگهی «{_escape(_truncate(job.title, _TITLE_MAX))}» در وضعیت «{status}» است؛ "
        "تنظیم رزومه فقط برای آگهی‌های اعلان‌شده ممکن است."
    )


def queued_message(job: JobPost, *, queued: bool) -> str:
    """The /tailor reply when tailoring is accepted."""
    title = _escape(_truncate(job.title, _TITLE_MAX))
    return f"{QUEUED_TOAST}: «{title}»" if queued else f"{STARTED_TOAST}: «{title}»"


def max_versions_toast(max_versions: int) -> str:
    """The toast when a job already has the maximum number of versions."""
    return f"🔁 به سقف {_persian_number(max_versions)} نسخه برای این آگهی رسیده‌اید"
