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

Request bodies are limited while they arrive, and database and model work runs off the event loop. The direct review route checks credentials before reading its body. The GitHub request path performs bounded body reading, HMAC verification, JSON parsing, event filtering, and an idempotent queue insert. It returns before provider, embedding, or model work starts.

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

Jobs retry with bounded exponential backoff. A processing job becomes claimable again after the configured lock timeout, allowing recovery after worker termination. A job that uses up its attempts, including one that keeps killing its worker, is not run again. It stays claimable until an `action_required` Check Run is published, because GitHub treats `neutral` as passing for required checks. Workers survive database outages and retry their queue operations. Each claim takes a lock token that every later queue write must present, and a heartbeat keeps the lock fresh while the worker is alive, so a worker that lost its lock cannot overwrite newer results. A database constraint allows one running job per repository, which keeps reviews and policy syncs in order. Embedding calls run outside database transactions. A retry that finds its Check Run already completed does not review again.

On a termination signal, the worker checks for shutdown before source embeddings,
retrieval embeddings, and the insight call. Work stopped at these boundaries returns
to pending immediately without consuming an attempt. An insight call already in
progress finishes, including persistence and publication.

### Bootstrap

Bootstrap applies ordered SQL migrations under a PostgreSQL advisory lock, verifies configured embedding dimensions, and imports changed standard packs. Migrations and content synchronization are deliberately separate concepts even though `repomonster bootstrap` runs both. Bootstrap reports a dimension mismatch as requiring database recreation or reindexing, and rejects missing applied migration files before applying changes.

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

Each API and worker process creates one `psycopg_pool.ConnectionPool`, shared by its
queue, standards repository and, in the worker, GitHub processor. A shared
`build_review_service()` factory wires the repository and optional insight provider
for both processes. `/readyz` uses the repository's public ready-pack count method.
Pools open without waiting for database availability and close at shutdown; every
checkout checks connection health so terminated connections are replaced.

Pool connections have configurable connect and statement timeouts, and checkout has
its own timeout. The worker retries checkout timeouts as database outages and requires
a pool maximum of at least two so its heartbeat can run beside a processing transaction.
`pool.connection()` commits on success and rolls back on exceptions. Claims still
commit before processing, every ownership update remains a short transaction, and
pooled connections are returned before HTTP calls. Migration and management commands
keep dedicated connections with the connect timeout but without the pool's statement
timeout, because they perform long single transactions.

## Trust boundaries

### Trusted operator configuration

Deployment environment variables, profiles, bundled packs, provider credentials, and model credentials are operator-controlled.

### Trusted repository policy

`.repomonster.yml` and referenced knowledge files are loaded from the repository's default branch through the GitHub installation token. PR-head changes cannot alter policy for the review currently being evaluated.

Repository configuration may select known profiles and packs but cannot define model endpoints, credentials, or files outside the repository. Local repository synchronization also skips knowledge paths that resolve outside the checkout.

Policy parsing validates mapping sections and string list entries before processing. Profile names contain only lowercase letters, digits, underscores, and hyphens; resolved profile files must stay within the operator's profiles directory. Profile availability and all selected pack versions are checked before knowledge synchronization. Invalid YAML, malformed configuration, unavailable profiles or packs, and NUL characters in repository knowledge produce a configuration-specific `action_required` Check Run on the first attempt without consuming retries or exposing database diagnostics.

### Untrusted review evidence

PR titles, descriptions, patches, source paths, issues, CI output, and retrieved text are treated as evidence, not instructions. The insight system prompt reinforces this boundary, but model behavior is not itself a security boundary.

Provider signatures, SQL namespace filters, credential isolation, and execution restrictions provide the actual security controls.

## Repository identity

Namespaces use provider origin and immutable provider repository ID:

```text
github:https://github.com:123456789
```

Mutable owner/repository names remain display metadata. This prevents repository rename or transfer from silently creating a second knowledge namespace.

Installation tokens are requested for the single repository ID that triggered the delivery. Tokens request only checks write access and contents, issues, metadata, pull requests, and statuses read access. They are cached in worker memory until close to expiry; expired cache entries are removed when requesting a token. Disabled repositories and installations remain disabled during synchronization, and the worker ignores their deliveries.

## Knowledge model

Knowledge is layered:

| Scope | Content | Identity | Current ingestion |
|---|---|---|---|
| `task` | issue requirements and acceptance criteria | repository key + task key | linked local GitHub issues |
| `repo` | project description, requirements, standards | repository key | default-branch config patterns |
| `company` | organization policy | tenant key | schema/retrieval only; no public ingestion workflow |
| `public` | language/framework packs | pack ID + version | bundled pack bootstrap |

