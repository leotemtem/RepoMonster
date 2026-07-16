# RepoMonster

RepoMonster is a self-hosted readiness gate for pull requests. It combines deterministic repository policy, repository-scoped retrieval-augmented generation (RAG), and an operator-supplied language model to answer one question:

> Is this change ready for a human maintainer to review?

RepoMonster does not approve or merge code. A successful check means that the change supplied enough evidence and did not produce blocking findings; responsibility remains with the maintainer.

## Current status

The complete automated workflow currently supports GitHub pull requests through a GitHub App:

```text
GitHub webhook
    -> signed ingestion
    -> durable PostgreSQL job
    -> trusted repository configuration
    -> authoritative PR, issue, file, and CI evidence
    -> pgvector retrieval
    -> deterministic and model findings
    -> GitHub Check Run
```

Implemented capabilities include:

- signed GitHub webhook verification and delivery deduplication
- short-lived GitHub App installation authentication scoped to one repository
- durable jobs with retries, backoff, and stale-job recovery
- trusted default-branch `.repomonster.yml` onboarding
- repository descriptions, requirements, standards, and linked GitHub issues as RAG sources
- versioned Python, FastAPI, TypeScript, and Node.js public standard packs
- PostgreSQL metadata filtering followed by pgvector similarity ranking
- deterministic description, traceability, testing, CI, change-size, and documentation checks
- structured findings from an OpenAI-compatible insight endpoint
- GitHub Check Run publication against the authoritative PR head SHA
- authenticated direct review API and an offline local simulation CLI
- checksummed SQL migrations and idempotent standard-pack bootstrap

The provider-neutral review core and GitLab event normalization exist, but GitLab authentication, authoritative merge-request retrieval, queue processing, and result publication are not implemented. See [Harness capabilities](docs/harness-capabilities.md) for the exact capability matrix.

## Documentation

- [Usage guide](docs/usage.md): repository setup, review behavior, policy, direct API, and local simulation
- [Operations guide](docs/operations.md): Docker deployment, GitHub App setup, model connectivity, upgrades, and troubleshooting
- [Harness capabilities](docs/harness-capabilities.md): current boundaries, extension points, and potential integrations
- [Architecture](docs/architecture.md): trust boundaries, storage model, retrieval, and execution flow
- [Repository example](examples/repository/.repomonster.yml): complete Python/FastAPI policy example

## Quick start

Copy the environment template and set at least the PostgreSQL password and embedding endpoint:

```bash
cp .env.example .env
```

RepoMonster's Compose file expects an external network named `shared` for a reverse proxy. Create it once if it does not already exist:

```bash
docker network create shared
```

Create the host directory for the GitHub App private key, place the generated PEM there, and make it readable by the non-root worker container:

```bash
sudo mkdir -p /opt/repomonster/secrets
sudo chown root:65532 /opt/repomonster/secrets/github-app.pem
sudo chmod 640 /opt/repomonster/secrets/github-app.pem
```

Then build and start the database, bootstrap job, API, and worker:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bootstrap api worker
```

The API binds to `127.0.0.1:8000` by default. Publish it through a TLS reverse proxy and configure the GitHub App webhook as:

```text
https://your-host.example/webhooks/github
```

Readiness is available at:

```bash
curl https://your-host.example/readyz
```

Full deployment and GitHub App instructions are in the [operations guide](docs/operations.md).

## Repository policy

Each installed repository owns its review context and policy through `.repomonster.yml` on its default branch:

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
  require_task_reference: false
  require_test_evidence: true
  auto_block:
    mode: shadow
    require_poor_documentation: true
    minimum_model_impact: significant

model:
  profile: default
```

An issue is optional. With `require_task_reference: false`, the structured PR description is the task specification. With it set to `true`, analysis still runs, but code changes cannot pass the readiness gate until a resolvable task is linked.

Repository configuration cannot contain model URLs or credentials. Those remain deployment-level secrets.

## Gate outcomes

The default profile applies these outcomes after deterministic and model findings are combined:

| Outcome | Default condition | GitHub conclusion |
|---|---|---|
| Ready for human review | no errors and at most one warning | `success` |
| Needs author updates | warnings exceed the profile tolerance | `action_required` |
| Blocked | one or more errors, or a matching enforced auto-block policy | `action_required` |
| Manual escalation | insight endpoint or result processing failed | `neutral` |

Informational findings never block. The default profile tolerates one warning and evaluates the composite auto-block policy in shadow mode. Shadow mode records when the policy would match but cannot change the gate. Set `checks.auto_block.mode: enforce` only after reviewing shadow results. Profiles are deployment-owned policy files selected by repository configuration.

## Supported standard packs

The initial release includes these pinned packs:

- `python@1.0.0`
- `fastapi@1.0.0`
- `typescript@1.0.0`
- `node@1.0.0`

Bundled packs are imported into PostgreSQL during bootstrap. Production reviews retrieve their database versions rather than reading authored files directly.

## Local development

```bash
python3 -m pip install -e '.[db,web]'
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src review-gate examples/review_request.json
```

The local `review-gate` simulation reads bundled pack files and runs deterministic checks without PostgreSQL or a language model. It is useful for policy development but is not equivalent to the production GitHub workflow.

## Security model

- Repository policy is read from the trusted default branch, never the untrusted PR head.
- GitHub signatures are checked against the unmodified request body before JSON parsing.
- Installation tokens are short-lived and restricted to the repository that triggered the event.
- The API does not receive the GitHub App private key; only the worker mounts it.
- Provider and model credentials cannot be supplied by repository configuration.
- Diffs, source files, issues, CI output, and retrieved documents are treated as untrusted evidence.
- RepoMonster consumes CI state but does not execute repository code in the credential-bearing containers.
- Model failure results in manual escalation, never an automated pass.

More detail is available in [Architecture](docs/architecture.md).

## Project boundary

RepoMonster currently publishes one summary Check Run. It does not yet publish inline annotations, submit GitHub review decisions, add labels, merge pull requests, execute linters, read PR comments, or provide a complete GitLab workflow. These boundaries are deliberate and documented in [Harness capabilities](docs/harness-capabilities.md).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development
setup, the required checks, and the trust boundaries to preserve. Participation is
governed by our [Code of Conduct](CODE_OF_CONDUCT.md).

To report a security vulnerability, follow the [security policy](SECURITY.md) and
use private reporting rather than a public issue.

## License

RepoMonster is released under the [MIT License](LICENSE).
