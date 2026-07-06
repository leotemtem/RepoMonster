# Usage guide

RepoMonster can be used in three distinct ways. They share the provider-independent review engine but do not provide identical context.

## Usage modes

| Mode | Intended use | RAG source | Publishes to provider |
|---|---|---|---|
| GitHub App | automatic pull-request readiness checks | PostgreSQL + pgvector | yes, one Check Run |
| Direct HTTP API | controlled internal integrations and testing | PostgreSQL + pgvector | no |
| Local `review-gate` CLI | deterministic policy development and examples | bundled files | no |

The GitHub App is the primary production workflow.

## GitHub App workflow

### 1. Prepare the default branch

Commit `.repomonster.yml` and every referenced knowledge file to the repository's default branch before expecting a pull request to be reviewed. RepoMonster deliberately ignores policy changes that exist only in the PR head.

A minimal Python/FastAPI setup is:

```text
.repomonster.yml
.repomonster/
  project.md
  requirements.md
  standards/
    backend.md
```

```yaml
version: 1

knowledge:
  description:
    - .repomonster/project.md
  requirements:
    - .repomonster/requirements.md
  standards:
    - .repomonster/standards/*.md

stacks:
  - paths:
      - app/**
      - tests/**
    language: python
    framework: fastapi
    packs:
      - python@1.0.0
      - fastapi@1.0.0

checks:
  required_ci: []
  require_task_reference: false
  require_test_evidence: true
  auto_block:
    mode: shadow
    require_poor_documentation: true
    minimum_model_impact: significant

model:
  profile: default
```

Use exact, repository-relative paths. Absolute paths and `..` traversal are rejected. Glob patterns are resolved against blobs in the default-branch Git tree.

### 2. Install the GitHub App

Install the App on the repository. Adding the App as a collaborator or reviewer is not sufficient; RepoMonster needs an installation identity to exchange its App JWT for a short-lived repository token.

