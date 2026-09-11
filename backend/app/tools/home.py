"""Selene's tools: reminders, groceries, inventory, documents, warranties.

Two of Selene's spec rules are enforced structurally here rather than by prompt,
because both fail silently when left to a model:

`SEL-05` — "grocery add for a recently-bought item notes the prior purchase".
`grocery_add` returns `days_since_purchase` as data. Selene reads the prior
purchase rather than being trusted to remember to look for it.

`SEL-07` — "document numbers never read aloud". The `documents` table has no
identifier column at all, and `document_track` strips anything identifier-shaped
out of the free-text fields before writing. A number that was never stored cannot
be read out within earshot of someone else.

Quantities are `quantity_dc` — tenths of a unit — so "half a packet" is 5 and all
stock arithmetic stays exact integers.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import sqlalchemy as sa

from app.tools.base import ToolContext, ToolResult, tool
from app.tools.when import parse_when, speak_when

DC_PER_UNIT = 10

# Identifier-shaped text.
#
# The first branch is deliberately blunt: any run of four or more digits, however
# it is grouped. Enumerating formats invites an off-by-one — an Aadhaar pattern
# that matches twelve digits leaves the last four of a sixteen-digit card behind,
# which is exactly the kind of near-miss that makes a redaction rule worthless.
# Four digits is below anything worth protecting (a flat number, a floor) and at
# or above every identifier that is.
#
# The remaining branches cover formats that are not mostly digits: PAN
# (ABCDE1234F) and passport (letter plus seven digits).
_IDENTIFIER = re.compile(
    r"\b\d(?:[ -]?\d){3,}\b"
    r"|\b[A-Z]{5}\d{4}[A-Z]\b"
    r"|\b[A-Z]\d{7}\b",
    re.IGNORECASE,
)

_FRACTIONS = {
    "half": 5, "a half": 5, "quarter": 3, "three quarters": 8,
    "one": 10, "two": 20, "three": 30, "four": 40, "five": 50,
    "a": 10, "an": 10, "some": 10, "a few": 30, "couple": 20,
}

_OUT_OF = re.compile(
    r"\b(?:we(?:'| a)?re |i(?:'m| am) |is |are )?(?:completely |totally |almost )?"
    r"(?:out of|finished|over|khatam|done with|run out of|ran out of)\b",
    re.IGNORECASE,
)


def _redact(value: str | None) -> tuple[str | None, bool]:
    """Strip identifier-shaped substrings. Returns the text and whether it changed."""
    if not value:
        return value, False
    cleaned = _IDENTIFIER.sub("[number not stored]", value)
    return cleaned, cleaned != value


def _quantity_from(text: str) -> int | None:
    """Spoken quantity to decicounts. 'two packets' -> 20, 'half' -> 5."""
    lowered = text.lower()
    if match := re.search(r"\b(\d{1,3})(?:\.(\d))?\b", lowered):
        whole = int(match.group(1))
        tenths = int(match.group(2) or 0)
        return whole * DC_PER_UNIT + tenths
    for word, value in _FRACTIONS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return value
    return None


_SPOKEN = (
    "no", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
)


def _spoken_count(value: int) -> str:
    """Small counts as words. Everything Selene says is read aloud, and the
    shared invariant is to write numbers the way they are said."""
    return _SPOKEN[value] if 0 <= value < len(_SPOKEN) else str(value)


def _title(value: str) -> str:
    return f"{value[0].upper()}{value[1:]}" if value else value


def _fmt_qty(decicount: int) -> str:
    whole, frac = divmod(decicount, DC_PER_UNIT)
    if frac == 0:
        return str(whole)
    if whole == 0 and frac == 5:
        return "half"
    return f"{whole}.{frac}"


# --------------------------------------------------------------------------- #
# Reminders
# --------------------------------------------------------------------------- #


@tool(
    name="reminder_create",
    description=(
        "Set a reminder. Pass the user's words verbatim as `text`; the date, time "
        "and recurrence are parsed here. Do not compute a timestamp yourself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The user's own words, e.g. 'remind me to pay rent on the 1st'",
            }
        },
        "required": ["text"],
    },
    writes=True,
)
async def reminder_create(*, context: ToolContext, text: str) -> ToolResult:
    when = parse_when(text)
    subject = when.subject or text.strip()

    if not subject:
        return ToolResult.failure("Nothing to remind about. Ask what they want remembered.")

    if when.at is None:
        # A reminder with no time is a note, and Selene's whole value is firing at
        # the right moment. Ask rather than guessing a date.
        return ToolResult.confirm(
            f"When should this fire? Subject: {subject}", subject=subject
        )

    reminder_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO reminders "
                "(text, due_at, recurrence, day_of_month, state, source_turn) "
                "VALUES (:text, :due, :rec, :dom, 'pending', :turn) RETURNING id"
            ),
            {
                "text": subject[:500],
                "due": when.at,
                "rec": when.recurrence,
                "dom": when.day_of_month,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    spoken = speak_when(when.at, when.recurrence)
    return ToolResult.success(
        f"Set. {subject[0].upper()}{subject[1:]}, {spoken}.",
        reminder_id=reminder_id,
        subject=subject,
        due_at=when.at.isoformat(),
        spoken_when=spoken,
        recurrence=when.recurrence,
        # Assumptions are returned so Selene can state them in the same breath —
        # her spec says infer rather than ask, and say what was assumed.
        assumed=when.assumed,
        storage="Astra, not the iPhone Reminders app",
    )


@tool(
    name="reminder_list",
    description="Reminders that are due or upcoming. Exact rows — quote them.",
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Look-ahead window. Default 7."}
        },
    },
)
async def reminder_list(*, context: ToolContext, days: int = 7) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT id, text, due_at, recurrence, state FROM reminders "
                "WHERE state IN ('pending','notified') "
                "  AND due_at IS NOT NULL "
                "  AND due_at <= now() + make_interval(days => :days) "
                "ORDER BY due_at LIMIT 20"
            ),
            {"days": days},
        )
    ).all()

    if not rows:
        return ToolResult.success(
            f"Nothing due in the next {days} days.", count=0, reminders=[]
        )

    overdue = sum(1 for r in rows if r.due_at < datetime.now(UTC))
    return ToolResult.success(
        f"{_spoken_count(len(rows))} due in the next {days} days"
        + (f", {_spoken_count(overdue)} of them overdue." if overdue else "."),
        count=len(rows),
        overdue=overdue,
        reminders=[
            {
                # No id: reminder_complete matches on text, so an id here would
                # only be something the model could read out loud.
                "text": r.text,
                "when": speak_when(r.due_at, r.recurrence),
                "due_at": r.due_at.isoformat(),
                "state": r.state,
            }
            for r in rows
        ],
    )


@tool(
    name="reminder_complete",
    description=(
        "Mark a reminder done. Match on a fragment of its text, e.g. 'rent'. "
        "Recurring reminders roll forward to their next occurrence."
    ),
    parameters={
        "type": "object",
        "properties": {
            "match": {"type": "string", "description": "Part of the reminder's text."}
        },
        "required": ["match"],
    },
    writes=True,
)
async def reminder_complete(*, context: ToolContext, match: str) -> ToolResult:
    row = (
        await context.session.execute(
            sa.text(
                "SELECT id, text, recurrence, day_of_month, due_at FROM reminders "
                "WHERE state IN ('pending','notified') AND text ILIKE :needle "
                "ORDER BY due_at NULLS LAST LIMIT 2"
            ),
            {"needle": f"%{match.strip()}%"},
        )
    ).all()

    if not row:
        return ToolResult.failure(f"No open reminder matching {match!r}.")
    if len(row) > 1:
        return ToolResult.confirm(
            f"Two reminders match {match!r}: {row[0].text}, and {row[1].text}. Which one?",
            candidates=[r.text for r in row],
        )

    target = row[0]

    if target.recurrence:
        # Rolling forward rather than closing is what makes a monthly reminder
        # survive being completed. Closing it would silently end the series.
        next_due = (
            await context.session.execute(
                sa.text(
                    "UPDATE reminders SET due_at = due_at + CASE recurrence "
                    "  WHEN 'daily' THEN interval '1 day' "
                    "  WHEN 'weekly' THEN interval '7 days' "
                    "  WHEN 'monthly' THEN interval '1 month' "
                    "  WHEN 'quarterly' THEN interval '3 months' "
                    "  ELSE interval '1 year' END, "
                    "  state = 'pending', notified_at = NULL "
                    "WHERE id = :id RETURNING due_at"
                ),
                {"id": target.id},
            )
        ).scalar_one()
        spoken = speak_when(next_due, target.recurrence)
        return ToolResult.success(
            f"Done. Next one {spoken}.",
            reminder_id=target.id,
            subject=target.text,
            next_due=next_due.isoformat(),
            spoken_when=spoken,
        )

    await context.session.execute(
        sa.text(
            "UPDATE reminders SET state = 'completed', completed_at = now() WHERE id = :id"
        ),
        {"id": target.id},
    )
    return ToolResult.success(
        "Done.", reminder_id=target.id, subject=target.text
    )


# --------------------------------------------------------------------------- #
# Groceries and inventory
# --------------------------------------------------------------------------- #


@tool(
    name="grocery_add",
    description=(
        "Add something to the grocery list. Returns when it was last bought and "
        "how long that usually lasts — state that context if it is unusual."
    ),
    parameters={
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "The item, e.g. 'rice'."},
            "quantity": {
                "type": "string",
                "description": "Optional, e.g. 'two packets'. Free text.",
            },
        },
        "required": ["item"],
    },
    writes=True,
)
async def grocery_add(
    *, context: ToolContext, item: str, quantity: str | None = None
) -> ToolResult:
    name = item.strip().lower()[:96]
    if not name:
        return ToolResult.failure("No item given.")

    existing = (
        await context.session.execute(
            sa.text(
                "SELECT id, name, last_purchased_at, typical_days_between, "
                "       quantity_dc, on_grocery_list "
                "FROM inventory_items WHERE name = :name"
            ),
            {"name": name},
        )
    ).one_or_none()

    if existing is None:
        item_id = (
            await context.session.execute(
                sa.text(
                    "INSERT INTO inventory_items "
                    "(name, quantity_dc, on_grocery_list, source_turn) "
                    "VALUES (:name, 0, TRUE, :turn) RETURNING id"
                ),
                {"name": name, "turn": context.turn_id},
            )
        ).scalar_one()
        return ToolResult.success(
            f"On the list. {_title(name)}.",
            item_id=item_id,
            item=name,
            first_time=True,
            days_since_purchase=None,
        )

    await context.session.execute(
        sa.text(
            "UPDATE inventory_items SET on_grocery_list = TRUE, updated_at = now(), "
            "       source_turn = :turn WHERE id = :id"
        ),
        {"id": existing.id, "turn": context.turn_id},
    )

    days_since = None
    if existing.last_purchased_at:
        days_since = (datetime.now(UTC) - existing.last_purchased_at).days

    # SEL-05: the prior purchase is handed over as data, and flagged when it is
    # sooner than the usual gap, so noticing does not depend on the model.
    early = (
        days_since is not None
        and existing.typical_days_between is not None
        and days_since < existing.typical_days_between * 2 // 3
    )

    message = f"On the list. {_title(name)}."
    if early:
        message = f"On the list. You bought {_title(name)} {days_since} days ago."

    return ToolResult.success(
        message,
        item_id=existing.id,
        item=name,
        first_time=False,
        days_since_purchase=days_since,
        typical_days_between=existing.typical_days_between,
        sooner_than_usual=early,
    )


@tool(
    name="grocery_list",
    description="Everything currently on the grocery list.",
    parameters={"type": "object", "properties": {}},
)
async def grocery_list(*, context: ToolContext) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT name, quantity_dc, unit, last_purchased_at FROM inventory_items "
                "WHERE on_grocery_list = TRUE ORDER BY name"
            )
        )
    ).all()

    if not rows:
        return ToolResult.success("The list is empty.", count=0, items=[])

    return ToolResult.success(
        f"{_spoken_count(len(rows))} items on the list.",
        count=len(rows),
        items=[
            {
                "name": r.name,
                "in_stock": _fmt_qty(r.quantity_dc),
                "unit": r.unit,
                "last_bought": r.last_purchased_at.date().isoformat()
                if r.last_purchased_at
                else None,
            }
            for r in rows
        ],
    )


@tool(
    name="grocery_clear",
    description="Clear the grocery list after a shop, and record the purchase date.",
    parameters={
        "type": "object",
        "properties": {
            "bought": {
                "type": "boolean",
                "description": "True if the items were actually bought. Default true.",
            }
        },
    },
    writes=True,
)
async def grocery_clear(*, context: ToolContext, bought: bool = True) -> ToolResult:
    if bought:
        # Restocking sets the purchase date, which is what makes the "you bought
        # this eleven days ago" observation possible later. It also learns the
        # gap between purchases rather than requiring it to be configured.
        rows = (
            await context.session.execute(
                sa.text(
                    "UPDATE inventory_items SET "
                    "  typical_days_between = CASE "
                    "    WHEN last_purchased_at IS NULL THEN typical_days_between "
                    "    ELSE COALESCE(("
                    "      typical_days_between + "
                    "      EXTRACT(DAY FROM now() - last_purchased_at)::int) / 2, "
                    "      EXTRACT(DAY FROM now() - last_purchased_at)::int) END, "
                    "  last_purchased_at = now(), "
                    "  quantity_dc = GREATEST(quantity_dc, 10), "
                    "  on_grocery_list = FALSE, "
                    "  updated_at = now() "
                    "WHERE on_grocery_list = TRUE RETURNING name"
                )
            )
        ).all()
        return ToolResult.success(
            f"Cleared. {_spoken_count(len(rows))} items restocked.",
            cleared=len(rows),
            items=[r.name for r in rows],
        )

    rows = (
        await context.session.execute(
            sa.text(
                "UPDATE inventory_items SET on_grocery_list = FALSE, updated_at = now() "
                "WHERE on_grocery_list = TRUE RETURNING name"
            )
        )
    ).all()
    return ToolResult.success(
        f"Cleared. {_spoken_count(len(rows))} items removed without restocking.",
        cleared=len(rows),
        items=[r.name for r in rows],
    )


@tool(
    name="inventory_consume",
    description=(
        "Record that something is running low or finished. Pass the user's words "
        "verbatim. Items that hit zero go onto the grocery list automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "item": {"type": "string"},
            "text": {
                "type": "string",
                "description": "The user's own words, e.g. 'we're out of rice'.",
            },
        },
        "required": ["item"],
    },
    writes=True,
)
async def inventory_consume(
    *, context: ToolContext, item: str, text: str = ""
) -> ToolResult:
    name = item.strip().lower()[:96]
    finished = bool(_OUT_OF.search(text or item))
    used = _quantity_from(text) if text else None

    existing = (
        await context.session.execute(
            sa.text(
                "SELECT id, quantity_dc, low_threshold_dc, last_purchased_at "
                "FROM inventory_items WHERE name = :name"
            ),
            {"name": name},
        )
    ).one_or_none()

    if existing is None:
        item_id = (
            await context.session.execute(
                sa.text(
                    "INSERT INTO inventory_items "
                    "(name, quantity_dc, on_grocery_list, source_turn) "
                    "VALUES (:name, 0, TRUE, :turn) RETURNING id"
                ),
                {"name": name, "turn": context.turn_id},
            )
        ).scalar_one()
        return ToolResult.success(
            f"Noted, and {_title(name)} is on the list.",
            item_id=item_id,
            item=name,
            remaining="0",
            added_to_list=True,
        )

    remaining = 0 if finished else max(0, existing.quantity_dc - (used or DC_PER_UNIT))
    low = remaining <= existing.low_threshold_dc

    await context.session.execute(
        sa.text(
            "UPDATE inventory_items SET quantity_dc = :qty, "
            "  on_grocery_list = CASE WHEN :low THEN TRUE ELSE on_grocery_list END, "
            "  updated_at = now(), source_turn = :turn WHERE id = :id"
        ),
        {"qty": remaining, "low": low, "turn": context.turn_id, "id": existing.id},
    )

    days_since = (
        (datetime.now(UTC) - existing.last_purchased_at).days
        if existing.last_purchased_at
        else None
    )

    if low:
        message = f"Noted, and {_title(name)} is on the list."
    else:
        message = f"Noted. About {_fmt_qty(remaining)} left."

    return ToolResult.success(
        message,
        item_id=existing.id,
        item=name,
        remaining=_fmt_qty(remaining),
        added_to_list=low,
        days_since_purchase=days_since,
    )


@tool(
    name="inventory_status",
    description=(
        "What is running low, or the stock level of one item. Exact counts — "
        "quote them rather than estimating."
    ),
    parameters={
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "Optional. Omit for a low-stock sweep."}
        },
    },
)
async def inventory_status(
    *, context: ToolContext, item: str | None = None
) -> ToolResult:
    if item:
        row = (
            await context.session.execute(
                sa.text(
                    "SELECT name, quantity_dc, unit, low_threshold_dc, "
                    "       last_purchased_at, typical_days_between, on_grocery_list "
                    "FROM inventory_items WHERE name ILIKE :needle LIMIT 1"
                ),
                {"needle": f"%{item.strip().lower()}%"},
            )
        ).one_or_none()

        if row is None:
            return ToolResult.success(
                f"I'm not tracking {item}.", tracked=False, item=item
            )

        days_since = (
            (datetime.now(UTC) - row.last_purchased_at).days
            if row.last_purchased_at
            else None
        )
        return ToolResult.success(
            f"{_fmt_qty(row.quantity_dc)} {row.unit} of {row.name}.",
            tracked=True,
            item=row.name,
            quantity=_fmt_qty(row.quantity_dc),
            unit=row.unit,
            low=row.quantity_dc <= row.low_threshold_dc,
            on_grocery_list=row.on_grocery_list,
            days_since_purchase=days_since,
            typical_days_between=row.typical_days_between,
        )

    rows = (
        await context.session.execute(
            sa.text(
                "SELECT name, quantity_dc, unit, on_grocery_list FROM inventory_items "
                "WHERE quantity_dc <= low_threshold_dc ORDER BY quantity_dc, name LIMIT 20"
            )
        )
    ).all()

    if not rows:
        return ToolResult.success("Nothing is running low.", low_count=0, items=[])

    return ToolResult.success(
        f"{_spoken_count(len(rows))} items running low.",
        low_count=len(rows),
        items=[
            {
                "name": r.name,
                "quantity": _fmt_qty(r.quantity_dc),
                "unit": r.unit,
                "on_grocery_list": r.on_grocery_list,
            }
            for r in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Documents and warranties
# --------------------------------------------------------------------------- #


@tool(
    name="document_track",
    description=(
        "Track that a document exists and when it expires. Never pass an "
        "identifier, account number, or ID number — there is nowhere to put one "
        "and it will be stripped."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "e.g. passport, visa, insurance, licence, lease.",
            },
            "label": {"type": "string", "description": "What it is, in plain words."},
            "expires_on": {"type": "string", "description": "ISO date, if known."},
            "location_note": {
                "type": "string",
                "description": "Where it is kept. No numbers.",
            },
        },
        "required": ["kind", "label"],
    },
    writes=True,
)
async def document_track(
    *,
    context: ToolContext,
    kind: str,
    label: str,
    expires_on: str | None = None,
    location_note: str | None = None,
) -> ToolResult:
    # SEL-07 enforced at the boundary. The table has no identifier column, so the
    # only way a number could survive is inside free text — and it does not.
    clean_label, label_redacted = _redact(label.strip())
    clean_note, note_redacted = _redact(location_note)

    expiry: date | None = None
    if expires_on:
        try:
            expiry = date.fromisoformat(expires_on[:10])
        except ValueError:
            parsed = parse_when(expires_on)
            expiry = parsed.at.date() if parsed.at else None

    document_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO documents (kind, label, expires_on, location_note, source_turn) "
                "VALUES (:kind, :label, :expires, :note, :turn) RETURNING id"
            ),
            {
                "kind": kind.strip().lower()[:48],
                "label": (clean_label or kind)[:96],
                "expires": expiry,
                "note": clean_note,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    message = f"Tracked. {clean_label}"
    if expiry:
        message += f", expires {expiry.isoformat()}"
    message += "."

    return ToolResult.success(
        message,
        document_id=document_id,
        kind=kind,
        label=clean_label,
        expires_on=expiry.isoformat() if expiry else None,
        identifier_stripped=label_redacted or note_redacted,
    )


@tool(
    name="document_expiry",
    description="Documents expiring soon. Exact dates — quote them.",
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Look-ahead window. Default 120."}
        },
    },
)
async def document_expiry(*, context: ToolContext, days: int = 120) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT kind, label, expires_on FROM documents "
                "WHERE expires_on IS NOT NULL "
                "  AND expires_on <= (now() + make_interval(days => :days))::date "
                "ORDER BY expires_on LIMIT 20"
            ),
            {"days": days},
        )
    ).all()

    if not rows:
        return ToolResult.success(
            f"Nothing expiring in the next {days} days.", count=0, documents=[]
        )

    today = datetime.now(UTC).date()
    return ToolResult.success(
        f"{_spoken_count(len(rows))} documents expiring within {days} days.",
        count=len(rows),
        documents=[
            {
                "kind": r.kind,
                "label": r.label,
                "expires_on": r.expires_on.isoformat(),
                "days_left": (r.expires_on - today).days,
            }
            for r in rows
        ],
    )


@tool(
    name="warranty_track",
    description="Record a warranty window for something bought.",
    parameters={
        "type": "object",
        "properties": {
            "item": {"type": "string"},
            "expires_on": {"type": "string", "description": "ISO date."},
            "purchased_on": {"type": "string", "description": "ISO date, if known."},
            "note": {"type": "string"},
        },
        "required": ["item"],
    },
    writes=True,
)
async def warranty_track(
    *,
    context: ToolContext,
    item: str,
    expires_on: str | None = None,
    purchased_on: str | None = None,
    note: str | None = None,
) -> ToolResult:
    def _as_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            parsed = parse_when(value)
            return parsed.at.date() if parsed.at else None

    clean_note, _ = _redact(note)

    warranty_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO warranties (item, purchased_on, expires_on, note, source_turn) "
                "VALUES (:item, :bought, :expires, :note, :turn) RETURNING id"
            ),
            {
                "item": item.strip()[:96],
                "bought": _as_date(purchased_on),
                "expires": _as_date(expires_on),
                "note": clean_note,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    expiry = _as_date(expires_on)
    message = f"Tracked. {item}"
    if expiry:
        message += f", warranty to {expiry.isoformat()}"
    message += "."

    return ToolResult.success(
        message,
        warranty_id=warranty_id,
        item=item,
        expires_on=expiry.isoformat() if expiry else None,
    )


SELENE_TOOLS: list[str] = [
    "reminder_create",
    "reminder_list",
    "reminder_complete",
    "grocery_add",
    "grocery_list",
    "grocery_clear",
    "inventory_consume",
    "inventory_status",
    "document_track",
    "document_expiry",
    "warranty_track",
]

# Astraea reports across domains but never writes a specialist's data.
ASTRAEA_HOME_TOOLS: list[str] = [
    "reminder_list",
    "inventory_status",
    "document_expiry",
]
