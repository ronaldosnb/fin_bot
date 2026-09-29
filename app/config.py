from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://finbot:finbot@localhost:5432/finbot"
    app_secret: str = ""
    admin_password_hash_b64: str = ""
    owner_jid: str = ""
    owner_self_jid: str = ""
    evolution_api_url: str = "http://evolution:8080"
    evolution_api_key: str = ""
    evolution_instance: str = "finbot"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    timezone: str = "America/Sao_Paulo"
    session_secure: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_runtime() -> None:
    settings = get_settings()
    missing = []
    for field in ("app_secret", "admin_password_hash_b64", "owner_jid", "evolution_api_key", "gemini_api_key"):
        value = getattr(settings, field)
        if not value or value.startswith(("troque_", "gere_", "chave_")):
            missing.append(field.upper())
    if len(settings.app_secret) < 32:
        missing.append("APP_SECRET (mínimo 32 caracteres)")
    if missing:
        raise RuntimeError("Configuração incompleta: " + ", ".join(sorted(set(missing))))
