# Operations guide

This guide covers a single-host Docker Compose deployment with PostgreSQL/pgvector, a GitHub App, and operator-controlled OpenAI-compatible embedding and insight endpoints.

## Prerequisites

- Docker Engine with the Compose plugin
- a DNS name and TLS reverse proxy for GitHub webhooks
- a GitHub App installed on repositories to review
- an OpenAI-compatible embedding endpoint
- optionally, an OpenAI-compatible chat-completions endpoint for model findings
- outbound connectivity from the worker to GitHub and model endpoints

The supplied Compose file binds the API to `127.0.0.1:8000` and attaches it to an external Docker network named `shared`. A reverse proxy is not included.

## Environment configuration

Start from the template:

```bash
cp .env.example .env
```

### Database and API

| Variable | Required | Purpose |
|---|---|---|
| `POSTGRES_DB` | no | database name; defaults to `repomonster` |
| `POSTGRES_USER` | no | database user; defaults to `repomonster` |
| `POSTGRES_PASSWORD` | yes | database password used by every service |
| `REPOMONSTER_PORT` | no | loopback host port; defaults to `8000` |
| `REPOMONSTER_API_KEY` | direct API only | bearer key for `POST /review`; empty disables the route |
| `TENANT_KEY` | no | optional company-policy retrieval namespace; ingestion is not yet public |
| `LOG_LEVEL` | no | worker logging level; defaults to `INFO` |

Generate passwords and API keys with a cryptographically secure tool. Do not commit `.env`.

### Embedding endpoint

| Variable | Required | Purpose |
|---|---|---|
| `EMBEDDING_BASE_URL` | yes | OpenAI-compatible base URL ending before `/embeddings` |
| `EMBEDDING_MODEL` | yes | exact model identifier exposed by the endpoint |
| `EMBEDDING_DIMENSIONS` | yes | vector length returned by the model |
| `EMBEDDING_API_KEY` | endpoint-dependent | bearer token sent to the endpoint |

Example:

```dotenv
EMBEDDING_BASE_URL=http://100.80.20.15:1234/v1
EMBEDDING_MODEL=text-embedding-model
EMBEDDING_DIMENSIONS=1024
EMBEDDING_API_KEY=local-server-token
```

Embedding dimensions are unrelated to context length. They become part of the pgvector schema during initial migration. A model returning 1024-dimensional vectors must use `EMBEDDING_DIMENSIONS=1024`.

### Insight endpoint

| Variable | Required | Purpose |
|---|---|---|
| `INSIGHT_BASE_URL` | no | OpenAI-compatible base URL ending before `/chat/completions` |
| `INSIGHT_MODEL` | when base URL is set | exact chat model identifier |
| `INSIGHT_API_KEY` | endpoint-dependent | bearer token sent to the endpoint |
| `INSIGHT_TIMEOUT_SECONDS` | no | model request timeout; defaults to 90 seconds |
| `INSIGHT_MAX_TOKENS` | no | optional client-side generation ceiling; omitted by default so the endpoint controls generation |

Without `INSIGHT_BASE_URL`, RepoMonster runs deterministic checks only. When configured, insight failures result in manual escalation.

Local models may need a larger timeout:

```dotenv
INSIGHT_TIMEOUT_SECONDS=900
WEBHOOK_LOCK_TIMEOUT_SECONDS=1200
```

Set these values in the deployment `.env` next to `docker-compose.yml`; do not
commit that file. Leave `INSIGHT_MAX_TOKENS` unset when RepoMonster should impose
no client-side generation ceiling. The model endpoint and context window can still
have their own limits. Keep the webhook lock timeout comfortably above the longest
expected end-to-end review duration.

The embedding and insight endpoints can share a base URL and API key, but they must use appropriate separate model identifiers.

### GitHub and worker limits

| Variable | Default | Purpose |
|---|---:|---|
| `GITHUB_APP_ID` | none | numeric GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | `/run/secrets/github_app_key` in the example | private key path inside the worker |
| `GITHUB_WEBHOOK_SECRET` | none | HMAC secret shared with GitHub |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub REST base URL |
| `GITHUB_API_VERSION` | `2026-03-10` | REST API version header |
| `GITHUB_TIMEOUT_SECONDS` | `30` | timeout per GitHub request |
| `GITHUB_MAX_CHANGED_FILES` | `1000` | maximum changed files accepted for a review |
| `GITHUB_MAX_KNOWLEDGE_FILES` | `100` | maximum default-branch knowledge files |
| `GITHUB_MAX_KNOWLEDGE_FILE_BYTES` | `500000` | maximum bytes per knowledge file |
| `MAX_WEBHOOK_BYTES` | `2000000` | maximum raw webhook body size |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | attempts before terminal failure |
| `WEBHOOK_LOCK_TIMEOUT_SECONDS` | `900` | stale processing-job recovery threshold |
| `WORKER_POLL_SECONDS` | `2` | queue polling interval |

