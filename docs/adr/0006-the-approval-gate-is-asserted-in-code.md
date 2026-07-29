# The approval gate is asserted in code, not delegated to the consumer

The human gate is currently an Actions environment: `approve` declares
`environment: ai-pr-review`, and untrusted or draft pull requests wait there for
a required reviewer before the generator runs. Extraction breaks this, because a
called workflow's `environment:` resolves against the *caller*, and GitHub's
documented behaviour is that referencing a non-existent environment creates it:

> "Running a workflow that references an environment that does not exist will
> create an environment with the referenced name."
>
> "the newly created environment will not have any protection rules or secrets
> configured."

So a consumer that installs the reusable workflow without first creating
`ai-pr-review` gets an environment with no required reviewers. The `approve` job
succeeds instantly, `needs.approve.result == 'success'`, and the generator runs
against an untrusted fork pull request with a live credential in scope. The run
is green and nothing warns. That is a fail-open in the one place the design is
fail-closed, and it is silent by design rather than by accident.

A second limitation compounds it: `on.workflow_call` does not support the
`environment` keyword, and environment secrets cannot be passed from the caller.
The model credential must therefore be a repository-or-organization secret
rather than an environment secret, which removes the property that only a job
passing the gate can read it.

The gate is therefore asserted in code. Before the generator runs, the harness
queries the environment's protection rules and refuses to proceed when the
author is untrusted or the pull request is a draft and the environment has no
required reviewers. Any failure to resolve — 404, empty reviewer list, HTTP
error — is treated as ungated, the same fail-closed posture `author_trust.py`
takes for permissions.

## Consequences

- The assertion must run *inside* the gated job, not as a job the chain depends
  on. A separate gate job can itself be skipped, and a skipped ancestor skips
  its descendants — the failure mode fixed in 4b7299fc, which looks exactly like
  a refusal.
- This is the extraction's first new security-critical module, and it gets the
  mutation-verified discipline: the test must be shown to fail against a version
  that accepts an empty reviewer list.
- Consumer setup documentation cannot be advisory. "Create these two
  environments with required reviewers" is a precondition the harness enforces,
  not a recommendation, because the failure mode of skipping it is invisible.
- Verified against the GitHub documentation rather than inferred. Worth
  re-checking if GitHub changes implicit environment creation, since the
  assertion's necessity depends on that behaviour.
