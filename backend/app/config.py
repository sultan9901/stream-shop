"""Application configuration — all settings come from environment / .env."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = .../STREAM-CORPORATION  (this file is backend/app/config.py)
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- core ----------
    app_name: str = "STREAM CORPORATION"
    environment: str = "development"
    debug: bool = True
    base_url: str = "http://localhost:8000"
    secret_key: str = ""
    allowed_hosts: str = "*"
    cors_origins: str = "http://localhost:8000"

    # ---------- database ----------
    database_url: str = "sqlite+aiosqlite:///./stream_corporation.db"
    redis_url: str = ""

    # ---------- sessions ----------
    session_cookie_viewer: str = "sc_viewer"
    session_cookie_staff: str = "sc_staff"
    session_cookie_device: str = "sc_device"
    session_ttl_hours: int = 72
    staff_session_ttl_hours: int = 12
    cookie_secure: bool = False
    cookie_samesite: str = "lax"

    # ---------- bootstrap master ----------
    default_master_username: str = "Admin"
    default_master_password: str = "admin"

    # ---------- google oauth ----------
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    allow_dev_google_stub: bool = True

    # ---------- email ----------
    email_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    email_from: str = "no-reply@streamcorporation.local"
    email_from_name: str = "STREAM CORPORATION"

    # ---------- uploads ----------
    upload_dir: str = "./uploads"
    max_screenshot_mb: int = 8
    max_product_file_mb: int = 200
    max_image_mb: int = 10

    # ---------- downloads ----------
    download_token_ttl_hours: int = 72
    download_max_attempts: int = 10

    # ---------- rate limiting ----------
    rate_limit_enabled: bool = True
    login_rate_limit: str = "10/5m"
    upload_rate_limit: str = "20/1h"
    purchase_rate_limit: str = "30/1h"

    @field_validator("secret_key")
    @classmethod
    def _secret(cls, v: str) -> str:
        return v or secrets.token_urlsafe(64)

    # ---------- derived ----------
    @property
    def is_production(self) -> bool:
        return self.environment.lower().startswith("prod")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        return p if p.is_absolute() else (ROOT_DIR / p).resolve()

    @property
    def host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def cors_list(self) -> list[str]:
        return [h.strip() for h in self.cors_origins.split(",") if h.strip()]

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def dev_stub_enabled(self) -> bool:
        return self.allow_dev_google_stub and not self.google_enabled and not self.is_production


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# directory layout used across the app
FRONTEND_DIR = ROOT_DIR / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"

UPLOAD_ROOT = settings.upload_path
SUBDIRS = ("screenshots", "products", "media", "outbox")
for _name in SUBDIRS:
    (UPLOAD_ROOT / _name).mkdir(parents=True, exist_ok=True)
