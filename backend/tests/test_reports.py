"""Reports: figures come from SQL, prose comes from the model, never the reverse."""

import pytest
import sqlalchemy as sa

from app.reports import builders
from app.reports.narrate import _is_usable, render_plain

pytestmark = pytest.mark.asyncio


async def _expense(db, minor, category, days_ago=0):
    await db.execute(
        sa.text(
            "INSERT INTO expenses (amount_minor, category, occurred_at) "
            "VALUES (:m, :c, now() - make_interval(days => :d))"
        ),
        {"m": minor, "c": category, "d": days_ago},
    )


# ── Empty state ──────────────────────────────────────────────────────────────

async def test_sections_are_absent_rather_than_empty(db):
    """A section with nothing to say returns None so it never reaches the user."""
    assert await builders.spending(db) is None
    assert await builders.nutrition(db) is None
    assert await builders.due_reminders(db) is None
    assert await builders.open_conflicts(db) is None


async def test_empty_briefing_says_nothing_matters(db):
    """Astraea's spec: say nothing matters rather than manufacture content."""
    report = await builders.daily_briefing(db)
    assert report.sections == []
    assert render_plain(report) == "Nothing needing you today."


# ── Spending ─────────────────────────────────────────────────────────────────

async def test_spending_totals_and_categories(db):
    await _expense(db, 25_000, "auto_taxi", 1)
    await _expense(db, 82_000, "groceries", 2)
    await _expense(db, 18_000, "dining_out", 3)

    section = await builders.spending(db, days=30)
    assert section.data["total_minor"] == 125_000
    assert section.data["entry_count"] == 3
    assert section.data["by_category"][0]["category"] == "groceries"
    assert section.data["by_category"][0]["share_pct"] == 65.6


async def test_spending_excludes_superseded_rows(db):
    await _expense(db, 25_000, "auto_taxi", 1)
    original = (await db.execute(
        sa.text("SELECT id FROM expenses ORDER BY id DESC LIMIT 1")
    )).scalar_one()
    await _expense(db, 28_000, "auto_taxi", 1)
    new_id = (await db.execute(
        sa.text("SELECT id FROM expenses ORDER BY id DESC LIMIT 1")
    )).scalar_one()
    await db.execute(
        sa.text("UPDATE expenses SET superseded_by = :n WHERE id = :o"),
        {"n": new_id, "o": original},
    )

    section = await builders.spending(db, days=30)
    assert section.data["total_minor"] == 28_000


async def test_spending_compares_against_the_previous_window(db):
    await _expense(db, 100_000, "groceries", 2)   # current 7-day window
    await _expense(db, 50_000, "groceries", 10)   # prior window

    section = await builders.spending(db, days=7)
    assert section.data["total_minor"] == 100_000
    assert section.data["previous_minor"] == 50_000
    assert section.data["direction"] == "up"
    assert section.data["change_pct"] == 100.0


async def test_budget_flags_at_ninety_percent_not_only_at_breach(db):
    """Warning before the breach is the useful moment."""
    await db.execute(
        sa.text(
            "INSERT INTO budgets (category, period, limit_minor, effective_from) "
            "VALUES ('groceries','monthly', 100000, current_date)"
        )
    )
    await _expense(db, 95_000, "groceries", 0)

    section = await builders.budget_position(db)
    assert section is not None
    assert section.data["categories"][0]["used_pct"] == 95.0


async def test_budget_silent_when_comfortably_under(db):
    await db.execute(
        sa.text(
            "INSERT INTO budgets (category, period, limit_minor, effective_from) "
            "VALUES ('groceries','monthly', 100000, current_date)"
        )
    )
    await _expense(db, 20_000, "groceries", 0)
    assert await builders.budget_position(db) is None


# ── Health ───────────────────────────────────────────────────────────────────

async def test_nutrition_averages_and_target(db, turn_id):
    from app.tools.base import ToolContext
    from app.tools.health import meal_log

    ctx = ToolContext(session=db, agent="lyra", turn_id=turn_id)
    await meal_log(context=ctx, text="two eggs")

    await db.execute(
        sa.text(
            "INSERT INTO memory_facts (namespace, entity, predicate, value, "
            "value_text, asserted_by, valid_from) VALUES "
            "('health','user','daily_protein_target', CAST(:v AS jsonb), "
            "'target 150g','lyra', now())"
        ),
        {"v": '{"grams": 150}'},
    )

    section = await builders.nutrition(db, days=7)
    assert section.data["avg_kcal"] == 156
    assert section.data["protein_target_g"] == 150
    assert section.data["days_on_target"] == 0


async def test_training_raises_priority_after_a_gap(db):
    """A four-day gap is worth surfacing early; a recent session is not."""
    await db.execute(
        sa.text(
            "INSERT INTO workouts (kind, occurred_at) "
            "VALUES ('legs', now() - interval '5 days')"
        )
    )
    stale = await builders.training(db, days=14)
    assert stale.data["days_since_last"] >= 4
    assert stale.priority == 3

    await db.execute(sa.text("INSERT INTO workouts (kind, occurred_at) VALUES ('push', now())"))
    fresh = await builders.training(db, days=14)
    assert fresh.priority == 5


