# RepoMonster project description

RepoMonster is a self-hosted review-readiness gate with a complete GitHub pull-request workflow and a provider-neutral core intended for future providers. It combines deterministic policy checks with repository-scoped retrieval and an operator-configured language model. Its decision is whether a change is ready for a human maintainer; it never approves or merges code autonomously.

The FastAPI process verifies and enqueues signed GitHub App webhooks. Slow provider, embedding, and insight calls run in a separate worker backed by a durable PostgreSQL queue. PostgreSQL and pgvector store repository configuration, versioned standards, task context, retrieval chunks, review runs, and findings. GitLab event normalization exists, but its authenticated provider workflow is not implemented.