Keep the lock timeout longer than the maximum expected full review duration.

## GitHub App setup

### URLs

- Homepage URL: your project or deployment page
- Webhook URL: `https://your-host.example/webhooks/github`
- Callback URL: leave blank; RepoMonster does not use user OAuth
- Setup URL: leave blank unless you add an installation UI

Set a random webhook secret and place the same value in `GITHUB_WEBHOOK_SECRET`.

### Repository permissions

Configure:

- Checks: read and write
- Commit statuses: read-only
- Contents: read-only
- Issues: read-only
- Metadata: read-only
- Pull requests: read-only

RepoMonster does not need administration, workflows, secrets, deployments, or write access to repository contents.

### Events

Subscribe to:

- Pull request

GitHub sends the `ping` event automatically when the webhook is configured. RepoMonster acknowledges it without queuing a review. Other event types are currently ignored by the GitHub webhook route.

### Private key

Generate a private key in the GitHub App settings and copy the PEM to the host:

```bash
sudo mkdir -p /opt/repomonster/secrets
sudo cp /path/to/downloaded-key.pem /opt/repomonster/secrets/github-app.pem
sudo chown root:65532 /opt/repomonster/secrets/github-app.pem
sudo chmod 640 /opt/repomonster/secrets/github-app.pem
```

The worker image runs as UID/GID `65532:65532`. The Compose mount is:

```yaml
volumes:
  - /opt/repomonster/secrets/github-app.pem:/run/secrets/github_app_key:ro
```

Therefore `.env` must contain:

```dotenv
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_app_key
```

Only the worker needs this mount. The API verifies webhook signatures with the webhook secret and should not receive the App private key.

Verify access without printing the key:

```bash
docker compose exec worker sh -lc '
  id
  ls -ln "$GITHUB_PRIVATE_KEY_PATH"
  test -r "$GITHUB_PRIVATE_KEY_PATH" && echo "private key readable"
'
```

## Model connectivity

### Private local endpoint with Tailscale

Tailscale is optional but useful when LM Studio or another model server runs on a workstation and RepoMonster runs on a VPS.

1. Join the workstation and VPS to the same tailnet.
2. Bind the model server so it accepts network connections.
3. Enable the model server's API authentication if available.
4. Set the endpoint base URLs to the workstation's Tailscale IP.
5. Test from the VPS before starting bootstrap.

```bash
curl -H "Authorization: Bearer $MODEL_API_KEY" \
  http://100.80.20.15:1234/v1/models
```

The model server's MCP options are unrelated to RepoMonster's OpenAI-compatible HTTP calls.

If Tailscale or the local model server is stopped, insight reviews will fail safely with manual escalation. Bootstrap and vector retrieval also require the embedding endpoint when new content must be embedded.

## Docker deployment

Create the reverse-proxy network once:

```bash
docker network inspect shared >/dev/null 2>&1 || docker network create shared
```

