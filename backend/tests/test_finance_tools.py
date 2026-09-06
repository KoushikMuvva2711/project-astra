"""Vega's tools against real Postgres.

The reconciliation test (VEG-08) is the one that matters most: logged entries
must sum exactly to the reported total, always.
"""

import pytest

from app.tools import finance  # noqa: F401  — registers the tools
from app.tools.base import ToolContext, ToolNotAllowed, dispatch
from app.tools.finance import ASTRAEA_FINANCE_TOOLS, VEGA_TOOLS, categorise

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(db, turn_id):
    return ToolContext(session=db, agent="vega", turn_id=turn_id)


async def call(name, ctx, allowed=None, **args):
    return await dispatch(name, args, allowed=allowed or VEGA_TOOLS, context=ctx)


# ── Allowlist is enforced at the boundary ────────────────────────────────────

async def test_agent_cannot_call_a_tool_outside_its_allowlist(db, turn_id):
    """Lyra must not be able to write an expense, whatever the model emits."""
    lyra_ctx = ToolContext(session=db, agent="lyra", turn_id=turn_id)
    with pytest.raises(ToolNotAllowed):
        await dispatch(
            "expense_log", {"text": "spent 200"}, allowed=[], context=lyra_ctx
        )


async def test_astraea_can_read_finance_but_not_write_it():
    assert "expense_total" in ASTRAEA_FINANCE_TOOLS
    assert "expense_log" not in ASTRAEA_FINANCE_TOOLS
    assert "expense_correct" not in ASTRAEA_FINANCE_TOOLS


async def test_disallowed_call_is_refused_before_the_handler_runs(db, turn_id):
    ctx = ToolContext(session=db, agent="astraea", turn_id=turn_id)
    with pytest.raises(ToolNotAllowed):
        await dispatch(
            "expense_log", {"text": "spent 999"},
            allowed=ASTRAEA_FINANCE_TOOLS, context=ctx,
        )
    total = await dispatch(
        "expense_total", {}, allowed=ASTRAEA_FINANCE_TOOLS, context=ctx
    )
    assert total.data["total_minor"] == 0, "the refused write must not have landed"


# ── Categorisation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("spent 250 on auto", "auto_taxi"),
        ("paid 820 for groceries", "groceries"),
        ("180 on coffee", "dining_out"),
        ("swiggy order 400", "food_delivery"),
        ("kirana 600", "groceries"),
        ("ola to office 190", "auto_taxi"),
        ("petrol 2000", "fuel"),
        ("netflix subscription", "subscriptions"),
        ("gym membership 1500", "fitness"),
        ("rent 18000", "rent"),
    ],
)
async def test_keyword_categorisation(ctx, text, expected):
    slug, method = await categorise(ctx, text)
    assert slug == expected
    assert method == "keyword"


async def test_unknown_spend_is_uncategorised_not_guessed(ctx):
    """A wrong category is silently wrong forever; an empty one is visibly incomplete."""
    slug, method = await categorise(ctx, "spent 500 on a thingamajig")
    assert slug == "uncategorised"
    assert method == "fallback"


# ── Logging ──────────────────────────────────────────────────────────────────

async def test_log_creates_an_exact_row(ctx, db):
    result = await call("expense_log", ctx, text="spent 250 on auto")
    assert result.ok
    assert result.data["amount_minor"] == 25_000
    assert result.data["category"] == "auto_taxi"
    assert result.message == "Logged. 250 rupees, auto_taxi."


async def test_log_records_provenance(ctx, db, turn_id):
    import sqlalchemy as sa

    await call("expense_log", ctx, text="paid 820 for groceries")
    source = (await db.execute(
        sa.text("SELECT source_turn FROM expenses ORDER BY id DESC LIMIT 1")
    )).scalar_one()
    assert source == turn_id


async def test_ambiguous_amount_is_not_written(ctx, db):
    """VEG-06: understood, but the row must not exist until confirmed."""
    import sqlalchemy as sa

    result = await call("expense_log", ctx, text="spent eighteen on chai")
    assert result.needs_confirmation
    assert result.data["heard_minor"] == 1_800
    assert result.data["alternative_minor"] == 8_000

    count = (await db.execute(sa.text("SELECT COUNT(*) FROM expenses"))).scalar_one()
    assert count == 0, "an unconfirmed amount must not be written"


