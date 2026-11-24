"""Configuration and input-contract tests."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from incident_mcp.models import (
    DEFAULT_WINDOW,
    MAX_LOG_WINDOW,
    TimeRange,
    resolve_window,
)
from incident_mcp.settings import ConfigurationError, Settings, load_settings
from tests.conftest import PROJECT_ROOT

VALID_DSN = "postgresql://alice:s3cret@db.example:5432/oncall"


def test_missing_dsn_is_a_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAND_DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError) as caught:
        load_settings(env_file=())

    message = str(caught.value)
    assert "STAND_DATABASE_URL" in message
    assert "postgresql://" not in message


@pytest.mark.parametrize(
    "dsn",
    [
        "mysql://alice:s3cret@db/oncall",
        "db.example:5432",
        "   ",
        "postgresql:///oncall",
    ],
)
def test_malformed_dsn_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, dsn: str
) -> None:
    monkeypatch.setenv("STAND_DATABASE_URL", dsn)

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    message = str(caught.value)
    assert "s3cret" not in message
    if dsn.strip():
        assert dsn.strip() not in message


def test_dsn_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAND_DATABASE_URL", VALID_DSN)
    settings = load_settings()
    assert settings.database_url.get_secret_value() == VALID_DSN


def test_secret_never_appears_in_diagnostics() -> None:
    settings = Settings(database_url=VALID_DSN)

    assert "s3cret" not in settings.redacted_dsn
    assert "s3cret" not in settings.describe()
    assert settings.redacted_dsn == "postgresql://alice:***@db.example:5432/oncall"


def test_pool_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=VALID_DSN, pool_min_size=10, pool_max_size=2)

    with pytest.raises(ValidationError):
        Settings(database_url=VALID_DSN, pool_max_size=0)


def test_relative_runbooks_dir_is_resolved_against_the_project() -> None:
    settings = Settings(database_url=VALID_DSN, runbooks_dir="lesson_4/runbooks")
    assert settings.runbooks_dir == PROJECT_ROOT / "lesson_4" / "runbooks"


# --- time range contract ----------------------------------------------------


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TimeRange(
            start=datetime(2026, 7, 28, 9, 40),
            end=datetime(2026, 7, 28, 9, 45),
        )


def test_start_must_precede_end() -> None:
    moment = datetime(2026, 7, 28, 9, 40, tzinfo=UTC)
    with pytest.raises(ValidationError, match="start must be earlier than end"):
        TimeRange(start=moment, end=moment)


def test_time_range_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TimeRange.model_validate(
            {
                "start": "2026-07-28T09:40:00Z",
                "end": "2026-07-28T09:45:00Z",
                "timezone": "UTC",
            }
        )


def test_offsets_are_normalized_to_utc() -> None:
    window = TimeRange.model_validate(
        {"start": "2026-07-28T12:40:00+03:00", "end": "2026-07-28T12:45:00+03:00"}
    )
    assert window.utc() == (
        datetime(2026, 7, 28, 9, 40, tzinfo=UTC),
        datetime(2026, 7, 28, 9, 45, tzinfo=UTC),
    )


def test_default_window_is_the_last_five_minutes() -> None:
    now = datetime(2026, 7, 28, 9, 45, tzinfo=UTC)
    assert resolve_window(None, MAX_LOG_WINDOW, now=now) == (
        now - DEFAULT_WINDOW,
        now,
    )
    assert timedelta(minutes=5) == DEFAULT_WINDOW


# --- repository hygiene -----------------------------------------------------


def test_env_is_gitignored_but_example_is_not() -> None:
    def ignored(path: str) -> bool:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", path],
                cwd=PROJECT_ROOT,
                capture_output=True,
            ).returncode
            == 0
        )

    if (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=PROJECT_ROOT,
            capture_output=True,
        ).returncode
        != 0
    ):
        pytest.skip("not a git working tree")

    assert ignored(".env") is True
    assert ignored(".env.example") is False
