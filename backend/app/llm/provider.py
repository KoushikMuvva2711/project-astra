"""Model provider abstraction: one interface over Ollama and Anthropic.

Deviation from the plan, recorded deliberately: the plan named LiteLLM. Two
providers behind a ~200-line adapter is smaller, has no dependency tree to break
on an unreliable network, and keeps token accounting explicit. The interface is
narrow enough that dropping LiteLLM in behind it later is mechanical, should a
third or fourth provider ever be needed.

Fallback is a first-class path, not an error handler. When the cloud is
unreachable, over budget, or unconfigured, the local model answers and the turn
is marked degraded so the agent can say so once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    is_local: bool = False
    degraded: str | None = None
    """Set when this came from a fallback path, so the agent can mention it once."""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    name: str
    is_local: bool

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> LLMResponse: ...


# ── Ollama ───────────────────────────────────────────────────────────────────

class OllamaProvider:
    """Local models over Ollama's /api/chat."""

    name = "ollama"
    is_local = True

    def __init__(self, base_url: str | None = None, timeout: float = 120.0) -> None:
        self.base_url = (base_url or get_settings().ollama_base_url).rstrip("/")
        self.timeout = timeout

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_to_ollama(m) for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc

        message = body.get("message", {})
        calls = [
            ToolCall(
                id=f"call_{i}",
                name=c["function"]["name"],
                arguments=_coerce_args(c["function"].get("arguments")),
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        ]

        return LLMResponse(
            text=message.get("content", "") or "",
            tool_calls=calls,
            model=model,
            tokens_in=body.get("prompt_eval_count", 0) or 0,
            tokens_out=body.get("eval_count", 0) or 0,
            is_local=True,
        )


def _to_ollama(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "content": message.content}
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}}
            for c in message.tool_calls
        ]
    return payload


def _coerce_args(raw: Any) -> dict[str, Any]:
    """Small models sometimes emit arguments as a JSON string rather than an object."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# ── Anthropic ────────────────────────────────────────────────────────────────

class AnthropicProvider:
    """Cloud reasoning. Used when a key is configured and the budget allows."""

    name = "anthropic"
    is_local = False

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else get_settings().anthropic_api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> LLMResponse:
        if not self.configured:
            raise ProviderError("no Anthropic API key configured")

        from anthropic import AsyncAnthropic

        system = "\n\n".join(m.content for m in messages if m.role == "system")
        conversation = [_to_anthropic(m) for m in messages if m.role != "system"]

        try:
            client = AsyncAnthropic(api_key=self.api_key)
            response = await client.messages.create(
                model=model,
                system=system or None,
                messages=conversation,
                tools=tools or [],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc

        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            model=model,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            is_local=False,
        )


def _to_anthropic(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            ],
        }
    if message.tool_calls:
        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in message.tool_calls
        )
        return {"role": "assistant", "content": content}
    return {"role": message.role, "content": message.content}