Start the complete deployment:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bootstrap api worker
```

Expected bootstrap output on first installation:

```text
migrations applied: 001_initial.sql, 002_webhook_jobs.sql
standard packs updated: python@1.0.0, fastapi@1.0.0, typescript@1.0.0, node@1.0.0
```

Subsequent starts normally report no migrations and no changed packs.

### Services

| Service | Role | Lifecycle |
|---|---|---|
| `postgres` | PostgreSQL 16 + pgvector, queue, policy, vectors, results | long-running |
| `bootstrap` | migrations and bundled-pack synchronization | one-shot |
| `api` | health, direct review API, webhook verification and enqueue | long-running |
| `worker` | GitHub retrieval, repository sync, review, publication | long-running |

### Reverse proxy

The API joins the external `shared` network with alias `repomonster-api`. A Dockerized Nginx proxy on that network can use:

```nginx
location / {
    proxy_pass http://repomonster-api:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The public endpoint must preserve the webhook request body. Do not cache `/webhooks/*`, and do not place interactive browser challenges in front of `/webhooks/github`. Cloudflare does not replace GitHub HMAC verification.

The GitLab route is normalization-only and does not yet verify a GitLab secret. Do not treat it as a production integration; deny `/webhooks/gitlab` at the reverse proxy until the adapter is implemented. Protect or disable FastAPI's `/docs` and `/openapi.json` routes if API discovery should not be public.

Use a valid origin certificate when Cloudflare SSL mode is Full (strict). A Cloudflare 526 indicates origin certificate validation failure; a 502 commonly indicates that Nginx cannot reach the API container or the containers do not share a network.

## Health and readiness

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl https://your-host.example/readyz
```

- `/health` and `/healthz` report process health.
- `/readyz` checks database access and counts ready standard packs.
- `/` intentionally returns 404; RepoMonster does not currently serve a website.

Unknown JavaScript, CSS, login, and `robots.txt` requests are normally automated internet scans. A 404 response is expected.

## Observability

Follow application logs:

```bash
docker compose logs -f api worker
```

The API should return `202 Accepted` for valid GitHub webhook deliveries. The worker logs completion or an exception and attempt number.

Inspect recent queue jobs:

```bash
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
    SELECT delivery_id, event_name, status, attempts, last_error, created_at
    FROM webhook_deliveries
    ORDER BY id DESC
    LIMIT 20;
  "'
```

Inspect recent review outcomes:

```bash
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
    SELECT provider, repository_key, external_id, head_sha, gate_state, created_at
    FROM review_runs
    ORDER BY id DESC
    LIMIT 20;
  "'
```

## Updating

Pull and rebuild after code or dependency changes:

```bash
git pull --ff-only origin main
docker compose up -d --build
docker compose logs --tail=100 bootstrap api worker
```

Bootstrap applies pending migrations automatically. Applied migration files are checksummed and must not be edited; add a new numbered SQL migration.

If only worker code changed and no migration was added:

```bash
git pull --ff-only origin main
docker compose up -d --build --force-recreate worker
```

Pulling does not update `.env` because it is deployment-local. Compare new variables in `.env.example` after upgrades.

## Management commands

The image exposes these commands:

```bash
repomonster db migrate
repomonster standards sync
repomonster standards reindex
repomonster bootstrap
repomonster worker
repomonster repository sync-local /path/to/checkout \
  --provider github \
  --provider-base-url https://github.com \
  --external-id 123456 \
  --full-name owner/repository
review-gate examples/review_request.json
```

- `db migrate` applies pending schema migrations only.
- `standards sync` imports changed bundled pack versions.
- `standards reindex` regenerates bundled-pack vectors even when source checksums are unchanged.
- `bootstrap` runs migration followed by standard synchronization.
- `worker` starts the long-running queue consumer used by Compose.
- `repository sync-local` indexes one trusted local checkout for development or operator-controlled onboarding.
- `review-gate` runs the offline deterministic fixture path and does not use PostgreSQL.

## Stopping and restarting

Stop containers while preserving PostgreSQL data:

```bash
docker compose down
```

Restart later:

```bash
docker compose up -d
```

Do not use `docker compose down -v` unless intentionally deleting the PostgreSQL volume and all indexed standards, repository knowledge, jobs, and review history.

Tailscale and a local model server can be stopped independently while RepoMonster is down. If SSH to the VPS itself uses Tailscale, stopping Tailscale on the VPS will terminate that access path.

## Backup

The named PostgreSQL volume is persistent but is not a backup. Use `pg_dump` before migrations or major upgrades:

```bash
docker compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > repomonster.dump
```

Store the dump outside the VPS and protect it as potentially sensitive repository metadata.

RepoMonster currently has no automatic retention or pruning command. Webhook payloads, document versions, review runs, and findings accumulate until an operator introduces a retention policy. Do not delete rows without understanding document and review foreign-key relationships.

## Troubleshooting

### Embedding request times out

Confirm the endpoint is reachable from the VPS and worker container, the model is loaded, the server accepts network connections, and Tailscale is connected when used.

### Embedding dimensions do not match

Example:

```text
Embedding service returned 1024 dimensions; expected 1536
```

Set `EMBEDDING_DIMENSIONS` to the model's actual vector length before initializing the database. If the database already exists with the wrong dimensions, migrate/reindex or recreate the volume during development. Context length is not the embedding dimension.

### Private key not found

Ensure the `.env` path is the container path, not the host path:

```dotenv
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_app_key
```

### Private key permission denied

Set host ownership to `root:65532` and mode `640`, then recreate the worker.

### PostgreSQL cannot determine a parameter type

Update to a release containing explicit nullable retrieval casts. This was fixed after the initial GitHub integration release.

### Insight endpoint timed out

Increase `INSIGHT_TIMEOUT_SECONDS`, verify the model remains loaded, and ensure the worker lock timeout exceeds the full review duration. If the model returns an empty `message.content`, leave `INSIGHT_MAX_TOKENS` unset and confirm the model endpoint itself permits enough generation and has sufficient context for the prompt, reasoning, and final JSON. A timeout or missing final answer produces manual escalation rather than a pass.

### Model output has an invalid severity or evidence format

Current parsing accepts case-insensitive `info`, `warning`, and `error`, and normalizes scalar evidence into a one-item list. Upgrade older deployments if model output causes manual escalation or character-by-character evidence.

### No linked task or issue

Either:

- create a local issue and reference its actual number, or
- set `checks.require_task_reference: false` on the repository's default branch.

GitHub issues and PRs share numbering; referencing a PR number does not count as task context.

### Delivery exhausted all retries

Fix the underlying configuration or endpoint problem, then edit the PR description or push another commit to create a fresh GitHub delivery. Redelivering the same GitHub delivery ID is deduplicated.

### Check remains manual escalation

Read the Check Run detail and worker exception. Manual escalation is expected for endpoint failures, malformed model output, and terminal worker failures. RepoMonster deliberately does not convert automation uncertainty into success.
