from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


class LostJobLock(RuntimeError):
    """Another worker now owns this job, so this worker must stop working on it."""


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
    lock_token: str | None = None


class WebhookQueue:
    """Durable PostgreSQL queue for short webhook requests and slow reviews."""

    def __init__(
        self,
        pool: ConnectionPool,
        max_attempts: int = 5,
        lock_timeout_seconds: int = 900,
        retry_base_seconds: float = 15,
        retry_max_seconds: float = 900,
    ) -> None:
        self.pool = pool
        self.max_attempts = max_attempts
        self.lock_timeout = timedelta(seconds=lock_timeout_seconds)
        self.heartbeat_seconds = lock_timeout_seconds / 3
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    def enqueue(
        self,
        *,
        provider: str,
        delivery_id: str,
        event_name: str,
        payload: dict[str, Any],
        delay_seconds: int = 0,
    ) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO webhook_deliveries (
                    provider, delivery_id, event_name, payload, serial_key, available_at
                ) VALUES (
                    %s, %s, %s, %s::jsonb, %s, now() + make_interval(secs => %s)
                )
                ON CONFLICT (provider, delivery_id) DO NOTHING
                RETURNING id
                """,
                (
                    provider,
                    delivery_id,
                    event_name,
                    json.dumps(payload),
                    _serial_key(payload),
                    delay_seconds,
                ),
            )
            return cursor.fetchone() is not None

    def claim(self) -> WebhookJob | None:
        import psycopg

        try:
            return self._claim()
        except psycopg.errors.UniqueViolation:
            # Another worker just started a job for this repository.
            return None

    def _claim(self) -> WebhookJob | None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, provider, delivery_id, event_name, payload, attempts,
                       external_result_id, last_error
                FROM webhook_deliveries AS job
                WHERE (
                    (status = 'pending' AND available_at <= now())
                    OR (status = 'processing' AND locked_at <= now() - %s)
                ) AND NOT EXISTS (
                    SELECT 1 FROM webhook_deliveries AS running
                    WHERE running.status = 'processing'
                      AND running.provider = job.provider
                      AND running.serial_key = job.serial_key
                      AND running.id <> job.id
                )
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
            lock_token = str(uuid.uuid4())
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'processing', locked_at = now(), updated_at = now(),
                    lock_token = %s, attempts = attempts + %s,
                    last_error = CASE WHEN %s THEN last_error END
                WHERE id = %s
                """,
                (lock_token, 0 if exhausted else 1, exhausted, row[0]),
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
                lock_token=lock_token,
            )

    def _update_owned(self, job: WebhookJob, assignments: str, *values: Any) -> bool:
        """Apply a change only if this worker still owns the job."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE webhook_deliveries
                SET {assignments}, updated_at = now()
                WHERE id = %s AND status = 'processing' AND lock_token = %s
                """,  # nosec B608
                (*values, job.id, job.lock_token),
            )
            return cursor.rowcount == 1

    def heartbeat(self, job: WebhookJob) -> bool:
        return self._update_owned(job, "locked_at = now()")

    def verify_lock(self, job: WebhookJob) -> None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM webhook_deliveries
                WHERE id = %s AND status = 'processing' AND lock_token = %s
                """,
                (job.id, job.lock_token),
            )
            if cursor.fetchone() is None:
                raise LostJobLock(f"delivery {job.delivery_id} has a new owner")

    def has_newer_pull_request_delivery(self, job: WebhookJob) -> bool:
        """Report whether a later delivery will review the same pull request anyway."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM webhook_deliveries
                WHERE provider = %s AND serial_key = %s AND status = 'pending'
                  AND event_name = 'pull_request' AND id > %s
                  AND payload ->> 'number' = %s
                LIMIT 1
                """,
                (
                    job.provider,
                    _serial_key(job.payload),
                    job.id,
                    str(job.payload.get("number")),
                ),
            )
            return cursor.fetchone() is not None

    def complete(self, job: WebhookJob, *, ignored: bool = False) -> bool:
        return self._update_owned(
            job,
            "status = %s, locked_at = NULL, lock_token = NULL",
            "ignored" if ignored else "completed",
        )

    def fail(self, job: WebhookJob, error: Exception) -> bool:
        """Record a failure and return True when no retries remain.

        The row stays pending either way. The status only becomes `failed` once the
        failure has been published, so a crash here cannot leave a stuck Check Run.
        """
        exhausted = job.attempts >= self.max_attempts
        delay = 0.0 if exhausted else self._retry_delay(job.attempts, error)
        self._update_owned(
            job,
            """status = 'pending',
                    available_at = now() + make_interval(secs => %s),
                    locked_at = NULL, lock_token = NULL, last_error = %s""",
            delay,
            f"{type(error).__name__}: {error}"[:4000],
        )
        return exhausted

    def defer_publication(self, job: WebhookJob) -> None:
        """Try again later to publish a failure, after the provider has recovered."""
        self._update_owned(
            job,
            """status = 'pending',
                    available_at = now() + make_interval(secs => %s),
                    locked_at = NULL, lock_token = NULL""",
            self.retry_max_seconds,
        )

    def mark_failed(self, job: WebhookJob) -> None:
        self._update_owned(
            job, "status = 'failed', locked_at = NULL, lock_token = NULL"
        )

    def _retry_delay(self, attempts: int, error: Exception) -> float:
        delay = min(self.retry_max_seconds, self.retry_base_seconds * 2**attempts)
        retry_after = getattr(error, "retry_after_seconds", None)
        return max(delay, min(retry_after, 3600)) if retry_after else delay

    def set_external_result_id(self, job: WebhookJob, external_result_id: str) -> None:
        if not self._update_owned(job, "external_result_id = %s", external_result_id):
            raise LostJobLock(f"delivery {job.delivery_id} has a new owner")


def _serial_key(payload: dict[str, Any]) -> str | None:
    repository_id = (payload.get("repository") or {}).get("id")
    return str(repository_id) if repository_id else None
