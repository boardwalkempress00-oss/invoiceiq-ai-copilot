"""Application settings, read from environment variables (and an optional local `.env` file).

Secrets are only ever read from the environment and are kept server-side. See `.env.example`.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AIProviderName(StrEnum):
    MOCK = "mock"
    AZURE_OPENAI = "azure_openai"
    DISABLED = "disabled"


class UsageStoreName(StrEnum):
    MEMORY = "memory"
    SQLITE = "sqlite"
    AZURE_TABLE = "azure_table"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application ---
    app_env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    data_dir: Path = PROJECT_ROOT / "data"

    # --- AI provider ---
    ai_provider: AIProviderName = AIProviderName.MOCK
    # Resource endpoint, e.g. https://<resource>.openai.azure.com. The provider calls the GA v1
    # API at <endpoint>/openai/v1/, which does not need an api-version parameter.
    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str | None = None
    # Optional. When unset, Microsoft Entra ID (DefaultAzureCredential / managed identity) is used.
    azure_openai_api_key: SecretStr | None = None

    # --- AI request limits ---
    # Worst-case duration of one chat request ≈ timeout × (retries + 1) + SDK backoff (≤ 8 s each).
    ai_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    ai_max_retries: int = Field(default=2, ge=0, le=5)
    ai_max_output_tokens: int = Field(default=700, ge=50, le=4000)
    # Character budget for the evidence sent to the model (~4 characters per token).
    ai_max_context_chars: int = Field(default=12_000, ge=1_000, le=60_000)

    # --- Cost estimation (prices per 1 million tokens; 0 = not configured) ---
    ai_price_input_per_1m: Decimal = Field(default=Decimal("0"), ge=0)
    ai_price_output_per_1m: Decimal = Field(default=Decimal("0"), ge=0)
    ai_price_currency: str = "USD"

    # --- Chat input limits ---
    chat_max_question_chars: int = Field(default=500, ge=20, le=2_000)
    chat_max_history_messages: int = Field(default=6, ge=0, le=20)
    chat_max_history_message_chars: int = Field(default=1_500, ge=100, le=5_000)

    # --- Usage limits (cost protection for the public demo) ---
    session_request_limit: int = Field(default=10, ge=0)
    global_daily_request_limit: int = Field(default=200, ge=0)
    # Daily budget for *estimated* AI cost, in AI_PRICE_CURRENCY. Enforced before each call.
    global_daily_cost_limit: Decimal = Field(default=Decimal("1.00"), ge=0)
    usage_store: UsageStoreName = UsageStoreName.MEMORY
    usage_sqlite_path: Path = PROJECT_ROOT / ".data" / "usage.sqlite3"
    # Azure Table Storage (production): endpoint + managed identity, e.g.
    # https://<account>.table.core.windows.net
    azure_table_endpoint: str | None = None
    azure_table_name: str = "aiusage"
    # Local development against Azurite only. Refused when APP_ENV=production.
    azure_table_connection_string: SecretStr | None = None

    # --- Retrieval ---
    retrieval_top_k: int = Field(default=3, ge=1, le=10)
    retrieval_min_score: float = Field(default=0.5, ge=0)

    @property
    def invoices_dir(self) -> Path:
        return self.data_dir / "invoices"

    @property
    def policy_path(self) -> Path:
        return self.data_dir / "policies" / "purchasing_policy.md"


@lru_cache
def get_settings() -> Settings:
    return Settings()
