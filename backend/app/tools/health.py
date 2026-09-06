"""Lyra's tools: meals, macros, workouts, recovery.

Same rule as Vega's: every figure Lyra states comes from a SQL aggregate here.
Macros in decigrams, loads in grams, so totals are exact integer arithmetic.

Lyra's spec forbids implying she can see the Apple Watch when she cannot. That is
enforced structurally: `health_recovery` reports staleness as data, so "no watch
data since Tuesday" is something she reads rather than something she remembers to
say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from app.tools.base import ToolContext, ToolResult, tool
from app.tools.portions import detect_meal_kind, parse_meal

DG_PER_G = 10
G_PER_KG = 1000


def _fmt_g(decigrams: int) -> str:
    """Decigrams to a speakable gram figure."""
    whole, frac = divmod(decigrams, DG_PER_G)
    return f"{whole}" if frac == 0 else f"{whole}.{frac}"


async def _match_food(context: ToolContext, name: str) -> Any:
    """Resolve a spoken food name against the table, by name then alias.

    Longest match wins so "chicken curry" beats "chicken".
    """
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT id, name, aliases, kcal, protein_dg, carbs_dg, fat_dg, "
                "       serving_label FROM foods"
            )
        )
    ).all()

    best, best_len = None, 0
    for row in rows:
        candidates = [row.name, *(row.aliases or [])]
        for candidate in candidates:
            if candidate in name and len(candidate) > best_len:
                best, best_len = row, len(candidate)
    return best


@tool(
    name="meal_log",
    description=(
        "Record what the user ate. Pass their words verbatim as `text`; do not "
        "pre-parse portions or estimate macros yourself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The user's own words, e.g. 'two rotis, dal and curd'",
            }
        },
        "required": ["text"],
    },
    writes=True,
)
async def meal_log(*, context: ToolContext, text: str) -> ToolResult:
    parsed = parse_meal(text)
    if not parsed:
        return ToolResult.failure("No food found in that. Ask what they ate.")

    meal_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO meals (kind, note, occurred_at, source_turn) "
                "VALUES (:kind, :note, now(), :turn) RETURNING id"
            ),
            {"kind": detect_meal_kind(text), "note": text, "turn": context.turn_id},
        )
    ).scalar_one()

    totals = {"kcal": 0, "protein_dg": 0, "carbs_dg": 0, "fat_dg": 0}
    unmatched: list[str] = []

    for item in parsed:
        food = await _match_food(context, item.food_name)

        if food is None:
            unmatched.append(item.food_name)
            # Recorded with zero macros and flagged, so the day is visibly
            # incomplete rather than quietly understated.
            await context.session.execute(
                sa.text(
                    "INSERT INTO meal_items "
                    "(meal_id, raw_text, quantity_dc, estimated) "
                    "VALUES (:meal, :raw, :qty, TRUE)"
                ),
                {"meal": meal_id, "raw": item.raw_text[:128], "qty": item.quantity_dc},
            )
            continue

        scaled = {
            "kcal": food.kcal * item.quantity_dc // 10,
            "protein_dg": food.protein_dg * item.quantity_dc // 10,
            "carbs_dg": food.carbs_dg * item.quantity_dc // 10,
            "fat_dg": food.fat_dg * item.quantity_dc // 10,
        }
        for key, value in scaled.items():
            totals[key] += value

        await context.session.execute(
            sa.text(
                "INSERT INTO meal_items "
                "(meal_id, food_id, raw_text, quantity_dc, kcal, protein_dg, "
                " carbs_dg, fat_dg, estimated) "
                "VALUES (:meal, :food, :raw, :qty, :kcal, :p, :c, :f, TRUE)"
            ),
            {
                "meal": meal_id,
                "food": food.id,
                "raw": item.raw_text[:128],
                "qty": item.quantity_dc,
                "kcal": scaled["kcal"],
                "p": scaled["protein_dg"],
                "c": scaled["carbs_dg"],
                "f": scaled["fat_dg"],
            },
        )

    day = await _day_totals(context)
    message = (
        f"Logged. About {totals['kcal']} calories, "
        f"{_fmt_g(totals['protein_dg'])} grams protein. "
        f"Day's at {day['kcal']} calories, {_fmt_g(day['protein_dg'])} grams protein."
    )
    if unmatched:
        message += f" I don't have {unmatched[0]} in my table, so it isn't counted."

    return ToolResult.success(
        message,
        meal_id=meal_id,
        items=len(parsed),
        unmatched=unmatched,
        meal_kcal=totals["kcal"],
        meal_protein_g=_fmt_g(totals["protein_dg"]),
        day_kcal=day["kcal"],
        day_protein_g=_fmt_g(day["protein_dg"]),
    )


async def _day_totals(context: ToolContext, *, days_back: int = 0) -> dict[str, int]:
    since = (datetime.now(UTC) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    row = (
        await context.session.execute(
            sa.text(
                "SELECT COALESCE(SUM(i.kcal),0) AS kcal, "
                "       COALESCE(SUM(i.protein_dg),0) AS protein_dg, "
                "       COALESCE(SUM(i.carbs_dg),0) AS carbs_dg, "
                "       COALESCE(SUM(i.fat_dg),0) AS fat_dg "
                "FROM meal_items i JOIN meals m ON m.id = i.meal_id "
                "WHERE m.superseded_by IS NULL AND m.occurred_at >= :since"
            ),
            {"since": since},
        )
    ).one()
    return {
        "kcal": int(row.kcal),
        "protein_dg": int(row.protein_dg),
        "carbs_dg": int(row.carbs_dg),
        "fat_dg": int(row.fat_dg),
    }


@tool(
    name="macro_totals",
    description=(
        "Calories and macros for a day, against targets. Exact SQL aggregates — "
        "quote them verbatim, never recalculate."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days_back": {"type": "integer", "description": "0 = today, 1 = yesterday."}
        },
    },
)
async def macro_totals(*, context: ToolContext, days_back: int = 0) -> ToolResult:
    totals = await _day_totals(context, days_back=days_back)

    targets = (
        await context.session.execute(
            sa.text(
                "SELECT predicate, value FROM memory_facts "
                "WHERE namespace = 'health' AND valid_to IS NULL "
                "  AND retracted_at IS NULL "
                "  AND predicate IN ('daily_protein_target','daily_calorie_target')"
            )
        )
    ).all()
    target_map = {r.predicate: r.value for r in targets}

    protein_target = (target_map.get("daily_protein_target") or {}).get("grams")
    calorie_target = (target_map.get("daily_calorie_target") or {}).get("kcal")

    message = (
        f"{totals['kcal']} calories and {_fmt_g(totals['protein_dg'])} grams protein"
    )
    if protein_target:
        message += f", against a target of {protein_target}"
    message += "."

    return ToolResult.success(
        message,
        kcal=totals["kcal"],
        protein_g=_fmt_g(totals["protein_dg"]),
        carbs_g=_fmt_g(totals["carbs_dg"]),
        fat_g=_fmt_g(totals["fat_dg"]),
        protein_target_g=protein_target,
        calorie_target=calorie_target,
    )


@tool(
    name="workout_log",
    description="Record a training session. Pass the user's words verbatim.",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {
                "type": "string",
                "description": "e.g. legs, push, pull, run, cycling.",
            },
            "duration_min": {"type": "integer"},
            "perceived_effort": {"type": "integer", "description": "1-10, optional."},
        },
        "required": ["text"],
    },
    writes=True,
)
async def workout_log(
    *,
    context: ToolContext,
    text: str,
    kind: str | None = None,
    duration_min: int | None = None,
    perceived_effort: int | None = None,
) -> ToolResult:
    workout_id = (
        await context.session.execute(
            sa.text(
                "INSERT INTO workouts "
                "(kind, note, duration_min, perceived_effort, occurred_at, source_turn) "
                "VALUES (:kind, :note, :dur, :effort, now(), :turn) RETURNING id"
            ),
            {
                "kind": (kind or _infer_workout_kind(text))[:32],
                "note": text,
                "dur": duration_min,
                "effort": perceived_effort,
                "turn": context.turn_id,
            },
        )
    ).scalar_one()

    week = (
        await context.session.execute(
            sa.text(
                "SELECT COUNT(*) FROM workouts WHERE occurred_at >= now() - interval '7 days'"
            )
        )
    ).scalar_one()

    return ToolResult.success(
        f"Logged. That's {week} this week.",
        workout_id=workout_id,
        sessions_this_week=int(week),
    )


_WORKOUT_KINDS = (
    "legs", "push", "pull", "chest", "back", "shoulders", "arms", "core",
    "run", "running", "cycling", "swim", "yoga", "cardio", "walk", "hiit",
)


def _infer_workout_kind(text: str) -> str:
    lowered = text.lower()
    for kind in _WORKOUT_KINDS:
        if kind in lowered:
            return kind
    return "session"


@tool(
    name="health_recovery",
    description=(
        "Sleep, HRV and resting heart rate from Apple Health, with how stale the "
        "data is. Check this before prescribing intensity."
    ),
    parameters={"type": "object", "properties": {}},
)
async def health_recovery(*, context: ToolContext) -> ToolResult:
    """Recovery signals plus explicit staleness.

    Staleness is returned as data rather than left to the agent's memory, because
    Lyra's spec forbids implying she can see the watch when she cannot, and a
    prompt instruction is not a guarantee.
    """
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT DISTINCT ON (metric) metric, value_milli, unit, started_at "
                "FROM health_samples "
                "WHERE metric IN ('sleep','hrv','resting_hr','steps') "
                "ORDER BY metric, started_at DESC"
            )
        )
    ).all()

    if not rows:
        return ToolResult.success(
            "No Apple Health data has been received yet.",
            connected=False,
            samples={},
        )

    latest = max(r.started_at for r in rows)
    stale_hours = int((datetime.now(UTC) - latest).total_seconds() // 3600)

    samples = {
        r.metric: {
            "value": r.value_milli / 1000,
            "unit": r.unit,
            "at": r.started_at.isoformat(),
        }
        for r in rows
    }

    return ToolResult.success(
        f"Latest health data is {stale_hours} hours old.",
        connected=True,
        stale_hours=stale_hours,
        samples=samples,
    )


@tool(
    name="workout_history",
    description="Recent training sessions, newest first.",
    parameters={
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "Default 14."}},
    },
)
async def workout_history(*, context: ToolContext, days: int = 14) -> ToolResult:
    rows = (
        await context.session.execute(
            sa.text(
                "SELECT kind, duration_min, perceived_effort, occurred_at "
                "FROM workouts WHERE occurred_at >= now() - make_interval(days => :days) "
                "ORDER BY occurred_at DESC LIMIT 20"
            ),
            {"days": days},
        )
    ).all()

    return ToolResult.success(
        f"{len(rows)} sessions in the last {days} days.",
        session_count=len(rows),
        sessions=[
            {
                "kind": r.kind,
                "duration_min": r.duration_min,
                "effort": r.perceived_effort,
                "at": r.occurred_at.isoformat(),
            }
            for r in rows
        ],
    )


LYRA_TOOLS: list[str] = [
    "meal_log",
    "macro_totals",
    "workout_log",
    "workout_history",
    "health_recovery",
]
