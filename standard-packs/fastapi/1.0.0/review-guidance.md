# FastAPI review baseline

Apply this pack together with the Python baseline and any repository-specific API policy.

## Request and response contracts

Represent externally supplied data with explicit validation models. Review changes to field optionality, defaults, aliases, response filtering, and status codes as public contract changes. Response models should prevent accidental exposure of internal fields.

## Routes and dependencies

Keep route handlers focused on HTTP concerns and orchestration. Use dependencies for shared request-time concerns where their lifecycle and failure behaviour remain clear. Domain logic should remain independently testable rather than depending directly on framework request objects.

## Errors and observability

Translate expected domain failures into deliberate HTTP responses. Unexpected failures should retain diagnostic context without disclosing secrets or internal details to clients. Changes to authentication, authorization, or data ownership require explicit review evidence.

## Tests and documentation

Test changed routes through the HTTP boundary, including validation and expected failures. If a public contract changes, update the relevant API documentation, examples, or migration guidance.
