"""Read-only MCP server for on-call incident investigation.

The package exposes seven domain tools backed by the PostgreSQL instance of the
``lesson_4`` incident stand. It never writes to that database, never executes
user-supplied SQL, and speaks MCP over stdio only.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
