from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from .config import load_repository_config
from .database import DatabaseMigrator, database_connect_timeout, project_root
from .factory import build_review_service
from .github import GitHubClient, GitHubReviewProcessor, GitHubSettings
from .jobs import LostJobLock, WebhookJob, WebhookQueue, WorkerStopping
from .knowledge import RepositoryKnowledgeSynchronizer, RepositorySource
from .packs import PackSynchronizer, discover_packs
from .pool import build_connection_pool
from .repository import OpenAICompatibleEmbeddingProvider


def _connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Install database dependencies with: pip install -e '.[db]'"
        ) from exc
    return psycopg.connect(
        os.environ["DATABASE_URL"], connect_timeout=database_connect_timeout()
    )


def _migrate() -> int:
    root = project_root()
    migrator = DatabaseMigrator(
        os.environ["DATABASE_URL"],
        root / "db" / "migrations",
        int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
    )
    applied = migrator.migrate()
    print("migrations applied: " + (", ".join(applied) if applied else "none"))
    return 0


def _sync(force: bool) -> int:
    root = project_root()
    packs = discover_packs(root / "standard-packs")
    if not packs:
        raise RuntimeError("No bundled standard packs found")
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT value FROM application_settings WHERE key = 'embedding_dimensions'"
            )
            configured_dimensions = int(cursor.fetchone()[0])
        provider = OpenAICompatibleEmbeddingProvider.from_environment()
        if provider.dimensions != configured_dimensions:
            raise RuntimeError(
                "EMBEDDING_DIMENSIONS does not match the initialized database "
                f"({provider.dimensions} != {configured_dimensions})"
            )
        changed = PackSynchronizer(connection, provider).sync(packs, force=force)
    print("standard packs updated: " + (", ".join(changed) if changed else "none"))
    return 0


def _sync_local_repository(args) -> int:
    checkout = args.checkout.resolve()
    config_path = checkout / ".repomonster.yml"
    if not config_path.resolve().is_relative_to(checkout) or not config_path.is_file():
        raise RuntimeError(f"Repository configuration not found: {config_path}")
    config_content = config_path.read_text()
    config = load_repository_config(config_content)
    sources: list[RepositorySource] = []
    seen: set[str] = set()
    source_groups = (
        ("description", config.knowledge.description),
        ("requirements", config.knowledge.requirements),
        ("standards", config.knowledge.standards),
    )
    for source_type, patterns in source_groups:
        for pattern in patterns:
            for path in sorted(checkout.glob(pattern)):
                if not path.resolve().is_relative_to(checkout) or not path.is_file():
                    continue
                relative = path.relative_to(checkout).as_posix()
                if relative in seen:
                    continue
                seen.add(relative)
                content = path.read_text()
                source_sha = hashlib.sha256(content.encode()).hexdigest()[:40]
                sources.append(
                    RepositorySource(
                        path=relative,
                        title=relative,
                        content=content,
                        source_sha=source_sha,
                        source_type=source_type,
                    )
                )

    provider_base_url = args.provider_base_url.rstrip("/")
    repository_key = f"{args.provider}:{provider_base_url}:{args.external_id}"
    config_sha = hashlib.sha256(config_content.encode()).hexdigest()[:40]
    provider = OpenAICompatibleEmbeddingProvider.from_environment()
    with _connect() as connection:
        repository_id = RepositoryKnowledgeSynchronizer(connection, provider).sync(
            provider=args.provider,
            provider_base_url=provider_base_url,
            external_id=args.external_id,
            full_name=args.full_name or checkout.name,
            repository_key=repository_key,
            default_branch=args.default_branch,
            config_sha=config_sha,
            config=config,
            sources=sources,
        )
    print(
        f"repository synchronized: id={repository_id} key={repository_key} "
        f"sources={len(sources)}"
    )
    return 0


