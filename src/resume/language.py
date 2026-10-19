"""Detect whether a text (a job posting or a tailored resume) is Persian or English.

The share of Persian letters among all letters is compared with
`tailoring.persian_ratio_threshold`. Persian letters are the letters of the
Arabic script blocks, which Persian uses. Digits, punctuation, and spaces are
not letters and do not count.
"""

from __future__ import annotations

from src.models import Language

# Unicode blocks of the Arabic script: Arabic, Arabic Supplement, Arabic
# Extended-A, and Arabic Presentation Forms-A and -B.
_PERSIAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)


def _is_persian_letter(char: str) -> bool:
    """Return True for a letter in the Arabic-script Unicode blocks."""
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in _PERSIAN_RANGES)


def _persian_ratio(text: str) -> float | None:
    """Return the share of Persian letters among all letters, or None if the text has no letters."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return None
    persian = sum(1 for char in letters if _is_persian_letter(char))
    return persian / len(letters)


def detect_language(text: str, *, persian_ratio_threshold: float) -> Language:
    """Return "fa" if Persian letters make up at least `persian_ratio_threshold` of all letters, else "en".

    A text without any letters counts as Persian, the boards' main language.
    """
    ratio = _persian_ratio(text)
    if ratio is None:
        return "fa"
    return "fa" if ratio >= persian_ratio_threshold else "en"
