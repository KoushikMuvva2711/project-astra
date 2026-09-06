"""Selene's tools against the real schema.

The acceptance tests from docs/agents/selene.md that can be enforced in code
live here. SEL-05 and SEL-07 are the two that matter most: both describe
behaviour that fails silently when left to a prompt.
"""

import pytest
import sqlalchemy as sa

from app.tools import home  # noqa: F401  — registers Selene's tools
from app.tools.base import ToolContext, ToolNotAllowed, dispatch
from app.tools.home import SELENE_TOOLS

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(db, turn_id):
    return ToolContext(session=db, agent="selene", turn_id=turn_id)


async def call(name, ctx, **args):
    return await dispatch(name, args, allowed=SELENE_TOOLS, context=ctx)


# ── Reminders ────────────────────────────────────────────────────────────────

async def test_reminder_is_written_with_a_resolved_time(ctx, db):
    result = await call("reminder_create", ctx, text="remind me to pay rent on the 1st")
    assert result.ok

    row = (
        await db.execute(
            sa.text("SELECT text, due_at, recurrence, day_of_month, state FROM reminders")
        )
    ).one()
    assert row.text == "pay rent"
    assert row.due_at is not None
    assert row.state == "pending"


async def test_rent_recurrence_is_inferred_not_asked(ctx, db):
    """SEL-04. A clarifying question here is the failure."""
    result = await call("reminder_create", ctx, text="remind me to pay rent on the 1st")
    assert not result.needs_confirmation
    assert result.data["recurrence"] == "monthly"
    assert "monthly" in result.data["assumed"]


async def test_confirmation_is_one_line(ctx):
    """SEL-01. The spec's own example is "Set. Rent, the 1st, monthly." — a lead
    word and a clause, on one line."""
    result = await call("reminder_create", ctx, text="remind me to call the bank tomorrow at 10 am")
    assert "\n" not in result.message
    assert len(result.message) < 90
    assert result.message.startswith("Set. Call the bank,")


async def test_a_reminder_with_no_time_asks_rather_than_guessing(ctx, db):
    result = await call("reminder_create", ctx, text="remind me to think about the trip")
    assert result.needs_confirmation

    count = (await db.execute(sa.text("SELECT COUNT(*) FROM reminders"))).scalar_one()
    assert count == 0, "an unscheduled reminder must not be written"


async def test_reminder_result_states_where_reminders_live(ctx):
    """SEL-06. Selene must not let the user believe this reached iOS Reminders."""
    result = await call("reminder_create", ctx, text="remind me to stretch tomorrow at 7 am")
    assert "iPhone" in result.data["storage"]


async def test_reminder_list_reports_exact_counts(ctx, db):
    await call("reminder_create", ctx, text="remind me to call mum tomorrow at 6 pm")
    await call("reminder_create", ctx, text="remind me to book tickets in 3 days")

    result = await call("reminder_list", ctx, days=7)
    assert result.data["count"] == 2
    assert len(result.data["reminders"]) == 2


async def test_completing_a_one_off_closes_it(ctx, db):
    await call("reminder_create", ctx, text="remind me to collect the parcel tomorrow")
    result = await call("reminder_complete", ctx, match="parcel")
    assert result.ok

    state = (await db.execute(sa.text("SELECT state FROM reminders"))).scalar_one()
    assert state == "completed"


async def test_completing_a_recurring_reminder_rolls_it_forward(ctx, db):
    """Closing a monthly reminder would silently end the series."""
    await call("reminder_create", ctx, text="remind me to pay rent on the 1st")
    first_due = (await db.execute(sa.text("SELECT due_at FROM reminders"))).scalar_one()

    result = await call("reminder_complete", ctx, match="rent")
    assert result.ok

    row = (await db.execute(sa.text("SELECT state, due_at FROM reminders"))).one()
    assert row.state == "pending"
    assert row.due_at > first_due