def _prune(days: int) -> int:
    if days < 1:
        raise ValueError("Retention days must be positive")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM webhook_deliveries "
            "WHERE status IN ('completed', 'failed', 'ignored') "
            "AND updated_at < now() - make_interval(days => %s)",
            (days,),
        )
        deliveries = cursor.rowcount
        cursor.execute(
            "DELETE FROM review_runs WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )
        reviews = cursor.rowcount
    print(f"pruned: {deliveries} deliveries, {reviews} reviews")
    return 0


class _Heartbeat:
    """Keep a job's lock fresh while this worker is alive and working on it."""

    def __init__(
        self, queue: WebhookQueue, job: WebhookJob, logger: logging.Logger
    ) -> None:
        self.queue = queue
        self.job = job
        self.logger = logger
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _Heartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.done.set()
        self.thread.join()

    def _run(self) -> None:
        while not self.done.wait(self.queue.heartbeat_seconds):
            try:
                if not self.queue.heartbeat(self.job):
                    return
            except Exception:
                self.logger.exception(
                    "could not renew the lock for delivery %s", self.job.delivery_id
                )


def _retry_on_database_error(
    operation: Callable[[], Any], stop: threading.Event, logger: logging.Logger
) -> Any:
    """Keep the worker alive through a database outage or failover."""
    import psycopg

    delay = 1.0
    while True:
        try:
            return operation()
        except psycopg.OperationalError:
            # Includes psycopg_pool.PoolTimeout when no connection is available.
            logger.exception("database unavailable; retrying in %s seconds", delay)
            if stop.wait(delay):
                return None
            delay = min(delay * 2, 30.0)


def _publish_terminal_failure(
    job: WebhookJob,
    queue: WebhookQueue,
    processor: GitHubReviewProcessor,
    stop: threading.Event,
    logger: logging.Logger,
) -> None:
    try:
        processor.mark_terminal_failure(
            job, job.last_error or "The worker stopped before finishing this review."
        )
    except LostJobLock:
        logger.warning("delivery %s lost its lock; stopping", job.delivery_id)
        return
    except Exception:
        logger.exception("could not publish failure for delivery %s", job.delivery_id)
        _retry_on_database_error(partial(queue.defer_publication, job), stop, logger)
        return
    _retry_on_database_error(partial(queue.mark_failed, job), stop, logger)
    logger.error("delivery %s failed permanently", job.delivery_id)


def _worker() -> int:
    pool = build_connection_pool(os.environ["DATABASE_URL"], worker=True)
    embedding_provider = OpenAICompatibleEmbeddingProvider.from_environment()
    review_service = build_review_service(pool, embedding_provider)
    queue = WebhookQueue(
        pool,
        max_attempts=int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "8")),
        lock_timeout_seconds=int(os.getenv("WEBHOOK_LOCK_TIMEOUT_SECONDS", "900")),
        retry_base_seconds=float(os.getenv("WEBHOOK_RETRY_BASE_SECONDS", "15")),
        retry_max_seconds=float(os.getenv("WEBHOOK_RETRY_MAX_SECONDS", "900")),
    )
    stop = threading.Event()
    processor = GitHubReviewProcessor(
        pool=pool,
        queue=queue,
        client=GitHubClient(GitHubSettings.from_environment()),
        embedding_provider=embedding_provider,
        review_service=review_service,
        stop=stop,
    )
    poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("repomonster.worker")
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    pool.open(wait=False)
    try:
        logger.info("GitHub review worker started")
        while not stop.is_set():
            job = _retry_on_database_error(queue.claim, stop, logger)
            if job is None:
                stop.wait(poll_seconds)
                continue
            if job.exhausted:
                _publish_terminal_failure(job, queue, processor, stop, logger)
                continue
            try:
                with _Heartbeat(queue, job, logger):
                    processed = processor.process(job)
            except LostJobLock:
                logger.warning("delivery %s lost its lock; stopping", job.delivery_id)
                continue
            except WorkerStopping:
                _retry_on_database_error(partial(queue.release, job), stop, logger)
                logger.info(
                    "delivery %s returned to pending for shutdown", job.delivery_id
                )
                continue
            except Exception as exc:
                logger.exception(
                    "delivery %s failed on attempt %s", job.delivery_id, job.attempts
                )
                _retry_on_database_error(partial(queue.fail, job, exc), stop, logger)
                continue
            _retry_on_database_error(
                partial(queue.complete, job, ignored=not processed), stop, logger
            )
            logger.info("delivery %s completed", job.delivery_id)
    finally:
        pool.close()
    logger.info("GitHub review worker stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repomonster")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate")
    standards = commands.add_parser("standards")
    standard_commands = standards.add_subparsers(
        dest="standards_command", required=True
    )
    standard_commands.add_parser("sync")
    standard_commands.add_parser("reindex")
    repository = commands.add_parser("repository")
    repository_commands = repository.add_subparsers(
        dest="repository_command", required=True
    )
    sync_local = repository_commands.add_parser("sync-local")
    sync_local.add_argument("checkout", type=Path)
    sync_local.add_argument("--provider", choices=("github", "gitlab"), required=True)
    sync_local.add_argument("--provider-base-url", required=True)
    sync_local.add_argument("--external-id", required=True)
    sync_local.add_argument("--full-name")
    sync_local.add_argument("--default-branch", default="main")
    commands.add_parser("bootstrap")
    commands.add_parser("worker")
    prune = commands.add_parser("prune")
    prune.add_argument(
        "--days", type=int, default=int(os.getenv("RETENTION_DAYS", "90"))
    )
    args = parser.parse_args(argv)

    if args.command == "db" and args.db_command == "migrate":
        return _migrate()
    if args.command == "standards":
        return _sync(force=args.standards_command == "reindex")
    if args.command == "repository" and args.repository_command == "sync-local":
        return _sync_local_repository(args)
    if args.command == "bootstrap":
        _migrate()
        return _sync(force=False)
    if args.command == "worker":
        return _worker()
    if args.command == "prune":
        return _prune(args.days)
    parser.error("Unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
