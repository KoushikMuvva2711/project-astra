"""Lyra's portion parsing and macro arithmetic.

LYR-04 is the headline: "two rotis and dal" must resolve without asking for grams.
"""

import pytest

from app.tools import health  # noqa: F401  — registers Lyra's tools
from app.tools.base import ToolContext, dispatch
from app.tools.health import LYRA_TOOLS
from app.tools.portions import detect_meal_kind, parse_meal

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(db, turn_id):
    return ToolContext(session=db, agent="lyra", turn_id=turn_id)


async def call(name, ctx, **args):
    return await dispatch(name, args, allowed=LYRA_TOOLS, context=ctx)


# ── Portion parsing (no DB) ──────────────────────────────────────────────────

async def test_indian_meal_parses_without_asking_for_grams():
    """LYR-04. The canonical input from the spec."""
    items = parse_meal("two rotis, dal and a katori of curd")
    names = [i.food_name for i in items]
    assert "rotis" in names
    assert "dal" in names
    assert "curd" in names


async def test_quantities_are_tenths_of_a_serving():
    items = {i.food_name: i.quantity_dc for i in parse_meal("two rotis and one egg")}
    assert items["rotis"] == 20
    assert items["egg"] == 10


async def test_bare_food_defaults_to_one_serving():
    assert parse_meal("dal")[0].quantity_dc == 10


async def test_container_words_are_not_treated_as_food():
    items = parse_meal("a bowl of rice and a glass of milk")
    names = [i.food_name for i in items]
    assert "rice" in names
    assert "milk" in names
    assert not any("bowl" in n or "glass" in n for n in names)


async def test_digit_quantities_work():
    assert parse_meal("3 idlis")[0].quantity_dc == 30


async def test_half_portions():
    assert parse_meal("half a paratha")[0].quantity_dc == 5


async def test_conversational_filler_is_stripped():
    items = parse_meal("I had two eggs and some dal for lunch")
    names = [i.food_name for i in items]
    assert "eggs" in names
    assert "dal" in names


async def test_empty_input_yields_nothing_to_log():
    assert parse_meal("") == []


@pytest.mark.parametrize(
    "text,kind",
    [
        ("had eggs for breakfast", "breakfast"),
        ("lunch was dal chawal", "lunch"),
        ("dinner: two rotis", "dinner"),
        ("just a snack", "snack"),
        ("two rotis", "meal"),
    ],
)
async def test_meal_kind_detection(text, kind):
    assert detect_meal_kind(text) == kind


# ── Logging and macro arithmetic ─────────────────────────────────────────────

async def test_meal_log_computes_macros_from_the_food_table(ctx):
    result = await call("meal_log", ctx, text="two eggs")
    assert result.ok
    # 2 x egg: 78 kcal, 6.3 g protein each.
    assert result.data["meal_kcal"] == 156
    assert result.data["meal_protein_g"] == "12.6"


async def test_quantities_scale_macros_exactly(ctx):
    single = await call("meal_log", ctx, text="one egg")
    triple = await call("meal_log", ctx, text="3 eggs")
    assert triple.data["meal_kcal"] == single.data["meal_kcal"] * 3


async def test_day_total_accumulates_across_meals(ctx):
    await call("meal_log", ctx, text="two eggs")
    result = await call("meal_log", ctx, text="one banana")
    # 156 + 105
    assert result.data["day_kcal"] == 261


async def test_unknown_food_is_recorded_but_not_counted(ctx):
    """Visibly incomplete beats quietly understated."""
    result = await call("meal_log", ctx, text="one zorbfruit")
    assert result.ok
    assert result.data["unmatched"] == ["zorbfruit"]
    assert result.data["meal_kcal"] == 0
    assert "isn't counted" in result.message


async def test_longest_food_match_wins(ctx):
    """'chicken curry' must not resolve to 'chicken'."""
    result = await call("meal_log", ctx, text="one chicken curry")
    assert result.data["meal_kcal"] == 240


async def test_aliases_resolve(ctx):
    for phrasing in ("one chapati", "one roti", "one phulka"):
        result = await call("meal_log", ctx, text=phrasing)
        assert result.data["meal_kcal"] == 71, phrasing


async def test_meal_with_no_food_fails_rather_than_writing_an_empty_meal(ctx):
    result = await call("meal_log", ctx, text="I had something")
    assert not result.ok


async def test_macro_totals_match_the_sum_of_logged_meals(ctx):
    await call("meal_log", ctx, text="two eggs")
    await call("meal_log", ctx, text="one banana")
    await call("meal_log", ctx, text="one katori dal")

    totals = await call("macro_totals", ctx)
    assert totals.data["kcal"] == 156 + 105 + 120


async def test_macro_totals_report_the_stored_target(ctx, db):
    import sqlalchemy as sa

    await db.execute(
        sa.text(
            "INSERT INTO memory_facts (namespace, entity, predicate, value, "
            "value_text, asserted_by, valid_from) VALUES "
            "('health','user','daily_protein_target', CAST(:v AS jsonb), "
            "'protein target 150g', 'lyra', now())"
        ),
        {"v": '{"grams": 150}'},
    )
    totals = await call("macro_totals", ctx)
    assert totals.data["protein_target_g"] == 150
    assert "150" in totals.message


# ── Workouts ─────────────────────────────────────────────────────────────────

async def test_workout_log_counts_the_week(ctx):
    await call("workout_log", ctx, text="legs day")
    result = await call("workout_log", ctx, text="push day")
    assert result.data["sessions_this_week"] == 2
    assert "2 this week" in result.message


async def test_workout_kind_is_inferred(ctx, db):
    import sqlalchemy as sa

    await call("workout_log", ctx, text="did legs today, felt heavy")
    kind = (await db.execute(
        sa.text("SELECT kind FROM workouts ORDER BY id DESC LIMIT 1")
    )).scalar_one()
    assert kind == "legs"


# ── Health data honesty (LYR-06) ─────────────────────────────────────────────

async def test_recovery_reports_no_connection_when_no_data_exists(ctx):
    """Lyra must not imply she can see the watch when nothing has arrived."""
    result = await call("health_recovery", ctx)
    assert result.data["connected"] is False
    assert "No Apple Health data" in result.message


async def test_recovery_reports_staleness_as_data(ctx, db):
    import sqlalchemy as sa

    await db.execute(
        sa.text(
            "INSERT INTO health_samples (metric, value_milli, unit, started_at) "
            "VALUES ('sleep', 21600000, 'seconds', now() - interval '30 hours')"
        )
    )
    result = await call("health_recovery", ctx)
    assert result.data["connected"] is True
    assert result.data["stale_hours"] >= 29
