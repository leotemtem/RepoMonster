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
    # Attempts are used up: publish the failure instead of running the review again.
    exhausted: bool = False
    last_error: str | None = None


class WebhookQueue:
    """Durable PostgreSQL queue for short webhook requests and slow reviews."""

    def __init__(
        self,
        database_url: str,
        max_attempts: int = 5,
        lock_timeout_seconds: int = 900,
        retry_base_seconds: float = 15,
        retry_max_seconds: float = 900,
    ) -> None:
        self.database_url = database_url
        self.max_attempts = max_attempts
        self.lock_timeout = timedelta(seconds=lock_timeout_seconds)
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

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
        delay_seconds: int = 0,
    ) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO webhook_deliveries (
                    provider, delivery_id, event_name, payload, available_at
                ) VALUES (
                    %s, %s, %s, %s::jsonb, now() + make_interval(secs => %s)
                )
                ON CONFLICT (provider, delivery_id) DO NOTHING
                RETURNING id
                """,
                (provider, delivery_id, event_name, json.dumps(payload), delay_seconds),
            )
            return cursor.fetchone() is not None

    def claim(self) -> WebhookJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, provider, delivery_id, event_name, payload, attempts,
                       external_result_id, last_error
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
            # This also catches a job that keeps killing its worker, which never
            # reaches fail(). A claim to publish a failure keeps its error and count.
            exhausted = int(row[5]) >= self.max_attempts
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'processing', locked_at = now(), updated_at = now(),
                    attempts = attempts + %s,
                    last_error = CASE WHEN %s THEN last_error END
                WHERE id = %s
                """,
                (0 if exhausted else 1, exhausted, row[0]),
            )
            return WebhookJob(
                id=int(row[0]),
                provider=row[1],
                delivery_id=row[2],
                event_name=row[3],
                payload=dict(row[4]),
                attempts=int(row[5]) + (0 if exhausted else 1),
                external_result_id=row[6],
                exhausted=exhausted,
                last_error=row[7],
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
        """Record a failure and return True when no retries remain.

        The row stays pending either way. The status only becomes `failed` once the
        failure has been published, so a crash here cannot leave a stuck Check Run.
        """
        exhausted = job.attempts >= self.max_attempts
        delay = 0.0 if exhausted else self._retry_delay(job.attempts, error)
        message = f"{type(error).__name__}: {error}"[:4000]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending',
                    available_at = now() + make_interval(secs => %s),
                    locked_at = NULL, last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (delay, message, job.id),
            )
        return exhausted

    def defer_publication(self, job: WebhookJob) -> None:
        """Try again later to publish a failure, after the provider has recovered."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending',
                    available_at = now() + make_interval(secs => %s),
                    locked_at = NULL, updated_at = now()
                WHERE id = %s
                """,
                (self.retry_max_seconds, job.id),
            )

    def mark_failed(self, job_id: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'failed', locked_at = NULL, updated_at = now()
                WHERE id = %s
                """,
                (job_id,),
            )

    def _retry_delay(self, attempts: int, error: Exception) -> float:
        delay = min(self.retry_max_seconds, self.retry_base_seconds * 2**attempts)
        retry_after = getattr(error, "retry_after_seconds", None)
        return max(delay, min(retry_after, 3600)) if retry_after else delay

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
