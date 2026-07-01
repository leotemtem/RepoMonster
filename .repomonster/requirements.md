# RepoMonster requirements

- Provider webhook signatures must be verified against the unmodified request body before JSON is trusted or work is queued.
- GitHub access must use short-lived installation tokens scoped to the repository that triggered the delivery.
- Repository policy and RAG knowledge must come from the trusted default branch, never from pull-request head content.
- Webhook delivery IDs and review identities must be idempotent so retries cannot create uncontrolled duplicate work.
- The API request path must not wait for provider fetching, embeddings, or model inference.
- The review process may consume CI evidence but must not execute arbitrary repository code in a container that holds provider or model credentials.
- Model failures and malformed model output must fail toward manual review, not an automated approval.
- Database schema changes must be new ordered migrations; applied migration files are immutable.
- Behavior changes require focused tests and documented test evidence in the pull-request description.
