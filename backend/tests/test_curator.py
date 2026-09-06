"""The curator and cross-agent propagation.

This is the test for the claim that makes Astra a constellation rather than six
chatbots: telling Vega about groceries must reach Selene and Lyra without either
being invoked.
"""

import pytest

from app.memory.curator import Curator, get_redis
from app.memory.propagation import Event, hints_for
from app.memory.store import MemoryStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def redis():
    client = get_redis()
    yield client
    await client.aclose()


@pytest.fixture
def curator(db, redis):
    return Curator(db, redis)


def grocery_event(turn_id=None):
    return Event(
        type="expense.logged",
        agent="vega",
        payload={"category": "groceries", "amount_minor": 82_000},
        turn_id=turn_id,
    )


# ── Rules, in isolation ──────────────────────────────────────────────────────

async def test_grocery_spend_produces_hints_for_selene_and_lyra():
    """The example from the original brief, made literal."""
    targets = {h.target_agent for h in hints_for(grocery_event())}
    assert targets == {"selene", "lyra"}


async def test_unrelated_category_produces_no_hints():
    event = Event("expense.logged", "vega", {"category": "auto_taxi", "amount_minor": 25_000})
    assert hints_for(event) == []


async def test_large_spend_reaches_the_coordinator():
    event = Event("expense.logged", "vega", {"category": "shopping", "amount_minor": 900_000})
    assert any(h.target_agent == "astraea" for h in hints_for(event))


async def test_small_spend_does_not_reach_the_coordinator():
    """Astraea prioritises; she does not need every coffee."""
    event = Event("expense.logged", "vega", {"category": "dining_out", "amount_minor": 18_000})
    assert not any(h.target_agent == "astraea" for h in hints_for(event))


async def test_malformed_payload_does_not_raise():
    """A bad event must not stop the curator."""
    assert hints_for(Event("expense.logged", "vega", {})) == []


async def test_unknown_event_type_is_ignored():
    assert hints_for(Event("nonsense.happened", "vega", {"category": "groceries"})) == []


# ── End to end through the database ──────────────────────────────────────────

async def test_grocery_expense_reaches_selene_and_lyra(curator, db, turn_id):
    """Neither agent was invoked; both know at their next turn."""
    await curator.process(grocery_event(turn_id))

    selene = await MemoryStore(db, "selene").pending_hints()
    lyra = await MemoryStore(db, "lyra").pending_hints()
    nova = await MemoryStore(db, "nova").pending_hints()

    assert [h.kind for h in selene] == ["inventory_likely_replenished"]
    assert [h.kind for h in lyra] == ["groceries_purchased"]
    assert nova == [], "hints must not broadcast to uninterested agents"


async def test_curation_writes_an_audit_row(curator, db, turn_id):
    import sqlalchemy as sa

    await curator.process(grocery_event(turn_id), stream_id="1-1")
    row = (await db.execute(
        sa.text("SELECT type, agent, processed_at FROM memory_events WHERE stream_id = '1-1'")
    )).one()
    assert row.type == "expense.logged"
    assert row.agent == "vega"
    assert row.processed_at is not None


async def test_rent_expense_derives_a_global_fact(curator, db, turn_id):
    """A rent payment tells the whole system something, not just Vega."""
    await curator.process(
        Event("expense.logged", "vega", {"category": "rent", "amount_minor": 1_800_000}, turn_id)
    )
    facts = await MemoryStore(db, "astraea").current_facts(
        namespace="global", predicates=["monthly_rent"]
    )
    assert len(facts) == 1
    assert facts[0].value["minor"] == 1_800_000
    # Derived facts are less certain than stated ones and must say so.
    assert facts[0].confidence < 1.0


async def test_second_rent_payment_supersedes_rather_than_conflicts(curator, db, turn_id):
    for amount in (1_650_000, 1_800_000):
        await curator.process(
            Event("expense.logged", "vega", {"category": "rent", "amount_minor": amount}, turn_id)
        )
    facts = await MemoryStore(db, "astraea").current_facts(
        namespace="global", predicates=["monthly_rent"]
    )
    assert len(facts) == 1, "single-cardinality predicate must not accumulate"
    assert facts[0].value["minor"] == 1_800_000


async def test_conflicting_facts_are_queued_not_raised(curator, db, turn_id):
    """Conflicts belong in the daily briefing, never mid-conversation."""
    import sqlalchemy as sa

    store = MemoryStore(db, "vega")
    for minor, label in ((1_650_000, "rent 16500"), (1_800_000, "rent 18000")):
        await db.execute(
            sa.text(
                "INSERT INTO memory_facts (namespace, entity, predicate, value, "
                "value_text, asserted_by, valid_from) VALUES "
                "('finance','user','monthly_rent', CAST(:v AS jsonb), :t, 'vega', now())"
            ),
            {"v": f'{{"minor": {minor}}}', "t": label},
        )
    assert await store.detect_conflicts()

    await curator.process(grocery_event(turn_id))

    conflicts = (await db.execute(
        sa.text("SELECT predicate, state FROM memory_conflicts WHERE state = 'open'")
    )).all()
    assert any(c.predicate == "monthly_rent" for c in conflicts)


async def test_conflict_is_not_queued_twice(curator, db, turn_id):
    import sqlalchemy as sa

    for minor in (1_650_000, 1_800_000):
        await db.execute(
            sa.text(
                "INSERT INTO memory_facts (namespace, entity, predicate, value, "
                "value_text, asserted_by, valid_from) VALUES "
                "('finance','user','monthly_rent', CAST(:v AS jsonb), 'rent', 'vega', now())"
            ),
            {"v": f'{{"minor": {minor}}}'},
        )

    await curator.process(grocery_event(turn_id))
    await curator.process(grocery_event(turn_id))

    count = (await db.execute(
        sa.text(
            "SELECT COUNT(*) FROM memory_conflicts "
            "WHERE predicate = 'monthly_rent' AND state = 'open'"
        )
    )).scalar_one()
    assert count == 1, "a standing conflict must not be re-queued on every event"


async def test_hints_carry_an_expiry(curator, db, turn_id):
    import sqlalchemy as sa

    await curator.process(grocery_event(turn_id))
    expires = (await db.execute(
        sa.text("SELECT expires_at FROM memory_hints WHERE target_agent = 'selene'")
    )).scalar_one()
    assert expires is not None
