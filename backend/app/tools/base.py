"""Tool registry and boundary-enforced allowlists.

An agent's tool allowlist is enforced here, not described in its prompt. Lyra
physically cannot write to `expenses` even if a confused model emits that call —
the dispatcher refuses it before any handler runs.

Prompt-level guardrails are guidance for a cooperative model. This holds when the
model misbehaves, which is the only time it matters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs beyond its own arguments."""

    session: AsyncSession
    agent: str
    turn_id: int | None = None
    asr_confidence: float | None = None


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a tool call.

    `ok=False` is a real failure and the agent must report it honestly rather
    than confirming. A write that did not happen must never be announced as
    though it did — see docs/conversation-architecture.md §10.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    needs_confirmation: bool = False

    @classmethod
    def success(cls, message: str = "", **data: Any) -> ToolResult:
        return cls(ok=True, data=data, message=message)

    @classmethod
    def failure(cls, message: str, **data: Any) -> ToolResult:
        return cls(ok=False, data=data, message=message)

    @classmethod
    def confirm(cls, message: str, **data: Any) -> ToolResult:
        """Understood, but not committed. The agent must ask before writing."""
        return cls(ok=True, data=data, message=message, needs_confirmation=True)


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    writes: bool = False
    """True if the tool mutates state. Used to reason about rollback and to keep
    read-only agents (Astraea outside `global`) from mutating anything."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name, None)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas_for(self, allowed: list[str]) -> list[dict[str, Any]]:
        """JSON schemas for the tools an agent may call, for the model prompt."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for name in allowed
            if (tool := self._tools.get(name)) is not None
        ]


REGISTRY = ToolRegistry()


class ToolNotAllowed(PermissionError):
    """An agent attempted a tool outside its allowlist."""


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    *,
    allowed: list[str],
    context: ToolContext,
) -> ToolResult:
    """Invoke a tool after checking it against the calling agent's allowlist.

    The allowlist check happens before the handler is resolved, so an
    out-of-scope call cannot reach a handler even if the tool exists.
    """
    if name not in allowed:
        raise ToolNotAllowed(
            f"{context.agent} may not call {name!r} (allowed: {sorted(allowed)})"
        )

    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult.failure(f"unknown tool {name!r}")

    try:
        return await tool.handler(context=context, **arguments)
    except TypeError as exc:
        # Bad arguments from the model: recoverable, the agent may retry once.
        return ToolResult.failure(f"invalid arguments for {name!r}: {exc}")


def tool(
    name: str, description: str, parameters: dict[str, Any], *, writes: bool = False
) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        REGISTRY.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                writes=writes,
            )
        )
        return handler

    return decorator
