"""Integration tests against a real Postgres.

These assert that the design commitments in docs/ are enforced by the database
rather than merely described in it. A constraint that exists only in a document
is a suggestion.

Requires the stack to be up:  docker compose exec api pytest tests/test_schema_integration.py
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

# db and turn_id fixtures live in conftest.py


# ── Reference data ───────────────────────────────────────────────────────────

async def test_all_six_agents_seeded(db: AsyncSession):
    names = (await db.execute(sa.text("SELECT name FROM agents ORDER BY name"))).scalars().all()
    assert names == ["astraea", "athena", "lyra", "nova", "selene", "vega"]


async def test_each_agent_owns_a_distinct_namespace(db: AsyncSession):
    rows = (await db.execute(sa.text("SELECT namespace FROM agents"))).scalars().all()
    assert len(set(rows)) == 6, "namespace isolation requires one namespace per agent"


async def test_predicate_cardinality_is_declared(db: AsyncSession):
    """Conflict detection is mechanical only because cardinality is declared."""
    single = (await db.execute(sa.text(
        "SELECT cardinality FROM fact_predicates WHERE slug = 'monthly_rent'"
    ))).scalar_one()
    multi = (await db.execute(sa.text(
        "SELECT cardinality FROM fact_predicates WHERE slug = 'dietary_restriction'"
    ))).scalar_one()
    assert single == "single"
    assert multi == "multi"


# ── Money invariants (PRD FR-T1) ─────────────────────────────────────────────

async def test_negative_expense_is_rejected(db: AsyncSession, turn_id: int):
    """A refund is its own row with its own semantics, not a sign flip."""
    with pytest.raises(IntegrityError):
        await db.execute(
            sa.text(
                "INSERT INTO expenses (amount_minor, category, occurred_at, source_turn) "
                "VALUES (-100, 'coffee_placeholder', now(), :t)"
            ).bindparams(t=turn_id)
        )


async def test_amount_survives_as_exact_integer(db: AsyncSession, turn_id: int):
    """No float anywhere on this path. 180.55 rupees is 18055 paise, exactly."""
    await db.execute(
        sa.text(
            "INSERT INTO expenses (amount_minor, category, occurred_at, source_turn) "
            "VALUES (18055, 'dining_out', now(), :t)"
        ),
        {"t": turn_id},
    )
    got = (await db.execute(sa.text(
        "SELECT amount_minor FROM expenses WHERE source_turn = :t"
    ), {"t": turn_id})).scalar_one()
    assert got == 18055
    assert isinstance(got, int)


async def test_unknown_category_is_rejected(db: AsyncSession, turn_id: int):
    with pytest.raises(IntegrityError):
        await db.execute(
            sa.text(
                "INSERT INTO expenses (amount_minor, category, occurred_at) "
                "VALUES (100, 'not_a_real_category', now())"
            )
        )


async def test_correction_supersedes_rather_than_mutates(db: AsyncSession, turn_id: int):
    """'Make that 280' must leave the original row intact and auditable."""
    original = (await db.execute(sa.text(
        "INSERT INTO expenses (amount_minor, category, occurred_at, source_turn) "
        "VALUES (25000, 'auto_taxi', now(), :t) RETURNING id"
    ), {"t": turn_id})).scalar_one()

    corrected = (await db.execute(sa.text(
        "INSERT INTO expenses (amount_minor, category, occurred_at, source_turn) "
        "VALUES (28000, 'auto_taxi', now(), :t) RETURNING id"
    ), {"t": turn_id})).scalar_one()

    await db.execute(sa.text(
        "UPDATE expenses SET superseded_by = :new WHERE id = :old"
    ), {"new": corrected, "old": original})

    # The original still exists...
    assert (await db.execute(sa.text(
        "SELECT amount_minor FROM expenses WHERE id = :i"
    ), {"i": original})).scalar_one() == 25000

    # ...but the live total counts it once, at the corrected value.
    live_total = (await db.execute(sa.text(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM expenses "
        "WHERE superseded_by IS NULL AND source_turn = :t"
    ), {"t": turn_id})).scalar_one()
    assert live_total == 28000


# ── Bitemporal facts (memory-design.md §4) ───────────────────────────────────

async def test_fact_supersession_preserves_history(db: AsyncSession, turn_id: int):
    """Rent rising must not erase what rent used to be."""
    now = datetime.now(UTC)
    old_from = now - timedelta(days=200)

    # JSON is bound, never inlined: sa.text() reads ':1650000' inside a literal
    # as a bind parameter and the statement fails to compile.
    insert_fact = sa.text(
        "INSERT INTO memory_facts "
        "(namespace, entity, predicate, value, value_text, asserted_by, valid_from, source_turn) "
        "VALUES ('finance','user','monthly_rent', CAST(:v AS jsonb), :vt, 'vega', :vf, :t) "
        "RETURNING id"
    )

    old_id = (await db.execute(insert_fact, {
        "v": '{"minor": 1650000}', "vt": "rent 16500", "vf": old_from, "t": turn_id,
    })).scalar_one()

    new_id = (await db.execute(insert_fact, {
        "v": '{"minor": 1800000}', "vt": "rent 18000", "vf": now, "t": turn_id,
    })).scalar_one()

    await db.execute(sa.text(
        "UPDATE memory_facts SET valid_to = :vt, superseded_by = :n WHERE id = :o"
    ), {"vt": now, "n": new_id, "o": old_id})

    # Exactly one currently-true rent.
    current = (await db.execute(sa.text(
        "SELECT value_text FROM memory_facts WHERE namespace='finance' "
        "AND predicate='monthly_rent' AND valid_to IS NULL AND retracted_at IS NULL"
    ))).scalars().all()
    assert current == ["rent 18000"]

    # The old value is still queryable as of a past date.
    historical = (await db.execute(sa.text(
        "SELECT value_text FROM memory_facts WHERE predicate='monthly_rent' "
        "AND valid_from <= :d AND (valid_to IS NULL OR valid_to > :d)"
    ), {"d": old_from + timedelta(days=1)})).scalars().all()
    assert historical == ["rent 16500"]


async def test_inverted_validity_window_is_rejected(db: AsyncSession, turn_id: int):
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        await db.execute(sa.text(
            "INSERT INTO memory_facts "
            "(namespace, entity, predicate, value, value_text, asserted_by, "
            " valid_from, valid_to) "
            "VALUES ('finance','user','monthly_rent','{}','x','vega', :vf, :vt)"
        ), {"vf": now, "vt": now - timedelta(days=5)})


async def test_conflict_detection_finds_two_open_single_valued_facts(db: AsyncSession):
    """The query the curator runs. Two open 'single' facts is a contradiction."""
    now = datetime.now(UTC)
    for text_val, minor in (("rent 16500", 1650000), ("rent 18000", 1800000)):
        await db.execute(sa.text(
            "INSERT INTO memory_facts "
            "(namespace, entity, predicate, value, value_text, asserted_by, valid_from) "
            "VALUES ('finance','user','monthly_rent', :v, :vt, 'vega', :vf)"
        ), {"v": f'{{"minor":{minor}}}', "vt": text_val, "vf": now})

    conflicts = (await db.execute(sa.text(
        "SELECT f.predicate, COUNT(*) AS n FROM memory_facts f "
        "JOIN fact_predicates p ON p.slug = f.predicate "
        "WHERE p.cardinality = 'single' "
        "  AND f.valid_to IS NULL AND f.retracted_at IS NULL "
        "GROUP BY f.entity, f.predicate HAVING COUNT(*) > 1"
    ))).all()
    assert ("monthly_rent", 2) in [(r.predicate, r.n) for r in conflicts]


# ── Vector search ────────────────────────────────────────────────────────────

async def test_hnsw_cosine_search_returns_nearest(db: AsyncSession):
    """pgvector is wired up and the HNSW index is usable."""
    vectors = {
        "groceries at the market": [1.0] + [0.0] * 767,
        "leg day at the gym": [0.0, 1.0] + [0.0] * 766,
    }
    for content, vec in vectors.items():
        await db.execute(sa.text(
            "INSERT INTO memory_chunks "
            "(namespace, source_kind, source_id, content, embedding, embed_model, occurred_at) "
            "VALUES ('finance','turn','t1', :c, CAST(:e AS vector), 'test', now())"
        ), {"c": content, "e": str(vec)})

    query = str([0.99, 0.01] + [0.0] * 766)
    nearest = (await db.execute(sa.text(
        "SELECT content FROM memory_chunks "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 1"
    ), {"q": query})).scalar_one()
    assert nearest == "groceries at the market"


async def test_hnsw_index_exists_on_both_embedding_columns(db: AsyncSession):
    rows = (await db.execute(sa.text(
        "SELECT indexname FROM pg_indexes WHERE indexdef LIKE '%hnsw%' ORDER BY indexname"
    ))).scalars().all()
    assert rows == ["ix_chunks_embedding_hnsw", "ix_facts_embedding_hnsw"]


# ── Namespace integrity ──────────────────────────────────────────────────────

async def test_invalid_namespace_is_rejected(db: AsyncSession):
    """Namespaces are a closed set; a typo must not silently create a new one."""
    with pytest.raises(IntegrityError):
        await db.execute(sa.text(
            "INSERT INTO memory_facts "
            "(namespace, entity, predicate, value, value_text, asserted_by, valid_from) "
            "VALUES ('finanace','user','monthly_rent','{}','x','vega', now())"
        ))
