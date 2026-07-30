# Addendum to ADR-0006: four observed ways the gate goes wrong

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

## 4. Protecting the wrong environment defeats the gate without failing open

The other direction, and the one committed *here* rather than inherited. There are
two environments and they do different jobs:

    ai-pr-review          required_reviewers, no branch policy    <- the gate
    ai-pr-review-runtime  branch policy only, NO reviewers        <- scopes the
                                                                    credential

Read from the origin repository's live API, not inferred from its comments.

Putting `required_reviewers` on the *runtime* environment looks like defence in
depth and is not. `eval_approve` deliberately **skips** when the author holds
write-or-above on a non-draft pull request, and skips entirely on
`workflow_dispatch` and `schedule` — but a protected runtime environment blocks
the eval job on every path regardless, so the trusted-maintainer fast path waits
for a click too. The conditional's logic stays correct and the configuration
silently overrides it.

This fails *closed*, so nothing leaks. It is still a defect: a gate that fires for
everyone carries no information about anyone, and the cost lands on the routine
path, which is where people learn to click without reading. A gate that is
always-on trains the reviewer to approve reflexively, which is how the gate stops
being a gate long before anyone edits its configuration.

The mirror-image error — binding the gate to the runtime environment, so it
resolves instantly against a branch policy with no human involved — is the origin
repository's own recorded defect. Both errors are one question asked the wrong way
round, which is why the environments' names are worth reading carefully every time
either is touched.

The runtime environment's real protection is a **branch policy**: only the default
branch may reach the credential. That is also what makes `pull_request_target`
load-bearing, since the policy checks the ref the *run* executes under — the base
branch for `pull_request_target`, versus `refs/pull/N/merge` for `pull_request`,
which would never match.

## Status

smtithy is **public**. `ai-pr-review` holds the `required_reviewers` rule and is
the gate. `ai-pr-review-runtime` should hold a branch policy allowing only `main`
and **no** reviewers — as of this writing it still carries the reviewers that
failure 4 describes, and swapping them for the branch policy is outstanding.

Everything asserted here was verified by reading protection back from the API
rather than by trusting a create call to have worked — the practical lesson of
failure 1, where the call returned 422 and produced an environment anyway.

The sequence this repository actually went through, since it is the sequence any
consumer might:

    private  -> create gate  -> 422, environment exists with NO protection
             -> deleted
    public   -> create gate  -> protection_rules: ["required_reviewers"]

In between, the repository was public with a working gate, then private again for
unrelated reasons (git-defender blocks pushes to unapproved public repos), and
that transition silently stripped the rule — failure 2, observed rather than
reasoned about.

Public is therefore not incidental here: on a free plan it is a *precondition*
for the gate existing at all. It also has a cost that pushed the other way for a
while — Actions minutes are free on public repositories and metered on private
ones, so CI only runs while public on this account.

The `BEDROCK_ROLE_ARN` secret has been set throughout, including while no
environment existed. That was safe, and checking why is worth the two commands:
the role's trust policy requires the subject claim

    repo:svozza/smtithy:environment:ai-pr-review-runtime

so with no such environment GitHub mints no matching token and the ARN is
unusable by whoever holds it. The claim and the environment name must stay
character-identical; a rename on either side silently breaks federation, and the
symptom is an OIDC error rather than anything mentioning environments.

Still true, and the reason this addendum exists: none of the above is a substitute
for the in-code assertion. Every state above was reached without a single run
failing, so nothing here would have been noticed by CI.
