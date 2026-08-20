"""
Environment-driven settings. Nothing here is a real secret — every value is
read from the environment (or a local .env file for development) so that
credentials never live in source code or get committed to a repo.

In production (Cloud Run), these are set as environment variables / Secret
Manager references on the service and the Cloud Run Job, not baked into the
container image.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql+psycopg2://estkana:estkana@localhost:5432/estkana"

    # --- Session / auth ---
    session_secret: str = "dev-only-insecure-secret-change-me"
    session_cookie_name: str = "estkana_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14  # 14 days

    # --- Odoo ---
    odoo_url: str | None = None          # e.g. https://estkana.odoo.com
    odoo_db: str | None = None           # database name
    odoo_username: str | None = None
    odoo_api_key: str | None = None

    # --- Loyverse ---
    loyverse_api_token: str | None = None
    loyverse_base_url: str = "https://api.loyverse.com/v1.0"

    # --- App ---
    schema_version: str = "1"
    environment: str = "development"  # "development" | "production"

    @property
    def odoo_configured(self) -> bool:
        return bool(self.odoo_url and self.odoo_db and self.odoo_username and self.odoo_api_key)

    @property
    def loyverse_configured(self) -> bool:
        return bool(self.loyverse_api_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