async def test_ambiguous_completion_asks_which_one(ctx):
    await call("reminder_create", ctx, text="remind me to pay the water bill tomorrow at 9 am")
    await call("reminder_create", ctx, text="remind me to pay the gas bill in 2 days")

    result = await call("reminder_complete", ctx, match="bill")
    assert result.needs_confirmation
    assert len(result.data["candidates"]) == 2


async def test_completing_a_missing_reminder_fails_loudly(ctx):
    """A write that did not happen is never reported as success."""
    result = await call("reminder_complete", ctx, match="nonexistent")
    assert not result.ok


# ── Groceries and inventory ──────────────────────────────────────────────────

async def test_grocery_add_creates_the_item(ctx, db):
    result = await call("grocery_add", ctx, item="rice")
    assert result.ok
    assert result.data["first_time"] is True

    row = (
        await db.execute(
            sa.text("SELECT name, on_grocery_list FROM inventory_items")
        )
    ).one()
    assert (row.name, row.on_grocery_list) == ("rice", True)


async def test_grocery_add_reports_a_recent_purchase(ctx, db):
    """SEL-05. The prior purchase is data Selene reads, not something she recalls."""
    await call("grocery_add", ctx, item="rice")
    await call("grocery_clear", ctx, bought=True)

    await db.execute(
        sa.text(
            "UPDATE inventory_items SET last_purchased_at = now() - interval '11 days', "
            "typical_days_between = 30 WHERE name = 'rice'"
        )
    )

    result = await call("grocery_add", ctx, item="rice")
    assert result.data["days_since_purchase"] == 11
    assert result.data["sooner_than_usual"] is True
    assert "11 days ago" in result.message


async def test_a_normal_repurchase_is_not_flagged(ctx, db):
    await call("grocery_add", ctx, item="atta")
    await call("grocery_clear", ctx, bought=True)
    await db.execute(
        sa.text(
            "UPDATE inventory_items SET last_purchased_at = now() - interval '28 days', "
            "typical_days_between = 30 WHERE name = 'atta'"
        )
    )

    result = await call("grocery_add", ctx, item="atta")
    assert result.data["sooner_than_usual"] is False
    assert "days ago" not in result.message


async def test_clearing_the_list_records_the_purchase_and_learns_the_gap(ctx, db):
    await call("grocery_add", ctx, item="milk")
    await db.execute(
        sa.text(
            "UPDATE inventory_items SET last_purchased_at = now() - interval '10 days' "
            "WHERE name = 'milk'"
        )
    )

    result = await call("grocery_clear", ctx, bought=True)
    assert result.data["cleared"] == 1

    row = (
        await db.execute(
            sa.text(
                "SELECT on_grocery_list, typical_days_between, last_purchased_at "
                "FROM inventory_items WHERE name = 'milk'"
            )
        )
    ).one()
    assert row.on_grocery_list is False
    assert row.typical_days_between == 10


async def test_running_out_puts_it_back_on_the_list(ctx, db):
    await call("grocery_add", ctx, item="rice")
    await call("grocery_clear", ctx, bought=True)

    result = await call("inventory_consume", ctx, item="rice", text="we're out of rice")
    assert result.data["added_to_list"] is True
    assert result.data["remaining"] == "0"


async def test_partial_consumption_leaves_stock_and_stays_off_the_list(ctx, db):
    await db.execute(
        sa.text(
            "INSERT INTO inventory_items (name, quantity_dc, low_threshold_dc) "
            "VALUES ('sugar', 50, 20)"
        )
    )
    result = await call("inventory_consume", ctx, item="sugar", text="used some sugar")
    assert result.data["added_to_list"] is False
    assert result.data["remaining"] == "4"


async def test_inventory_status_reports_exact_stock(ctx, db):
    await db.execute(
        sa.text(
            "INSERT INTO inventory_items (name, quantity_dc, unit, low_threshold_dc) "
            "VALUES ('dal', 25, 'packet', 10)"
        )
    )
    result = await call("inventory_status", ctx, item="dal")
    assert result.data["quantity"] == "2.5"
    assert result.data["low"] is False


