"""Wake-name routing: which agent does this utterance belong to?

The hardest correctness problem in the system, because it fails in front of the
user and it fails on speech, which is noisy by nature. `Selene` and `celine` are
the same word spoken and share no useful prefix, so string equality resolves
almost nothing.

Two rules govern everything here:

  1. Never guess between two agents. A misrouted turn writes data into the wrong
     namespace and is discovered weeks later. Asking costs one turn.
  2. With a session open, an unnamed utterance belongs to the *active* agent, not
     to Astraea. "Make that 280" must not be hijacked away from Vega.

See docs/conversation-architecture.md §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from metaphone import doublemetaphone
from rapidfuzz.distance import DamerauLevenshtein

from app.config import get_settings

AGENTS: tuple[str, ...] = ("astraea", "lyra", "vega", "nova", "athena", "selene")

# Leading words people say before a name, especially after months of Siri habit.
# Stripped before routing so "hey Vega" resolves like "Vega".
FILLERS: frozenset[str] = frozenset(
    {"hey", "hi", "hello", "ok", "okay", "um", "uh", "yo", "so", "and", "please"}
)

# Hand-curated mis-transcriptions the algorithm does not catch on its own.
# "extra" is the motivating case: Double Metaphone renders it AKSTR against
# astraea's ASTR, and edit distance is far too low to match, yet it is a common
# rendering of "Astraea". The general algorithm handles the general case; this
# table absorbs the specific observed failures, and grows from production logs.
ALIASES: dict[str, str] = {
    "extra": "astraea",
    "astro": "astraea",
    "astra": "astraea",
    "estra": "astraea",
    "lira": "lyra",
    "leera": "lyra",
    "lyre": "lyra",
    "vegas": "vega",
    "bega": "vega",
    "veiga": "vega",
    "nofa": "nova",
    "noba": "nova",
    "aetna": "athena",
    "athina": "athena",
    "saleen": "selene",
    "celine": "selene",
    "sealine": "selene",
    "nowa": "nova",
}

# Ordinary English that must never be read as a wake name, regardless of score.
#
# This exists because fuzzy distance provably cannot separate these cases. Real
# mis-transcriptions "atena", "athene" and "seline" score 0.833 against their
# agents; the English word "athens" also scores 0.833 against "athena". There is
# no threshold that admits the first three and rejects the fourth, so the
# residue has to be curated. Grows from production logs alongside ALIASES.
NEVER_ROUTE: frozenset[str] = frozenset(
    {
        "athens", "athlete", "athletic", "athletics",
        "novel", "november", "novelty",
        "vegan", "veganism", "vegetable", "vegetables", "vegetarian",
        "selenium", "saline",
        "astrology", "astronomy", "astronaut", "asteroid",
        "laser", "lyric", "lyrics",
    }
)

_PUNCT = re.compile(r"[^\w\s']")
_WS = re.compile(r"\s+")


class RouteMethod(StrEnum):
    EXACT = "exact"
    ALIAS = "alias"
    PHONETIC = "phonetic"
    FUZZY = "fuzzy"
    TWO_TOKEN = "two_token"
    SESSION = "session"
    FALLTHROUGH = "fallthrough"
    EXPLICIT = "explicit"


# Confidence by method. Fuzzy reports its own similarity instead of a constant.
_CONFIDENCE = {
    RouteMethod.EXACT: 1.0,
    RouteMethod.ALIAS: 0.95,
    RouteMethod.PHONETIC: 0.85,
    RouteMethod.SESSION: 1.0,
    RouteMethod.FALLTHROUGH: 0.0,
}

# A name found in the second token position is slightly less certain than one
# leading the utterance.
_TWO_TOKEN_PENALTY = 0.95

# Phonetic match on the alternate Double Metaphone code is weaker evidence than
# on the primary.
_ALTERNATE_CODE_PENALTY = 0.94


@dataclass(frozen=True)
class RouteResult:
    """Outcome of routing one utterance."""

    agent: str
    confidence: float
    method: RouteMethod
    body: str
    """Transcript with the wake name removed — what the agent actually reads."""

    needs_clarification: bool = False
    """True when two agents scored within the ambiguity margin. Do not route."""

    candidates: tuple[str, ...] = field(default_factory=tuple)
    """The tied agents, when needs_clarification is set."""

    switched_from: str | None = None
    """Set when this closed an open session belonging to another agent."""

    @property
    def is_confident(self) -> bool:
        return self.confidence >= get_settings().route_confidence_floor


@lru_cache(maxsize=1)
def _agent_codes() -> dict[str, tuple[str, str]]:
    """Double Metaphone codes for each agent name, computed once."""
    return {agent: doublemetaphone(agent) for agent in AGENTS}


def normalise(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def _strip_fillers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens) and tokens[i] in FILLERS:
        i += 1
    return tokens[i:]


def _score_token(token: str) -> list[tuple[str, float, RouteMethod]]:
    """Score one token against every agent name. Returns all plausible matches.

    Every method is evaluated rather than returning on the first hit, because
    ambiguity detection needs to see the runner-up. A cascade that returns early
    cannot tell "clearly Vega" from "Vega or Selene, roughly equally".
    """
    if not token or token in NEVER_ROUTE:
        return []

    scored: dict[str, tuple[float, RouteMethod]] = {}

    def offer(agent: str, confidence: float, method: RouteMethod) -> None:
        existing = scored.get(agent)
        if existing is None or confidence > existing[0]:
            scored[agent] = (confidence, method)

    # 1. Exact
    if token in AGENTS:
        offer(token, _CONFIDENCE[RouteMethod.EXACT], RouteMethod.EXACT)

    # 2. Curated alias
    if (aliased := ALIASES.get(token)) is not None:
        offer(aliased, _CONFIDENCE[RouteMethod.ALIAS], RouteMethod.ALIAS)

    # 3. Phonetic
    primary, alternate = doublemetaphone(token)
    for agent, (agent_primary, agent_alternate) in _agent_codes().items():
        agent_codes = {c for c in (agent_primary, agent_alternate) if c}
        if primary and primary in agent_codes:
            offer(agent, _CONFIDENCE[RouteMethod.PHONETIC], RouteMethod.PHONETIC)
        elif alternate and alternate in agent_codes:
            offer(
                agent,
                _CONFIDENCE[RouteMethod.PHONETIC] * _ALTERNATE_CODE_PENALTY,
                RouteMethod.PHONETIC,
            )

    # 4. Fuzzy. Reports its own similarity so a near-miss stays distinguishable
    #    from a solid match.
    for agent in AGENTS:
        similarity = DamerauLevenshtein.normalized_similarity(token, agent)
        if similarity >= _FUZZY_FLOOR:
            offer(agent, similarity, RouteMethod.FUZZY)

    return [(agent, conf, method) for agent, (conf, method) in scored.items()]


_FUZZY_FLOOR = 0.82


def resolve(
    transcript: str,
    *,
    active_agent: str | None = None,
) -> RouteResult:
    """Route an utterance to an agent.

    `active_agent` is the agent of the currently open session, or None if no
    session is open. It changes the default: with a session open an unnamed
    utterance continues with that agent; without one it falls through to Astraea.
    """
    settings = get_settings()
    normalised = normalise(transcript)
    tokens = _strip_fillers(normalised.split())

    if not tokens:
        return _no_name(transcript, active_agent, body=normalised)

    # First token, then the first two joined ("no va" -> "nova").
    candidates = _score_token(tokens[0])
    name_token_count = 1

    if len(tokens) >= 2:
        joined = _score_token(tokens[0] + tokens[1])
        joined = [
            (agent, conf * _TWO_TOKEN_PENALTY, RouteMethod.TWO_TOKEN)
            for agent, conf, _ in joined
        ]
        best_single = max((c for _, c, _ in candidates), default=0.0)
        best_joined = max((c for _, c, _ in joined), default=0.0)
        if best_joined > best_single:
            candidates = joined
            name_token_count = 2

    if not candidates:
        return _no_name(transcript, active_agent, body=normalised)

    candidates.sort(key=lambda c: c[1], reverse=True)
    top_agent, top_confidence, top_method = candidates[0]

    if top_confidence < settings.route_confidence_floor:
        return _no_name(transcript, active_agent, body=normalised)

    body = " ".join(tokens[name_token_count:]).strip()

    # Ambiguity: two agents too close to separate. Never pick — ask.
    if len(candidates) > 1:
        runner_up_agent, runner_up_confidence, _ = candidates[1]
        if top_confidence - runner_up_confidence <= settings.route_ambiguity_margin:
            return RouteResult(
                agent="astraea",
                confidence=top_confidence,
                method=top_method,
                body=body,
                needs_clarification=True,
                candidates=(top_agent, runner_up_agent),
            )

    return RouteResult(
        agent=top_agent,
        confidence=top_confidence,
        method=top_method,
        body=body,
        switched_from=active_agent if active_agent and active_agent != top_agent else None,
    )


def _no_name(transcript: str, active_agent: str | None, *, body: str) -> RouteResult:
    """No confident wake name.

    With a session open this is a follow-up and belongs to the active agent. With
    no session open it is Astraea's, flagged as unrouted. Astraea is the default
    only in the absence of context.
    """
    if active_agent:
        return RouteResult(
            agent=active_agent,
            confidence=_CONFIDENCE[RouteMethod.SESSION],
            method=RouteMethod.SESSION,
            body=body or normalise(transcript),
        )
    return RouteResult(
        agent="astraea",
        confidence=_CONFIDENCE[RouteMethod.FALLTHROUGH],
        method=RouteMethod.FALLTHROUGH,
        body=body or normalise(transcript),
    )
