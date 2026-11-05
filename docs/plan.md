# RepoMonster: implementation plan

As of 2026-11-05. Companion to [`docs/roadmap.md`](roadmap.md), which holds the audit findings (referenced here as F1 to F13) and the design for each phase.

Each numbered item below is sized to be one pull request. Work top to bottom; later items assume earlier ones are merged.

## Starting a new session on this plan

1. Read `docs/roadmap.md`, then this file.
2. Find the first unticked item and branch from `main` as `phase-N/<short-name>`.
3. Before opening the PR, run the baseline checks from the lockfile:

```bash
python -m pip install uv==0.11.28
uv lock --check
uv sync --frozen --extra db --extra web --extra dev
PYTHONPATH=src uv run --frozen python -m unittest discover -s tests
uv run --frozen python -m unittest discover -s tests_integration
uv run --frozen python -m unittest discover -s tests_e2e  # requires Docker
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen bandit -q -r src
```

4. Tick the item in this file in the same PR.

## Decisions in force

- Testing: prefer end-to-end tests in `tests_e2e/`. Keep unit tests only for logic with many branches, such as signature checks, parsers and gate decisions, and remove unit tests that end-to-end tests make redundant.
- Infrastructure: **Docker Compose for the local demo**; Terraform deferred with AWS hosting.
- Review model: **Claude via the Anthropic API**, key in Secrets Manager (AWS) or `.env` (local).
- Hosting: **no-cost local demo on the owner's machine through a Cloudflare Quick Tunnel**. AWS hosting is deferred until there is a reason to run a hosted service.
- Embeddings: **Voyage AI `voyage-4` at 1024 dimensions**, key in Secrets Manager or `.env` as `VOYAGE_API_KEY`.

## Phase 0: streamline

- [x] **0.0 End-to-end harness.** `tests_e2e/` runs the real API and worker against real PostgreSQL with pgvector and a fake GitHub that enforces App JWTs and scoped tokens. Fixes the webhook routes, which rejected every delivery with a 422 because `Request` was `Any` at runtime.
- [x] **0.1 Fix the CI-timing bug (F1).** Pending CI states (`queued`, `in_progress`, `pending`, `requested`, `waiting`) and required checks that have not appeared yet become a "waiting" state: the Check Run stays `in_progress` with a summary naming the pending checks, no gate decision is published, and no model call is made. A required check still missing after a configurable wait ends in `manual_escalation`, naming the check. Queue `check_run` (`completed`) and `status` events, ignoring RepoMonster's own Check Runs by app ID so a review cannot re-trigger itself. Map each event to its open PRs by head SHA and re-review once, reusing the open Check Run for that SHA. Subscribe the GitHub App to those events and document it in `docs/operations.md`.
    - Done when: end-to-end tests cover pending to success, pending to failure, a missing check timing out, RepoMonster's own Check Run not re-triggering a review, and several checks completing without repeated reviews.
- [x] **0.1a Fail closed on escalation.** `manual_escalation` and terminal review failures publish `action_required` instead of `neutral`, which GitHub treats as passing for required checks.
    - Done when: end-to-end tests cover a model failure and exhausted retries.
- [x] **0.2 Pin tooling (F3, F12, F13).** Add a `dev` extra with pinned ruff, mypy, types-PyYAML and Bandit. Add `[tool.ruff]` with an explicit rule set, run `ruff check --fix`, fix the remainder. Raise `requires-python` to `>=3.12`. Merge the format, ruff and mypy CI jobs into one.
    - Done when: CI is green with pinned versions and no unpinned `pip install` remains in `ci.yml`.
