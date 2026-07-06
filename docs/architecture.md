# Architecture

## Product boundary

RepoMonster decides whether a change is ready to consume human maintainer attention. It is a gate before detailed human review, not an autonomous reviewer of record.

Deterministic checks establish objective completeness and evidence requirements. Retrieval supplies task, repository, and stack-specific guidance. A language model evaluates implementation fit, clarity, maintainability, and architectural consistency. The combined findings produce a readiness state.

RepoMonster does not approve or merge changes and does not claim functional correctness.

## Runtime topology

The current production provider is GitHub:

```text
GitHub App
    │ signed pull_request webhook
    ▼
RepoMonster API
    │ verify + deduplicate + enqueue
    ▼
PostgreSQL webhook_deliveries
    │ claim with FOR UPDATE SKIP LOCKED
    ▼
RepoMonster worker
    ├── GitHub REST API
    ├── embedding endpoint
    ├── PostgreSQL + pgvector
    └── insight endpoint
             │
             ▼
      GitHub Check Run
```

The same image supplies the API, worker, and management commands. Docker Compose runs a one-shot bootstrap container before starting the API and worker.

## Components

### API

The API exposes:

- process health and database/pack readiness
- authenticated direct reviews
- signed GitHub webhook ingestion
- a normalization-only GitLab endpoint

The GitHub request path performs bounded body reading, HMAC verification, JSON parsing, event filtering, and an idempotent queue insert. It returns before provider, embedding, or model work starts.

### Worker

The worker:

1. claims one available job
2. authenticates as the GitHub App installation
3. fetches authoritative repository and PR state
4. creates an in-progress Check Run
5. reads trusted repository policy and knowledge
6. fetches changed files, linked local issues, and CI state
7. synchronizes repository/task RAG documents
8. runs deterministic and model-assisted review
9. persists the run and findings
10. completes the Check Run

Jobs retry with bounded exponential backoff. A processing job becomes claimable again after the configured lock timeout, allowing recovery after worker termination. Terminal failure attempts to publish a neutral Check Run.

### Bootstrap

Bootstrap applies ordered SQL migrations under a PostgreSQL advisory lock, verifies configured embedding dimensions, and imports changed standard packs. Migrations and content synchronization are deliberately separate concepts even though `repomonster bootstrap` runs both.

### PostgreSQL and pgvector

PostgreSQL stores:

- schema migration history
- provider installations and repositories
- standard pack metadata and selected versions
- versioned knowledge documents and vector chunks
- structured standard rules
- repository source synchronization state
- durable webhook deliveries
- review runs and findings

The database is both system-of-record and work queue for the current single-host design.

## Trust boundaries

### Trusted operator configuration

Deployment environment variables, profiles, bundled packs, provider credentials, and model credentials are operator-controlled.

### Trusted repository policy

`.repomonster.yml` and referenced knowledge files are loaded from the repository's default branch through the GitHub installation token. PR-head changes cannot alter policy for the review currently being evaluated.

Repository configuration may select known profiles and packs but cannot define model endpoints, credentials, or files outside the repository.

### Untrusted review evidence

PR titles, descriptions, patches, source paths, issues, CI output, and retrieved text are treated as evidence, not instructions. The insight system prompt reinforces this boundary, but model behavior is not itself a security boundary.

Provider signatures, SQL namespace filters, credential isolation, and execution restrictions provide the actual security controls.

## Repository identity

Namespaces use provider origin and immutable provider repository ID:

```text
github:https://github.com:123456789
```

Mutable owner/repository names remain display metadata. This prevents repository rename or transfer from silently creating a second knowledge namespace.

Installation tokens are requested for the single repository ID that triggered the delivery. Tokens are cached in worker memory until close to expiry.

## Knowledge model

Knowledge is layered:

