from __future__ import annotations

import hashlib
import os
from pathlib import Path


class DatabaseMigrator:
    def __init__(self, database_url: str, migrations_root: Path, embedding_dimensions: int) -> None:
        self.database_url = database_url
        self.migrations_root = migrations_root
        self.embedding_dimensions = embedding_dimensions

    def migrate(self) -> list[str]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install database dependencies with: pip install -e '.[db]'") from exc

        applied: list[str] = []
        paths = sorted(self.migrations_root.glob("[0-9][0-9][0-9]_*.sql"))
        if not paths:
            raise RuntimeError(f"No migrations found in {self.migrations_root}")

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('repomonster:migrations'))")
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

                for path in paths:
                    version = path.name.split("_", 1)[0]
                    raw_sql = path.read_text()
                    rendered_sql = raw_sql.replace(
                        "__EMBEDDING_DIMENSIONS__", str(self.embedding_dimensions)
                    )
                    checksum = hashlib.sha256(rendered_sql.encode()).hexdigest()
                    if version in existing:
                        if existing[version] != checksum:
                            raise RuntimeError(f"Applied migration {version} has been modified")
                        continue
                    cursor.execute(rendered_sql)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
                    applied.append(path.name)
        return applied


def project_root() -> Path:
    return Path(os.getenv("REPOMONSTER_ROOT", Path(__file__).resolve().parents[2]))
