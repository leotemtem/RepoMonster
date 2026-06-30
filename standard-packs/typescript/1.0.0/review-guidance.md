# TypeScript review baseline

This pack assumes strict type checking unless the repository documents a narrower compatibility requirement.

## Runtime boundaries

Treat network responses, environment variables, parsed files, and other external values as untrusted runtime data. Validate or narrow them before they enter typed domain code. Avoid assertions that merely silence uncertainty without establishing a runtime fact.

## Types and interfaces

Types should expose useful domain distinctions and make invalid states harder to represent. Avoid unnecessary `any`, broad casts, and optional fields used as substitutes for deliberate variants. Public interfaces should remain stable or include migration evidence.

## Modules and control flow

Keep module responsibilities cohesive and dependency direction understandable. Prefer narrowing and exhaustive handling for meaningful variants. Asynchronous control flow should make error propagation and cancellation behaviour visible.

## Tests

Type checking belongs in CI but does not replace runtime tests. Tests should exercise changed behaviour, external-data validation, important asynchronous failures, and boundary conditions.
