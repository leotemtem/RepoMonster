# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Open-source project governance: `LICENSE` (MIT), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue templates, and a protected `main`
  branch ruleset.

### Changed

- Ticket-style references such as `AB-1` no longer satisfy the task-reference rule
  unless a repository sets `checks.allow_unverified_tickets: true`.
- A model failure now shows as `blocked` when deterministic errors exist, instead of
  `manual_escalation`.

### Added

- `checks.required_ci` entries can pin a check to a GitHub App with `name` and
  `app_id`.

### Fixed

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
