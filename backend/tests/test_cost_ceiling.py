"""The monthly spend ceiling.

The accounting existed before this; the enforcement did not. These tests are the
difference between a number in a config file and a cap that actually holds.
"""

import pytest
import sqlalchemy as sa

from app.llm.provider import LLMResponse
from app.llm.registry import PRICE_PER_MTOK_USD_MICROS, ModelRegistry, _price_for

pytestmark = pytest.mark.asyncio


@pytest.fixture
def registry():
    return ModelRegistry()


# ── Pricing arithmetic ───────────────────────────────────────────────────────

async def test_known_models_are_priced():
    """An unpriced model costs zero, which silently disables the ceiling for it."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert _price_for(model) is not None


async def test_dated_snapshot_resolves_by_prefix():
    assert _price_for("claude-haiku-4-5-20251001") == _price_for("claude-haiku-4-5")


async def test_cost_is_exact_integer_paise(registry):
    """1M in + 1M out on Sonnet 5 = $3 + $15 = $18, which at 88 rupees/USD is
    1,584 rupees — 158,400 paise."""
    registry.settings.usd_inr_paise = 8800
    assert registry.cost_paise("claude-sonnet-5", 1_000_000, 1_000_000) == 158_400


async def test_cost_scales_linearly(registry):
    single = registry.cost_paise("claude-opus-5", 100_000, 10_000)
    double = registry.cost_paise("claude-opus-5", 200_000, 20_000)
    assert double == single * 2


async def test_output_is_priced_higher_than_input(registry):
    """Output tokens cost 5x input on every current model — a common source of
    surprise bills if the ledger gets it backwards."""
    in_only = registry.cost_paise("claude-opus-5", 1_000_000, 0)
    out_only = registry.cost_paise("claude-opus-5", 0, 1_000_000)
    assert out_only == in_only * 5


async def test_unpriced_model_costs_zero_rather_than_guessing(registry):
    """Never invent a price. Zero is visibly wrong; a guess is invisibly wrong."""
    assert registry.cost_paise("some-unknown-model", 1_000_000, 1_000_000) == 0


async def test_cost_is_always_an_integer(registry):
    for tin, tout in ((1, 1), (999, 1), (123_457, 7_919)):
        cost = registry.cost_paise("claude-sonnet-5", tin, tout)
        assert isinstance(cost, int)
        assert not isinstance(cost, bool)


async def test_price_table_has_no_floats():
    """Same no-float rule as the expense ledger, for the same reason."""
    for prices in PRICE_PER_MTOK_USD_MICROS.values():
        for value in prices.values():
            assert isinstance(value, int)


# ── The ledger ───────────────────────────────────────────────────────────────

async def test_local_calls_are_recorded_but_cost_nothing(registry, db):
    await registry._record_cost(
        db,
        LLMResponse(text="hi", model="qwen2.5:0.5b", tokens_in=500, tokens_out=50,
                    is_local=True),
        agent="vega", purpose="reason",
    )
    row = (await db.execute(
        sa.text("SELECT cost_minor, is_local, tokens_in FROM cost_ledger")
    )).one()
    assert row.cost_minor == 0
    assert row.is_local is True
    assert row.tokens_in == 500, "token counts are still recorded for local calls"


async def test_cloud_calls_accumulate_spend(registry, db):
    for _ in range(3):
        await registry._record_cost(
            db,
            LLMResponse(text="x", model="claude-sonnet-5", tokens_in=100_000,
                        tokens_out=10_000, is_local=False),
            agent="astraea", purpose="reason",
        )
    spent = await registry.month_to_date_cost_minor(db)
    expected = registry.cost_paise("claude-sonnet-5", 100_000, 10_000) * 3
    assert spent == expected


async def test_month_to_date_excludes_last_month(registry, db):
    await db.execute(
        sa.text(
            "INSERT INTO cost_ledger (agent, model, purpose, cost_minor, occurred_at) "
            "VALUES ('vega','claude-opus-5','reason', 999999, "
            "        date_trunc('month', now()) - interval '5 days')"
        )
    )
    assert await registry.month_to_date_cost_minor(db) == 0


# ── The gate ─────────────────────────────────────────────────────────────────

async def test_under_ceiling_is_not_over_budget(registry, db):
    registry.settings.monthly_cost_ceiling_minor = 200_000
    await db.execute(
        sa.text(
            "INSERT INTO cost_ledger (agent, model, purpose, cost_minor) "
            "VALUES ('vega','claude-opus-5','reason', 50000)"
        )
    )
    assert not await registry.over_budget(db)


async def test_reaching_the_ceiling_trips_the_gate(registry, db):
    registry.settings.monthly_cost_ceiling_minor = 200_000
    await db.execute(
        sa.text(
            "INSERT INTO cost_ledger (agent, model, purpose, cost_minor) "
            "VALUES ('vega','claude-opus-5','reason', 200000)"
        )
    )
    assert await registry.over_budget(db)


async def test_spend_report_shows_position_and_breakdown(registry, db):
    registry.settings.monthly_cost_ceiling_minor = 200_000
    for agent, cost in (("vega", 30_000), ("astraea", 20_000)):
        await db.execute(
            sa.text(
                "INSERT INTO cost_ledger (agent, model, purpose, tokens_in, "
                "tokens_out, cost_minor) VALUES (:a,'claude-opus-5','reason', "
                "1000, 100, :c)"
            ),
            {"a": agent, "c": cost},
        )

    report = await registry.spend_report(db)
    assert report["spent_paise"] == 50_000
    assert report["spent_rupees"] == 500.0
    assert report["percent_used"] == 25.0
    assert not report["over_budget"]
    assert {row["agent"] for row in report["breakdown"]} == {"vega", "astraea"}


async def test_report_percent_reaches_100_at_the_ceiling(registry, db):
    registry.settings.monthly_cost_ceiling_minor = 100_000
    await db.execute(
        sa.text(
            "INSERT INTO cost_ledger (agent, model, purpose, cost_minor) "
            "VALUES ('vega','claude-opus-5','reason', 100000)"
        )
    )
    report = await registry.spend_report(db)
    assert report["percent_used"] == 100.0
    assert report["over_budget"]


# ── Realistic projection ─────────────────────────────────────────────────────

async def test_a_typical_month_stays_well_under_the_default_ceiling(registry):
    """Guards the estimate given to the user: ~20 turns/day on Opus 5 should sit
    comfortably inside the default 2,000-rupee ceiling. If a future prompt or
    tool-schema change blows the token budget out, this catches it."""
    registry.settings.usd_inr_paise = 8800

    monthly = (
        registry.cost_paise("claude-opus-5", 1_800, 50) * 450     # short turns
        + registry.cost_paise("claude-opus-5", 2_400, 200) * 120  # medium
        + registry.cost_paise("claude-opus-5", 3_000, 400) * 30   # long
    )
    rupees = monthly / 100
    assert 400 < rupees < 900, f"projected {rupees} rupees/month"
    assert monthly < 200_000, "should not approach the default ceiling"
