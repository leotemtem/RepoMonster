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
- GitHub and GitLab webhook normalization
- Docker image and Docker Compose deployment

Provider authentication, authoritative diff fetching, asynchronous jobs, and publishing checks/comments are the next provider-specific implementation milestone. Current webhook routes normalize events but intentionally do not claim to complete that workflow.

## Self-hosted deployment

Copy and edit the environment file:

```bash
cp .env.example .env
```

At minimum, configure a PostgreSQL password and an OpenAI-compatible embedding endpoint. The insight endpoint is optional; without it RepoMonster only runs deterministic checks.

Start the database, bootstrap job, and API:

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

Check readiness:

```bash
curl http://localhost:8000/readyz
```

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

During onboarding, a provider adapter must read this configuration from the trusted default branch, fetch its referenced files, and call `RepositoryKnowledgeSynchronizer`. That service registers the stable repository identity, stores selected packs, chunks and embeds repository documents, and disables superseded document versions.

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
- Keep provider and model credentials in deployment secrets.
- Treat code and retrieved text as evidence, not model instructions.
- Consume existing CI/lint results; do not execute arbitrary repository commands in the API container.
- Use immutable provider repository IDs for database namespaces.
- Publish model output only after schema validation and deterministic gate evaluation.
