"""Standalone read-only status API (EC2 sidecar).

The routing and SQL live in :mod:`web`, which ``main.py`` can also start
in-process (that is how a single Render service serves both the bot and the
dashboard). This entry point keeps the original loopback-only sidecar used by
``docker-compose.yml``:

    LEDGER_PATH=/data/ledger.db API_HOST=127.0.0.1 API_PORT=8099 \\
        python dashboard_api.py

It is deliberately narrow: GET/HEAD only, ``query_only`` SQLite connections,
no order endpoint, no wallet access, and it never reads a private key.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import web

LEDGER_PATH = Path(os.getenv("LEDGER_PATH", "/data/ledger.db"))


def build_status(limit: int = web.DEFAULT_LIMIT) -> dict[str, Any]:
    """Backwards-compatible wrapper around :func:`web.build_status`."""
    return web.build_status(LEDGER_PATH, limit)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server, thread = web.start_status_server(LEDGER_PATH)
    logger = logging.getLogger("dashboard_api")
    logger.info("Serving on %s (Ctrl-C to stop)", server.server_address[:2])
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    finally:
        web.stop_status_server(server, None)


if __name__ == "__main__":
    main()
