"""Tests for `normalize_text` (one group per normalization step) and `human_delay`."""

import asyncio

import pytest

from src import utils
from src.utils import human_delay, normalize_text


# Step 1: zero-width and bidi controls are removed (ZWNJ is not).
@pytest.mark.parametrize(
    "char",
    ["​", "‍", "‎", "‏", "‪", "‫", "‬", "‭", "‮", "﻿"],
)
def test_step1_removes_zero_width_and_bidi_controls(char: str) -> None:
    assert normalize_text(f"ab{char}cd") == "abcd"


def test_step1_removes_controls_inside_persian_word() -> None:
    assert normalize_text("‏مشهد‎") == "مشهد"


# Step 2: ZWNJ becomes a space.
def test_step2_zwnj_becomes_space() -> None:
    assert normalize_text("هوش‌مصنوعی") == "هوش مصنوعی"


def test_step2_zwnj_next_to_space_collapses() -> None:
    assert normalize_text("می‌ خواهم") == "می خواهم"


# Step 3: Arabic Yeh / Alef Maksura -> Persian Yeh, Arabic Kaf -> Keheh.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("علي", "علی"),  # علي -> علی
        ("موسى", "موسی"),  # موسى -> موسی
        ("كار", "کار"),  # كار -> کار
    ],
)
def test_step3_arabic_letters_to_persian(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


# Step 4: diacritics and tatweel are removed.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("مُهَنْدِس", "مهندس"),  # damma, fatha, sukun, kasra
        ("شدّه", "شده"),  # shadda
        ("مهــندس", "مهندس"),  # tatweel
        ("هٰذا", "هذا"),  # superscript alef
    ],
)
def test_step4_removes_diacritics_and_tatweel(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


# Step 5: Persian and Arabic-Indic digits -> ASCII.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("۰۱۲۳۴۵۶۷۸۹", "0123456789"),
        ("٠١٢٣٤٥٦٧٨٩", "0123456789"),
        ("حقوق ۱۵ تا ٢٠ میلیون", "حقوق 15 تا 20 میلیون"),
    ],
)
def test_step5_digits_to_ascii(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


# Step 6: casefold.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Senior AI Engineer", "senior ai engineer"), ("Straße", "strasse"), ("LLMs", "llms")],
)
def test_step6_casefold(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


# Step 7: whitespace collapse and strip.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  a   b  ", "a b"),
        ("a\t\n\r b", "a b"),
        ("a b", "a b"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_step7_collapses_whitespace(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_combined_realistic_persian_title() -> None:
    raw = "‏  كارشناس   هوش‌مصنوعي (AI) - ۳ سال سابقه﻿ "
    assert normalize_text(raw) == "کارشناس هوش مصنوعی (ai) - 3 سال سابقه"


def test_human_delay_sleeps_within_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(utils.asyncio, "sleep", fake_sleep)
    for _ in range(20):
        asyncio.run(human_delay(0.5, 1.0))
    assert len(slept) == 20
    assert all(0.5 <= value <= 1.0 for value in slept)


def test_human_delay_zero_runs_for_real() -> None:
    asyncio.run(human_delay(0, 0))


@pytest.mark.parametrize(("min_s", "max_s"), [(-1.0, 1.0), (2.0, 1.0)])
def test_human_delay_rejects_invalid_bounds(min_s: float, max_s: float) -> None:
    with pytest.raises(ValueError):
        asyncio.run(human_delay(min_s, max_s))
