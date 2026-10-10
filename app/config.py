from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    allowed_user_id: int
    tz: str = "Europe/Moscow"
    database_url: str
    log_level: str = "INFO"
    seed_dir: str = "seed"
    heartbeat_file: str = "/tmp/heartbeat"
    # Цены модели, $ за 1 млн токенов (для /usage); по умолчанию — Claude Sonnet 5.5
    price_input: float = 2.0
    price_output: float = 10.0
    price_cache_read: float = 0.2
    price_cache_write: float = 2.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
