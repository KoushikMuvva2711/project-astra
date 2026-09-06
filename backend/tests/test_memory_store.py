"""MemoryStore against real Postgres: scoped access, bitemporal facts, hints.

Runs in the container:  docker compose exec api pytest tests/test_memory_store.py
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.memory.namespaces import NamespaceAccessError
from app.memory.store import MemoryStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def vega(db):
    return MemoryStore(db, "vega")


@pytest.fixture
def lyra(db):
    return MemoryStore(db, "lyra")


@pytest.fixture
def astraea(db):
    return MemoryStore(db, "astraea")


# ── Isolation, enforced against the database ─────────────────────────────────

async def test_lyra_cannot_query_finance_facts(lyra, vega, turn_id):
    """Not 'returns nothing' — refuses. A silent empty result hides a wiring bug."""
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    with pytest.raises(NamespaceAccessError):
        await lyra.current_facts(namespace="finance")


async def test_lyras_unscoped_read_excludes_finance(lyra, vega, turn_id):
    """The subtler case: an unscoped read must not leak a peer's rows."""
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    facts = await lyra.current_facts()
    assert all(f.namespace != "finance" for f in facts)


async def test_astraea_sees_across_namespaces(astraea, vega, lyra, turn_id):
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    await lyra.record_fact(
        namespace="health", predicate="daily_protein_target",
        value={"grams": 150}, value_text="protein target 150g", source_turn=turn_id,
    )
    namespaces = {f.namespace for f in await astraea.current_facts()}
    assert {"finance", "health"} <= namespaces


async def test_astraea_cannot_write_a_specialist_namespace(astraea):
    with pytest.raises(NamespaceAccessError):
        await astraea.record_fact(
            namespace="finance", predicate="monthly_rent",
            value={"minor": 1}, value_text="x",
        )


async def test_vega_cannot_write_global(vega):
    with pytest.raises(NamespaceAccessError):
        await vega.record_fact(
            namespace="global", predicate="city", value={"v": "Bangalore"},
            value_text="lives in Bangalore",
        )


# ── Bitemporal behaviour ─────────────────────────────────────────────────────

async def test_single_cardinality_fact_supersedes_predecessor(vega, turn_id):
    old = await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1650000}, value_text="rent 16500", source_turn=turn_id,
        valid_from=datetime.now(UTC) - timedelta(days=200),
    )
    new = await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    current = await vega.current_facts(namespace="finance", predicates=["monthly_rent"])
    assert [f.id for f in current] == [new]
    assert old != new


async def test_superseded_fact_is_preserved_not_deleted(vega, db, turn_id):
    old = await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1650000}, value_text="rent 16500", source_turn=turn_id,
        valid_from=datetime.now(UTC) - timedelta(days=200),
    )
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    row = (await db.execute(
        sa.text("SELECT value_text, valid_to, superseded_by FROM memory_facts WHERE id = :i"),
        {"i": old},
    )).one()
    assert row.value_text == "rent 16500"
    assert row.valid_to is not None
    assert row.superseded_by is not None


async def test_past_value_remains_queryable(vega, turn_id):
    """'What was my rent in March' must still answer after it changed."""
    long_ago = datetime.now(UTC) - timedelta(days=200)
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1650000}, value_text="rent 16500", source_turn=turn_id,
        valid_from=long_ago,
    )
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    historical = await vega.facts_as_of(
        long_ago + timedelta(days=1), namespace="finance", predicate="monthly_rent"
    )
    assert [f.value_text for f in historical] == ["rent 16500"]


async def test_multi_cardinality_facts_accumulate(lyra, turn_id):
    """Several dietary restrictions is normal, not a contradiction."""
    for restriction in ("vegetarian", "lactose intolerant"):
        await lyra.record_fact(
            namespace="health", predicate="dietary_restriction",
            value={"v": restriction}, value_text=restriction, source_turn=turn_id,
        )
    facts = await lyra.current_facts(
        namespace="health", predicates=["dietary_restriction"]
    )
    assert len(facts) == 2


