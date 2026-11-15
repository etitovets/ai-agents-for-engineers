"""Application settings loaded from the environment or a `.env` file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the weather proxy."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    request_timeout: float = 10.0


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()
