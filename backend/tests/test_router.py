"""Wake-name routing tests, driven by the fixture corpus.

Routing is the single most user-visible failure mode in the system. These tests
are the regression net; every mis-route seen in production gets added to
tests/fixtures/wake_names.yaml rather than fixed ad hoc.
"""

from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.orchestration.router import AGENTS, RouteMethod, normalise, resolve

FIXTURES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "wake_names.yaml").read_text(encoding="utf-8")
)

ROUTES = FIXTURES["routes"]
CONTINUATIONS = FIXTURES["session_continuations"]
SWITCHES = FIXTURES["session_switches"]


def _id(case: dict) -> str:
    return case["say"][:40]


# ── Corpus ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", ROUTES, ids=_id)
def test_corpus_routes_as_expected(case):
    result = resolve(case["say"])
    expected = case["expect"]

    if expected is None:
        # Must fall through, not be forced onto the nearest-sounding agent.
        assert result.method == RouteMethod.FALLTHROUGH, (
            f"{case['say']!r} wrongly routed to {result.agent} "
            f"via {result.method} at {result.confidence:.2f}"
        )
        assert result.agent == "astraea"
        return

    assert result.agent == expected, (
        f"{case['say']!r} routed to {result.agent}, expected {expected} "
        f"(method={result.method}, confidence={result.confidence:.2f})"
    )
    assert result.is_confident, f"{case['say']!r} matched but below the confidence floor"


@pytest.mark.parametrize("case", CONTINUATIONS, ids=_id)
def test_follow_ups_stay_with_the_active_agent(case):
    """'Make that 280' must not be hijacked away from Vega."""
    result = resolve(case["say"], active_agent=case["active"])
    assert result.agent == case["active"]
    assert result.method == RouteMethod.SESSION
    assert result.switched_from is None


@pytest.mark.parametrize("case", SWITCHES, ids=_id)
def test_explicit_name_switches_agent_mid_session(case):
    result = resolve(case["say"], active_agent=case["active"])
    assert result.agent == case["expect"]
    assert result.switched_from == case["active"]


# ── The two governing rules ──────────────────────────────────────────────────

def test_unnamed_utterance_falls_through_when_no_session_is_open():
    """Astraea is the default only in the absence of context."""
    result = resolve("how much did I spend on coffee")
    assert result.agent == "astraea"
    assert result.method == RouteMethod.FALLTHROUGH


def test_same_utterance_stays_with_active_agent_when_a_session_is_open():
    """The asymmetry that makes multi-turn work."""
    result = resolve("how much did I spend on coffee", active_agent="vega")
    assert result.agent == "vega"
    assert result.method == RouteMethod.SESSION


def test_ambiguous_input_asks_rather_than_guesses(monkeypatch):
    """Never pick between two close candidates — a misroute surfaces weeks later.

    The six names are phonetically well separated, so no real token currently
    produces a tie. The branch is exercised directly rather than left untested,
    because a seventh agent could easily collide with an existing name.
    """
    import app.orchestration.router as router

    monkeypatch.setattr(
        router,
        "_score_token",
        lambda token: [("vega", 0.86, RouteMethod.PHONETIC), ("selene", 0.84, RouteMethod.FUZZY)]
        if token == "vaselene"
        else [],
    )

    result = resolve("vaselene do the thing")
    assert result.needs_clarification
    assert set(result.candidates) == {"vega", "selene"}
    assert result.agent == "astraea", "clarification is Astraea's to ask"


def test_clear_winner_is_not_treated_as_ambiguous(monkeypatch):
    """The margin must not swallow a decisive match."""
    import app.orchestration.router as router

    monkeypatch.setattr(
        router,
        "_score_token",
        lambda token: [("vega", 1.0, RouteMethod.EXACT), ("selene", 0.83, RouteMethod.FUZZY)]
        if token == "vega"
        else [],
    )

    result = resolve("vega spent 200")
    assert not result.needs_clarification
    assert result.agent == "vega"


def test_no_natural_token_collides_across_agents():
    """Regression guard on name separation. If a future agent name breaks this,
    that is a reason to rename it rather than to widen the margin."""
    from app.orchestration.router import _score_token

    settings = get_settings()
    collisions = []
    for case in ROUTES:
        first = normalise(case["say"]).split()
        if not first:
            continue
        scored = sorted(_score_token(first[0]), key=lambda c: c[1], reverse=True)
        if (
            len(scored) >= 2
            and scored[0][1] >= settings.route_confidence_floor
            and scored[0][1] - scored[1][1] <= settings.route_ambiguity_margin
        ):
            collisions.append((first[0], scored[0][0], scored[1][0]))

    assert not collisions, f"wake names collide: {collisions}"


# ── Mechanics ────────────────────────────────────────────────────────────────

def test_wake_name_is_stripped_from_the_body():
    """The agent reads the request, not its own name."""
    assert resolve("Vega, I spent 180 on coffee").body == "i spent 180 on coffee"
    assert resolve("hey Lyra I had two eggs").body == "i had two eggs"
    assert resolve("no va continue the project").body == "continue the project"


def test_bare_name_yields_an_empty_body():
    result = resolve("Vega")
    assert result.agent == "vega"
    assert result.body == ""


def test_punctuation_and_case_do_not_matter():
    for phrasing in ("VEGA, spend 200", "vega: spend 200", "Vega -- spend 200"):
        assert resolve(phrasing).agent == "vega"


def test_empty_and_whitespace_input_falls_through():
    for text in ("", "   ", "\n"):
        assert resolve(text).method == RouteMethod.FALLTHROUGH


def test_filler_only_input_falls_through():
    assert resolve("um uh hey").method == RouteMethod.FALLTHROUGH


def test_every_agent_routes_from_its_own_bare_name():
    for agent in AGENTS:
        result = resolve(agent)
        assert result.agent == agent
        assert result.method == RouteMethod.EXACT
        assert result.confidence == 1.0


def test_normalise_collapses_punctuation_and_whitespace():
    assert normalise("  Vega,,,   spent   180!  ") == "vega spent 180"


def test_confidence_is_bounded():
    for case in ROUTES:
        confidence = resolve(case["say"]).confidence
        assert 0.0 <= confidence <= 1.0
