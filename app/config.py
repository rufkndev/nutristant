from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    allowed_user_id: int
    tz: str = "Europe/Moscow"
    database_url: str
    log_level: str = "INFO"
    # Прокси для Telegram и OpenAI: http://, https://, socks5:// или socks5h:// (пусто — без прокси)
    proxy_url: str = ""
    seed_dir: str = "seed"
    heartbeat_file: str = "/tmp/heartbeat"
    # Цены модели, $ за 1 млн токенов (для /usage); по умолчанию — gpt-6-luna
    price_input: float = 0.10
    price_output: float = 0.50
    price_cache_read: float = 0.01
    price_cache_write: float = 0.125

    @field_validator("proxy_url")
    @classmethod
    def _proxy(cls, v: str) -> str:
        v = v.strip()
        if v and not v.lower().startswith(PROXY_SCHEMES):
            raise ValueError("PROXY_URL должен начинаться с http://, https://, socks5:// или socks5h:// "
                             "(ссылки tg://, t.me/proxy, vless://, ss:// боту не подходят)")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
