from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any


@dataclass(slots=True)
class WebhookJob:
    id: int
    provider: str
    delivery_id: str
    event_name: str
    payload: dict[str, Any]
    attempts: int
    external_result_id: str | None = None


class WebhookQueue:
    """Durable PostgreSQL queue for short webhook requests and slow reviews."""

    def __init__(
        self,
        database_url: str,
        max_attempts: int = 5,
        lock_timeout_seconds: int = 900,
    ) -> None:
        self.database_url = database_url
        self.max_attempts = max_attempts
        self.lock_timeout = timedelta(seconds=lock_timeout_seconds)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install database dependencies with: pip install -e '.[db]'"
            ) from exc
        return psycopg.connect(self.database_url)

    def enqueue(
        self,
        *,
        provider: str,
        delivery_id: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO webhook_deliveries (
                    provider, delivery_id, event_name, payload
                ) VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (provider, delivery_id) DO NOTHING
                RETURNING id
                """,
                (provider, delivery_id, event_name, json.dumps(payload)),
            )
            return cursor.fetchone() is not None

    def claim(self) -> WebhookJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, provider, delivery_id, event_name, payload, attempts,
                       external_result_id
                FROM webhook_deliveries
                WHERE (status = 'pending' AND available_at <= now())
                   OR (status = 'processing' AND locked_at <= now() - %s)
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (self.lock_timeout,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'processing', attempts = attempts + 1,
                    locked_at = now(), updated_at = now(), last_error = NULL
                WHERE id = %s
                """,
                (row[0],),
            )
            return WebhookJob(
                id=int(row[0]),
                provider=row[1],
                delivery_id=row[2],
                event_name=row[3],
                payload=dict(row[4]),
                attempts=int(row[5]) + 1,
                external_result_id=row[6],
            )

    def complete(self, job_id: int, *, ignored: bool = False) -> None:
        status = "ignored" if ignored else "completed"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = %s, locked_at = NULL, updated_at = now()
                WHERE id = %s
                """,
                (status, job_id),
            )

    def fail(self, job: WebhookJob, error: Exception) -> bool:
        """Record a failure and return True when no retries remain."""
        exhausted = job.attempts >= self.max_attempts
        delay = min(300, 2**job.attempts)
        message = f"{type(error).__name__}: {error}"[:4000]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = %s, available_at = %s, locked_at = NULL,
                    last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (
                    "failed" if exhausted else "pending",
                    _utc_now(connection) + timedelta(seconds=delay),
                    message,
                    job.id,
                ),
            )
        return exhausted

    def set_external_result_id(self, job_id: int, external_result_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET external_result_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (external_result_id, job_id),
            )


def _utc_now(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT now()")
        return cursor.fetchone()[0]
