from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING

from .database import database_connect_timeout

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool


def build_connection_pool(database_url: str, *, worker: bool = False) -> ConnectionPool:
    """Build one process-owned pool; its caller opens and closes it."""
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Install database dependencies with: pip install -e '.[db]'"
        ) from exc

    min_size = int(os.getenv("DATABASE_POOL_MIN_SIZE", "1"))
    max_size = int(os.getenv("DATABASE_POOL_MAX_SIZE", "5"))
    if worker and max_size < 2:
        raise ValueError("DATABASE_POOL_MAX_SIZE must be at least 2 in the worker")
    timeout = float(os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "10"))
    statement_timeout = float(os.getenv("DATABASE_STATEMENT_TIMEOUT_SECONDS", "30"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DATABASE_POOL_TIMEOUT_SECONDS must be positive and finite")
    if not math.isfinite(statement_timeout) or statement_timeout < 0.001:
        raise ValueError(
            "DATABASE_STATEMENT_TIMEOUT_SECONDS must be at least 0.001 and finite"
        )

    def configure(connection: Connection) -> None:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (str(int(statement_timeout * 1000)),),
        )
        connection.commit()

    return ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        check=ConnectionPool.check_connection,
        configure=configure,
        kwargs={
            "connect_timeout": database_connect_timeout(),
            "application_name": "repomonster-worker" if worker else "repomonster-api",
        },
    )
