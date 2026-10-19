"""Tests for bot texts and keyboards: escaping, chunking within Telegram's 1,024/4,096 limits, and buttons."""

import re
from pathlib import Path
from typing import Any

import pytest

from src.bot import messages
from src.bot.callbacks import MAX_CALLBACK_BYTES, decode_callback
from src.models import JobPost, JobStatus, Settings, WorkMode
from src.notifier.telegram import _build_keyboard
from src.resume.changes import compute_changes
from src.resume.models import MasterResume, TailoringPlan, load_master_resume

_FIXTURE = Path(__file__).parent / "fixtures" / "resume" / "master_resume.yaml"
_ENTITY_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27);)")


@pytest.fixture(scope="module")
def master() -> MasterResume:
    """The fictional master resume."""
    return load_master_resume(_FIXTURE)


def _job(**overrides: Any) -> JobPost:
    """A fictional job posting."""
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "tER9",
        "title": "Python Backend Engineer",
        "company": "Omega Labs",
        "city": "Mashhad",
        "work_mode": WorkMode.REMOTE,
        "url": "https://jobinja.ir/jobs/tER9",
        "status": JobStatus.NOTIFIED,
    }
    data.update(overrides)
    return JobPost(**data)


def _plan(**overrides: Any) -> TailoringPlan:
    """An English plan for the fixture master resume."""
    data: dict[str, Any] = {
        "language": "en",
        "summary": "Python engineer who builds API services.",
        "experiences": [
            {
                "experience_id": "alpha-backend",
                "bullets": [{"bullet_id": "alpha-cache", "text": "Cut service response time by 40% with Redis"}],
            }
        ],
        "projects": [],
        "skill_ids": ["python", "redis"],
        "cover_letter": "Dear Omega Labs team, I would like to apply.",
        "relevance_notes": "Good fit; Kubernetes is missing.",
    }
    data.update(overrides)
    return TailoringPlan.model_validate(data)


def _assert_entities_intact(text: str) -> None:
    """Every '&' must start a complete HTML entity (no entity was cut by a split)."""
    assert _ENTITY_RE.search(text) is None, text[-80:]


# ── Lengths and chunking ───────────────────────────────────────────────────


def test_telegram_length_counts_utf16_units() -> None:
    assert messages.telegram_length("abc") == 3
    assert messages.telegram_length("سلام") == 4
    assert messages.telegram_length("📄") == 2  # outside the BMP: a surrogate pair


