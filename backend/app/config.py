from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from cryptography.fernet import Fernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path("data")
    duckdb_path: Path = Path("data/quant.db")
    deployment_mode: Literal["single_instance"] = "single_instance"
    encryption_key: Optional[str] = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    stock_picker_alert_webhook_enabled: bool = False
    stock_picker_alert_webhook_url: Optional[str] = None
    stock_picker_alert_webhook_secret: Optional[str] = None
    stock_picker_alert_webhook_timeout_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
    )
    quant_selector_bundle_path: Optional[Path] = None
    quant_selector_product_metadata_path: Optional[Path] = None
    quant_selector_outcome_bundle_path: Optional[Path] = None
    deepseek_base_url: str = "https://api.deepseek.com"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_fernet(self) -> Fernet:
        key = self.encryption_key or self._load_or_create_key()
        return Fernet(key)

    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def _load_or_create_key(self) -> bytes:
        key_file = self.data_dir / "encryption.key"
        if key_file.exists():
            return key_file.read_bytes().strip()

        key = Fernet.generate_key()
        key_file.write_bytes(key)
        # Restrict permissions (best effort on POSIX)
        try:
            os.chmod(key_file, 0o600)
        except PermissionError:
            pass
        return key


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