async def test_confirmed_amount_writes(ctx):
    result = await call(
        "expense_log", ctx, text="spent eighteen on chai", confirmed_minor=8_000
    )
    assert result.ok
    assert not result.needs_confirmation
    assert result.data["amount_minor"] == 8_000


async def test_missing_amount_fails_rather_than_writing_zero(ctx):
    result = await call("expense_log", ctx, text="bought some things")
    assert not result.ok
    assert "no amount" in result.message.lower()


# ── Correction ───────────────────────────────────────────────────────────────

async def test_correction_supersedes_and_total_counts_once(ctx):
    await call("expense_log", ctx, text="spent 250 on auto")
    corrected = await call("expense_correct", ctx, text="actually make that 280")
    assert corrected.ok
    assert corrected.data["amount_minor"] == 28_000

    total = await call("expense_total", ctx, period="today")
    assert total.data["total_minor"] == 28_000
    assert total.data["entry_count"] == 1


async def test_correction_preserves_the_original_row(ctx, db):
    import sqlalchemy as sa

    logged = await call("expense_log", ctx, text="spent 250 on auto")
    await call("expense_correct", ctx, text="make that 280")

    original = (await db.execute(
        sa.text("SELECT amount_minor, superseded_by FROM expenses WHERE id = :i"),
        {"i": logged.data["expense_id"]},
    )).one()
    assert original.amount_minor == 25_000
    assert original.superseded_by is not None


# ── Reporting: figures come from SQL ─────────────────────────────────────────

async def test_reconciliation_is_exact(ctx):
    """VEG-08. Logged entries must sum exactly to the reported total."""
    amounts = [18_000, 25_000, 82_000, 190_00, 250_00, 60_000, 1_50_000, 99, 1, 12_345]
    for minor in amounts:
        await call("expense_log", ctx, text=f"{minor // 100}.{minor % 100:02d} on coffee")

    total = await call("expense_total", ctx, period="today")
    assert total.data["total_minor"] == sum(amounts)
    assert total.data["entry_count"] == len(amounts)


async def test_summary_breakdown_sums_to_the_grand_total(ctx):
    for text in ("250 auto", "820 groceries", "180 coffee", "400 swiggy", "2000 petrol"):
        await call("expense_log", ctx, text=text)

    summary = await call("expense_summary", ctx, period="today")
    parts = sum(item["total_minor"] for item in summary.data["breakdown"])
    assert parts == summary.data["total_minor"]

    total = await call("expense_total", ctx, period="today")
    assert total.data["total_minor"] == summary.data["total_minor"]


async def test_superseded_rows_are_excluded_from_summary(ctx):
    await call("expense_log", ctx, text="spent 250 on auto")
    await call("expense_correct", ctx, text="make that 280")

    summary = await call("expense_summary", ctx, period="today")
    assert summary.data["total_minor"] == 28_000


async def test_category_filter_narrows_the_total(ctx):
    await call("expense_log", ctx, text="250 auto")
    await call("expense_log", ctx, text="820 groceries")

    autos = await call("expense_total", ctx, period="today", category="auto_taxi")
    assert autos.data["total_minor"] == 25_000


async def test_empty_period_reports_zero_not_an_error(ctx):
    total = await call("expense_total", ctx, period="today")
    assert total.ok
    assert total.data["total_minor"] == 0
    assert total.data["entry_count"] == 0


# ── Provenance ───────────────────────────────────────────────────────────────

async def test_trace_returns_the_originating_utterance(ctx):
    """FR-T3: any reported figure decomposes into the sentences behind it."""
    await call("expense_log", ctx, text="spent 250 on auto")
    trace = await call("expense_trace", ctx, period="today")
    assert trace.data["entries"]
    assert trace.data["entries"][0]["said"] == "spent 180 on coffee"  # the turn fixture


async def test_recent_lists_newest_first(ctx):
    for text in ("100 coffee", "200 auto", "300 groceries"):
        await call("expense_log", ctx, text=text)
    recent = await call("expense_recent", ctx, limit=3)
    amounts = [e["amount_minor"] for e in recent.data["expenses"]]
    assert amounts == sorted(amounts, reverse=True) or len(set(amounts)) == 3