| Scope | Content | Identity | Current ingestion |
|---|---|---|---|
| `task` | issue requirements and acceptance criteria | repository key + task key | linked local GitHub issues |
| `repo` | project description, requirements, standards | repository key | default-branch config patterns |
| `company` | organization policy | tenant key | schema/retrieval only; no public ingestion workflow |
| `public` | language/framework packs | pack ID + version | bundled pack bootstrap |

`standard_documents` stores identity, provenance, scope, source text, checksums, and enabled state. `standard_chunks` stores retrieval-sized text and embeddings. `standard_rules` stores structured rules authored by standard packs.

Repository source versions use provider blob SHA. A changed blob creates a new document version and disables its predecessor. Unchanged content with the same embedding model and dimensions reuses existing chunks.

## Retrieval

For each review, RepoMonster:

1. builds a semantic query from title, description, language, framework, and changed paths
2. iterates profile retrieval scopes in precedence order
3. filters documents by scope, repository/tenant/task identity, selected pack version, language, framework, and enabled state
4. ranks eligible chunks by cosine distance
5. loads structured rules for selected documents
6. constructs a bounded review brief

The default precedence is:

1. task
2. repository
3. company
4. public packs

SQL metadata filtering happens before pgvector ranking. Similar content from another repository cannot cross the repository-key predicate.

Changed-file patches are transient evidence. GitHub-provided patch excerpts are bounded per file, and the model brief imposes an additional total diff-character limit. Diffs are not stored as permanent public-standard content.

## Review engine

The provider-independent `ReviewRequest` contains:

- provider and change type
- stable repository key and external change ID
- title and description
- target/source branches
- language, framework, and selected packs
- task references
- normalized changed files and bounded patches
- provider metadata such as head SHA and CI state

The deterministic `RuleEngine` produces findings first. Retrieved guidance and deterministic findings are then included in the insight brief. Validated model findings are appended to the deterministic findings.

The default gate decision is:

```text
any error
    -> blocked
else warnings > max_warnings_for_ready
    -> needs_author_updates
else
    -> ready_for_human_review
```

After that base decision, an optional composite auto-block policy can upgrade a non-blocked result to `blocked`. The policy requires all configured conditions: a deterministic poor-PR-documentation signal, a model recommendation of `request_changes` or `block`, and at least one evidence-backed model warning or error at or above the configured impact threshold. `shadow` mode appends an informational audit finding without changing the base decision; `enforce` mode applies the upgrade. Raw warning counts and model recommendations by themselves are insufficient.

An insight exception returns `manual_escalation` with the deterministic findings preserved. It does not fall through to a successful deterministic-only decision when an insight endpoint was configured but failed.

## Provider boundary

Provider adapters own:

- webhook authentication
- immutable installation and repository identity
- short-lived access credentials
- authoritative change metadata and current head SHA
- changed-file, task, and CI evidence
- publication

The GitHub adapter implements this boundary for pull requests. Stale deliveries are ignored when their head SHA no longer matches the current PR. Drafts receive a neutral check. Supported actions are opened, reopened, synchronize, edited, and ready-for-review.

GitLab event normalization exists for the provider-independent shape, but the HTTP GitLab route does not yet verify secrets, enqueue jobs, fetch merge requests, or publish results. It must not be described as a complete production provider.

## Execution boundary

RepoMonster consumes provider-reported CI checks and commit statuses. It does not execute commands from a repository in the API or worker containers.

Any future execution adapter requires a separate ephemeral runner with:

- no GitHub App private key
- no model or embedding credentials
- constrained filesystem and network access
- CPU, memory, and time limits
- explicit artifact transfer back to the review worker

This separation prevents untrusted repository code from running beside provider and model secrets.

## Scaling characteristics

The queue claim uses row locking and `SKIP LOCKED`, so multiple workers can claim different jobs. Repository knowledge synchronization and review persistence use database uniqueness constraints for idempotency.

The current Compose deployment runs one worker and has not been load-tested as a distributed service. Before horizontal production scaling, add metrics, structured job timing, graceful deployment coordination, model concurrency limits, and database connection management.
