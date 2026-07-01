from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import threading
from pathlib import Path

from .config import load_repository_config
from .database import DatabaseMigrator, project_root
from .knowledge import RepositoryKnowledgeSynchronizer, RepositorySource
from .github import GitHubClient, GitHubReviewProcessor, GitHubSettings
from .insights import OpenAICompatibleInsightProvider
from .jobs import WebhookQueue
from .packs import PackSynchronizer, discover_packs
from .repository import OpenAICompatibleEmbeddingProvider, PostgresStandardsRepository
from .service import ReviewService


def _connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install database dependencies with: pip install -e '.[db]'") from exc
    return psycopg.connect(os.environ["DATABASE_URL"])


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
        changed = PackSynchronizer(
            connection, provider
        ).sync(packs, force=force)
    print("standard packs updated: " + (", ".join(changed) if changed else "none"))
    return 0


def _sync_local_repository(args) -> int:
    checkout = args.checkout.resolve()
    config_path = checkout / ".repomonster.yml"
    if not config_path.is_file():
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
                if not path.is_file():
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


def _worker() -> int:
    root = project_root()
    database_url = os.environ["DATABASE_URL"]
    embedding_provider = OpenAICompatibleEmbeddingProvider.from_environment()
    insight_provider = (
        OpenAICompatibleInsightProvider.from_environment()
        if os.getenv("INSIGHT_BASE_URL")
        else None
    )
    review_service = ReviewService(
        PostgresStandardsRepository(
            database_url=database_url,
            embedding_provider=embedding_provider,
            profiles_root=root / "profiles",
            tenant_key=os.getenv("TENANT_KEY"),
        ),
        insight_provider=insight_provider,
    )
    queue = WebhookQueue(
        database_url,
        max_attempts=int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "5")),
        lock_timeout_seconds=int(os.getenv("WEBHOOK_LOCK_TIMEOUT_SECONDS", "900")),
    )
    processor = GitHubReviewProcessor(
        database_url=database_url,
        queue=queue,
        client=GitHubClient(GitHubSettings.from_environment()),
        embedding_provider=embedding_provider,
        review_service=review_service,
    )
    poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    stop = threading.Event()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("repomonster.worker")
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logger.info("GitHub review worker started")
    while not stop.is_set():
        job = queue.claim()
        if job is None:
            stop.wait(poll_seconds)
            continue
        try:
            processed = processor.process(job)
            queue.complete(job.id, ignored=not processed)
            logger.info("delivery %s completed", job.delivery_id)
        except Exception as exc:
            exhausted = queue.fail(job, exc)
            logger.exception(
                "delivery %s failed on attempt %s", job.delivery_id, job.attempts
            )
            if exhausted:
                processor.mark_terminal_failure(job, exc)
    logger.info("GitHub review worker stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repomonster")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate")
    standards = commands.add_parser("standards")
    standard_commands = standards.add_subparsers(dest="standards_command", required=True)
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
    parser.error("Unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
