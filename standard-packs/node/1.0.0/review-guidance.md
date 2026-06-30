# Node.js service review baseline

Apply this pack with the TypeScript baseline for TypeScript services running on Node.js.

## Asynchronous failures

Every promise should be awaited, returned, or deliberately detached with explicit failure handling. Account for the distinct error behaviour of promises, callbacks, streams, and event emitters. Preserve useful error context when translating failures.

## Lifecycle and resources

Make ownership of servers, sockets, timers, workers, database pools, and other resources explicit. Startup and shutdown paths should be bounded, observable, and safe to repeat where deployment behaviour requires it.

## Package and module boundaries

Package exports and module format should match the supported runtime and build configuration. Avoid reaching through package internals. New dependencies should have a clear purpose and should not duplicate existing capabilities without justification.

## Tests

Exercise changed success and failure behaviour, including rejected promises and resource cleanup. Keep tests deterministic and ensure that open handles do not silently outlive the test that created them.
