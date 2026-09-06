"""Deterministic parsing of spoken times, dates, and recurrence.

Same argument as `money.parse_amount`: a reminder set for the wrong day is worse
than no reminder, and a language model asked to emit an ISO timestamp will
occasionally emit a plausible wrong one with total confidence. So the time comes
from here, not from the model.

Everything this returns is either grounded in the text or recorded in `assumed`.
Selene's spec says to infer recurrence rather than ask, and to state what was
assumed — the second half of that is only possible if the assumptions are data,
so they are returned rather than left implicit.

All datetimes are resolved in the configured local timezone (IST) and returned
timezone-aware. "Tomorrow at eight" means eight in the morning where the user is,
not eight UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings

# When a time of day is not given at all. Early enough to act on, late enough not
# to fire while asleep.
DEFAULT_HOUR = 9

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

DAYPARTS = {
    "morning": 9,
    "afternoon": 15,
    "evening": 19,
    "tonight": 21,
    "night": 21,
    "noon": 12,
    "midday": 12,
    "midnight": 0,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_RECURRENCE_WORDS = {
    "daily": "daily",
    "every day": "daily",
    "each day": "daily",
    "weekly": "weekly",
    "every week": "weekly",
    "monthly": "monthly",
    "every month": "monthly",
    "each month": "monthly",
    "quarterly": "quarterly",
    "every quarter": "quarterly",
    "yearly": "yearly",
    "annually": "yearly",
    "every year": "yearly",
}

# Subjects whose recurrence is not worth asking about. Selene's spec: "Infer
# recurrence rather than asking. Rent is monthly." Getting this wrong is cheap —
# the user says "just once" and it is corrected — while asking every time is the
# behaviour the spec exists to prevent.
_IMPLIED_MONTHLY = re.compile(
    r"\b(rent|emi|premium|maintenance charge|maintenance fee|"
    r"electricity bill|water bill|gas bill|phone bill|internet bill|"
    r"broadband|wifi bill|subscription|salary|sip|instalment|installment)\b",
    re.IGNORECASE,
)
_IMPLIED_WEEKLY = re.compile(
    r"\b(trash|garbage|bin day|laundry|water the plants|watering)\b",
    re.IGNORECASE,
)

# "remind me to X" / "set a reminder to X" — the wrapper, not the subject.
_LEAD_IN = re.compile(
    r"^\s*(?:selene[,\s]+)?(?:please\s+)?"
    r"(?:remind me(?:\s+to| that| about)?|set (?:a |an )?reminder(?:\s+to| for| about)?|"
    r"don'?t let me forget(?:\s+to)?|note that i need to|i need to)\s+",
    re.IGNORECASE,
)

_ORDINAL = re.compile(
    r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b|\b(\d{1,2})(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b"
    r"|\bat\s+(\d{1,2})(?::(\d{2}))?\b",
    re.IGNORECASE,
)
_IN_DURATION = re.compile(
    r"\bin\s+(\d{1,3}|a|an|half an)\s+(minute|minutes|hour|hours|day|days|week|weeks|"
    r"month|months)\b",
    re.IGNORECASE,
)
# The month name is part of the pattern rather than checked afterwards. With a
# bare `[a-z]+` the first alternative matches "on 20" in "visa on 20 October" and
# the scan never reaches the month.
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_EXPLICIT_DATE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b"
    rf"|\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_WORD_CLOCK = re.compile(
    r"\bat\s+(" + "|".join(_NUMBER_WORDS) + r")\b(?:\s*(am|pm|in the morning|"
    r"in the evening|at night))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class When:
    """A resolved time, plus every assumption made to get there."""

    at: datetime | None = None
    recurrence: str | None = None
    day_of_month: int | None = None
    subject: str = ""
    assumed: list[str] = field(default_factory=list)

    @property
    def has_time(self) -> bool:
        return self.at is not None or self.recurrence is not None


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now_local() -> datetime:
    return datetime.now(local_tz())


def _disambiguate_hour(hour: int, meridiem: str | None, assumed: list[str]) -> int:
    """Resolve a bare clock hour to a 24-hour value.

    "At eight" is genuinely ambiguous. Rather than ask — Selene's spec says infer
    and state the assumption — the common reading wins: single digits from 1 to 6
    are afternoon or evening, 7 to 11 are morning. The choice is recorded so it
    can be spoken and corrected in one turn.
    """
    if meridiem:
        marker = meridiem.replace(".", "").lower()
        if marker.startswith("p") or "evening" in marker or "night" in marker:
            return hour if hour == 12 else hour + 12
        return 0 if hour == 12 else hour

    if hour == 12:
        assumed.append("midday")
        return 12
    if 1 <= hour <= 6:
        assumed.append(f"{hour} in the evening")
        return hour + 12
    assumed.append(f"{hour} in the morning")
    return hour


def _next_dom(reference: datetime, day: int, hour: int) -> datetime:
    """The next occurrence of a day-of-month, at `hour`, at or after `reference`."""
    year, month = reference.year, reference.month
    for _ in range(14):  # 12 months is enough; 14 tolerates day 29-31 skips
        try:
            candidate = reference.replace(
                year=year, month=month, day=day, hour=hour,
                minute=0, second=0, microsecond=0,
            )
        except ValueError:
            month, year = (month % 12) + 1, year + (month // 12)
            continue
        if candidate > reference:
            return candidate
        month, year = (month % 12) + 1, year + (month // 12)
    raise ValueError(f"could not resolve day {day}")


def parse_when(text: str, *, reference: datetime | None = None) -> When:
    """Extract time, recurrence, and the reminder's subject from an utterance."""
    reference = reference or now_local()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=local_tz())

    lowered = text.lower()
    assumed: list[str] = []
    consumed: list[tuple[int, int]] = []

    recurrence: str | None = None
    for phrase, value in _RECURRENCE_WORDS.items():
        match = re.search(rf"\b{re.escape(phrase)}\b", lowered)
        if match:
            recurrence, _ = value, consumed.append(match.span())
            break

    # "every monday" is weekly even though "monday" alone is a single date.
    weekday: int | None = None
    every_weekday = re.search(
        r"\bevery\s+(" + "|".join(WEEKDAYS) + r")\b", lowered
    )
    if every_weekday:
        recurrence = "weekly"
        weekday = WEEKDAYS[every_weekday.group(1)]
        consumed.append(every_weekday.span())

    hour: int | None = None
    minute = 0

    clock = _CLOCK.search(lowered)
    if clock:
        if clock.group(1):
            hour = int(clock.group(1))
            minute = int(clock.group(2) or 0)
            hour = _disambiguate_hour(hour, clock.group(3), assumed)
        else:
            hour = int(clock.group(4))
            minute = int(clock.group(5) or 0)
            hour = _disambiguate_hour(hour, None, assumed)
        consumed.append(clock.span())

    if hour is None:
        worded = _WORD_CLOCK.search(lowered)
        if worded:
            hour = _disambiguate_hour(
                _NUMBER_WORDS[worded.group(1)], worded.group(2), assumed
            )
            consumed.append(worded.span())

    for word, daypart_hour in DAYPARTS.items():
        match = re.search(rf"\b{word}\b", lowered)
        if match:
            if hour is None:
                hour = daypart_hour
            consumed.append(match.span())
            break

    at: datetime | None = None
    effective_hour = hour if hour is not None else DEFAULT_HOUR

    duration = _IN_DURATION.search(lowered)
    if duration:
        raw = duration.group(1).lower()
        count = 0.5 if raw == "half an" else (1 if raw in ("a", "an") else int(raw))
        unit = duration.group(2).rstrip("s")
        delta = {
            "minute": timedelta(minutes=count),
            "hour": timedelta(hours=count),
            "day": timedelta(days=count),
            "week": timedelta(weeks=count),
            "month": timedelta(days=30 * count),
        }[unit]
        at = reference + delta
        consumed.append(duration.span())

    day_of_month: int | None = None

    if at is None:
        if match := re.search(r"\btomorrow\b", lowered):
            at = (reference + timedelta(days=1)).replace(
                hour=effective_hour, minute=minute, second=0, microsecond=0
            )
            consumed.append(match.span())
        elif match := re.search(r"\b(today|tonight)\b", lowered):
            at = reference.replace(
                hour=effective_hour, minute=minute, second=0, microsecond=0
            )
            if at <= reference:
                at += timedelta(days=1)
                assumed.append("tomorrow, since that time has passed today")
            consumed.append(match.span())
        elif match := re.search(r"\bday after tomorrow\b", lowered):
            at = (reference + timedelta(days=2)).replace(
                hour=effective_hour, minute=minute, second=0, microsecond=0
            )
            consumed.append(match.span())

    if at is None and weekday is None:
        named = re.search(
            r"\b(?:next\s+|this\s+|on\s+)?(" + "|".join(WEEKDAYS) + r")\b", lowered
        )
        if named:
            weekday = WEEKDAYS[named.group(1)]
            consumed.append(named.span())

    if at is None and weekday is not None:
        ahead = (weekday - reference.weekday()) % 7 or 7
        at = (reference + timedelta(days=ahead)).replace(
            hour=effective_hour, minute=minute, second=0, microsecond=0
        )

    if at is None:
        # Every candidate, not just the first: "visa on 20 October" matches
        # "on 20" before "20 October", and stopping there loses the date.
        explicit = None
        month_num = day_num = None
        for candidate in _EXPLICIT_DATE.finditer(lowered):
            if candidate.group(2) and candidate.group(2).lower() in MONTHS:
                day_num = int(candidate.group(1))
                month_num = MONTHS[candidate.group(2).lower()]
            elif candidate.group(3) and candidate.group(3).lower() in MONTHS:
                month_num = MONTHS[candidate.group(3).lower()]
                day_num = int(candidate.group(4))
            if month_num and day_num:
                explicit = candidate
                break
        if explicit and month_num and day_num:
            year = reference.year
            candidate = reference.replace(
                year=year, month=month_num, day=day_num,
                hour=effective_hour, minute=minute, second=0, microsecond=0,
            )
            if candidate <= reference:
                candidate = candidate.replace(year=year + 1)
            at, _ = candidate, consumed.append(explicit.span())

    if at is None:
        ordinal = _ORDINAL.search(lowered)
        if ordinal:
            day_num = int(ordinal.group(1) or ordinal.group(2))
            if 1 <= day_num <= 31:
                day_of_month = day_num
                at = _next_dom(reference, day_num, effective_hour)
                consumed.append(ordinal.span())

    subject = _subject_from(text, consumed)

    # Recurrence the user did not state but the subject implies.
    if recurrence is None:
        if _IMPLIED_MONTHLY.search(subject):
            recurrence = "monthly"
            assumed.append("monthly")
        elif _IMPLIED_WEEKLY.search(subject):
            recurrence = "weekly"
            assumed.append("weekly")

    if recurrence == "monthly" and day_of_month is None and at is not None:
        day_of_month = at.day

    if recurrence and at is None:
        # "remind me monthly to pay rent" with no date: next month's default day.
        at = _next_dom(reference, day_of_month or reference.day, effective_hour)
        if day_of_month is None:
            day_of_month = at.day

    return When(
        at=at.astimezone(UTC) if at else None,
        recurrence=recurrence,
        day_of_month=day_of_month,
        subject=subject,
        assumed=assumed,
    )


