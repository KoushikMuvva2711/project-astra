"""Spoken-amount parsing. INR, integer paise, no float anywhere.

Two hard requirements from the PRD meet here:

  FR-T1  no floating point on any financial path
  VEG-06 a low-confidence amount is confirmed, never guessed

The second is not paranoia. Speech recognition confuses "eighteen" with
"eighty", "fifteen" with "fifty", and so on down the whole teen/tens series —
the two differ by one unstressed syllable. Getting it wrong silently means a
₹62 error the user discovers weeks later while reconciling, with no idea which
entry is wrong. Asking costs one turn.

Decimal handling never touches a float: "250.50" is parsed as two integer
groups, 250 and 50, and combined as 250*100 + 50 = 25050 paise. Going via
float("250.50") * 100 yields 25049.999... on some inputs, and int() would
silently truncate it to 25049.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MINOR_PER_MAJOR = 100

# Indian numbering. Ordered longest-first so "crore" matches before "core".
MULTIPLIERS: dict[str, int] = {
    "crore": 10_000_000,
    "crores": 10_000_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "thousand": 1_000,
    "k": 1_000,
    "hundred": 100,
}

UNITS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Pairs a speech recogniser routinely confuses. Each maps to the value it is
# most often mistaken for, so the agent can offer the alternative by name
# instead of vaguely asking the user to repeat themselves.
HOMOPHONES: dict[str, str] = {
    "thirteen": "thirty", "thirty": "thirteen",
    "fourteen": "forty", "forty": "fourteen",
    "fifteen": "fifty", "fifty": "fifteen",
    "sixteen": "sixty", "sixty": "sixteen",
    "seventeen": "seventy", "seventy": "seventeen",
    "eighteen": "eighty", "eighty": "eighteen",
    "nineteen": "ninety", "ninety": "nineteen",
}

# Below this, a transcript is not trusted with an unconfirmed write.
ASR_CONFIDENCE_FLOOR = 0.75

_CURRENCY_NOISE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?|rupaye|bucks?)", re.IGNORECASE
)
_DIGITS = re.compile(r"(\d[\d,]*)(?:\.(\d{1,2}))?")
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class ParsedAmount:
    """A parsed monetary amount and how much to trust it."""

    minor: int
    raw: str
    needs_confirmation: bool = False
    alternative_minor: int | None = None
    """The competing reading, when the spoken form is homophone-prone."""
    reason: str | None = None

    @property
    def major_str(self) -> str:
        """Speakable form: 25050 -> '250.50', 25000 -> '250'."""
        whole, frac = divmod(self.minor, MINOR_PER_MAJOR)
        return f"{whole}.{frac:02d}" if frac else str(whole)


class AmountParseError(ValueError):
    """No amount could be found. The caller must ask rather than assume."""


def _words_to_number(tokens: list[str]) -> int | None:
    """Convert spelled-out English numerals, including Indian multipliers.

    Handles 'two fifty' (250), 'one lakh twenty thousand' (120000), and
    'three hundred and fifty' (350).
    """
    total = 0
    current = 0
    seen_any = False

    for token in tokens:
        if token == "and":
            continue
        if token in UNITS:
            current += UNITS[token]
            seen_any = True
        elif token in MULTIPLIERS:
            multiplier = MULTIPLIERS[token]
            if multiplier >= 1_000:
                # Larger multipliers close the current group: "one lakh twenty
                # thousand" is (1 * 100000) + (20 * 1000), not 120 * 1000.
                total += max(current, 1) * multiplier
                current = 0
            else:
                current = max(current, 1) * multiplier
            seen_any = True
        else:
            # Unknown word ends the numeric run rather than being skipped —
            # "250 on auto" must not read "auto" as part of the number.
            if seen_any:
                break

    return total + current if seen_any else None


def _bare_pair_to_number(tokens: list[str]) -> int | None:
    """'two fifty' means 250 in speech, not 52 or 2 and 50.

    Only applies to exactly two tokens where the first is a single digit and
    the second is a tens multiple — the common Indian spoken shorthand.
    """
    if len(tokens) != 2:
        return None
    first, second = tokens
    if first in UNITS and second in UNITS:
        a, b = UNITS[first], UNITS[second]
        if 1 <= a <= 9 and b >= 20 and b % 10 == 0:
            return a * 100 + b
    return None


def parse_amount(text: str, *, asr_confidence: float | None = None) -> ParsedAmount:
    """Extract an INR amount as integer paise.

    Raises AmountParseError when nothing numeric is present — the caller must
    ask rather than default to zero or guess.
    """
    cleaned = _CURRENCY_NOISE.sub(" ", text.lower())

    digit_match = _DIGITS.search(cleaned)
    if digit_match:
        whole = int(digit_match.group(1).replace(",", ""))
        frac_raw = digit_match.group(2)

        # A multiplier may follow the digits: "2.5k", "1.5 lakh".
        tail = cleaned[digit_match.end():].strip()
        tail_word = _WORD.match(tail)
        multiplier = MULTIPLIERS.get(tail_word.group(0)) if tail_word else None

        if multiplier:
            # Scale without float: 2.5k -> (2 * 1000) + (5 * 1000 // 10).
            minor = whole * multiplier * MINOR_PER_MAJOR
            if frac_raw:
                scale = 10 ** len(frac_raw)
                minor += int(frac_raw) * multiplier * MINOR_PER_MAJOR // scale
        else:
            frac = 0
            if frac_raw:
                # ".5" is fifty paise, ".05" is five. Pad to exactly 2 digits.
                frac = int(frac_raw.ljust(2, "0")[:2])
            minor = whole * MINOR_PER_MAJOR + frac

        confident = asr_confidence is None or asr_confidence >= ASR_CONFIDENCE_FLOOR
        return ParsedAmount(
            minor=minor,
            raw=digit_match.group(0),
            needs_confirmation=not confident,
            reason=None if confident else "low speech-recognition confidence",
        )

    # No digits — fall back to spelled-out numerals.
    tokens = _WORD.findall(cleaned)
    numeric_tokens = [t for t in tokens if t in UNITS or t in MULTIPLIERS or t == "and"]
    if not numeric_tokens:
        raise AmountParseError(f"no amount found in {text!r}")

    value = _bare_pair_to_number(numeric_tokens) or _words_to_number(numeric_tokens)
    if value is None:
        raise AmountParseError(f"no amount found in {text!r}")

    # Homophone check: only meaningful for spelled-out numbers, since a digit
    # transcript has already committed to a reading.
    alternative = None
    reason = None
    for token in numeric_tokens:
        if token in HOMOPHONES:
            swapped = [HOMOPHONES[token] if t == token else t for t in numeric_tokens]
            candidate = _bare_pair_to_number(swapped) or _words_to_number(swapped)
            if candidate is not None and candidate != value:
                alternative = candidate * MINOR_PER_MAJOR
                reason = f"{token!r} and {HOMOPHONES[token]!r} sound alike"
            break

    low_asr = asr_confidence is not None and asr_confidence < ASR_CONFIDENCE_FLOOR
    if low_asr and reason is None:
        reason = "low speech-recognition confidence"

    return ParsedAmount(
        minor=value * MINOR_PER_MAJOR,
        raw=" ".join(numeric_tokens),
        needs_confirmation=alternative is not None or low_asr,
        alternative_minor=alternative,
        reason=reason,
    )


def format_inr(minor: int) -> str:
    """Speakable rupees. Vega reads figures aloud, so no symbols or separators."""
    whole, frac = divmod(abs(minor), MINOR_PER_MAJOR)
    sign = "minus " if minor < 0 else ""
    if frac:
        return f"{sign}{whole:,} rupees {frac} paise"
    return f"{sign}{whole:,} rupees"
