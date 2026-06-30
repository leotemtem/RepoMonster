# Python review baseline

This pack is a curated review baseline. Repository policy takes precedence where it makes a deliberate, documented choice.

## Interfaces and typing

Changed public functions and module boundaries should make their accepted inputs, returned values, and failure behaviour understandable. Type annotations are most valuable at boundaries and should not be weakened without a documented reason. Avoid broad escape hatches where a narrower type or explicit validation would preserve useful guarantees.

## Structure and readability

Names should communicate domain intent. Keep responsibilities cohesive and control flow direct enough that a reviewer can follow important state changes. Comments should explain constraints, trade-offs, or reasons that are not apparent from the code; they should not narrate straightforward statements.

## Errors and resource handling

Catch exceptions at a level that can add context, recover, or translate the failure. Avoid suppressing unrelated exceptions. Resource ownership and cleanup should remain explicit, particularly around files, network clients, database sessions, and transactions.

## Tests

Tests should demonstrate the changed behaviour at the narrowest useful level. Include important failure paths and boundary conditions. Assertions should verify outcomes rather than merely proving that code executed.
