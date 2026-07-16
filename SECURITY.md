# Security Policy

RepoMonster is a self-hosted service that handles provider webhooks, short-lived
credentials, and untrusted pull-request evidence. Security reports are taken
seriously.

## Supported versions

RepoMonster is pre-1.0. Security fixes are applied to the latest `main` and the
most recent tagged release.

| Version | Supported |
|---|---|
| `0.1.x` / `main` | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Please report privately using GitHub's private vulnerability reporting:

- <https://github.com/leotemtem/RepoMonster/security/advisories/new>

If you cannot use that channel, email **leotemtem@gmail.com** with details.

Please include:

- a description of the issue and its impact,
- the affected component or endpoint,
- reproduction steps or a proof of concept,
- any relevant configuration or version information.

## What to expect

- Acknowledgement of your report within a few business days.
- An assessment and, where confirmed, a remediation plan.
- Coordinated disclosure once a fix is available. We are happy to credit
  reporters who wish to be named.

## Scope

RepoMonster's security model depends on the following principles. Reports that
demonstrate a break in any of these are especially valuable:

- Provider signatures are verified before webhook content is trusted.
- Provider credentials are short-lived and scoped to the triggering repository.
- Provider and model secrets are never read from repository-controlled config.
- All pull-request evidence is treated as untrusted data, never instructions.
- Repository code is never executed inside the API or worker containers.
- Model failure fails closed to `manual_escalation`, never an automated pass.
- Database uniqueness and metadata filters preserve namespace isolation.

Because RepoMonster is self-hosted, operators are responsible for securing their
own deployment (TLS termination, secret storage, database access, and network
exposure). See [`docs/operations.md`](docs/operations.md) and
[`docs/architecture.md`](docs/architecture.md) for deployment guidance.
