"""Deterministic time parsing for Selene's reminders.

A reminder set for the wrong day is worse than no reminder, so every branch that
turns speech into a timestamp is pinned here. Reference times are fixed — these
must not start failing on a Tuesday.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.tools.when import DEFAULT_HOUR, parse_when, speak_when

IST = ZoneInfo("Asia/Kolkata")

# Wednesday, 10 September 2025, 14:30 IST.
REF = datetime(2025, 9, 10, 14, 30, tzinfo=IST)


def local(text, *, reference=REF):
    """Parse and return the resolved moment in IST for readable assertions."""
    when = parse_when(text, reference=reference)
    return when, (when.at.astimezone(IST) if when.at else None)


# ── Relative days ────────────────────────────────────────────────────────────

def test_tomorrow_uses_the_default_hour_when_no_time_given():
    _, at = local("remind me to call the plumber tomorrow")
    assert (at.year, at.month, at.day) == (2025, 9, 11)
    assert at.hour == DEFAULT_HOUR


def test_tomorrow_at_eight_is_morning():
    """Bare hours 7-11 read as morning. The assumption is recorded, not silent."""
    when, at = local("remind me to leave tomorrow at 8")
    assert (at.day, at.hour) == (11, 8)
    assert "8 in the morning" in when.assumed


def test_bare_single_digit_hour_reads_as_evening():
    when, at = local("remind me tomorrow at 6")
    assert at.hour == 18
    assert "6 in the evening" in when.assumed


def test_explicit_meridiem_wins_over_the_assumption():
    when, at = local("remind me tomorrow at 6 am")
    assert at.hour == 6
    assert not when.assumed


def test_today_rolls_forward_when_the_hour_has_passed():
    """14:30 reference, 'today at 9' would already be gone."""
    when, at = local("remind me today at 9 am")
    assert at.day == 11
    assert any("passed" in note for note in when.assumed)


def test_in_two_hours():
    _, at = local("remind me in 2 hours")
    assert (at.day, at.hour, at.minute) == (10, 16, 30)


def test_in_three_days():
    _, at = local("remind me in 3 days")
    assert at.day == 13


# ── Weekdays and dates ───────────────────────────────────────────────────────

def test_named_weekday_resolves_forward():
    """Reference is a Wednesday; Friday is two days out."""
    _, at = local("remind me to submit it on friday")
    assert at.day == 12
    assert at.weekday() == 4


def test_same_weekday_as_today_goes_to_next_week():
    """'Wednesday' on a Wednesday means the next one, not this instant."""
    _, at = local("remind me on wednesday")
    assert at.day == 17


def test_day_of_month_resolves_to_the_next_occurrence():
    when, at = local("remind me to pay rent on the 1st")
    assert (at.month, at.day) == (10, 1)
    assert when.day_of_month == 1


def test_explicit_date_with_month_name():
    _, at = local("remind me about the visa on 20 October")
    assert (at.month, at.day) == (10, 20)


def test_date_already_past_this_year_rolls_to_next_year():
    _, at = local("remind me on 3 March")
    assert (at.year, at.month, at.day) == (2026, 3, 3)


# ── Recurrence ───────────────────────────────────────────────────────────────

def test_explicit_recurrence_is_read():
    when, _ = local("remind me every month to check the meter")
    assert when.recurrence == "monthly"


def test_every_weekday_is_weekly():
    when, at = local("remind me every monday to take out the trash")
    assert when.recurrence == "weekly"
    assert at.weekday() == 0


def test_rent_is_inferred_monthly_without_asking():
    """SEL-04. Selene's spec forbids a clarifying question here."""
    when, _ = local("remind me to pay rent on the 1st")
    assert when.recurrence == "monthly"
    assert when.day_of_month == 1
    assert "monthly" in when.assumed


def test_electricity_bill_is_inferred_monthly():
    when, _ = local("remind me about the electricity bill on the 15th")
    assert when.recurrence == "monthly"


def test_a_one_off_errand_is_not_made_recurring():
    when, _ = local("remind me to collect the parcel tomorrow")
    assert when.recurrence is None


def test_monthly_recurrence_carries_a_day_of_month():
    when, _ = local("remind me every month to pay the maid on the 5th")
    assert (when.recurrence, when.day_of_month) == ("monthly", 5)


# ── Subject extraction ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("remind me to pay rent on the 1st", "pay rent"),
        ("remind me to call mum tomorrow at 7 pm", "call mum"),
        ("set a reminder to renew the insurance on 20 October", "renew the insurance"),
        ("don't let me forget to book the train friday", "book the train"),
    ],
)
def test_the_time_phrase_is_stripped_from_the_subject(utterance, expected):
    when, _ = local(utterance)
    assert when.subject == expected


def test_no_time_at_all_yields_no_moment():
    when, at = local("remind me to think about the trip")
    assert at is None
    assert when.subject == "think about the trip"


# ── Speaking it back ─────────────────────────────────────────────────────────

def test_speak_when_uses_tomorrow_rather_than_a_date():
    when = parse_when("remind me tomorrow at 8 am", reference=REF)
    spoken = speak_when(when.at, when.recurrence)
    assert "at 8 in the morning" in spoken


def test_speak_when_appends_recurrence():
    when = parse_when("remind me to pay rent on the 1st", reference=REF)
    assert speak_when(when.at, when.recurrence).endswith("monthly")


def test_speak_when_has_no_markdown_or_iso_timestamps():
    """Everything Selene says is spoken aloud."""
    when = parse_when("remind me on 20 October at 9 am", reference=REF)
    spoken = speak_when(when.at, when.recurrence)
    assert "T" not in spoken.replace("October", "")
    assert not any(ch in spoken for ch in "*_#[]")
