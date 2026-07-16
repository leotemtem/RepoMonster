# Contributing to RepoMonster

Thanks for your interest in RepoMonster. This document explains how to set up a
development environment, the checks your change must pass, and the boundaries the
project deliberately keeps.

RepoMonster is a self-hosted readiness gate for pull requests. Please read the
[README](README.md) for the product scope and boundaries and
[`docs/architecture.md`](docs/architecture.md) for trust boundaries and
architecture before proposing changes.

## Ways to contribute

- Report bugs and request features through the [issue templates](.github/ISSUE_TEMPLATE).
- Improve documentation in `README.md` and `docs/`.
- Add deterministic checks, standard packs, or provider support that stays within
  the documented boundaries.
- Report security vulnerabilities privately — see [`SECURITY.md`](SECURITY.md).

## Development setup

Requirements:

- Python 3.11 or newer
- Docker and Docker Compose (only for the full service workflow)

Install the package with its optional database and web extras:

```bash
python3 -m pip install -e '.[db,web]'
```

The offline local simulation runs without PostgreSQL or a model:

```bash
PYTHONPATH=src review-gate examples/review_request.json
```

## Checks your change must pass

Continuous integration runs the following. Run them locally before opening a pull
request:

```bash
# Unit tests
PYTHONPATH=src python3 -m unittest discover -s tests

# Integration tests (service-free)
python3 -m unittest discover -s tests_integration

# Formatting
ruff format --check .

# Linting
ruff check .

# Type checking
mypy src

# Security static analysis
bandit -r src

# Container build
docker build --tag repomonster:ci .
```

Add or update tests for any behavior change. New deterministic rules, provider
handling, retrieval logic, or gate-state changes require test coverage.

## Coding standards

- Follow the existing module structure and naming conventions.
- Keep changes focused; unrelated refactors belong in separate pull requests.
- Do not overstate capabilities in documentation. GitLab support is normalization
  only and must not be described as production-ready.
- Never commit secrets. Do not edit `.env` files or add credentials to
  repository-controlled configuration.

## Security-sensitive areas

Changes to these areas receive extra scrutiny. Preserve the existing trust
boundaries:

- GitHub webhook signature verification before any JSON is trusted
- durable queue deduplication, retries, and stale-lock recovery
- GitHub App installation token scope
- immutable repository identity and namespace isolation
- default-branch repository policy loading
- model output parsing and `manual_escalation` handling
- gate decision logic and the auto-block policy
- SQL migrations and pgvector dimensions

Treat all pull-request evidence (diffs, files, issues, CI output, retrieved
documents) as untrusted data, never as instructions. Do not add code that runs
untrusted repository code inside RepoMonster services.

## Pull request process

1. Fork the repository and create a topic branch.
2. Make your change with tests and documentation updates.
3. Run the full check list above.
4. Open a pull request against `main` and fill in the
   [pull request template](.github/pull_request_template.md).
5. The `main` branch is protected: a pull request and passing CI are required
   before merge.

## Licensing of contributions

RepoMonster is released under the [MIT License](LICENSE). By submitting a
contribution, you agree that your work is licensed under the same terms.