The App needs the permissions and event described in the [operations guide](operations.md#github-app-setup).

### 3. Open or update a pull request

RepoMonster reacts to these `pull_request` actions:

- `opened`
- `reopened`
- `synchronize`
- `edited`
- `ready_for_review`

Draft PRs receive a neutral result and are analyzed after being marked ready. A delivery is ignored when its recorded head SHA has already been superseded or the PR is no longer open.

RepoMonster currently reads the PR title and description but not PR comments. Put required context in the PR description.

The default profile expects these sections for code changes:

```markdown
## Problem

What problem is being solved?

## Approach

How does the implementation solve it, and what tradeoffs were made?

## Acceptance Criteria

- State observable completion criteria.

## Test Evidence

Describe the tests and their results.
```

### 4. Read the Check Run

RepoMonster creates a check named `RepoMonster review gate` on the authoritative PR head SHA. The check combines deterministic findings and model findings.

The result is a readiness recommendation, not code approval:

- `success`: ready for a human maintainer
- `action_required`: blocked or needs author updates
- `neutral`: automation could not make a safe decision

Editing the PR description or pushing another commit creates a new webhook delivery and another review for the current state.

## Task and issue context

Task references are a repository policy choice.

### Description-led workflow

Use this when the PR description is the complete task specification:

```yaml
checks:
  require_task_reference: false
```

The model receives the PR description, repository knowledge, selected standards, changed paths, bounded diff excerpts, and CI state. No issue is required.

### Issue-led workflow

Use this when every code change must be traceable to an issue:

```yaml
checks:
  require_task_reference: true
```

Reference a local GitHub issue in the PR title or description:

```markdown
Fixes #42
```

GitHub issues and PRs share one numbering sequence. A reference to another PR is not accepted as task context. RepoMonster fetches a valid local issue, stores its title and body as task-scoped RAG content, and gives task content retrieval precedence.

Ticket-like identifiers such as `PAY-142` are recognized as references, but RepoMonster does not currently fetch Jira or another external tracker. Add a provider extension if external acceptance criteria must be retrieved authoritatively.

Analysis still runs when a required task is missing. The missing reference is a deterministic error that prevents the readiness check from passing.

## Repository configuration reference

### `version`

Required and currently fixed at `1`.

### `knowledge`

All fields are lists of repository-relative files or glob patterns:

| Field | Intended content |
|---|---|
| `description` | system purpose, architecture, boundaries, domain vocabulary |
| `requirements` | behavioral requirements, acceptance rules, operational constraints |
| `standards` | repository-specific coding, testing, security, and design standards |

Matched files are UTF-8 text. The operator controls maximum file count and file size with GitHub environment settings.

### `stacks`

Each stack mapping requires:

| Field | Meaning |
|---|---|
| `paths` | changed-file glob patterns belonging to the stack |
| `language` | normalized language name used by retrieval filters |
| `framework` | optional framework name used by retrieval filters |
| `packs` | one or more explicitly versioned `pack@version` selections |

For monorepositories, define multiple mappings:

```yaml
stacks:
  - paths: [services/api/**]
    language: python
    framework: fastapi
    packs: [python@1.0.0, fastapi@1.0.0]
  - paths: [packages/web/**]
    language: typescript
    framework: node
    packs: [typescript@1.0.0, node@1.0.0]
```

The dominant changed stack supplies the review request's primary language and framework. All matched pack selections are pinned during repository onboarding.

### `checks.required_ci`

An exact list of GitHub Check Run names or commit-status contexts that must report success:

```yaml
checks:
  required_ci:
    - test
    - lint
```

Start with an empty list until the repository's actual GitHub check names are known. Missing, queued, or failed required checks produce deterministic errors.

### `checks.require_task_reference`

- `true`: source changes require a task reference before the gate can pass
- `false`: the structured PR description can be the task specification

This setting controls readiness, not whether retrieval or model analysis runs.

### `checks.require_test_evidence`

When `true`, source changes require a `Test Evidence` section. A source change with no obvious changed test file also produces a warning.

### `checks.auto_block`

Controls the optional composite policy that combines deterministic PR-documentation quality with the model's evidence-backed recommendation:

```yaml
checks:
  auto_block:
    mode: shadow # off, shadow, or enforce
    require_poor_documentation: true
    minimum_model_impact: significant # significant or blocking
```

- `off`: do not evaluate the composite policy
- `shadow`: record an informational `auto-block-shadow` finding when the policy matches, without changing the gate
- `enforce`: return `blocked` when the policy matches

With `require_poor_documentation: true`, enforcement requires a deterministic PR-description finding such as missing required sections, missing test evidence, or weak written context for a large change. The model must also recommend `request_changes` or `block` and return at least one warning or error whose impact meets the configured threshold and whose evidence is non-empty. Model output alone cannot trigger this composite rule. Existing deterministic errors continue to block independently.

Start in `shadow` and review false positives before enabling `enforce`. Insight timeouts, malformed output, and missing recommendations remain `manual_escalation`; they never become automatic blocks.

### `model.profile`

Selects a deployment-owned JSON profile under `profiles/`. The repository can select a profile name but cannot define endpoint URLs, API keys, or arbitrary model settings.

The shipped `default` profile:

- requires Problem, Approach, Acceptance Criteria, and Test Evidence sections
- blocks on any error
- permits at most one warning before requiring author updates
- considers a change large at 400 changed lines
- evaluates auto-block matches in shadow mode
- retrieves task, repository, company, then public knowledge

## What the review evaluates

Deterministic checks currently evaluate:

- required PR-description sections
- required task reference for source changes
- documented test evidence
- presence of changed test files
- required CI status
- large changes with weak written context
- likely API/schema changes without documentation changes

The model is asked to evaluate:

- implementation fit with the stated task and acceptance criteria
- language and framework professionalism
- repository-specific architectural fit
- readability and maintainability
- whether comments explain useful intent rather than restating code
- whether the change should be blocked, updated, or sent to human review

Model findings use `info`, `warning`, or `error` severity, classify impact as `advisory`, `significant`, or `blocking`, and include an overall `ready`, `request_changes`, or `block` recommendation. Findings participate in the normal gate decision; the recommendation only affects the optional composite auto-block policy when all deterministic and evidence requirements also match.

## Direct HTTP API

The direct API is disabled unless `REPOMONSTER_API_KEY` is configured. Call it with a provider-independent review request:

```bash
curl -X POST https://your-host.example/review \
  -H "Authorization: Bearer $REPOMONSTER_API_KEY" \
  -H "Content-Type: application/json" \
  --data @examples/review_request.json
```

The response contains:

- `gate_state`
- `summary`
- structured `findings`
- the model's `insight_recommendation` and rationale when insight analysis ran
- `applied_profile`
- retrieved document IDs
- the generated model review brief

For production RAG, the supplied `repository_key` must identify a repository already onboarded in PostgreSQL. The direct endpoint does not fetch provider data, validate that a diff is authoritative, or publish a Check Run; the caller is responsible for constructing trustworthy request evidence.

## Local simulation

Run a deterministic example without PostgreSQL or model access:

```bash
PYTHONPATH=src review-gate examples/review_request.json
```

This mode reads bundled standards directly from disk. It is intended for developing request payloads and deterministic rules, not reproducing production vector retrieval or model output.

## Local repository onboarding

Operators and contributors can index a local checkout into PostgreSQL:

```bash
repomonster repository sync-local /path/to/checkout \
  --provider github \
  --provider-base-url https://github.com \
  --external-id 123456 \
  --full-name owner/repository
```

Use the provider's immutable repository ID for `--external-id`. This command reads `.repomonster.yml`, resolves its knowledge files, pins packs, chunks and embeds text, and stores repository-scoped documents. It does not configure a GitHub installation or publish reviews.
