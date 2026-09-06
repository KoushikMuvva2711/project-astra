"""Application settings, loaded from environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Environment ──────────────────────────────────────────────────────────
    env: Literal["development", "production"] = Field("development", alias="ASTRA_ENV")
    log_level: str = Field("INFO", alias="ASTRA_LOG_LEVEL")

    # ── Auth ─────────────────────────────────────────────────────────────────
    device_token: str = Field(..., alias="ASTRA_DEVICE_TOKEN")

    # ── Database ─────────────────────────────────────────────────────────────
    postgres_user: str = Field("astra", alias="POSTGRES_USER")
    postgres_password: str = Field("astra-local-dev", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("astra", alias="POSTGRES_DB")
    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_host: str = Field("redis", alias="REDIS_HOST")
    redis_port: int = Field(6379, alias="REDIS_PORT")

    # ── Models ───────────────────────────────────────────────────────────────
    model_profile: Literal["cloud_first", "local_only"] = Field(
        "cloud_first", alias="ASTRA_MODEL_PROFILE"
    )
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    ollama_base_url: str = Field("http://host.docker.internal:11434", alias="OLLAMA_BASE_URL")

    # Sonnet, never Opus: this is a personal-assistant workload, not heavy code
    # reasoning. Haiku handles the scheduled maintenance jobs that never face
    # the user. See .env.example for the reasoning.
    model_reasoning: str = Field("anthropic/claude-sonnet-5", alias="ASTRA_MODEL_REASONING")
    model_cheap: str = Field("anthropic/claude-haiku-4-5", alias="ASTRA_MODEL_CHEAP")
    model_local: str = Field("ollama/qwen2.5:14b-instruct", alias="ASTRA_MODEL_LOCAL")

    embed_model: str = Field("ollama/nomic-embed-text", alias="ASTRA_EMBED_MODEL")
    embed_dim: int = Field(768, alias="ASTRA_EMBED_DIM")

    # ── Behaviour ────────────────────────────────────────────────────────────
    session_timeout_seconds: int = Field(90, alias="ASTRA_SESSION_TIMEOUT_SECONDS")
    route_confidence_floor: float = Field(0.70, alias="ASTRA_ROUTE_CONFIDENCE_FLOOR")
    route_ambiguity_margin: float = Field(0.05, alias="ASTRA_ROUTE_AMBIGUITY_MARGIN")
    timezone: str = Field("Asia/Kolkata", alias="ASTRA_TIMEZONE")
    currency: str = Field("INR", alias="ASTRA_CURRENCY")

    monthly_cost_ceiling_minor: int = Field(
        200_000, alias="ASTRA_MONTHLY_COST_CEILING_MINOR"
    )
    cost_ceiling_action: Literal["fallback", "block"] = Field(
        "fallback", alias="ASTRA_COST_CEILING_ACTION"
    )
    # Paise per USD, used only to convert vendor list prices into the INR ledger.
    # Vendor prices are quoted in USD and do not move with the exchange rate, so
    # the rate is configuration rather than a constant baked into the price table.
    usd_inr_paise: int = Field(8800, alias="ASTRA_USD_INR_PAISE")

    @field_validator("device_token")
    @classmethod
    def _reject_placeholder_token(cls, v: str) -> str:
        """A public tunnel with the example token is an open door to personal data."""
        if v.startswith("change-me") or len(v) < 32:
            raise ValueError(
                "ASTRA_DEVICE_TOKEN is unset, still the placeholder, or too short. "
                "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
        return v

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