async def test_low_stock_sweep_lists_only_low_items(ctx, db):
    await db.execute(
        sa.text(
            "INSERT INTO inventory_items (name, quantity_dc, low_threshold_dc) VALUES "
            "('oil', 5, 20), ('salt', 90, 10)"
        )
    )
    result = await call("inventory_status", ctx)
    names = [i["name"] for i in result.data["items"]]
    assert names == ["oil"]


async def test_untracked_item_says_so_rather_than_inventing_a_number(ctx):
    result = await call("inventory_status", ctx, item="saffron")
    assert result.data["tracked"] is False
    assert "quantity" not in result.data


# ── Documents ────────────────────────────────────────────────────────────────

async def test_document_is_tracked_with_its_expiry(ctx, db):
    result = await call(
        "document_track", ctx, kind="passport", label="my passport",
        expires_on="2029-04-14", location_note="in the blue folder",
    )
    assert result.ok

    row = (
        await db.execute(sa.text("SELECT kind, label, expires_on FROM documents"))
    ).one()
    assert row.kind == "passport"
    assert row.expires_on.isoformat() == "2029-04-14"


async def test_identifier_numbers_are_stripped_before_storage(ctx, db):
    """SEL-07. The table has no identifier column; free text is the only leak
    path, and it is closed here rather than in the prompt."""
    result = await call(
        "document_track", ctx, kind="passport", label="passport",
        location_note="passport Z1234567 is in the safe",
    )
    assert result.data["identifier_stripped"] is True

    note = (await db.execute(sa.text("SELECT location_note FROM documents"))).scalar_one()
    assert "Z1234567" not in note
    assert "safe" in note


@pytest.mark.parametrize(
    "leak",
    [
        "aadhaar 1234 5678 9012",
        "PAN ABCDE1234F",
        "account 918273645510",
        "card 4111 1111 1111 1111",
    ],
)
async def test_common_indian_identifier_formats_never_reach_the_database(ctx, db, leak):
    await call("document_track", ctx, kind="misc", label="doc", location_note=leak)
    stored = (await db.execute(sa.text("SELECT location_note FROM documents"))).scalar_one()
    assert not any(chunk.isdigit() and len(chunk) >= 4 for chunk in stored.split())


async def test_document_expiry_reports_days_left(ctx, db):
    await db.execute(
        sa.text(
            "INSERT INTO documents (kind, label, expires_on) "
            "VALUES ('visa', 'schengen visa', (now() + interval '30 days')::date)"
        )
    )
    result = await call("document_expiry", ctx, days=90)
    assert result.data["count"] == 1
    assert result.data["documents"][0]["days_left"] == 30


async def test_distant_expiries_are_not_surfaced(ctx, db):
    await db.execute(
        sa.text(
            "INSERT INTO documents (kind, label, expires_on) "
            "VALUES ('passport', 'passport', (now() + interval '900 days')::date)"
        )
    )
    result = await call("document_expiry", ctx, days=120)
    assert result.data["count"] == 0


async def test_warranty_is_tracked(ctx, db):
    result = await call(
        "warranty_track", ctx, item="washing machine", expires_on="2027-03-01",
        purchased_on="2025-03-01",
    )
    assert result.ok
    row = (await db.execute(sa.text("SELECT item, expires_on FROM warranties"))).one()
    assert row.item == "washing machine"


# ── Boundaries ───────────────────────────────────────────────────────────────

async def test_selene_cannot_write_an_expense(ctx):
    """Enforced by the dispatcher, not by her prompt."""
    with pytest.raises(ToolNotAllowed):
        await dispatch(
            "expense_log", {"text": "spent 200"}, allowed=SELENE_TOOLS, context=ctx
        )


async def test_astraea_may_read_home_data_but_not_write_it(db, turn_id):
    from app.orchestration.agents.spec import ASTRAEA

    assert "reminder_list" in ASTRAEA.tools
    assert "reminder_create" not in ASTRAEA.tools
    assert "grocery_add" not in ASTRAEA.tools


async def test_every_selene_tool_is_registered():
    from app.tools.base import REGISTRY

    missing = [name for name in SELENE_TOOLS if REGISTRY.get(name) is None]
    assert not missing
