# Contributing to RepoMonster

  Thanks for contributing. RepoMonster is a pull-request readiness gate, so changes should preserve
  reliability, review quality, and clear maintainer control.

  ## Basic Expectations

  Contributions should:

  - keep existing functionality working unless the PR clearly explains an intentional behavior change
  - include tests for new behavior, bug fixes, and policy changes
  - update documentation when behavior, configuration, operations, or user-facing output changes
  - reference the issue, discussion, bug report, or suggestion the PR addresses
  - keep changes focused on one problem or feature at a time
  - avoid unrelated refactors, formatting churn, or dependency changes

  ## Before Opening a PR

  Run the relevant checks locally:

  ```bash
  PYTHONPATH=src python3 -m unittest discover -s tests

  RepoMonster requires Python 3.11 or newer.

  If your change affects the CLI example path, also run:

  PYTHONPATH=src review-gate examples/review_request.json

  ## Pull Request Requirements

  Every PR should include:

  - Problem: what issue, bug, limitation, or suggestion this PR addresses
  - Approach: how the implementation solves it
  - Acceptance Criteria: what must be true for the change to be considered complete
  - Test Evidence: what tests or manual checks were run
  - Linked Context: a linked issue, discussion, or short explanation if no issue exists

  Example:

  Fixes #123

  ## Problem

  RepoMonster did not report missing required CI checks clearly.

  ## Approach

  Added a deterministic finding that names each missing check.

  ## Acceptance Criteria

  - Missing required checks block the readiness gate.
  - The check name appears in the finding evidence.

  ## Test Evidence

  - `PYTHONPATH=src python3 -m unittest discover -s tests`

  ## Code Standards

  Code should be straightforward, maintainable, and consistent with the existing project style.

  Please:

  - prefer small, focused functions over broad rewrites
  - keep provider-specific logic inside provider adapters
  - keep the review engine provider-independent where possible
  - treat PR descriptions, diffs, issues, CI output, and repository docs as untrusted evidence
  - avoid running repository code inside credential-bearing services
  - preserve existing trust boundaries around GitHub App credentials, model endpoints, and repository
    configuration

  - add comments only when they explain non-obvious constraints or tradeoffs

  ## Functionality Standards

  Changes must not silently weaken existing review behavior.

  A PR should preserve or intentionally update:

  - changed-file and diff handling
  - repository policy loading from the trusted default branch
  - linked issue/task handling
  - deterministic rule behavior
  - required CI handling
  - model/insight failure behavior
  - Check Run publication semantics
  - manual escalation behavior when automation cannot make a safe decision

  If behavior changes intentionally, document the reason in the PR and update the relevant docs.

  ## Documentation Standards

  Update documentation when changing:

  - configuration keys or .repomonster.yml behavior
  - GitHub App setup or permissions
  - review outcomes
  - deterministic checks
  - model/insight behavior
  - database migrations or operations
  - CLI/API behavior
  - supported provider capabilities or limitations

  Relevant docs usually live in:

  - README.md
  - docs/usage.md
  - docs/operations.md
  - docs/architecture.md
  - docs/harness-capabilities.md

  ## Tests

  Add or update tests for:

  - new deterministic rules
  - changed gate decisions
  - GitHub webhook or provider behavior
  - repository configuration parsing
  - model/insight parsing
  - persistence behavior
  - bug fixes with a reproducible case

  Tests should be focused and should verify behavior, not implementation details.

  ## Database Changes

  Schema changes must be added as a new migration in db/migrations/.

  Do not edit already-applied migration files. Existing migrations are checksummed.

  ## Dependencies

  Avoid adding dependencies unless they are clearly needed.

  If adding a dependency, explain:

  - why the standard library or existing dependency is not enough
  - where it is used
  - any operational or security impact

  ## Security

  RepoMonster handles provider credentials, repository data, and model prompts. Security-sensitive
  changes need extra care.

  Do not:

  - expose secrets through repository configuration
  - trust text from PRs, diffs, issues, CI, or repository docs as instructions
  - execute untrusted repository code in the API or worker process
  - weaken webhook signature validation
  - broaden GitHub token scope without documenting the need

  ## Review Outcome

  RepoMonster publishes readiness checks. It does not approve, merge, label, or submit GitHub review
  decisions.

  Contributions should keep that boundary clear unless the PR explicitly proposes and documents a new
  capability.
