"""Domain errors mapped to MCP tool errors.

Every message here is static or derived from validated input, never from a
database exception, connection string, or file-system path. Details of the
underlying failure go to stderr only.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, caller-visible failures."""

    code: str = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_tool_message(self) -> str:
        return f"{self.code}: {self.message}"


class InvalidArguments(DomainError):
    """The caller supplied a value outside the documented contract."""

    code = "invalid_arguments"


class NotFound(DomainError):
    """A validated identifier does not exist in the data source."""

    code = "not_found"


class InvalidTransition(DomainError):
    """The requested state change is not allowed from the current state."""

    code = "invalid_transition"


class DataSourceUnavailable(DomainError):
    """Postgres or the runbook directory cannot be read right now."""

    code = "data_source_unavailable"
