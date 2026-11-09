from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    app_env: Literal["development", "production", "test"] = "development"
    app_name: str = "Nikee Migrations API"
    api_v1_prefix: str = "/api/v1"
    frontend_url: str = "http://localhost:5000"
    cors_origins: str = ""

    # Database
    database_url: str
    test_database_url: str | None = None

    # Auth
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    otp_expire_minutes: int = 10
    otp_max_attempts: int = 5
    invite_expire_hours: int = 72
    rate_limit_enabled: bool = True

    # Seed
    seed_admin_email: str = "admin@nikeemigration.com"
    seed_admin_password: str = ""
    seed_admin_name: str = "Nikee Admin"
    seed_demo_password: str = ""

    # Background jobs
    redis_url: str = ""
    jobs_eager: bool = False  # run jobs inline and await them (tests)

    # Email
    email_backend: Literal["console", "file", "memory", "smtp", "resend"] = "console"
    email_from: str = "Nikee Migrations <no-reply@nikeemigration.com>"
    dev_mail_dir: str = "./dev_mail"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    resend_api_key: str = ""

    # Storage
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./storage"
    signed_url_expire_seconds: int = 300
    max_upload_bytes: int = 10 * 1024 * 1024
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = ""

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def effective_database_url(self) -> str:
        if self.is_test:
            if not self.test_database_url:
                raise RuntimeError("TEST_DATABASE_URL must be set when APP_ENV=test")
            return self.test_database_url
        return self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else BASE_DIR / path


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
