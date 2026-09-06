"""Namespace access control.

Each agent owns one namespace. Astraea reads all of them and writes only
`global`. This is enforced here and in the repository layer rather than in
prompts, because prompt-level scoping holds only while the model cooperates —
a confused Lyra asking for finance facts must get an empty result, not private
data.

See docs/memory-design.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass

GLOBAL = "global"

# agent -> the namespace it owns and writes
OWNED: dict[str, str] = {
    "astraea": GLOBAL,
    "vega": "finance",
    "lyra": "health",
    "nova": "work",
    "athena": "learning",
    "selene": "home",
}

ALL_NAMESPACES: frozenset[str] = frozenset(OWNED.values())

# Astraea is the only cross-domain reader — correlation is her job. She is
# deliberately NOT a cross-domain writer: a figure written by the coordinator has
# no clear owner and no clear source, which destroys provenance.
CROSS_DOMAIN_READERS: frozenset[str] = frozenset({"astraea"})


class NamespaceAccessError(PermissionError):
    """Raised when an agent attempts access outside its scope.

    This is a programming error, not a user-facing condition. It means a tool or
    query was wired to the wrong agent identity, and it should fail loudly in
    tests rather than silently return nothing in production.
    """


@dataclass(frozen=True)
class AccessScope:
    """What one agent may read and write."""

    agent: str
    readable: frozenset[str]
    writable: frozenset[str]

    def can_read(self, namespace: str) -> bool:
        return namespace in self.readable

    def can_write(self, namespace: str) -> bool:
        return namespace in self.writable

    def assert_read(self, namespace: str) -> None:
        if not self.can_read(namespace):
            raise NamespaceAccessError(
                f"{self.agent} may not read namespace {namespace!r} "
                f"(readable: {sorted(self.readable)})"
            )

    def assert_write(self, namespace: str) -> None:
        if not self.can_write(namespace):
            raise NamespaceAccessError(
                f"{self.agent} may not write namespace {namespace!r} "
                f"(writable: {sorted(self.writable)})"
            )


def scope_for(agent: str) -> AccessScope:
    """Resolve an agent's access scope.

    Every agent reads its own namespace plus `global` (the shared profile).
    Astraea additionally reads everything. Only Astraea writes `global`.
    """
    if agent not in OWNED:
        raise NamespaceAccessError(f"unknown agent {agent!r}")

    owned = OWNED[agent]

    readable = ALL_NAMESPACES if agent in CROSS_DOMAIN_READERS else frozenset({owned, GLOBAL})

    # An agent writes only what it owns. For Astraea that is `global` alone.
    writable = frozenset({owned})

    return AccessScope(agent=agent, readable=readable, writable=writable)
