"""Application settings backed by environment variables.

All runtime options are read from the environment (or a .env file loaded by
docker-compose). No secrets are hardcoded here.
"""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Monitor ---
    monitor_port: int = 8000
    poll_interval_seconds: int = 300
    feed_urls: str = (
        "https://ria.ru/export/rss2/archive/index.xml,"
        "https://tass.ru/rss/v2.xml,"
        "https://www.kommersant.ru/RSS/news.xml"
    )
    http_request_timeout_seconds: float = 20
    http_user_agent: str = "NewsPipelineMonitor/1.0 (+https://example.org)"
    dedup_db_path: str = "/data/seen.sqlite"

    # --- RabbitMQ ---
    rabbitmq_user: str = "guest"
    rabbitmq_pass: str = "guest"
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_queue: str = "news.incoming"

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_pass}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )

    # --- PostgreSQL ---
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "news"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- Worker ---
    ner_model: str = "ru_core_news_sm"
    ner_max_chars: int = 20000
    worker_max_retries: int = 3

    # --- Shared ---
    startup_retry_attempts: int = 10
    startup_retry_delay_seconds: float = 3.0
    log_level: str = "INFO"


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
