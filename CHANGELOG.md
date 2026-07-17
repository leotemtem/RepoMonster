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

- Recorded MIT licensing in `pyproject.toml` project metadata.

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
