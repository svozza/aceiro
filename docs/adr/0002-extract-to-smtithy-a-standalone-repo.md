# Extract the harness into smtithy, a standalone repo

The AI review harness lives in `.github/scripts/ai_review/` in a fork of the
Powertools repo, which is the wrong home for something that is about to grow a
remediation mode and a plan prover. It moves to its own repo, named `smtithy` —
a smithy, where proposals get forged, with SMT visible in the name because
§20's review-and-remediate extension makes a solver the sound implementation
rather than an option.

Consumers use it from unrelated repositories: this staging repo, personal
projects as the proving ground, and possibly
`aws-powertools/powertools-lambda-oss-automation` later. Personal projects come
first because the real repos should not be the place a new trust architecture
is proven.

## Considered Options

- **Fork a public repo and build there.** Rejected: a fork gives git ancestry,
  which is the one thing not needed, and leaves the harness inside a consumer's
  `.github/`, which is what the extraction exists to undo.
- **Build directly in `powertools-lambda-oss-automation`.** Rejected for now.
  That repo is a webhook-driven CDK app whose one-Lambda-per-automation shape
  has no analogue of the credential split, whose conventions assume a single
  TypeScript toolchain, and which is org-scoped with an SSM allowlist so a
  personal project could never consume it. It remains a plausible destination.

## Consequences

- The consumer's checkout is the *subject* of review, so the harness must not
  be distributed by checking it out into the consumer's workspace. It arrives
  as a dependency with entry points on `PATH`.
- Repo-specific coupling to remove: `policy.json`'s `link_host_allowlist`, the
  prompt's project description, and `post.py`'s hardcoded `BOT_LOGIN`. The last
  is a security property, not a cosmetic one, and becomes a runtime-resolved
  identity rather than a config input.
- Two consumer-configurable credential axes fall out: model access (Bedrock via
  OIDC, or an API key for personal projects) and GitHub identity
  (`GITHUB_TOKEN` or a GitHub App).
- Actions environments are resolved against the *caller*, so a consumer without
  the approval environments configured may get an unprotected gate. This needs
  verifying empirically and, if confirmed, a startup assertion — it would be a
  fail-open in the one place the design is fail-closed.
- The harness must not assume it is inside Actions: context acquisition is
  transport-specific, verification and rendering are not. Drawing the package
  boundary there costs nothing now and is the line a Lambda port would need.
