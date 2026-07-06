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

## Container operations

Run Compose commands from the directory containing `docker-compose.yml` and the
deployment `.env` file.

### Inspect container state

```bash
docker compose config --quiet
docker compose ps --all
docker compose top
```

`config --quiet` validates the merged Compose configuration. `ps --all` includes
the one-shot `bootstrap` container, which should normally be `Exited (0)` after
startup. `postgres`, `api`, and `worker` should be running; PostgreSQL should also
be healthy. Get the full PostgreSQL health-check result with:

```bash
docker inspect "$(docker compose ps -q postgres)" \
  --format '{{json .State.Health}}'
```

Inspect live container resource use and Docker disk use:

```bash
docker stats --no-stream
docker system df
docker compose images
```

`docker system prune` is not a routine RepoMonster operation. Review its proposed
deletions before using it, and never remove the `repomonster-data` volume unless
database loss is intentional.

### Read logs

```bash
docker compose logs --tail=200 --timestamps postgres bootstrap api worker
docker compose logs --since=30m --timestamps api worker
docker compose logs -f --tail=100 api worker
```

Follow only the worker when investigating review execution:

```bash
docker compose logs -f --tail=100 worker
```

The API should return `202 Accepted` for a valid GitHub delivery. The worker logs
its startup, a delivery ID on completion, and the delivery ID plus attempt number
and traceback on failure. Locate one delivery in retained worker logs with:

```bash
docker compose logs --since=24h worker | grep -F '<delivery-id>'
```

Docker logging configuration controls retention. Configure rotation in the Docker
daemon or Compose logging settings before relying on container logs for historical
auditing.

### Restart or recreate services

Use `restart` when the image and environment are unchanged:

```bash
docker compose restart api worker
```

Recreate a service after changing its environment, image, or mounted secret:

```bash
docker compose up -d --build --force-recreate api worker
docker compose ps --all
```

Restart PostgreSQL only during a maintenance window. Stop API ingestion first and
allow the worker to finish its current job:

```bash
docker compose stop api
docker compose stop -t 180 worker
docker compose restart postgres
docker compose up -d api worker
```

The worker handles `SIGTERM` by finishing the job it is processing and then
exiting. The Compose file allows three minutes for this. Increase
`stop_grace_period` if reviews can take longer; forcibly killing a worker leaves
its job in `processing` until `WEBHOOK_LOCK_TIMEOUT_SECONDS` elapses.

### Run one-shot administrative containers

Rerun migrations and bundled-pack synchronization with an ephemeral bootstrap
container:

```bash
docker compose run --rm bootstrap
```

Run only one management operation by overriding that service's command:

```bash
docker compose run --rm bootstrap repomonster db migrate
docker compose run --rm bootstrap repomonster standards sync
```

These commands use the deployment environment and internal database network. Do
not run multiple migrations or reindexes concurrently. Migrations take a
transaction-scoped advisory lock, but concurrent embedding and indexing work still
adds avoidable load.

## Database, worker, and job operations

### Open the database operations console

Open `psql` inside the database container. This uses the configured database name
and user rather than assuming the defaults:

```bash
docker compose exec postgres sh -lc \
  'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The queries below are read-only and can be pasted into that console. Use `\q` to
exit. Avoid selecting the `payload`, `request_payload`, document body, or finding
detail columns during routine checks because they can contain repository data.

### Understand queue state and progress

A webhook delivery follows this state model:

```text
pending -> processing -> completed
                      -> ignored
                      -> pending (retry scheduled)
                      -> failed (attempts exhausted)
```

`attempts` increments when a worker claims a job. `available_at` is the time a
pending retry becomes claimable. `locked_at` is when processing began.
`external_result_id` is populated after the worker creates the GitHub Check Run.

The queue does not record a percentage or the worker's current internal stage.
For a processing job, use `locked_at`, `external_result_id`, worker logs, and the
presence of a `review_runs` row as milestones. A `review_runs` row means the gate
result was persisted; publication to GitHub happens immediately afterward.

Summarize the whole queue:

```sql
SELECT status,
       count(*) AS jobs,
       min(created_at) AS oldest_created_at,
       max(updated_at) AS last_updated_at
FROM webhook_deliveries
GROUP BY status
ORDER BY status;
```

Inspect recent jobs without printing webhook bodies:

```sql
SELECT id,
       delivery_id,
       event_name,
       status,
       attempts,
       available_at,
       locked_at,
       external_result_id,
       left(last_error, 160) AS error,
       created_at,
       updated_at
FROM webhook_deliveries
ORDER BY id DESC
LIMIT 20;
```

Focus on outstanding work and show how long it has waited or processed:

```sql
SELECT id,
       delivery_id,
       status,
       attempts,
       CASE
         WHEN status = 'pending'
           THEN greatest(available_at - now(), interval '0')
       END AS retry_in,
       CASE
         WHEN status = 'processing' THEN now() - locked_at
       END AS processing_for,
       external_result_id,
       left(last_error, 160) AS error