`standard_documents` stores identity, provenance, scope, source text, checksums, and enabled state. `standard_chunks` stores retrieval-sized text and embeddings. `standard_rules` stores structured rules authored by standard packs.

Repository source versions use provider blob SHA. A changed blob creates a new document version and disables its predecessor. Unchanged content with the same embedding model and dimensions reuses existing chunks. A partial unique index permits only one enabled version of each repository or task logical document; public pack versions remain independently selectable.

## Retrieval

For each review, RepoMonster:

1. builds a semantic query from title, description, language, framework, and changed paths
2. iterates profile retrieval scopes in precedence order
3. filters documents by scope, repository/tenant/task identity, selected pack version, language, framework, enabled state, and current embedding model
4. ranks eligible chunks by cosine distance
5. loads structured rules for selected documents
6. constructs a bounded review brief

The default precedence is:

1. task
2. repository
3. company
4. public packs

SQL metadata filtering happens before pgvector ranking. Similar content from another repository cannot cross the repository-key predicate.

Changed-file patches are transient evidence. GitHub-provided patch excerpts are bounded per file, and the model brief imposes an additional total diff-character limit. Stored review request payloads omit diff excerpts. The bounded model brief can still contain diff evidence and is retained with its review run; diffs are not stored as permanent public-standard content. Webhook payloads and review persistence remove NUL and lone surrogate characters from text before storage.

Operators can run `repomonster prune` to remove terminal webhook deliveries by last
update time and review runs by creation time after a configurable retention period
(`--days`, otherwise `RETENTION_DAYS`, default 90). Findings are deleted with their
review runs. Pending and processing deliveries are preserved. Pruning requires an
operator schedule; knowledge document history has no automatic retention.

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

Retrieval runs its namespace filters before ranking, using exact distance over the filtered chunks rather than the HNSW index. Each scope gets a fair share of the results, so one scope cannot push out the others.

The model brief wraps author and issue text in blocks marked with a per-request random token. The diff budget is shared across files, and any file the model did not fully see is named in the brief and in an informational finding.

The deterministic `RuleEngine` produces findings first. Retrieved guidance and deterministic findings are then included in the insight brief. Validated model findings are appended to the deterministic findings.

Changed files are classified using directory segments and file-name conventions. Documentation requires a `.md`, `.mdx`, or `.rst` extension. Test directory segments and test file-name conventions identify test changes; other files remain source changes, including code in documentation and utility directories.

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

An insight exception returns `blocked` if deterministic errors exist, and `manual_escalation` otherwise, with the deterministic findings preserved. It does not fall through to a successful deterministic-only decision when an insight endpoint was configured but failed.

Check Run findings escape untrusted Markdown and HTML. Summaries are generated by
the service. Terminal failures publish stable error codes with a direction to inspect
worker logs; full diagnostics stay in queue error records and logs. Insight failures
publish only their exception type. The direct API omits the model brief. Model
responses must have `finish_reason: stop` before their findings are accepted.

## Provider boundary

Provider adapters own:

- webhook authentication
- immutable installation and repository identity
- short-lived access credentials
- authoritative change metadata and current head SHA
- changed-file, task, and CI evidence
- publication

The GitHub adapter implements this boundary for pull requests. Stale deliveries are ignored when their head SHA no longer matches the current PR, and a delivery is also ignored when a newer one for the same PR is waiting, so a burst of edits costs one review. At most five linked issues are embedded per delivery. Drafts receive a neutral check. Supported actions are opened, reopened, synchronize, edited, and ready-for-review.

Local issue references accept at most ten digits. CI collection paginates Check Runs and commit statuses and combines same-named evidence conservatively: any failure wins, otherwise any pending result wins, and success requires every result for that name to succeed.

While a required check is pending or has not appeared, the Check Run stays `in_progress`, no gate decision is published, and no model call is made. Completed `check_run` and `status` events for a required check re-review the open pull requests on that head SHA, continuing the open Check Run. RepoMonster ignores its own Check Run and skips a re-review if the required check results have not changed. A delayed queue job keyed to the Check Run ends the wait in `manual_escalation` after `CI_WAIT_TIMEOUT_SECONDS`.

GitLab event normalization code exists for the provider-independent shape, but there is no GitLab HTTP route. GitLab authentication, queue processing, authoritative merge-request retrieval, and result publication are not implemented. It must not be described as a complete production provider.

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

The current Compose deployment runs one worker and has not been load-tested as a distributed service. Each API and worker process has a bounded connection pool; total connection demand grows with the process count. Before horizontal production scaling, add metrics, structured job timing, graceful deployment coordination, and model concurrency limits, and size the pools against the database's connection budget.
