# Harness capabilities

RepoMonster is a review harness rather than a single prompt. It coordinates provider identity, trusted policy, retrieval, deterministic checks, model inference, persistence, and publication around a provider-independent `ReviewRequest`.

This document separates capabilities that work now from storage or extension points that require additional implementation.

## Capability matrix

| Area | Available now | Not currently implemented |
|---|---|---|
| GitHub | signed App webhooks, installation tokens, PR/files/issues/checks/status retrieval, Check Runs | inline annotations, review comments, labels, approvals, merges, push analysis |
| GitLab | event normalization functions and a normalization-only HTTP route | webhook authentication, project auth, MR retrieval, queue worker, result publication |
| Retrieval | public packs, repository knowledge, linked local GitHub issues, pgvector ranking | public pack marketplace, web crawling, arbitrary remote documentation ingestion |
| Organization policy | schema and retrieval filtering by `TENANT_KEY` | public ingestion and administration workflow for company documents |
| Models | OpenAI-compatible embeddings and chat completions, separate endpoints and keys | per-repository credentials, model routing UI, provider-specific SDK features |
| Policy | repository config, deployment profiles, deterministic rules, model findings | repository-authored executable rules or arbitrary scripts |
| Execution | consume provider CI/check state | run builds, tests, linters, or repository code |
| Publication | one summary GitHub Check Run | line annotations, SARIF upload, dashboards, notifications |
| Operations | Docker Compose, PostgreSQL queue, retries, idempotency, health endpoints | web administration UI, metrics endpoint, distributed tracing |

## Practical usage patterns

### Open-source maintainer readiness gate

Install one self-hosted GitHub App across selected repositories. Each repository commits its own project description, stack mappings, and review requirements. RepoMonster filters incomplete changes before a maintainer spends time on detailed review.

Typical policy:

- structured PR description required
- issue reference optional
- tests and evidence required
- public language/framework packs plus repository conventions
- one warning tolerated, errors blocked

### Internal engineering standards gate

Use repository standards to describe service boundaries, testing expectations, security constraints, and architecture decisions. Add organization policy ingestion as an operator extension when the same private standards must apply across repositories.

RepoMonster is suited to evidence-backed guidance and readiness gating. It is not a substitute for deterministic security scanners or CI enforcement.

### Monorepository review routing

Define multiple `stacks` entries so changed paths select Python/FastAPI or TypeScript/Node guidance. The dominant changed stack supplies the primary model context while selected pack versions remain pinned to the repository.

Current reviews produce one overall outcome. Per-stack parallel reviews or separate checks would require an orchestration extension.

### Documentation and contract discipline

Repository requirements can state that API, schema, or externally visible behavior changes need tests and documentation. The deterministic engine already warns when likely API/schema paths change without documentation; the model can evaluate the repository-specific requirement in more detail.

### Controlled internal review API

An internal service can submit normalized review requests directly to `POST /review`. This is useful when another trusted system already fetches provider evidence. The caller must establish authenticity, immutable identity, and diff provenance because the direct endpoint does not perform provider verification.

### Policy and prompt evaluation

Use `review-gate` with fixture JSON for fast deterministic evaluation, then use an isolated deployment to compare embedding models, standard-pack wording, review profiles, and insight models against known PR fixtures.

RepoMonster does not yet include an evaluation runner or scoring dashboard, but its provider-independent request and structured result types make those natural external harness extensions.

## Provider harness

The provider layer is responsible for facts that the model must not invent:

- verifying webhook authenticity
- resolving immutable repository and installation identity
- obtaining short-lived credentials
- fetching the current change request and head SHA
- fetching changed-file metadata and bounded patches
- retrieving linked tasks and CI/check state
- publishing a result against the reviewed SHA

The GitHub implementation follows this boundary. A GitLab or another provider adapter should produce the same `ReviewRequest` shape rather than leaking provider payloads into the review engine.

The current GitHub workflow supports repositories installed under one App configuration. It can process multiple repositories and installations because storage is namespaced by provider origin and immutable repository ID. One Compose worker is supplied; the PostgreSQL `SKIP LOCKED` queue design permits multiple workers, though distributed operation has not been load-tested.

## Knowledge and RAG harness

### Knowledge scopes

| Scope | Current source | Retrieval condition |
|---|---|---|
| `task` | linked local GitHub issue title and body | same repository and referenced task key |
| `repo` | default-branch files selected by `.repomonster.yml` | same immutable repository key |
| `company` | database schema supports operator-owned content | matching `TENANT_KEY`; ingestion must be added |
| `public` | bundled versioned standard packs | repository-selected pack and version |