- [x] **0.3 Lock dependencies and pin images (F4).** `uv lock` (commit `uv.lock`) or `pip-compile --generate-hashes`. Install from the lock in the Dockerfile and CI. Pin `python:3.12-slim` and `pgvector/pgvector:pg16` by digest. Pin every Action by commit SHA with the tag in a comment. Add a Renovate or Dependabot config covering pip, Docker and Actions.
- [x] **0.4 Close the GitLab route (F5).** Remove `/webhooks/gitlab` until GitLab support ships, keeping `normalize_gitlab_event` and its tests.
- [x] **0.5 Shared HTTP client (F7).** New `http.py` on httpx with per-call timeouts, retries with backoff on 429/5xx and `Retry-After`. Move the GitHub and embedding clients onto it; make the embedding timeout configurable.
- [x] **0.6 Connection pool and service factory (F8, F9).** One `psycopg_pool.ConnectionPool` built at startup and injected into the queue, repository and processor. One `build_review_service()` used by both `app.py` and the worker. `/readyz` uses a public repository method.
- [x] **0.7 Split `github/` (F10).** `github/client.py` (API and auth), `github/processor.py` (job handling), `github/publish.py` (Check Run text). No behaviour change; existing tests move with the code.
- [x] **0.8 Knowledge cache (F2).** Skip the tree walk and resync when the default-branch tree SHA matches the last sync. Queue default-branch `push` events to refresh knowledge.
- [x] **0.9 Naming (F11).** Rename the package `review_gatekeeper` to `repomonster`, keep one CLI (`repomonster`, with `review-gate` becoming a subcommand), one liveness route (`/healthz`), and the FastAPI title "RepoMonster". Note the rename in `CHANGELOG.md`.

## Phase 0.5: hardening

From the security and data-integrity audit of 2026-11-05. Finish H1 to H6 before Phase 1, keeping H3's transaction change ahead of item 0.6. Future autoscaled workers also depend on H3.

- [x] **H1 Gate integrity.** Stricter required-check evidence, permanent errors classified without retries, deterministic errors preserved when the model fails, repository configuration faults reported to the author, profile selection validated, and file classification tightened.
- [x] **H2 Queue recovery.** A longer retry schedule, attempt limits enforced on reclaim, terminal failures published durably, workers that survive database restarts, and no repeated side effects after a crash.
- [x] **H3 Concurrency.** Fenced job ownership, per-pull-request serialization, ordered policy syncs, and no HTTP calls inside database transactions.
- [x] **H4 Model input.** Per-scope retrieval quotas, untrusted text clearly separated in the brief, explicit truncation, and filtered vector search that cannot silently return nothing.
- [x] **H5 Resource limits.** Bounded request bodies, non-blocking routes, database timeouts, caps on embedding work, delivery coalescing, and safer YAML parsing.
- [x] **H6 Hardening.** Stable error codes and plain-text findings in Check Runs, retention maintenance, storage validation, schema constraints, scoped tokens, cooperative shutdown, and Compose hardening. Embedding-dimension migration checks report the required database transition. Separate database roles remain deferred with AWS hosting.

## Phase 1: no-cost local demo

- [x] **1.1 Demo overlay.** Keep the base Compose file unchanged. Add Caddy and a free Cloudflare Quick Tunnel, expose only the exact signed webhook route, remove host ports and the external network, and mount the operator's PEM read-only in the worker.
- [x] **1.2 Demo runbook.** Document local prerequisites, environment variable names, startup, finding the changing tunnel URL, updating the GitHub App, manually redelivering a webhook, shutdown and availability limits in `docs/operations.md`.
- [x] **1.3 Stack validation.** Test the real Compose image, Caddy routing, signature rejection, durable enqueue and worker claim against isolated provider stubs. Run it in the existing Docker end-to-end CI job.
- [ ] **1.4 Live run.** Through a real Quick Tunnel and GitHub App, confirm a pull request gets a Check Run with live embedding and model providers.

AWS, Terraform, managed databases, Secrets Manager, hosted CI/CD and autoscaling are
deferred until there is a reason to host the service. Revisit deployment security,
database roles, observability and cost before choosing that architecture.

## Phase 2: Claude and OpenAI providers

