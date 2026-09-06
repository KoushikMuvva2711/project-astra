"""Namespace access control.

The constellation's whole privacy model rests on this: Vega knows spending,
Lyra knows nutrition, and only Astraea sees both. If isolation is advisory
rather than structural, the separation is decorative.
"""

import pytest

from app.memory.namespaces import (
    ALL_NAMESPACES,
    GLOBAL,
    OWNED,
    NamespaceAccessError,
    scope_for,
)

SPECIALISTS = ["vega", "lyra", "nova", "athena", "selene"]


def test_every_agent_owns_exactly_one_namespace():
    assert len(set(OWNED.values())) == len(OWNED) == 6


@pytest.mark.parametrize("agent", SPECIALISTS)
def test_specialist_reads_only_its_own_namespace_and_global(agent):
    scope = scope_for(agent)
    assert scope.readable == {OWNED[agent], GLOBAL}


@pytest.mark.parametrize("agent", SPECIALISTS)
def test_specialist_cannot_read_a_peer_namespace(agent):
    scope = scope_for(agent)
    peers = [ns for a, ns in OWNED.items() if a != agent and ns != GLOBAL]
    for peer_namespace in peers:
        assert not scope.can_read(peer_namespace)
        with pytest.raises(NamespaceAccessError):
            scope.assert_read(peer_namespace)


def test_lyra_cannot_read_finance():
    """The named case from the design doc."""
    with pytest.raises(NamespaceAccessError, match="finance"):
        scope_for("lyra").assert_read("finance")


def test_astraea_reads_every_namespace():
    """Correlation is her job and she cannot do it half-sighted."""
    assert scope_for("astraea").readable == ALL_NAMESPACES


def test_astraea_writes_only_global():
    """A figure written by the coordinator would have no clear owner or source."""
    scope = scope_for("astraea")
    assert scope.writable == {GLOBAL}
    for namespace in ALL_NAMESPACES - {GLOBAL}:
        with pytest.raises(NamespaceAccessError):
            scope.assert_write(namespace)


@pytest.mark.parametrize("agent", SPECIALISTS)
def test_specialist_cannot_write_global(agent):
    """Global is Astraea's alone; specialists read it but never edit the profile."""
    with pytest.raises(NamespaceAccessError):
        scope_for(agent).assert_write(GLOBAL)


@pytest.mark.parametrize("agent", list(OWNED))
def test_every_agent_can_write_what_it_owns(agent):
    scope_for(agent).assert_write(OWNED[agent])


@pytest.mark.parametrize("agent", list(OWNED))
def test_writable_is_always_a_subset_of_readable(agent):
    scope = scope_for(agent)
    assert scope.writable <= scope.readable


def test_unknown_agent_is_rejected():
    with pytest.raises(NamespaceAccessError, match="unknown agent"):
        scope_for("hermes")
