# RepoMonster

RepoMonster is a self-hosted review gate for GitHub pull requests and GitLab merge requests. It combines deterministic checks with repository-scoped RAG and an operator-supplied model endpoint to decide whether a change is ready for human review.

It does not approve or merge changes autonomously.

## Implemented foundation

- PostgreSQL and pgvector production retrieval
- tracked, schema-only database migrations
- idempotent bundled-standard bootstrap and reindexing
- versioned Python, FastAPI, TypeScript, and Node.js standard packs
- repository-owned project descriptions, requirements, and standards
- metadata filtering before vector ranking
- OpenAI-compatible embedding and insight endpoint adapters
- deterministic review gates and structured model findings
- signed GitHub App webhook ingestion and delivery deduplication
- durable PostgreSQL review jobs with retries and stale-job recovery
- short-lived, repository-scoped GitHub installation authentication
- authoritative GitHub PR metadata, changed-file, issue, and CI retrieval
- GitHub Check Run publication against the PR head SHA
- GitLab webhook normalization (full GitLab provider workflow is not yet implemented)
- Docker image and Docker Compose deployment

RepoMonster publishes one summary Check Run. Inline annotations and the full GitLab provider workflow are separate milestones.

## Self-hosted deployment

Copy and edit the environment file:

```bash
cp .env.example .env
```

At minimum, configure a PostgreSQL password and an OpenAI-compatible embedding endpoint. The insight endpoint is optional; without it RepoMonster only runs deterministic checks.

Start the database, bootstrap job, API, and review worker:

```bash
docker compose up --build
```

The one-shot `bootstrap` service:

1. applies pending migrations
2. validates the configured embedding dimensions
3. discovers bundled packs under `standard-packs/`
4. embeds changed pack chunks
5. stores documents, rules, provenance, and vectors in PostgreSQL
6. skips unchanged pack versions on later starts

The PostgreSQL named volume preserves this data across container restarts.

The API verifies and enqueues GitHub webhooks, then returns immediately. The worker performs provider calls, repository knowledge synchronization, embedding retrieval, insight generation, persistence, and Check Run publication. Do not run the API without the worker when GitHub integration is enabled.

Check readiness:

```bash
curl http://localhost:8000/readyz
```

Follow worker activity separately:

```bash
docker compose logs -f worker
```

## GitHub App configuration

Configure the GitHub App with:

- webhook URL: `https://your-host.example/webhooks/github`
- a random webhook secret matching `GITHUB_WEBHOOK_SECRET`
- repository permissions: Checks read/write, Contents read-only, Issues read-only, Metadata read-only, Pull requests read-only, and Commit statuses read-only
- subscribed event: Pull request

Generate an App private key and mount it read-only into the `worker` service. The API verifies webhook HMAC signatures and does not need the App private key. The example Compose file maps the host key to `/run/secrets/github_app_key`, which must match `GITHUB_PRIVATE_KEY_PATH`.

```dotenv
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_app_key
GITHUB_WEBHOOK_SECRET=replace-with-the-app-webhook-secret
```

Install the App on each repository RepoMonster should review. A repository is onboarded automatically on its first supported pull-request delivery, provided `.repomonster.yml` exists on its default branch.

Supported pull-request actions are `opened`, `reopened`, `synchronize`, `edited`, and `ready_for_review`. Draft pull requests receive a neutral check and are reviewed after they are marked ready.

`POST /review` is a separate direct API intended for controlled integrations. It is disabled unless `REPOMONSTER_API_KEY` is set and then requires `Authorization: Bearer <key>`. This key is unrelated to the GitHub webhook secret, GitHub App private key, and model API keys.

## Repository configuration

Repositories own their policy through `.repomonster.yml` and referenced Markdown files. See [the complete example](examples/repository/.repomonster.yml).

```yaml
version: 1

knowledge:
  description: [.repomonster/project.md]
  requirements: [.repomonster/requirements.md]
  standards: [.repomonster/standards/*.md]

stacks:
  - paths: [app/**, tests/**]
    language: python
    framework: fastapi
    packs: [python@1.0.0, fastapi@1.0.0]

checks:
  required_ci: [test, lint]
  require_task_reference: true
  require_test_evidence: true

model:
  profile: default
```

Pack versions are pinned for reproducible reviews. Model URLs and API keys are deployment secrets and are rejected if placed in repository configuration.

`checks.require_task_reference` controls readiness, not whether analysis runs. When it is `true`, code changes without a resolvable issue or ticket receive a deterministic blocking finding, while repository and model analysis still continue. Set it to `false` when the structured pull-request description is the repository's authoritative task specification and a separate issue is not required.

During onboarding, a provider adapter must read this configuration from the trusted default branch, fetch its referenced files, and call `RepositoryKnowledgeSynchronizer`. That service registers the stable repository identity, stores selected packs, chunks and embeds repository documents, and disables superseded document versions.

The GitHub worker performs this onboarding automatically. Unchanged repository documents reuse their existing embeddings; changed blobs are versioned and re-embedded.

## Retrieval precedence

RepoMonster retrieves in this order:

1. task requirements and acceptance criteria
2. repository project description and standards
3. optional organization policy
4. repository-selected bundled packs

Repository identity, selected pack versions, language, framework, and scope are SQL filters. pgvector similarity ranks chunks only after those filters are applied.

## Management commands

```bash
repomonster db migrate
repomonster standards sync
repomonster standards reindex
repomonster bootstrap
repomonster repository sync-local /path/to/checkout \
  --provider github \
  --provider-base-url https://github.com \
  --external-id 123456 \
  --full-name owner/repository
```

`sync` is idempotent. `reindex` regenerates vectors, which is required after changing embedding models. Embedding dimensions are fixed when the database is first initialized; changing dimensions requires a migration and full reindex.

Database migrations are ordered SQL files under `db/migrations/`. The bootstrap command applies them transactionally under a PostgreSQL advisory lock and records a checksum in `schema_migrations`; an already-applied migration must never be edited. Add a new numbered migration instead.

`repository sync-local` exercises the same onboarding service that provider adapters use: it validates `.repomonster.yml`, pins selected packs, discovers referenced Markdown files, and stores repository-scoped embeddings. The external ID must be the immutable repository/project ID supplied by GitHub or GitLab.

## Local development

```bash
python3 -m pip install -e '.[db,web]'
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m review_gatekeeper.cli examples/review_request.json
```

The local simulation reads bundled pack source files directly for testability. The HTTP runtime retrieves standards only from PostgreSQL.

## Security boundaries

- Read `.repomonster.yml` from the base/default branch, never from untrusted PR head content.
- Validate GitHub's `X-Hub-Signature-256` against the unmodified request body before parsing JSON.
- Exchange the App JWT for a short-lived installation token scoped to the webhook repository.
- Keep provider and model credentials in deployment secrets.
- Treat code and retrieved text as evidence, not model instructions.
- Consume existing CI/lint results; do not execute arbitrary repository commands in the API container.
- Use immutable provider repository IDs for database namespaces.
- Publish model output only after schema validation and deterministic gate evaluation.

## Updating a deployment

After pulling a release that contains migrations or worker changes:

```bash
docker compose up -d --build
docker compose logs --tail=100 bootstrap
docker compose logs --tail=100 api worker
```

The bootstrap service should report `002_webhook_jobs.sql` once on this upgrade. Confirm that both `api` and `worker` remain running before testing a pull request.