- [ ] **2.1 Schema compatibility.** Remove `maxItems` from `FINDINGS_RESPONSE_FORMAT`, lower-case `recommendation` before parsing, and treat `refusal` and `max_tokens` stop reasons as explicit errors. Keep the evidence truncation in code.
- [ ] **2.2 Provider selection.** `INSIGHT_PROVIDER` (`anthropic` default, `openai`, `openai_compatible`) and `EMBEDDING_PROVIDER` (`voyage` default, `openai`, `openai_compatible`), resolved in the service factory from 0.6.
- [ ] **2.3 Anthropic adapter.** Official `anthropic` SDK, Messages API with `output_config.format` JSON schema, SDK retries enabled, prompt caching on the system prompt and standards block. Transient errors raise to the job queue (closes F6).
- [ ] **2.4 Voyage and OpenAI adapters.** `voyageai` SDK for embeddings with `input_type=document` for packs and knowledge and `input_type=query` for the review query, batched at up to 1,000 texts per request. Update `.env.example` to `EMBEDDING_PROVIDER=voyage`, `EMBEDDING_MODEL=voyage-4`, `EMBEDDING_DIMENSIONS=1024`. Official OpenAI SDK for chat and embeddings as the alternative.
    - Done when: a fresh database bootstraps at 1024 dimensions and `standards reindex` succeeds against Voyage.
- [ ] **2.5 Contract tests and comparison.** Recorded-response tests per adapter. A small fixture set of PRs with expected gate outcomes to compare models before switching defaults.

## Phase 3: security gate

- [ ] **3.1 Lockfile diff.** Parsers for `uv.lock`, `poetry.lock`, `requirements*.txt`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `go.sum`, `Cargo.lock`; base vs head diff of added, removed and changed packages.
- [ ] **3.2 Vulnerability and health lookups.** OSV `querybatch` for the whole diff; deps.dev for Scorecard, licences and graph; registry metadata for deprecated, yanked, release age, install scripts and maintainer changes. Cache by package and version.
- [ ] **3.3 Image checks.** Detect `FROM` changes and tag-only pins; resolve digests; Syft SBOM and Grype scan in the worker.
- [ ] **3.4 Findings and SARIF.** `supply_chain` findings feed the existing gate; SARIF upload to GitHub code scanning.
- [ ] **3.5 Zero Wire and KEV intelligence.** Scheduled poll of the Zero Wire RSS feed and the CISA KEV catalogue into a `threat_briefings` table; join on CVE aliases from OSV results; "Needs attention" raises the finding to an error with the briefing link. Store each default branch's dependency and image inventory and alert installed repositories when a new briefing matches.
    - Done when: a fixture briefing for a CVE present in a test lockfile produces an error finding with the briefing link, and a new briefing triggers an alert for a repository with no open PR.
- [ ] **3.6 Agentic audit job.** Scheduled and trigger-based runs of Cloudflare's security-audit-skill inside the Phase 4 sandbox; map `confirmed`, `needs_validation` and `rejected` into findings; token budget per run.

## Phase 4: verification sandbox and attack-pattern library

- [ ] **4.1 Sandbox account and network.** Separate AWS account, run subnets with no internet route, ECR endpoint only, no task role (or S3-results-only), Terraform module `sandbox`.
- [ ] **4.2 Two-stage runner.** Build stage with an allowlisted registry proxy; run stage with no egress, CPU and memory caps, read-only root, scratch volume, hard timeout.
- [ ] **4.3 Base and head verification.** Run the agent-written reproduction test against both commits and record the verdict from the roadmap's verdict table. Maintainer review before any test is committed.
- [ ] **4.4 Pattern library.** Migration for `attack_patterns` and `pattern_occurrences` with an embedding column; CWE and CAPEC tagging; retrieval into review briefs.
- [ ] **4.5 Recurrence actions.** Configurable threshold that proposes a `.repomonster/standards/security.md` entry and, where expressible, a deterministic rule; periodic cross-repository report.
