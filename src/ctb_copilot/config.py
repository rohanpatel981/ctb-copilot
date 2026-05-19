import json
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = Field(..., description="Anthropic API key")
    anthropic_model: str = "claude-opus-4-7"

    storage_dir: Path = Path("./data/uploads")
    duckdb_path: Path = Path("./data/ctb.duckdb")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_base_url: str = "http://127.0.0.1:8000"

    # Optional bearer-token auth on /sync, /query, /upload, /uploads, /periods,
    # /entities. If unset, the API runs in dev mode (no auth required). For any
    # multi-tenant deployment this MUST be set — without it, a network caller
    # can pass any tenant scope they like.
    api_token: str | None = None

    # --- DocumentDB sync (optional) ---
    docdb_uri: str | None = None
    docdb_database: str | None = None
    docdb_collection: str | None = None
    docdb_default_filter: dict[str, Any] = Field(default_factory=dict)
    docdb_period_field: str | None = None
    docdb_reporting_period: str | None = None
    docdb_reporting_period_field: str = "reporting_period_id"

    @field_validator("docdb_default_filter", mode="before")
    @classmethod
    def _parse_filter_json(cls, v: Any) -> Any:
        """Allow DOCDB_DEFAULT_FILTER to come in as either a JSON string from
        env or a dict from another source."""
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            return json.loads(v)
        return v

    @property
    def docdb_configured(self) -> bool:
        """True iff the minimum DocumentDB env vars are set."""
        return bool(self.docdb_uri and self.docdb_database and self.docdb_collection and self.docdb_period_field)


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
