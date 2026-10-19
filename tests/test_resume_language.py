"""Tests for Persian/English language detection."""

import pytest

from src.resume.language import detect_language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("کارشناس هوش مصنوعی با تسلط بر پایتون", "fa"),
        ("Senior AI Engineer with strong Python skills", "en"),
        # A Persian ad that names tools in Latin script is still Persian.
        ("استخدام برنامه‌نویس Python آشنا با Django و PostgreSQL در مشهد", "fa"),
        # An English ad with a Persian company name is still English.
        ("Backend Developer at شرکت نمونه. We are looking for an experienced engineer to build APIs.", "en"),
        # Arabic presentation forms count as Persian letters.
        ("ﻣﻬﻨﺪس ﻧﺮم اﻓﺰار", "fa"),
    ],
)
def test_detects_language(text: str, expected: str) -> None:
    assert detect_language(text, persian_ratio_threshold=0.3) == expected


@pytest.mark.parametrize("text", ["", "   ", "123 456 !!", "۱۲۳ ۴۵۶ - 2024"])
def test_text_without_letters_is_persian(text: str) -> None:
    assert detect_language(text, persian_ratio_threshold=0.3) == "fa"


def test_digits_and_punctuation_are_not_letters() -> None:
    # 3 Persian letters and 7 Latin letters: ratio 0.3 regardless of digits.
    text = "ابپ abcdefg ۱۲۳۴۵۶۷۸۹ 1234567890 !!!"
    assert detect_language(text, persian_ratio_threshold=0.3) == "fa"
    assert detect_language(text, persian_ratio_threshold=0.31) == "en"


def test_ratio_equal_to_threshold_counts_as_persian() -> None:
    text = "اب" + "c" * 8  # 2 of 10 letters are Persian
    assert detect_language(text, persian_ratio_threshold=0.2) == "fa"
    assert detect_language(text, persian_ratio_threshold=0.21) == "en"


@pytest.mark.parametrize("threshold", [0.05, 0.5, 0.95])
def test_threshold_moves_the_boundary(threshold: float) -> None:
    half = "اببب" + "abcd"  # ratio 0.5
    expected = "fa" if 0.5 >= threshold else "en"
    assert detect_language(half, persian_ratio_threshold=threshold) == expected
