from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

_UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")


def sanitize_storage(value: Any) -> Any:
    """Keep text representable in PostgreSQL text and JSONB values."""
    if isinstance(value, str):
        return _UNSTORABLE_TEXT.sub("", value)
    if isinstance(value, dict):
        return {sanitize_storage(k): sanitize_storage(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(sanitize_storage(item) for item in value)
    return value


class DatabaseMigrator:
    def __init__(
        self, database_url: str, migrations_root: Path, embedding_dimensions: int
    ) -> None:
        self.database_url = database_url
        self.migrations_root = migrations_root
        self.embedding_dimensions = embedding_dimensions

    def migrate(self) -> list[str]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install database dependencies with: pip install -e '.[db]'"
            ) from exc

        applied: list[str] = []
        paths = sorted(self.migrations_root.glob("[0-9][0-9][0-9]_*.sql"))
        if not paths:
            raise RuntimeError(f"No migrations found in {self.migrations_root}")

        with psycopg.connect(
            self.database_url, connect_timeout=database_connect_timeout()
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('repomonster:migrations'))"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute("SELECT version, checksum FROM schema_migrations")
                existing: dict[str, str] = dict(cursor.fetchall())

                missing = existing.keys() - {
                    path.name.split("_", 1)[0] for path in paths
                }
                if missing:
                    raise RuntimeError(
                        "Applied migrations missing from disk: "
                        + ", ".join(sorted(missing))
                    )
                cursor.execute("SELECT to_regclass('application_settings')")
                settings_table = cursor.fetchone()
                if settings_table and settings_table[0] is not None:
                    cursor.execute(
                        "SELECT value FROM application_settings WHERE key = 'embedding_dimensions'"
                    )
                    dimensions = cursor.fetchone()
                    if dimensions and int(dimensions[0]) != self.embedding_dimensions:
                        raise RuntimeError(
                            "EMBEDDING_DIMENSIONS differs from the initialized database; "
                            "the database needs recreating or reindexing before migration."
                        )

                for path in paths:
                    version = path.name.split("_", 1)[0]
                    raw_sql = path.read_text()
                    rendered_sql = raw_sql.replace(
                        "__EMBEDDING_DIMENSIONS__", str(self.embedding_dimensions)
                    )
                    checksum = hashlib.sha256(rendered_sql.encode()).hexdigest()
                    if version in existing:
                        if existing[version] != checksum:
                            raise RuntimeError(
                                f"Applied migration {version} has been modified"
                            )
                        continue
                    cursor.execute(rendered_sql)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
                    applied.append(path.name)
        return applied


def database_connect_timeout() -> int:
    timeout = int(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "10"))
    if timeout <= 0:
        raise ValueError("DATABASE_CONNECT_TIMEOUT_SECONDS must be positive")
    return timeout


def project_root() -> Path:
    return Path(os.getenv("REPOMONSTER_ROOT", Path(__file__).resolve().parents[2]))