FROM webhook_deliveries
WHERE status IN ('pending', 'processing', 'failed')
ORDER BY available_at, id;
```

Interpret the result as follows:

| Observation | Meaning | Operator action |
|---|---|---|
| pending, `available_at <= now()` | ready for a worker | verify a worker is running and can reach PostgreSQL |
| pending, `available_at > now()` | retry backoff | inspect `last_error`; fix the dependency before the next attempt |
| processing, recent `locked_at` | worker owns the job | follow worker logs; allow the configured model timeout |
| processing older than the lock timeout | worker may have died | verify worker/container state; a healthy worker will reclaim it |
| failed | attempts exhausted | fix the cause and trigger a new PR event |
| ignored | unsupported, draft, closed, or superseded work | no action unless the event should have been eligible |

The worker reclaims stale `processing` rows automatically after
`WEBHOOK_LOCK_TIMEOUT_SECONDS`. Do not manually unlock a live job: that can run the
same delivery concurrently. This release has no supported cancel or requeue CLI.
After a terminal failure, edit the PR description or push a commit to generate a
new delivery; redelivery with the same provider delivery ID is deduplicated.

### Inspect review outcomes

Review runs are written after evidence gathering and review evaluation complete.
Show recent results with finding counts:

```sql
SELECT rr.id,
       rr.provider,
       rr.repository_key,
       rr.external_id,
       left(rr.head_sha, 12) AS head_sha,
       rr.gate_state,
       rr.insight_recommendation,
       count(rf.id) FILTER (WHERE rf.severity = 'error') AS errors,
       count(rf.id) FILTER (WHERE rf.severity = 'warning') AS warnings,
       count(rf.id) FILTER (WHERE rf.severity = 'info') AS info,
       rr.created_at
FROM review_runs AS rr
LEFT JOIN review_findings AS rf ON rf.review_run_id = rr.id
GROUP BY rr.id
ORDER BY rr.id DESC
LIMIT 20;
```

When the auto-block policy is in `shadow` mode, inspect matches before enabling
enforcement:

```sql
SELECT rr.repository_key,
       rr.external_id,
       rr.gate_state,
       rr.insight_recommendation,
       rr.insight_recommendation_reason,
       rf.evidence AS significant_findings,
       rr.created_at
FROM review_runs AS rr
JOIN review_findings AS rf ON rf.review_run_id = rr.id
WHERE rf.rule_id = 'auto-block-shadow'
ORDER BY rr.id DESC
LIMIT 50;
```

Review these rows for false positives and evidence quality. Change a repository to
`checks.auto_block.mode: enforce` only after the shadow decisions match maintainer
expectations.

`external_id` is the provider's pull-request number. A completed queue job may
legitimately have no `review_runs` row when it was ignored or when repository
configuration failed before evaluation; inspect its GitHub Check Run and worker
log in that case.

### Inspect migrations and indexing

Confirm the schema versions applied to this database:

```sql
SELECT version, applied_at
FROM schema_migrations
ORDER BY version;
```

Inspect standard-pack and repository-source indexing state:

```sql
SELECT status, count(*) AS pack_versions, max(updated_at) AS last_updated_at
FROM standard_pack_versions
GROUP BY status
ORDER BY status;

SELECT status, count(*) AS repository_sources, max(indexed_at) AS last_indexed_at
FROM repository_standard_sources
GROUP BY status
ORDER BY status;
```

Any long-lived `indexing`, `pending`, or `failed` state warrants checking the
`bootstrap` or worker logs and the embedding endpoint. `/readyz` requires database
access and ready standard packs, but it does not prove that every repository source
has indexed successfully.

### Inspect PostgreSQL activity and size

```sql
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT pid,
       application_name,
       state,
       wait_event_type,
       wait_event,
       age(clock_timestamp(), query_start) AS runtime,
       left(query, 120) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY query_start;
```

Idle connections are normal. Investigate sessions that remain `active` or blocked
for longer than the expected embedding, insight, or migration operation. Do not
terminate a database backend until its owning container and transaction are
identified.

### Operate and scale workers

Each worker processes one delivery at a time. PostgreSQL row locking with
`FOR UPDATE SKIP LOCKED` allows multiple workers to consume different jobs safely:

```bash
docker compose up -d --scale worker=2 worker
docker compose ps worker
docker compose logs -f --tail=100 worker
```

Return to one worker with:

```bash
docker compose up -d --scale worker=1 worker
```

Scale only after confirming that the database, GitHub rate limit, embedding
endpoint, and insight endpoint can handle the additional concurrency. More workers
do not make a single review faster, and they do not bypass retry backoff.

### Routine triage sequence

When reviews appear stuck, check in this order:

1. Run `docker compose ps --all`; verify PostgreSQL is healthy and a worker is running.
2. Call `/readyz`; distinguish API/database readiness from worker execution.
3. Query outstanding queue rows and note `delivery_id`, status, attempts, and age.
4. Search worker logs for that delivery ID and read `last_error` in PostgreSQL.
5. Verify GitHub and model endpoint connectivity from the worker's network context.
6. Fix the dependency, then allow a scheduled retry or trigger a new PR event after terminal failure.

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

For a planned shutdown, stop webhook ingestion, allow the worker to drain its
current delivery, and then remove the containers:

```bash
docker compose stop api
docker compose stop -t 180 worker
docker compose down
```

The named PostgreSQL volume is preserved. Restart the deployment and verify both
the one-shot bootstrap result and long-running services:

```bash
docker compose up -d
docker compose ps --all
docker compose logs --tail=100 bootstrap api worker
```

Use `docker compose stop <service>` and `docker compose start <service>` when a
container should remain defined. Use `down` when the deployment's containers and
default network should be removed.

Do not use `docker compose down -v` unless intentionally deleting the PostgreSQL
volume and all indexed standards, repository knowledge, jobs, and review history.

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
