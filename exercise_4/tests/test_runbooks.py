"""Runbook path safety: allowlist, traversal, symlinks, size limits."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from oncall_mcp import repositories
from oncall_mcp.errors import DataSourceUnavailable, InvalidArguments, NotFound
from oncall_mcp.settings import Settings


@pytest.fixture
def runbook_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runbooks"
    directory.mkdir()
    (directory / "payments.md").write_text("# Runbook: payments\n", encoding="utf-8")
    return directory


def test_existing_runbook_is_returned(
    settings_factory: Callable[..., Settings], runbook_dir: Path
) -> None:
    settings = settings_factory(runbooks_dir=runbook_dir)

    result = repositories.read_runbook(settings, service="payments")

    assert result.item.service == "payments"
    assert result.item.path == "payments.md"
    assert result.item.content.startswith("# Runbook: payments")
    assert result.item.size_bytes == len(result.item.content.encode())


def test_missing_runbook_is_not_found(
    settings_factory: Callable[..., Settings], runbook_dir: Path
) -> None:
    settings = settings_factory(runbooks_dir=runbook_dir)

    with pytest.raises(NotFound, match="no runbook for service 'checkout'"):
        repositories.read_runbook(settings, service="checkout")


@pytest.mark.parametrize(
    "service",
    [
        "../payments",
        "../../etc/passwd",
        "/etc/passwd",
        "payments/../../etc/passwd",
        "payments.md",
        "payments-api",
        "",
        "PAYMENTS",
    ],
)
def test_traversal_and_unknown_names_are_invalid(
    settings_factory: Callable[..., Settings], runbook_dir: Path, service: str
) -> None:
    settings = settings_factory(runbooks_dir=runbook_dir)

    with pytest.raises(InvalidArguments):
        repositories.read_runbook(settings, service=service)


def test_symlinked_runbook_is_rejected(
    settings_factory: Callable[..., Settings], runbook_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside.md"
    secret.write_text("outside the runbooks directory", encoding="utf-8")
    (runbook_dir / "auth.md").symlink_to(secret)

    settings = settings_factory(runbooks_dir=runbook_dir)

    with pytest.raises(InvalidArguments, match="symlink"):
        repositories.read_runbook(settings, service="auth")


def test_oversized_runbook_is_rejected(
    settings_factory: Callable[..., Settings], runbook_dir: Path
) -> None:
    (runbook_dir / "auth.md").write_text("x" * 70_000, encoding="utf-8")

    settings = settings_factory(runbooks_dir=runbook_dir)

    with pytest.raises(InvalidArguments, match="maximum"):
        repositories.read_runbook(settings, service="auth")


def test_missing_runbook_directory_is_unavailable(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = settings_factory(runbooks_dir=tmp_path / "does-not-exist")

    with pytest.raises(DataSourceUnavailable, match="not available"):
        repositories.read_runbook(settings, service="payments")
