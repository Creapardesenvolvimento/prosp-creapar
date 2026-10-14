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

    # Rampa de aquecimento: "dia_inicial:limite_diario" separados por vírgula.
    rampa_limites: str = "1:10,4:20,8:40,15:70,22:100,31:150"
    conferir_conexoes_segundos: int = 120

    evolution_base_url: str = ""
    evolution_api_key: str = ""

    chatwoot_base_url: str = ""
    chatwoot_token: str = ""
    chatwoot_account_id: str = ""

    apify_token: str = ""
    anthropic_api_key: str = ""

    # Agenda interna de vendedores
    reuniao_minutos: int = 30
    reuniao_buffer_minutos: int = 15
    agenda_antecedencia_horas: int = 2
    agenda_dias_a_frente: int = 10

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