The retrieval order is controlled by the selected profile. The default order is task, repository, company, public.

### Retrieval behavior

RepoMonster builds a semantic query from the PR title, description, inferred language, framework, and changed paths. It then:

1. applies SQL identity, scope, pack, language, and framework filters
2. ranks eligible chunks by pgvector cosine similarity
3. reconstructs bounded standard documents and structured rules
4. inserts selected guidance into the model brief

This prevents semantically similar content from another repository or unselected pack from crossing the namespace boundary.

Repository files are versioned by provider blob SHA. Unchanged content and the same embedding model reuse existing vectors. Superseded versions remain stored but disabled.

### Embedding constraints

- Embeddings are required for production bootstrap and review retrieval.
- The embedding endpoint must implement the OpenAI-compatible `/embeddings` shape.
- `EMBEDDING_DIMENSIONS` must equal the actual vector length returned by the model.
- Dimensions are database schema state, not model context length.
- Changing dimensions requires a schema migration and full reindex or database recreation.
- Changing the embedding model at the same dimensions requires reindexing standards and repository knowledge.

## Policy harness

Policy has two layers.

### Deterministic policy

Deterministic rules evaluate observable request facts without model judgment. They currently cover required description sections, traceability, test evidence, changed tests, required CI, change size, and possible contract documentation.

These checks are appropriate for requirements that can be stated as stable predicates. Model output should not be used to replace CI status or signature verification.

### Model-assisted policy

The insight model receives:

- profile and repository identity
- PR title and description
- changed files and bounded diff excerpts
- task references and CI state
- retrieved guidance and structured standard rules
- deterministic findings already known

It is asked for structured `info`, `warning`, or `error` findings. Severity participates in the gate:

- any error blocks
- warnings above the profile tolerance require updates
- informational findings do not block

The parser normalizes severity casing and scalar/list evidence formatting. Unknown severities, malformed JSON, timeouts, or endpoint failures cause manual escalation rather than a pass.

### Profiles

Profiles are operator-owned JSON files, not arbitrary repository content. They define:

- required description sections
- whether task references and test evidence are required by default
- warning tolerance
- large-change threshold
- retrieval order
- label names reserved for future publication behavior

Repository configuration can select an installed profile by name. Adding a custom profile currently requires including it in the deployment image or mounted project root.

## Model endpoint harness

Embedding and insight models can use:

- the same OpenAI-compatible server and API key
- different servers and keys
- a local server reachable through a private network such as Tailscale
- a hosted endpoint controlled by the operator

The worker must be able to reach both endpoints. RepoMonster does not distribute user credentials from repository configuration and does not expose the model endpoint to GitHub.

For local models, review latency depends on model size, loaded context, hardware, and generated output. Leave `INSIGHT_MAX_TOKENS` unset to avoid a RepoMonster-imposed generation ceiling, set `INSIGHT_TIMEOUT_SECONDS` above observed completion time, and keep `WEBHOOK_LOCK_TIMEOUT_SECONDS` comfortably larger than the full review duration.

## Safe extension points

### Add a public standard pack

Add a versioned directory under `standard-packs/` with a manifest, source provenance, and review guidance. Run `repomonster standards sync` to import it. Do not edit an already published pack version; create a new version.

### Add a deterministic rule

Extend `RuleEngine` with a rule based on normalized `ReviewRequest` facts, add focused tests, and decide explicitly whether its severity gates readiness.

### Add a provider

Implement authentication, webhook verification, authoritative evidence collection, and publication outside `ReviewService`. Normalize into `ReviewRequest` and reuse the queue and result model.

### Add an external task tracker

Resolve recognized ticket identifiers through operator-owned credentials, map retrieved acceptance criteria into task-scoped `RepositorySource` documents, and keep the source tied to the immutable repository/task namespace.

### Add organization standards

Build an authenticated operator ingestion command or API that writes `company` documents with a `tenant_key`. Repository-controlled files must not be allowed to claim organization scope.

### Add execution

Do not run repository commands in the API or worker. A future execution harness should use ephemeral isolated workers without GitHub App or model secrets, constrained network access, resource limits, and explicit artifact transfer back to the review worker.

## Explicit non-goals of the current release

RepoMonster does not currently:

- approve or merge changes
- replace maintainer review
- guarantee functional correctness
- execute repository code
- ingest PR comments
- analyze standalone push events
- fetch external ticket bodies
- provide a complete GitLab integration
- manage per-repository model credentials
- expose a browser-based administration interface
- automatically update standard packs from the internet

These are capability boundaries, not claims that the architecture can never support them.
