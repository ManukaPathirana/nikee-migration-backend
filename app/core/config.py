from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
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
    # Times in emails are shown in this zone (apps show the device's local time).
    business_timezone: str = "Asia/Colombo"
    cors_origins: str = ""
    # The hosted web app (links in web push notifications open it).
    web_app_url: str = "https://nikee-migration-frontend.vercel.app"
    # Browsers allowed to call the API besides CORS_ORIGINS: the hosted web app (its Vercel
    # project addresses, including preview deployments) and any nikeeworld.com subdomain.
    cors_origin_regex: str = (
        r"https://(nikee-migration-frontend[a-z0-9-]*\.vercel\.app"
        r"|([a-z0-9-]+\.)?nikeeworld\.com)"
    )

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

    # Push notifications (Firebase Cloud Messaging). Both empty = push disabled.
    fcm_project_id: str = ""
    fcm_credentials_file: str = ""
    # The service-account JSON itself (for hosts without files, e.g. Vercel). Its project_id is
    # used when FCM_PROJECT_ID is empty.
    fcm_credentials_json: str = ""

    # Storage
    # "local" switches to "blob" by itself when BLOB_READ_WRITE_TOKEN is set (Vercel Blob store).
    storage_backend: Literal["local", "s3", "blob"] = "local"
    local_storage_dir: str = "./storage"
    signed_url_expire_seconds: int = 300
    max_upload_bytes: int = 10 * 1024 * 1024
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = ""
    # Vercel Blob (private store). Vercel adds this when the store is connected to the project.
    blob_read_write_token: str = ""

    # Payments. "test" shows a built-in checkout page so the flow works without a gateway
    # account (never in production); "stripe" uses Stripe Checkout in AUD.
    payments_backend: Literal["test", "stripe"] = "test"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "AUD"
    # Public address of this API; payment pages send customers back here.
    public_api_url: str = "http://localhost:8000"

    @field_validator(
        "app_env", "storage_backend", "payments_backend", "email_backend", mode="before"
    )
    @classmethod
    def _tidy_choice(cls, v: object) -> object:
        """Dashboard-pasted values often carry spaces, quotes or capitals ("Stripe ")."""
        return v.strip().strip("\"'").strip().lower() if isinstance(v, str) else v

    @field_validator(
        "stripe_secret_key",
        "stripe_webhook_secret",
        "blob_read_write_token",
        "public_api_url",
        mode="before",
    )
    @classmethod
    def _tidy_secret(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        v = v.strip().strip("\"'").strip()
        return v.rstrip("/") if v.startswith("http") else v

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
