from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    secret_key: str
    webhook_token: str
    public_base_url: str = "http://localhost:8000"

    database_url: str = "sqlite:///./dev.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_always_eager: bool = False

    timezone: str = "America/Sao_Paulo"

    evolution_base_url: str = ""
    evolution_api_key: str = ""

    chatwoot_base_url: str = ""
    chatwoot_token: str = ""
    chatwoot_account_id: str = ""

    apify_token: str = ""
    anthropic_api_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
