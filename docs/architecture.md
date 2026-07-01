# Architecture

## Product boundary

RepoMonster answers one question: is this change ready for a human maintainer to review?

It is not an autonomous approver. Missing required evidence, CI failures, and policy violations are deterministic gates. The model is used for task alignment, clarity, maintainability, and architectural fit.

## Deployment

```text
GitHub/GitLab
     │ webhook + provider installation identity
     ▼
RepoMonster API ──► PostgreSQL webhook queue ──► review worker
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
      PostgreSQL + pgvector   configured model endpoint
```

The same image supplies the API, worker, and management commands. Docker Compose runs a one-shot bootstrap container before starting the API and worker. Schema migrations and standards data synchronization are separate operations.

The webhook request path performs only bounded body reading, HMAC verification, JSON validation, event filtering, and an idempotent queue insert. Slow provider, embedding, and model calls occur in the worker. Jobs are claimed with `FOR UPDATE SKIP LOCKED`, retried with backoff, and recovered if a worker dies while holding a job.

## Knowledge model

Knowledge is layered:

| Scope | Content | Identity |
|---|---|---|
| `public` | bundled language/framework packs | pack ID and version |
| `company` | optional organization policy | tenant key |
| `repo` | project description, requirements, standards | immutable repository key |
| `task` | issue and PR acceptance criteria | repository key and task key |

`standard_documents` stores identity, provenance, scope, version, and source text. `standard_chunks` stores retrieval-sized text and embeddings. `standard_rules` stores structured policy used by deterministic or model-assisted evaluation.

Bundled packs are authored files shipped with the release, but production reviews never retrieve those files directly. `repomonster standards sync` imports them into PostgreSQL and records the pack checksum, embedding model, dimensions, and readiness state.

## Repository identity and configuration

Namespaces use provider host and immutable provider repository/project IDs rather than mutable names. Repository configuration is loaded from the trusted base branch and maps paths to stack packs.

Repository source updates are versioned by their base-branch blob or commit SHA. A changed source creates a new document version and disables the superseded version. Pack selections are pinned per repository.

## Retrieval

For each review:

1. identify changed-file stack mappings from trusted repository configuration
2. collect the PR description, linked task requirements, CI results, and immutable head SHA
3. run deterministic completeness and evidence checks
4. build semantic queries from task intent and changed paths
5. filter documents by repository, task, selected pack version, language, framework, scope, and enabled state
6. rank eligible chunks by cosine distance
7. construct a bounded review brief from the highest-value chunks and diff evidence
8. request structured findings from the configured insight endpoint
9. merge and validate findings before publishing a provider check

The diff and CI output are review evidence, not permanent public-standard content. Large diffs may be summarized or temporarily chunked under a review-run namespace.

## Provider boundary

Provider adapters are responsible for authentication, webhook verification, authoritative diff retrieval, linked-task retrieval, and publication. The review engine receives a provider-independent `ReviewRequest`.

The GitHub adapter verifies `X-Hub-Signature-256`, exchanges a signed App JWT for a short-lived installation token restricted to the webhook repository, reads policy from the default branch, and publishes a Check Run on the current PR head SHA. GitLab deployments will use a project/group token or OAuth integration; that full adapter is not implemented yet. Credentials are referenced from deployment secret storage and are never read from repository configuration.

## Execution boundary

RepoMonster consumes required CI checks and machine-readable lint artifacts. It does not execute arbitrary commands supplied by a repository inside the API or review worker. If direct tool execution is added later, it requires a separate ephemeral runner with no model/provider secrets and constrained network/filesystem access.
