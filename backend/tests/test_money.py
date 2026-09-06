"""Amount parsing. The tests that protect Vega's credibility.

PRD FR-T1 (no float on any financial path) and VEG-06 (confirm before writing an
ambiguous amount) both live here.
"""

import pytest

from app.tools.money import (
    AmountParseError,
    ParsedAmount,
    format_inr,
    parse_amount,
)

# ── Digits ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected_minor",
    [
        ("spent 180 on coffee", 18_000),
        ("250", 25_000),
        ("₹250", 25_000),
        ("rs 250", 25_000),
        ("Rs. 1,250 for groceries", 125_000),
        ("paid 820 for groceries", 82_000),
        ("2000 rupees rent", 200_000),
        ("18000 inr", 1_800_000),
    ],
)
def test_digit_amounts(text, expected_minor):
    assert parse_amount(text).minor == expected_minor


@pytest.mark.parametrize(
    "text,expected_minor",
    [
        ("250.50", 25_050),
        ("250.5", 25_050),    # ".5" is fifty paise, not five
        ("250.05", 25_005),
        ("0.99", 99),
        ("1.01", 101),
    ],
)
def test_decimal_amounts_are_exact(text, expected_minor):
    """float('250.50') * 100 is 25049.999... on some inputs; int() would truncate."""
    assert parse_amount(text).minor == expected_minor


@pytest.mark.parametrize(
    "text,expected_minor",
    [
        ("2k", 200_000),
        ("2.5k", 250_000),
        ("1 lakh", 10_000_000),
        ("1.5 lakh", 15_000_000),
        ("2 lakhs", 20_000_000),
        ("1 crore", 1_000_000_000),
        ("5 hundred", 50_000),
    ],
)
def test_indian_multipliers(text, expected_minor):
    assert parse_amount(text).minor == expected_minor


# ── Spelled-out numerals ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected_minor",
    [
        ("two fifty", 25_000),               # Indian spoken shorthand for 250
        ("three hundred", 30_000),
        ("three hundred and fifty", 35_000),
        ("one lakh twenty thousand", 12_000_000),
        ("five thousand", 500_000),
        ("twenty", 2_000),
    ],
)
def test_spelled_out_amounts(text, expected_minor):
    assert parse_amount(text).minor == expected_minor


# ── The homophone problem (VEG-06) ───────────────────────────────────────────

@pytest.mark.parametrize(
    "spoken,value_minor,alternative_minor",
    [
        ("eighteen", 1_800, 8_000),
        ("eighty", 8_000, 1_800),
        ("fifteen", 1_500, 5_000),
        ("fifty", 5_000, 1_500),
        ("thirteen", 1_300, 3_000),
        ("seventy", 7_000, 1_700),
    ],
)
def test_homophone_prone_amounts_request_confirmation(
    spoken, value_minor, alternative_minor
):
    """'eighteen' and 'eighty' differ by one unstressed syllable and ₹62."""
    result = parse_amount(f"spent {spoken} on chai")
    assert result.minor == value_minor
    assert result.needs_confirmation
    assert result.alternative_minor == alternative_minor
    assert result.reason and "sound alike" in result.reason


def test_digits_are_not_treated_as_homophone_ambiguous():
    """A digit transcript has already committed to a reading; don't re-litigate."""
    result = parse_amount("spent 18 on chai")
    assert result.minor == 1_800
    assert not result.needs_confirmation
    assert result.alternative_minor is None


def test_low_asr_confidence_forces_confirmation_even_for_digits():
    result = parse_amount("spent 180 on coffee", asr_confidence=0.4)
    assert result.minor == 18_000
    assert result.needs_confirmation
    assert "confidence" in result.reason


def test_high_asr_confidence_does_not_force_confirmation():
    result = parse_amount("spent 180 on coffee", asr_confidence=0.95)
    assert not result.needs_confirmation


# ── Failure ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["spent money on coffee", "", "log an expense"])
def test_missing_amount_raises_rather_than_defaulting(text):
    """Never default to zero. The agent must ask."""
    with pytest.raises(AmountParseError):
        parse_amount(text)


def test_trailing_words_do_not_join_the_number():
    """'250 on auto' must not absorb 'auto' into the numeric run."""
    assert parse_amount("spent 250 on auto").minor == 25_000


# ── Invariants ───────────────────────────────────────────────────────────────

def test_result_is_always_an_integer_number_of_paise():
    for text in ("250.50", "2.5k", "1.5 lakh", "0.99", "two fifty", "180"):
        minor = parse_amount(text).minor
        assert isinstance(minor, int)
        assert not isinstance(minor, bool)


def test_no_float_appears_in_the_parse_path():
    """Property check across many decimal inputs: exact integer paise, always."""
    for rupees in range(0, 500):
        for paise in (0, 1, 5, 9, 25, 50, 99):
            text = f"{rupees}.{paise:02d}"
            assert parse_amount(text).minor == rupees * 100 + paise


# ── Formatting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "minor,expected",
    [
        (25_000, "250 rupees"),
        (25_050, "250 rupees 50 paise"),
        (1_800_000, "18,000 rupees"),
        (99, "0 rupees 99 paise"),
    ],
)
def test_format_inr_is_speakable(minor, expected):
    """Vega reads figures aloud; no symbols, no bare decimals."""
    assert format_inr(minor) == expected


def test_major_str_round_trips():
    assert ParsedAmount(minor=25_050, raw="").major_str == "250.50"
    assert ParsedAmount(minor=25_000, raw="").major_str == "250"
