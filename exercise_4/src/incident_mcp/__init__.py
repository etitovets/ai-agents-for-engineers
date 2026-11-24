"""incident-mcp: MCP server for on-call incident investigation.

The package exposes seven read-only diagnostic tools and two explicitly
authorized write tools backed by the PostgreSQL instance of the ``lesson_4``
incident stand. Writes are limited to ``incidents.status`` and
``incidents.summary``. The server never executes user-supplied SQL and speaks
MCP over stdio only.
"""

__all__ = ["__version__"]

__version__ = "0.2.0"
