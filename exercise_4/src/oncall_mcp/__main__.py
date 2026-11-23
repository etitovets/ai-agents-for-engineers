"""Entry point: ``uv run python -m oncall_mcp``.

stdout is reserved for MCP protocol frames. Every log record, including startup
failures, goes to stderr.
"""

from __future__ import annotations

import logging
import sys

from .server import create_server
from .settings import ConfigurationError, load_settings


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> int:
    configure_logging()
    logger = logging.getLogger("oncall_mcp")

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        # Names fields and problems only; never echoes a value.
        logger.error("configuration error: %s", exc)
        return 2

    server = create_server(settings)
    try:
        server.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        return 130
    except Exception as exc:
        # The lifespan already logged the cause; keep the terminal message short.
        logger.error("server stopped: %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
