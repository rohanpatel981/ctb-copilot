from pathlib import Path

from pydantic import Field
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


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
