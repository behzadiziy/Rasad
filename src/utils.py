"""Shared helpers: Persian-aware text normalization and human-like delays.

`normalize_text` is used only for matching. Stored and displayed text always
keeps its original form.
"""

from __future__ import annotations

import asyncio
import random
import re

from loguru import logger

_ZWNJ = "‌"
_PERSIAN_YEH = "ی"
_KEHEH = "ک"

# Step 1: zero-width and bidi control characters (ZWNJ is handled in step 2).
_REMOVED_CONTROLS: tuple[int, ...] = (
    0x200B,  # zero width space
    0x200D,  # zero width joiner
    0x200E,  # left-to-right mark
    0x200F,  # right-to-left mark
    *range(0x202A, 0x202F),  # LRE, RLE, PDF, LRO, RLO
    *range(0x2066, 0x206A),  # LRI, RLI, FSI, PDI (bidi isolates)
    0x061C,  # Arabic letter mark
    0xFEFF,  # zero width no-break space / BOM
)

# Step 4: Arabic diacritics, superscript alef, and tatweel.
_REMOVED_MARKS: tuple[int, ...] = (*range(0x064B, 0x0660), 0x0670, 0x0640)


def _build_translation_table() -> dict[int, str | None]:
    """Build the single `str.translate` table covering normalization steps 1–5.

    Every step is a per-character mapping and no step produces a character that
    another step consumes, so one combined table is equivalent to applying the
    steps in order.
    """
    table: dict[int, str | None] = {}
    for codepoint in _REMOVED_CONTROLS:
        table[codepoint] = None
    table[ord(_ZWNJ)] = " "
    table[0x064A] = _PERSIAN_YEH  # Arabic Yeh
    table[0x0649] = _PERSIAN_YEH  # Alef Maksura
    table[0x0643] = _KEHEH  # Arabic Kaf
    for codepoint in _REMOVED_MARKS:
        table[codepoint] = None
    for offset in range(10):
        table[0x06F0 + offset] = str(offset)  # Persian digits
        table[0x0660 + offset] = str(offset)  # Arabic-Indic digits
    return table


_TRANSLATION_TABLE = _build_translation_table()
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalize text for matching.

    Steps, in order:
    1. Remove zero-width and bidi control characters (except ZWNJ).
    2. Replace ZWNJ with a space.
    3. Map Arabic Yeh/Alef Maksura to Persian Yeh, and Arabic Kaf to Keheh.
    4. Remove Arabic diacritics and tatweel.
    5. Map Persian and Arabic-Indic digits to ASCII.
    6. Casefold.
    7. Collapse whitespace runs into a single space and strip.
    """
    translated = text.translate(_TRANSLATION_TABLE).casefold()
    return _WHITESPACE_RE.sub(" ", translated).strip()


async def human_delay(min_s: float = 1.5, max_s: float = 3.5) -> None:
    """Sleep for a random duration between `min_s` and `max_s` seconds.

    Raises:
        ValueError: If `min_s` is negative or `max_s` is smaller than `min_s`.
    """
    if min_s < 0 or max_s < min_s:
        raise ValueError(f"Invalid delay bounds: min_s={min_s}, max_s={max_s}")
    delay = random.uniform(min_s, max_s)
    logger.debug("Sleeping {:.2f}s", delay)
    await asyncio.sleep(delay)
