from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

EB_BASE_URL = "https://api.enablebanking.com"
EB_AUDIENCE = "api.enablebanking.com"
EB_ISSUER = "enablebanking.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINANCE_", env_file=".env", extra="ignore")

    config_dir: Path = Path.home() / ".config" / "finance"
    data_dir: Path = Path.home() / ".local" / "share" / "finance"
    key_passphrase: str | None = None

    @property
    def keys_dir(self) -> Path:
        return self.config_dir / "keys"

    @property
    def private_key_path(self) -> Path:
        return self.keys_dir / "private.key.age"

    @property
    def public_cert_path(self) -> Path:
        return self.keys_dir / "public.crt"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finance.db"

    @property
    def rules_path(self) -> Path:
        return self.config_dir / "rules.yaml"


class AppConfig(BaseModel):
    app_id: str | None = None
    callback_url: str = "http://localhost:8000/callback"
    timezone: str = "Europe/Paris"
    sync_overlap_days: int | None = None
    minimal_retention: bool = False


def load_config(settings: Settings) -> AppConfig:
    if not settings.config_file.exists():
        return AppConfig()
    with settings.config_file.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig(**data)


def save_config(settings: Settings, config: AppConfig) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    with settings.config_file.open("wb") as f:
        tomli_w.dump(config.model_dump(exclude_none=True), f)
    os.chmod(settings.config_file, 0o600)


def get_settings() -> Settings:
    return Settings()
