# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Operator-controlled retention maintenance with `repomonster prune`, configurable
  through `--days` or `RETENTION_DAYS` (default 90).

- Open-source project governance: `LICENSE` (MIT), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue templates, and a protected `main`
  branch ruleset.

### Changed

- Split the GitHub adapter into client, processor and Check Run formatting modules,
  preserving existing public imports and behavior.

- Check Runs publish stable processing error codes and display finding text safely;
  full diagnostics stay in operator records. Direct review responses omit model briefs.
- Review request storage omits diff excerpts and normalizes text for PostgreSQL.
  Retrieval uses only the active embedding model, and repository/task documents
  have a single enabled version. Migration checks identify dimension mismatches
  and missing applied files.
- Worker shutdown returns unfinished work before model calls without spending an
  attempt. Installation tokens use explicit permissions, disabled identities stay
  disabled, and model responses require a completed final answer.
- Application containers use read-only filesystems, restricted capabilities, and
  configurable memory limits.

- API and worker database components share one bounded, health-checked connection
  pool per process and one review-service factory. Added configurable pool sizes,
  checkout, connect and statement timeouts. Pools open without waiting for database
  availability and close at shutdown; workers require at least two connections for
  heartbeats. Management commands retain dedicated connections without the pooled
  statement timeout, and `/readyz` uses a public repository method.

- GitHub and embedding requests share a redirect-rejecting HTTP client with
  bounded retries. Check Run creation is not retried within a queue attempt.
  Added `HTTP_MAX_RETRIES`, `EMBEDDING_TIMEOUT_SECONDS` and
  `EMBEDDING_BATCH_SIZE`; embedding requests are batched in input order.

- Ticket-style references such as `AB-1` no longer satisfy the task-reference rule
  unless a repository sets `checks.allow_unverified_tickets: true`.
- A model failure now shows as `blocked` when deterministic errors exist, instead of
  `manual_escalation`.

### Added

- `checks.required_ci` entries can pin a check to a GitHub App with `name` and
  `app_id`.

### Fixed

- Added resource limits: request bodies are rejected while they arrive, `/review`
  authenticates before parsing, slow database and model calls no longer stall the
  API, embedding work is capped per file and per delivery, a burst of edits to one
  pull request costs one review, and `.repomonster.yml` is size-limited and cannot
  use YAML aliases.
- Retrieval now filters before ranking and gives each knowledge scope a fair share.
  The model brief separates untrusted text from guidance and states when it has not
  seen a full diff.
- Removed the unauthenticated `/webhooks/gitlab` route until GitLab support ships.
  GitLab event normalization code remains available; HTTP requests return 404.
- Several workers can now run safely. A worker that lost its job lock can no longer
  overwrite newer results, jobs for one repository run one at a time, and embedding
  calls no longer hold database transactions open. Stop workers before applying
  migration 004.
- Tightened required CI evidence handling and pagination, repository configuration
  validation, profile selection, issue-reference parsing, and changed-file
  classification. Permanent repository configuration problems now receive an
  actionable result on the first attempt.
- Review retries now back off for about 45 minutes instead of 30 seconds and honour
  `Retry-After`. `WEBHOOK_MAX_ATTEMPTS` now defaults to 8.
- Failed reviews always publish a result, even after a worker crash or GitHub
  outage. Workers survive database outages, and a crash after publishing no longer
  reviews the change again.
- Pull requests whose required CI is still running or has not started now wait
  instead of being blocked, and are re-reviewed when the checks finish. The wait
  ends in `manual_escalation` after `CI_WAIT_TIMEOUT_SECONDS`. GitHub Apps must
  subscribe to the check run and status events.
- `manual_escalation` and terminal review failures now publish `action_required`,
  because GitHub treats `neutral` as passing for required checks.
- GitHub webhooks no longer fail with a 422: FastAPI treated the `request`
  argument as a required query parameter, so no delivery reached verification.

### Changed

- Recorded MIT licensing in `pyproject.toml` project metadata.
- Locked runtime and development dependencies in `uv.lock`; CI installs frozen
  dependencies and the runtime image installs hash-verified dependencies without
  development tools or uv. Pinned Python and PostgreSQL/pgvector images by digest
  and GitHub Actions by commit SHA, with weekly Dependabot updates.
- Raised the supported Python floor to 3.12 and pinned development tooling used
  by CI.

## [0.1.0]

### Added

- Self-hosted readiness gate for GitHub pull requests through a GitHub App.
- Signed webhook verification, delivery deduplication, and durable PostgreSQL
  jobs with retries, backoff, and stale-job recovery.
- Short-lived GitHub App installation authentication scoped to one repository.
- Trusted default-branch `.repomonster.yml` policy loading.
- pgvector-backed repository and standard-pack retrieval with namespace
  isolation.
- Deterministic description, traceability, testing, CI, change-size, and
  documentation checks.
- Optional OpenAI-compatible insight review with fail-closed
  `manual_escalation`.
- GitHub Check Run publication, a direct review API, and an offline local
  simulation CLI.

[Unreleased]: https://github.com/leotemtem/RepoMonster/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/leotemtem/RepoMonster/releases/tag/v0.1.0
