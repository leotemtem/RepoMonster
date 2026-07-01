# RepoMonster Python and provider-adapter standards

- Keep provider-specific authentication and payload handling outside the provider-independent review engine.
- Treat webhook payloads, repository files, diffs, issues, CI output, and retrieved text as untrusted evidence.
- Bound payload sizes, file counts, file sizes, diff context, model context, retries, and network timeouts.
- Never log private keys, webhook secrets, installation tokens, model API keys, or authorization headers.
- Use immutable provider repository IDs for namespaces and current authoritative head SHAs for published checks.
- Persist enough job state to recover after process termination and expose terminal failures to maintainers.
- Convert expected repository onboarding failures into actionable Check Run output; retry transient provider, database, and model failures.
- Keep deterministic gate findings separate from model-generated maintainability findings.