def _subject_from(text: str, consumed: list[tuple[int, int]]) -> str:
    """The reminder body: the utterance minus the lead-in and the time phrases."""
    kept, cursor = [], 0
    for start, end in sorted(consumed):
        if start >= cursor:
            kept.append(text[cursor:start])
            cursor = end
    kept.append(text[cursor:])

    subject = " ".join(part.strip() for part in kept if part.strip())
    subject = _LEAD_IN.sub("", subject).strip()
    subject = re.sub(r"\s+", " ", subject)
    subject = re.sub(r"^(?:to|that|about|on|at|for|every|next|this)\s+", "", subject, flags=re.I)

    # Removing a date span leaves the preposition that introduced it: "renew the
    # insurance on 20 October" becomes "renew the insurance on". Strip until stable.
    trailing = re.compile(r"[\s,]+(?:on|at|by|for|in|to|from|of|the|every|next|this)$", re.I)
    while (stripped := trailing.sub("", subject)) != subject:
        subject = stripped

    return subject.strip(" ,.;:-").strip()


def speak_when(moment: datetime | None, recurrence: str | None) -> str:
    """A short spoken rendering, e.g. "the 1st, monthly" or "tomorrow at 8"."""
    if moment is None:
        return recurrence or "no date"

    local = moment.astimezone(local_tz())
    today = now_local().date()
    delta = (local.date() - today).days

    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    elif 2 <= delta <= 6:
        day = local.strftime("%A")
    else:
        day = local.strftime("%-d %B") if hasattr(local, "strftime") else str(local.date())
        try:
            day = local.strftime("%-d %B")
        except ValueError:  # Windows strftime has no %-d
            day = f"{local.day} {local.strftime('%B')}"

    hour = local.hour % 12 or 12
    part = "in the morning" if local.hour < 12 else (
        "in the afternoon" if local.hour < 17 else "in the evening"
    )
    clock = f"{hour}" if local.minute == 0 else f"{hour}:{local.minute:02d}"

    spoken = f"{day} at {clock} {part}"
    return f"{spoken}, {recurrence}" if recurrence else spoken
