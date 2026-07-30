# Addendum to ADR-0006: three observed ways the gate fails open

ADR-0006 reasons from GitHub's documentation that a missing Actions environment
is auto-created without protection rules, and concludes the gate must be asserted
in code. Setting the environment up for real on 2026-07-30 produced three
concrete failures, two of which the ADR does not describe. Recorded because the
in-code assertion has to catch all three, and only one is the case the ADR
anticipated.

## 1. A rejected protection rule still creates the environment

Creating `ai-pr-review-runtime` with a required reviewer on a **private** repo on
a free plan returns:

    HTTP 422: Failed to create the environment protection rule. Please ensure
    the billing plan supports the required reviewers protection rule.

The call fails. **The environment is created anyway**, with
`protection_rules: []`. So a setup script that creates the environment, sees a
422, and stops — or one that ignores the error — leaves behind an environment
that exists, has no reviewers, resolves instantly, and reports green. This is not
the documented auto-creation path the ADR describes; it is a partial success that
looks like a total failure, and it fails open.

## 2. Changing visibility silently strips protection rules

Verified by API immediately either side of the change:

    public,  before:  protection_rules: ["required_reviewers"]
    private, after:   protection_rules: []

No warning, no error, no notification. The environment keeps its name, its
deployment history and its secrets; it loses the only thing that made it a gate.
A repository that was correctly configured months ago is now fail-open because
someone flipped visibility for an unrelated reason.

This is the most dangerous of the three, because nothing in the repository
changes and no run fails. The next credential-bearing job simply proceeds.

**Consequence for the assertion:** it is not enough to check the environment at
setup time, or to check that it exists. The reviewer list must be re-resolved
inside the gated job on every run, which is what ADR-0006 already requires — this
is the concrete reason why, and it is stronger than the reason given there.

## 3. Required reviewers are a paid feature on private repositories

Environment protection rules are free on public repositories and require
Pro/Team/Enterprise on private ones. So the gate cannot be made real on a private
repository on a free plan, at any price in code: the assertion can *detect* the
absence and refuse to run, but it cannot create protection that the plan does not
offer.

That makes the assertion's refusal path load-bearing rather than theoretical, and
it means "create these environments with required reviewers" is not always a
precondition a consumer *can* satisfy. A consumer on a free private repo has two
honest options: make the repository public, or run no credential-bearing job.

## Status

smtithy is private and has **no** environment at this point, deliberately. The
`ai-pr-review-runtime` environment was created, observed to lose its protection
on the visibility change, and then deleted rather than left in place — an
unprotected environment with the right name is worse than none, because the
in-code assertion is the only thing standing between it and a live credential,
and a future refactor could plausibly treat "environment exists" as sufficient.

The `BEDROCK_ROLE_ARN` secret remains set. It is inert: the role's trust policy
requires the subject claim
`repo:svozza/smtithy:environment:ai-pr-review-runtime`, and with no such
environment GitHub mints no token that matches, so the ARN is unusable by anyone
who holds it.

The environment gets recreated — with protection verified by API rather than
assumed — when the first credential-bearing job lands.
