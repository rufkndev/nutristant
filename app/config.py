from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    allowed_user_id: int
    tz: str = "Europe/Moscow"
    database_url: str
    log_level: str = "INFO"
    seed_dir: str = "seed"
    heartbeat_file: str = "/tmp/heartbeat"
    # Цены модели, $ за 1 млн токенов (для /usage); по умолчанию — gpt-6-luna
    price_input: float = 0.10
    price_output: float = 0.50
    price_cache_read: float = 0.01
    price_cache_write: float = 0.125


@lru_cache
def get_settings() -> Settings:
    return Settings()
