# Addendum to ADR-0006: six observed ways the gate goes wrong

ADR-0006 reasons from GitHub's documentation that a missing Actions environment
is auto-created without protection rules, and concludes the gate must be asserted
in code. Setting the environment up for real on 2026-07-30 produced three
concrete failures, two of which the ADR does not describe. Recorded because the
in-code assertion has to catch all three, and only one is the case the ADR
anticipated.

Failures 4 and 5 are a pair and are about the *conditions*, not the environment:
a gate that fires more often than the job it gates, and a gate that fires for a
job that cannot run. Both are cases of the gate condition and the worker
condition disagreeing.

Failure 6 is about neither, and is the assertion's own: a rule that exists, is
the right type, and carries reviewers whose approval means nothing.

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

## 5. A gate whose worker excludes drafts asks an unanswerable question

The same shape as failure 4, reached from the other end and found by review
rather than by configuration. Both workflows requested approval for a draft
(`|| github.event.pull_request.draft` on the approve job) while the job being
approved excluded drafts (`&& !github.event.pull_request.draft`). So every draft
push from a fork author parked a pending deployment review on the `ai-pr-review`
environment, and approving it ran nothing: the worker was guaranteed to skip
whatever the reviewer clicked.

This is §4's harm without §4's fail-closed consolation. Five approval requests
from one iterating draft, none of which does anything, is the most efficient way
to teach a maintainer that these requests are meaningless — and the one that
arrives after the PR leaves draft *does* execute fork code against a live
Bedrock credential.

It also left a coverage hole. Every run while the PR was a draft was skipped by
the exclusion, and `ready_for_review` was not a listed trigger type, so a PR
whose last push happened in draft could merge having never run a single eval
scenario, with a check list indistinguishable from any other PR's.

**Resolved by evaluating drafts**, which is what ADR-0008 and the README already
describe ("untrusted and draft authors wait at the environment's required
reviewer before their code runs with the credential, whatever the trigger") — the
code was the thing out of step. The `!draft` exclusion is gone from
`evals.yml`'s `evals` job and `ai-pr-review.yml`'s `review` job; the approve
conditions are unchanged. No new trigger type is needed: `opened` covers a
draft's first commit and `synchronize` every push after, so the state at the
moment a PR leaves draft has always been graded.

The generalisation worth keeping: **a gate job's condition must imply the worker
job's condition.** Whenever the two are edited apart, one of the two failures
follows — an unanswerable request (this failure) or an unconditional one (§4).
`tests/test_workflow_shape.py::TestDraftSemanticsAgree` asserts the implication
over both workflows, since it is a fact about YAML that no other test can see.

## 6. A rule of the right type with the wrong people on it

Found by review rather than by configuration, and it is the assertion's own blind
spot rather than GitHub's. `has_required_reviewers` asked whether the environment
carries a `required_reviewers` rule with a non-empty reviewer list, and stopped
there. It never asked what those reviewers can do.

GitHub permits a **read-only collaborator** as an environment reviewer, and a
deployment reviewer may approve their own run. So:

    reviewers: [read-only-collaborator]   ->  rule present, list non-empty
                                              -> "gate is real", run proceeds

That account opens a pull request, approves its own deployment, and executes its
own code against the live Bedrock credential without ever holding write. It is
§1 and §2's fail-open reached one step later: the rule exists, it is the right
type, and it gates nothing that matters.

`author_trust.py` already carries the whole argument for why the permission API
is the only answer here — a read-only outside collaborator reports
`COLLABORATOR`, the same value a maintainer reports on a private repo — so the
gate resolves reviewers through `is_trusted` rather than restating it. "Trusted"
means one thing in this harness: write-or-above, from the permission API,
fail-closed on anything unresolvable.

**EVERY eligible reviewer, not any.** Approval takes one of them, so the weakest
decides; a rule listing four maintainers and one drive-by contributor gates
nothing. That also disposes of `prevent_self_review`, which looks like a separate
obligation and is not: once every eligible reviewer could push the change
directly, approving their own run is not an escalation.

Teams need their own resolution, and the vocabularies differ — the repository's
team list reports git verbs (`pull`/`triage`/`push`/`maintain`/`admin`) while the
collaborator API reports the legacy quartet (`admin`/`write`/`read`/`none`). Two
sets, because one set covering both would silently mean "write" in one place and
nothing in the other. A team the repository does not list has no access.

**Consequence for the assertion, and it generalises §2's:** re-resolving the
reviewer LIST on every run is not enough either. The list can stay identical
while a member's permission is revoked, which is the same silent transition §2
describes with nothing at all changing in the repository. Both halves — the rule
and the people — are re-resolved inside the gated job on every run.

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
