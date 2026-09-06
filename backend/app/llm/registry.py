"""Logical model names, provider selection, fallback, and cost accounting.

Agents ask for a *role* ("reasoning", "cheap", "local"), never a model id. That
keeps per-agent model assignment a config decision and makes the cloud→local
fallback invisible to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.provider import (
    AnthropicProvider,
    LLMResponse,
    Message,
    OllamaProvider,
    ProviderError,
)
from app.logging_config import get_logger

log = get_logger(__name__)

ModelRole = Literal["reasoning", "cheap", "local"]


class BudgetExceeded(RuntimeError):
    """Monthly cloud ceiling reached and the configured action is to block.

    Only raised when ASTRA_COST_CEILING_ACTION is "block". The default is
    "fallback", which degrades to the local model instead — a system that
    silently stops answering is worse than one that answers less well.
    """

# List price per million tokens, in millionths of a US dollar.
#
# USD rather than paise so no exchange rate is baked into the table: the rate
# moves, the vendor's prices do not move with it. Integer micros throughout —
# the same no-float rule Vega's ledger follows, for the same reason.
#
# Verified against Anthropic's published pricing. Re-check when adding a model;
# an unlisted model costs 0, which silently disables the ceiling for it, so
# `_price_for` warns rather than failing open quietly.
PRICE_PER_MTOK_USD_MICROS: dict[str, dict[str, int]] = {
    "claude-opus-5": {"in": 5_000_000, "out": 25_000_000},
    "claude-opus-4-8": {"in": 5_000_000, "out": 25_000_000},
    "claude-sonnet-5": {"in": 3_000_000, "out": 15_000_000},
    "claude-sonnet-4-6": {"in": 3_000_000, "out": 15_000_000},
    "claude-haiku-4-5": {"in": 1_000_000, "out": 5_000_000},
    "claude-haiku-4-5-20251001": {"in": 1_000_000, "out": 5_000_000},
}


def _price_for(model: str) -> dict[str, int] | None:
    """Exact match, then prefix match so dated snapshots resolve."""
    if model in PRICE_PER_MTOK_USD_MICROS:
        return PRICE_PER_MTOK_USD_MICROS[model]
    for known, price in PRICE_PER_MTOK_USD_MICROS.items():
        if model.startswith(known):
            return price
    return None


@dataclass
class Resolution:
    model: str
    provider: Any
    role: ModelRole
    degraded: str | None = None


class ModelRegistry:
    """Resolves a role to a provider + model, with fallback."""

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.ollama = OllamaProvider()
        self.anthropic = AnthropicProvider()

    def resolve(self, role: ModelRole) -> Resolution:
        settings = self.settings

        if role == "local" or settings.model_profile == "local_only":
            return Resolution(settings.model_local.split("/", 1)[-1], self.ollama, role)

        wanted = settings.model_reasoning if role == "reasoning" else settings.model_cheap
        vendor, _, model_id = wanted.partition("/")

        if vendor == "anthropic" and self.anthropic.configured:
            return Resolution(model_id, self.anthropic, role)
        if vendor == "ollama":
            return Resolution(model_id, self.ollama, role)

        # No cloud key: fall back rather than fail. The agent is told once so it
        # can be honest about running degraded instead of silently sounding worse.
        return Resolution(
            settings.model_local.split("/", 1)[-1],
            self.ollama,
            role,
            degraded="no cloud model configured; using local model",
        )

    async def complete(
        self,
        messages: list[Message],
        *,
        role: ModelRole = "reasoning",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.3,
        session: AsyncSession | None = None,
        agent: str = "unknown",
        purpose: str = "reason",
    ) -> LLMResponse:
        """Complete a turn, falling back to local on cloud failure or over budget."""
        resolution = self.resolve(role)

        # Pre-flight budget gate. Checked before every cloud call, because a
        # call's cost is unknowable until it returns — so the ceiling can be
        # exceeded by at most one call's worth. That is a bound on *new* spend,
        # not an exact stop, and it is the same limitation any pre-request gate
        # has. Erring one call over beats refusing to answer at all.
        if session is not None and not resolution.provider.is_local:
            spent = await self.month_to_date_cost_minor(session)
            ceiling = self.settings.monthly_cost_ceiling_minor
            if spent >= ceiling:
                if self.settings.cost_ceiling_action == "block":
                    raise BudgetExceeded(
                        f"monthly ceiling reached: {spent} of {ceiling} paise spent"
                    )
                log.warning(
                    "cost.ceiling_reached_falling_back",
                    spent_paise=spent,
                    ceiling_paise=ceiling,
                    agent=agent,
                )
                resolution = Resolution(
                    self.settings.model_local.split("/", 1)[-1],
                    self.ollama,
                    role,
                    degraded=(
                        f"monthly cloud budget of {ceiling // 100} rupees is spent; "
                        "running on the local model until next month"
                    ),
                )

        try:
            response = await resolution.provider.complete(
                messages,
                model=resolution.model,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            response.degraded = resolution.degraded
        except ProviderError as exc:
            if resolution.provider.is_local:
                raise
            log.warning("llm.cloud_failed_falling_back", error=str(exc), agent=agent)
            local_model = self.settings.model_local.split("/", 1)[-1]
            response = await self.ollama.complete(
                messages,
                model=local_model,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            response.degraded = f"cloud model unavailable ({exc}); used local model"

        if session is not None:
            await self._record_cost(session, response, agent=agent, purpose=purpose)
        return response

    def cost_paise(self, model: str, tokens_in: int, tokens_out: int) -> int:
        """Exact integer cost in paise. No float touches this path.

        tokens x (micro-USD per Mtok) / 1e6 -> micro-USD, then x (paise per USD)
        / 1e6 -> paise. Integer division truncates, so the ledger very slightly
        under-reports rather than over-reports — the safe direction for a cap.
        """
        prices = _price_for(model)
        if prices is None:
            if not model.startswith("qwen") and "/" not in model:
                log.warning("cost.unpriced_model", model=model)
            return 0

        usd_micros = (
            tokens_in * prices["in"] + tokens_out * prices["out"]
        ) // 1_000_000
        return usd_micros * self.settings.usd_inr_paise // 1_000_000

    async def _record_cost(
        self, session: AsyncSession, response: LLMResponse, *, agent: str, purpose: str
    ) -> None:
        cost = (
            0
            if response.is_local
            else self.cost_paise(response.model, response.tokens_in, response.tokens_out)
        )

        await session.execute(
            sa.text(
                "INSERT INTO cost_ledger "
                "(agent, model, purpose, tokens_in, tokens_out, cost_minor, is_local) "
                "VALUES (:agent, :model, :purpose, :tin, :tout, :cost, :local)"
            ),
            {
                "agent": agent,
                "model": response.model,
                "purpose": purpose,
                "tin": response.tokens_in,
                "tout": response.tokens_out,
                "cost": cost,
                "local": response.is_local,
            },
        )

    async def month_to_date_cost_minor(self, session: AsyncSession) -> int:
        total = (
            await session.execute(
                sa.text(
                    "SELECT COALESCE(SUM(cost_minor), 0) FROM cost_ledger "
                    "WHERE occurred_at >= date_trunc('month', now())"
                )
            )
        ).scalar_one()
        return int(total)

    async def over_budget(self, session: AsyncSession) -> bool:
        return (
            await self.month_to_date_cost_minor(session)
            >= self.settings.monthly_cost_ceiling_minor
        )

    async def spend_report(self, session: AsyncSession) -> dict[str, Any]:
        """Month-to-date spend, per agent and per model. Astraea reports this."""
        spent = await self.month_to_date_cost_minor(session)
        ceiling = self.settings.monthly_cost_ceiling_minor

        rows = (
            await session.execute(
                sa.text(
                    "SELECT agent, model, is_local, COUNT(*) AS calls, "
                    "       SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, "
                    "       SUM(cost_minor) AS cost "
                    "FROM cost_ledger WHERE occurred_at >= date_trunc('month', now()) "
                    "GROUP BY agent, model, is_local ORDER BY cost DESC"
                )
            )
        ).all()

        return {
            "spent_paise": spent,
            "spent_rupees": spent / 100,
            "ceiling_paise": ceiling,
            "ceiling_rupees": ceiling / 100,
            "percent_used": round(spent * 100 / ceiling, 1) if ceiling else 0.0,
            "over_budget": spent >= ceiling,
            "action_on_breach": self.settings.cost_ceiling_action,
            "breakdown": [
                {
                    "agent": r.agent,
                    "model": r.model,
                    "is_local": r.is_local,
                    "calls": int(r.calls),
                    "tokens_in": int(r.tin or 0),
                    "tokens_out": int(r.tout or 0),
                    "cost_rupees": int(r.cost or 0) / 100,
                }
                for r in rows
            ],
        }


_registry: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