def test_chunk_lines_packs_lines_within_the_limit() -> None:
    lines = [f"line {index} " + "x" * 90 for index in range(200)]
    chunks = messages.chunk_lines(lines, limit=1000)
    assert len(chunks) > 1
    assert all(messages.telegram_length(chunk) <= 1000 for chunk in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_chunk_lines_rejects_an_oversized_line() -> None:
    with pytest.raises(ValueError, match="split it first"):
        messages.chunk_lines(["x" * 5000])


# ── Review caption (1,024) ─────────────────────────────────────────────────


def test_review_caption_contents(master: MasterResume) -> None:
    plan = _plan()
    caption = messages.review_caption(
        _job(title="AI <Engineer> & Co"),
        version=2,
        max_versions=3,
        language="en",
        changes=compute_changes(plan, master),
        warnings_count=1,
    )
    assert "نسخه ۲ از ۳" in caption
    assert "زبان: انگلیسی" in caption
    assert "AI &lt;Engineer&gt; &amp; Co" in caption
    assert "⚠️ هشدار: ۱" in caption
    assert "<Engineer>" not in caption


def test_review_caption_stays_within_1024_with_huge_fields(master: MasterResume) -> None:
    job = _job(title="&<>" * 500, company="شرکت " * 300)
    duplicates = [
        _job(platform="jobvision", external_id=str(index), url=f"https://jobvision.ir/jobs/{index}")
        for index in range(60)
    ]
    caption = messages.review_caption(
        job,
        version=1,
        max_versions=3,
        language="fa",
        changes=compute_changes(_plan(language="fa"), master),
        warnings_count=0,
        duplicates=duplicates,
    )
    assert messages.telegram_length(caption) <= messages.MAX_CAPTION_LENGTH
    _assert_entities_intact(caption)
    assert "۶۰ آگهی مشابه" in caption  # too many links: shown as a count


def test_review_caption_links_other_postings_and_skips_the_job_itself(master: MasterResume) -> None:
    job = _job()
    other = _job(platform="jobvision", external_id="1522572", url="https://jobvision.ir/jobs/1522572")
    caption = messages.review_caption(
        job,
        version=1,
        max_versions=3,
        language="en",
        changes=compute_changes(_plan(), master),
        warnings_count=0,
        duplicates=[job, other],
    )
    assert '🔁 آگهی مشابه: <a href="https://jobvision.ir/jobs/1522572">JobVision</a>' in caption
    assert caption.count("<a href") == 1


# ── Review details (4,096) ─────────────────────────────────────────────────


def test_review_details_sections(master: MasterResume) -> None:
    plan = _plan()
    changes = compute_changes(plan, master)
    (text,) = messages.review_details(_job(), plan, changes, ["Term 'Kubernetes' in the summary does not appear"])
    assert "✏️ <b>بازنویسی‌ها</b> (قبل ← بعد)" in text
    assert "• قبل: Software engineer with 5 years" in text
    assert "  ← بعد: Python engineer who builds API services." in text
    assert "• قبل: Cut response time by 40% with Redis" in text
    assert "  ← بعد: Cut service response time by 40% with Redis" in text
    assert "▫️ <i>سوابق کاری</i>" in text and "• Developer — Beta Soft" in text
    assert "▫️ <i>مهارت‌ها</i>: FastAPI، Django، C++، AWS" in text
    assert "⚠️ <b>هشدارها</b>" in text and "Term 'Kubernetes'" in text
    assert "🧭 <b>یادداشت تطبیق</b>" in text and "Kubernetes is missing" in text
    assert "✉️ <b>نامه پوششی</b>" in text and "Dear Omega Labs team" in text


def test_review_details_without_changes(master: MasterResume) -> None:
    plan = TailoringPlan.model_validate(
        {
            "language": "en",
            "summary": master.summary.en,
            "experiences": [
                {
                    "experience_id": item.id,
                    "bullets": [
                        {"bullet_id": bullet.id, "text": bullet.text.text_for("en")} for bullet in item.bullets
                    ],
                }
                for item in master.experiences
            ],
            "projects": [
                {
                    "project_id": item.id,
                    "bullets": [
                        {"bullet_id": bullet.id, "text": bullet.text.text_for("en")} for bullet in item.bullets
                    ],
                }
                for item in master.projects
            ],
            "skill_ids": [skill.id for skill in master.skills],
            "cover_letter": None,
            "relevance_notes": "",
        }
    )
    (text,) = messages.review_details(_job(), plan, compute_changes(plan, master), [])
    assert text.count("— موردی نیست") == 2
    assert "هشدارها" not in text and "نامه پوششی" not in text and "یادداشت تطبیق" not in text


def test_long_details_are_chunked_escaped_and_complete(master: MasterResume) -> None:
    letter = " ".join(f"word{index} <tag> & more" for index in range(1500))  # about 33,000 characters escaped
    plan = _plan(cover_letter=letter, relevance_notes="R&D " * 2000)
    chunks = messages.review_details(_job(), plan, compute_changes(plan, master), ["a & b"] * 300)
    assert len(chunks) > 5
    for chunk in chunks:
        assert messages.telegram_length(chunk) <= messages.MAX_MESSAGE_LENGTH
        assert "<tag>" not in chunk
        _assert_entities_intact(chunk)
    joined = " ".join(" ".join(chunks).split())  # a split turns a space into a line break
    assert "word0 &lt;tag&gt; &amp; more" in joined
    assert "word1499 &lt;tag&gt; &amp; more" in joined


def test_a_single_unbreakable_word_is_split_safely(master: MasterResume) -> None:
    plan = _plan(cover_letter="&" * 9000)
    chunks = messages.review_details(_job(), plan, compute_changes(plan, master), [])
    assert all(messages.telegram_length(chunk) <= messages.MAX_MESSAGE_LENGTH for chunk in chunks)
    for chunk in chunks:
        _assert_entities_intact(chunk)
    assert sum(chunk.count("&amp;") for chunk in chunks) == 9000


def test_failure_and_cover_letter_messages_are_chunked() -> None:
    chunks = messages.failure_messages(_job(), "Plan still has 300 error(s)", ["Number 9 & <x>"] * 400)
    assert len(chunks) > 1
    assert all(messages.telegram_length(chunk) <= messages.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "Number 9 &amp; &lt;x&gt;" in chunks[0]
    letters = messages.cover_letter_messages("Dear team,\n\n" + "x " * 5000)
    assert len(letters) >= 3 and letters[0].startswith("✉️ <b>نامه پوششی</b>")
    assert all(messages.telegram_length(letter) <= messages.MAX_MESSAGE_LENGTH for letter in letters)
    assert sum(letter.count("x") for letter in letters) == 5000


# ── Keyboards ──────────────────────────────────────────────────────────────


def _callbacks(keyboard: dict[str, Any]) -> list[str]:
    """Return every callback_data in a keyboard."""
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    return [button["callback_data"] for button in buttons if "callback_data" in button]


def test_review_keyboard_with_and_without_regenerate() -> None:
    assert _callbacks(messages.review_keyboard(42, can_regenerate=True)) == ["a:42", "r:42", "s:42"]
    assert _callbacks(messages.review_keyboard(42, can_regenerate=False)) == ["a:42", "s:42"]
    texts = [button["text"] for button in messages.review_keyboard(42, can_regenerate=True)["inline_keyboard"][0]]
    assert texts == ["✅ تأیید و ارسال", "🔁 تولید دوباره", "❌ رد"]


def test_job_keyboard_tailor_and_status_variants() -> None:
    job = _job()
    keyboard = messages.job_keyboard(job)
    assert keyboard["inline_keyboard"][0][0] == {"text": "🔗 مشاهده آگهی در سایت", "url": job.url}
    assert keyboard["inline_keyboard"][0][1] == {"text": "✍️ تنظیم رزومه", "callback_data": "t:jb:tER9"}
    status = messages.job_keyboard(job, status=messages.STATUS_TAILORING)
    assert status["inline_keyboard"][0][1] == {"text": "⏳ در حال تنظیم رزومه…", "callback_data": "n:status"}


def test_guard_message_and_keyboard() -> None:
    prior = _job(platform="jobvision", external_id="1", title="AI Engineer (LLM)", status=JobStatus.TAILORED)
    text = messages.guard_message(_job(title="مهندس هوش مصنوعی"), prior)
    assert text.startswith(
        "قبلاً برای آگهی مشابه «AI Engineer (LLM)» در JobVision اقدام کرده‌اید (وضعیت: در انتظار بررسی رزومه). "
        "ادامه می‌دهید؟"
    )
    assert _callbacks(messages.guard_keyboard(_job())) == ["d:jb:tER9", "n:cancel"]


def test_every_button_fits_the_callback_limit() -> None:
    long_id = "9" * 59  # "t:jv:" + 59 = 64 bytes
    job = _job(platform="jobvision", external_id=long_id)
    keyboards = (
        messages.job_keyboard(job),
        messages.guard_keyboard(job),
        messages.review_keyboard(10**15, can_regenerate=True),
    )
    for keyboard in keyboards:
        for data in _callbacks(keyboard):
            assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
            assert decode_callback(data) is not None


def _settings(allowed: str) -> Settings:
    """Settings without reading config/.env."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_CHAT_ID="@channel",
        TELEGRAM_ALLOWED_USER_IDS=allowed,
        TELEGRAM_REVIEW_CHAT_ID=None,
    )


def test_notifier_keyboard_matches_the_bot_keyboard() -> None:
    job = _job()
    assert _build_keyboard(job, tailor_button=True) == messages.job_keyboard(job)
    assert _build_keyboard(job) == {"inline_keyboard": [[{"text": messages.VIEW_BUTTON_TEXT, "url": job.url}]]}
    assert bool(_settings("111").allowed_user_ids) and not _settings("").allowed_user_ids


def test_notifier_omits_the_tailor_button_when_the_callback_would_be_too_long() -> None:
    job = _job(platform="jobvision", external_id="9" * 60)
    assert _build_keyboard(job, tailor_button=True) == {
        "inline_keyboard": [[{"text": messages.VIEW_BUTTON_TEXT, "url": job.url}]]
    }


# ── Other messages ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "language", "expected"),
    [
        ("Negar Azmoon", "en", "Negar_Azmoon_Resume_en.pdf"),
        ("نگار آزمون", "fa", "نگار_آزمون_Resume_fa.pdf"),
        ("a/b\\c:d*?", "en", "a_b_c_d_Resume_en.pdf"),
        ("  ", "en", "Resume_Resume_en.pdf"),
    ],
)
def test_clean_pdf_filename(name: str, language: Any, expected: str) -> None:
    assert messages.clean_pdf_filename(name, language) == expected


def test_id_reply() -> None:
    assert "<code>111</code>" in messages.id_reply(111, authorized=True)
    assert "✅" in messages.id_reply(111, authorized=True)
    refused = messages.id_reply(999, authorized=False)
    assert "<code>999</code>" in refused and "TELEGRAM_ALLOWED_USER_IDS" in refused


def test_manual_apply_message_links_only_web_urls() -> None:
    assert '<a href="https://jobinja.ir/jobs/tER9">' in messages.manual_apply_message(_job())
    assert "<a href" not in messages.manual_apply_message(_job(url="javascript:alert(1)"))
