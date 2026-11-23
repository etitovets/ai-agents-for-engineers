"""Runtime configuration, loaded from the environment or a local ``.env``.

Only ``STAND_DATABASE_URL`` is secret. It is held in a ``SecretStr``, validated
for shape, and never logged; :meth:`Settings.redacted_dsn` is the only string
form intended for diagnostics.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNBOOKS_DIR = PROJECT_ROOT / "lesson_4" / "runbooks"

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """Startup configuration is missing or malformed.

    The message is safe to print: it names fields and problems, never values.
    """


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", Path.cwd() / ".env"),
        env_file_encoding="utf-8",
        env_prefix="ONCALL_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    database_url: SecretStr = Field(
        alias="STAND_DATABASE_URL",
        description="PostgreSQL DSN for the incident stand (host side).",
    )

    runbooks_dir: Path = Field(
        default=DEFAULT_RUNBOOKS_DIR,
        description="Directory holding <service>.md runbooks.",
    )

    pool_min_size: int = Field(default=1, ge=0, le=100)
    pool_max_size: int = Field(default=5, ge=1, le=100)
    pool_acquire_timeout: float = Field(default=5.0, gt=0, le=300)
    db_connect_timeout: float = Field(default=5.0, gt=0, le=300)
    statement_timeout_ms: int = Field(default=5_000, ge=100, le=120_000)

    @field_validator("database_url")
    @classmethod
    def _validate_dsn(cls, value: SecretStr) -> SecretStr:
        dsn = value.get_secret_value().strip()
        if not dsn:
            raise ValueError("must not be empty")
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("must be a postgresql:// URL")
        # urlsplit raises on malformed IPv6 brackets; treat that as invalid too.
        try:
            parts = urlsplit(dsn)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError("is not a parseable URL") from exc
        if not parts.hostname:
            raise ValueError("must include a host")
        return SecretStr(dsn)

    @field_validator("runbooks_dir")
    @classmethod
    def _absolute_runbooks_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @model_validator(mode="after")
    def _pool_sizes(self) -> Settings:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size must not exceed pool_max_size")
        return self

    @property
    def redacted_dsn(self) -> str:
        """DSN with the password replaced; safe for logs and error messages."""

        parts = urlsplit(self.database_url.get_secret_value())
        netloc = parts.netloc
        if "@" in netloc:
            credentials, host = netloc.rsplit("@", 1)
            user = credentials.split(":", 1)[0]
            netloc = f"{user}:***@{host}"
        return urlunsplit(parts._replace(netloc=netloc))

    def describe(self) -> str:
        return (
            f"dsn={self.redacted_dsn} pool={self.pool_min_size}-{self.pool_max_size} "
            f"runbooks={self.runbooks_dir}"
        )


def _summarize_errors(exc: ValidationError) -> str:
    """Render validation errors without echoing any input value."""

    problems = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "settings"
        problems.append(f"{location}: {error['msg']}")
    return "; ".join(problems) or "invalid configuration"


def load_settings(*, env_file: Sequence[str | Path] | None = None) -> Settings:
    """Build :class:`Settings` or raise a value-free :class:`ConfigurationError`.

    ``env_file`` overrides the model's default dotenv locations; pass ``()`` to
    ignore dotenv files entirely (used by tests).
    """

    overrides: dict[str, object] = {} if env_file is None else {"_env_file": env_file}
    try:
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ConfigurationError(_summarize_errors(exc)) from None
    logger.info("configuration loaded: %s", settings.describe())
    return settings
