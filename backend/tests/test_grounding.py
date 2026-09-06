"""Agents must not describe activity they cannot see.

Written after Nova, asked what she was working on with zero projects on file,
invented an authentication module and a commit history to match. Nothing caught
it: no write was claimed and no figure was quoted, so neither output guard
applied. The fix is evidence rather than instruction — real record counts in
context, so there is no vacuum to fill.
"""

import pytest
import sqlalchemy as sa

from app.orchestration.graph import AGENT_TABLES, _inventory

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("agent", ["vega", "lyra", "nova", "athena", "selene"])
async def test_empty_domain_states_it_plainly(db, agent):
    line = await _inventory(db, agent)
    assert line is not None
    assert "no data on record" in line
    assert "do not describe activity you cannot see" in line


async def test_populated_domain_reports_real_counts(db, turn_id):
    await db.execute(
        sa.text(
            "INSERT INTO projects (slug, name, status) VALUES ('astra','Astra','active')"
        )
    )
    line = await _inventory(db, "nova")
    assert "projects: 1" in line
    assert "no data on record" not in line


async def test_counts_are_real_not_placeholders(db, turn_id):
    for i in range(3):
        await db.execute(
            sa.text(
                "INSERT INTO expenses (amount_minor, category, occurred_at) "
                "VALUES (:m, 'groceries', now())"
            ),
            {"m": 1000 * (i + 1)},
        )
    line = await _inventory(db, "vega")
    assert "expenses: 3" in line


async def test_astraea_has_no_inventory_line(db):
    """She reads every namespace; a per-agent count would be misleading."""
    assert await _inventory(db, "astraea") is None


async def test_unknown_agent_yields_nothing(db):
    assert await _inventory(db, "hermes") is None


async def test_every_specialist_has_tables_declared():
    """A specialist missing from the map gets no grounding and can fabricate."""
    for agent in ("vega", "lyra", "nova", "athena", "selene"):
        assert AGENT_TABLES.get(agent), f"{agent} has no tables declared"


async def test_inventory_survives_a_missing_table(db, monkeypatch):
    """Grounding is best-effort: a bad table name must not break the turn."""
    monkeypatch.setitem(AGENT_TABLES, "nova", ("projects", "table_that_does_not_exist"))
    line = await _inventory(db, "nova")
    assert line is not None
    assert "projects: 0" in line