# ── Learning ─────────────────────────────────────────────────────────────────

async def test_study_surfaces_repeated_blockers(db):
    """A blocker seen twice means the plan is wrong, not the person."""
    for _ in range(2):
        await db.execute(
            sa.text(
                "INSERT INTO study_sessions (minutes, blocker, occurred_at) "
                "VALUES (45, 'evening meetings ran over', now())"
            )
        )
    await db.execute(
        sa.text(
            "INSERT INTO study_sessions (minutes, blocker, occurred_at) "
            "VALUES (30, 'travelling', now())"
        )
    )

    section = await builders.study(db, days=7)
    recurring = section.data["recurring_blockers"]
    assert len(recurring) == 1
    assert recurring[0]["reason"] == "evening meetings ran over"
    assert recurring[0]["times"] == 2


# ── Cross-domain, the coordinator's reason to exist ──────────────────────────

async def test_grocery_drop_with_eating_out_rise_is_detected(db):
    """PRD success criterion 3: a pattern no specialist agent can see."""
    await _expense(db, 100_000, "groceries", 20)
    await _expense(db, 20_000, "food_delivery", 20)
    await _expense(db, 20_000, "groceries", 3)
    await _expense(db, 90_000, "food_delivery", 3)

    section = await builders.correlations(db, days=14)
    assert section is not None
    kinds = [p["kind"] for p in section.data["patterns"]]
    assert "groceries_down_eating_out_up" in kinds


async def test_no_correlation_reported_without_evidence(db):
    """Steady spending must not produce an invented pattern."""
    for days_ago in (3, 10, 17):
        await _expense(db, 50_000, "groceries", days_ago)
    assert await builders.correlations(db, days=14) is None


# ── Ranking ──────────────────────────────────────────────────────────────────

async def test_briefing_ranks_decisions_above_summaries(db):
    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state) "
            "VALUES ('pay rent', now() - interval '1 hour', 'pending')"
        )
    )
    await _expense(db, 25_000, "auto_taxi", 1)

    report = await builders.daily_briefing(db)
    assert report.top(1)[0].key == "reminders"


async def test_briefing_is_capped_at_three_items(db):
    """The cap is enforced by the data layer, not requested in a prompt."""
    await db.execute(
        sa.text(
            "INSERT INTO reminders (text, due_at, state) "
            "VALUES ('a', now() - interval '1 hour', 'pending')"
        )
    )
    await db.execute(
        sa.text(
            "INSERT INTO bills (name, amount_minor, next_due, active) "
            "VALUES ('rent', 1800000, current_date + 1, TRUE)"
        )
    )
    await db.execute(
        sa.text(
            "INSERT INTO documents (kind, label, expires_on) "
            "VALUES ('passport','Passport', current_date + 10)"
        )
    )
    await db.execute(
        sa.text("INSERT INTO projects (slug, name, status) VALUES ('astra','Astra','active')")
    )

    report = await builders.daily_briefing(db)
    assert len(report.sections) > 3
    assert len(report.top(3)) == 3


async def test_documents_section_carries_no_identifiers(db):
    """Selene tracks that a passport expires; she has nowhere to put its number."""
    await db.execute(
        sa.text(
            "INSERT INTO documents (kind, label, expires_on) "
            "VALUES ('passport','Passport', current_date + 20)"
        )
    )
    section = await builders.expiring_documents(db, days=60)
    assert set(section.data["documents"][0]) == {"kind", "label", "expires"}


# ── Narration guard ──────────────────────────────────────────────────────────

async def test_narration_with_an_invented_figure_is_rejected(db):
    section = builders.Section("spending", "1,250 rupees across 3 entries.",
                               {"total_minor": 125_000})
    assert not _is_usable("You spent 9,999 rupees this week.", [section])


async def test_narration_quoting_the_real_figure_is_accepted(db):
    section = builders.Section("spending", "1,250 rupees across 3 entries.",
                               {"total_minor": 125_000})
    assert _is_usable("Spending came to 1,250 rupees across 3 entries.", [section])


async def test_leaked_json_is_rejected(db):
    section = builders.Section("spending", "1,250 rupees.", {"total_minor": 125_000})
    assert not _is_usable('{"name": "report", "arguments": {}}', [section])


async def test_empty_narration_is_rejected(db):
    section = builders.Section("spending", "1,250 rupees.", {})
    assert not _is_usable("  ", [section])


async def test_plain_rendering_needs_no_model(db):
    await _expense(db, 25_000, "auto_taxi", 1)
    report = await builders.spending(db, days=30)
    assert "250 rupees" in report.headline


# ── Domains ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain", list(builders.DOMAINS))
async def test_every_domain_report_builds(db, domain):
    report = await builders.domain_report(db, domain, days=30)
    assert report.kind == f"{domain}_report"


async def test_unknown_domain_is_rejected(db):
    with pytest.raises(ValueError, match="unknown domain"):
        await builders.domain_report(db, "astrology")
