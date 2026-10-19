"""Inline-button callback data: `"{action}:{ref}"`, at most 64 bytes (Telegram's limit).

- `t` (tailor) and `d` (tailor despite a duplicate) use a job ref, `{jv|jb}:{external_id}`.
- `r`, `a`, `s`, `y`, `c`, and `x` use a tailored resume ID.
- `n` is a no-op status button; its ref is `status` or `cancel`.

The 64-byte limit is from `InlineKeyboardButton.callback_data` ("1-64 bytes"),
https://core.telegram.org/bots/api#inlinekeyboardbutton.
"""

from __future__ import annotations

from typing import Literal, get_args

from src.models import Platform

CallbackAction = Literal["t", "r", "a", "s", "y", "c", "x", "n", "d"]

MAX_CALLBACK_BYTES = 64

_ACTIONS: frozenset[str] = frozenset(get_args(CallbackAction))
_PLATFORM_CODES: dict[Platform, str] = {"jobvision": "jv", "jobinja": "jb"}
_CODE_PLATFORMS: dict[str, Platform] = {code: platform for platform, code in _PLATFORM_CODES.items()}


def encode_callback(action: CallbackAction, ref: str) -> str:
    """Return `"{action}:{ref}"`.

    Raises:
        ValueError: If the action is unknown, the ref is empty, or the result
            is longer than 64 bytes in UTF-8.
    """
    if action not in _ACTIONS:
        raise ValueError(f"Unknown callback action {action!r}")
    if not ref:
        raise ValueError("Callback ref must not be empty")
    data = f"{action}:{ref}"
    size = len(data.encode("utf-8"))
    if size > MAX_CALLBACK_BYTES:
        raise ValueError(f"Callback data is {size} bytes; Telegram allows at most {MAX_CALLBACK_BYTES}")
    return data


def decode_callback(data: str) -> tuple[CallbackAction, str] | None:
    """Split callback data into `(action, ref)`, or return None if it is malformed."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    action, separator, ref = data.partition(":")
    if not separator or action not in _ACTIONS or not ref:
        return None
    return action, ref  # type: ignore[return-value]


def job_ref(platform: Platform, external_id: str) -> str:
    """Return the compact job ref `{jv|jb}:{external_id}` used by `t` and `d`."""
    return f"{_PLATFORM_CODES[platform]}:{external_id}"


def parse_job_ref(ref: str) -> tuple[Platform, str] | None:
    """Parse `{jv|jb}:{external_id}` into `(platform, external_id)`, or return None."""
    code, separator, external_id = ref.strip().partition(":")
    platform = _CODE_PLATFORMS.get(code.lower())
    external_id = external_id.strip()
    if not separator or platform is None or not external_id:
        return None
    return platform, external_id


def parse_resume_ref(ref: str) -> int | None:
    """Parse a tailored resume ID (a positive integer in ASCII digits), or return None."""
    if not ref.isascii() or not ref.isdigit():
        return None
    resume_id = int(ref)
    return resume_id if resume_id > 0 else None