# ── Conflict detection ───────────────────────────────────────────────────────

async def test_conflict_surfaces_only_for_single_cardinality(astraea, db, turn_id):
    """Two open rents is a contradiction; two dietary restrictions is not."""
    now = datetime.now(UTC)
    for minor, label in ((1650000, "rent 16500"), (1800000, "rent 18000")):
        await db.execute(
            sa.text(
                "INSERT INTO memory_facts (namespace, entity, predicate, value, "
                "value_text, asserted_by, valid_from) VALUES "
                "('finance','user','monthly_rent', CAST(:v AS jsonb), :t, 'vega', :vf)"
            ),
            {"v": f'{{"minor": {minor}}}', "t": label, "vf": now},
        )
    for label in ("vegetarian", "no peanuts"):
        await db.execute(
            sa.text(
                "INSERT INTO memory_facts (namespace, entity, predicate, value, "
                "value_text, asserted_by, valid_from) VALUES "
                "('health','user','dietary_restriction', '{}'::jsonb, :t, 'lyra', :vf)"
            ),
            {"t": label, "vf": now},
        )

    conflicts = await astraea.detect_conflicts()
    predicates = {c["predicate"] for c in conflicts}
    assert "monthly_rent" in predicates
    assert "dietary_restriction" not in predicates


# ── Hints ────────────────────────────────────────────────────────────────────

async def test_hint_reaches_its_target_and_not_others(db, turn_id):
    """Vega logging groceries must reach Selene without Selene being invoked."""
    await db.execute(
        sa.text(
            "INSERT INTO memory_hints (target_agent, kind, content, expires_at, source_turn) "
            "VALUES ('selene','inventory_likely_replenished','groceries bought today', "
            "        now() + interval '3 days', :t)"
        ),
        {"t": turn_id},
    )
    selene_hints = await MemoryStore(db, "selene").pending_hints()
    nova_hints = await MemoryStore(db, "nova").pending_hints()

    assert [h.kind for h in selene_hints] == ["inventory_likely_replenished"]
    assert nova_hints == []


async def test_expired_hints_are_not_returned(db, turn_id):
    """A three-day-old grocery hint is noise, not context."""
    await db.execute(
        sa.text(
            "INSERT INTO memory_hints (target_agent, kind, content, expires_at) "
            "VALUES ('selene','stale','old news', now() - interval '1 hour')"
        )
    )
    assert await MemoryStore(db, "selene").pending_hints() == []


async def test_consumed_hints_are_not_returned_again(db):
    await db.execute(
        sa.text(
            "INSERT INTO memory_hints (target_agent, kind, content, expires_at) "
            "VALUES ('lyra','groceries_purchased','food available', now() + interval '3 days')"
        )
    )
    store = MemoryStore(db, "lyra")
    hints = await store.pending_hints()
    assert len(hints) == 1

    await store.consume_hints([h.id for h in hints])
    assert await store.pending_hints() == []


# ── Recall assembly ──────────────────────────────────────────────────────────

async def test_recall_degrades_without_embeddings_rather_than_failing(vega, turn_id):
    """No embedding model available must not break the turn."""
    await vega.record_fact(
        namespace="finance", predicate="monthly_rent",
        value={"minor": 1800000}, value_text="rent 18000", source_turn=turn_id,
    )
    bundle = await vega.recall()
    assert bundle.agent == "vega"
    assert any(f.value_text == "rent 18000" for f in bundle.agent_facts)
    assert "semantic_unavailable" in bundle.degraded


async def test_recall_stays_within_the_token_budget(vega, turn_id):
    """Overflow drops whole components; a truncated fact reads as a complete one."""
    for i in range(200):
        await vega.record_fact(
            namespace="finance", predicate="financial_goal",
            value={"i": i}, value_text=f"goal {i} " + "padding " * 40,
            source_turn=turn_id,
        )
    bundle = await vega.recall()
    from app.memory.store import TOTAL_BUDGET

    assert bundle.estimated_tokens() <= TOTAL_BUDGET
