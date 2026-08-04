# Second addendum to ADR-0007: the commanded finding is derived, not supplied

The first addendum made the commanded scope a checked property and, in doing so,
made `finding.json` an input to a gate rather than evidence. It also recorded what
that check did *not* establish:

> It now passes exactly the gate an accepted artifact's finding passed.

That is shape. It is not membership. A well-shaped finding that no accepted
artifact contained passed both gates, so a forged `finding.json` could direct a
real remediation at a defect no reviewer ever found, on any file in the pull
request. Two independent review engines raised it, and it was closed on
**reachability**: the remediation lane had no workflow, so nothing a contributor
could reach composed `context_dir`. `plan_loop`'s own docstring said what would
change that — "wiring one without binding the commanded finding to an accepted
artifact would make this a live gap rather than a latent one."

Wiring the `/fix` channel is that wiring. So the binding comes first.

## The key that was not needed

The property was described as needing a signature over the accepted artifact,
which the harness has no key for. That framing is what made this look like a
design decision with no good answer, and it is the wrong frame: it assumes the
gate must authenticate a finding handed to it.

It does not have to be handed one. **The finding is derived.** The context and the
bundle carry the accepted artifact and the ordinal the command named; each gate
runs the review verifier over `review.json` — provenance, the markdown allowlist,
the secret scan, the same gate that accepted it — and indexes the result.

Membership then holds structurally. There is no separate finding to forge, because
there is no finding input. And no key is required, because neither gate is
comparing its input against another job's copy of the same thing: each **accepts
the artifact it holds**, exactly as `post.py` accepts the review job's artifact
before posting it. The recursion the old framing implied — "verify the copy
against the copy" — never arises.

What travels in the bundle is therefore the artifact plus one integer. The
integer is the only part that cannot be re-derived, and the first addendum already
said why: which finding a maintainer commanded is a fact about the *command*, not
about the pull request.

## The ordinal is the rendered position

`/fix 3` means the third finding of the comment the commander read.
`post.render` sorts findings by severity; `review.json` holds the order the model
emitted them in. So resolving the ordinal against the artifact names a *different*
finding than the human pointed at whenever those two orders differ.

This failure is worth stating plainly because it is invisible. Both findings are
real, both are in the same accepted artifact, and every gate passes — the
remediation is simply for the wrong defect. Nothing in a log would look wrong.

`artifact.rendered_findings` is therefore the single sort, in the shared contract
module rather than in the executor: the plan session resolves the same ordinal,
and the generator side must not import the executor to do it. `render` calls it, so
the comment and the ordinal cannot drift apart.

The bounds on the integer are ordinary except for two that are not: `bool` is an
`int` in Python, so `true` would resolve `findings[1]`; and a negative value is the
one out-of-range ordinal that silently resolves to a real finding, because Python
indexes from the end.

## The compensating witness

Deriving from the artifact proves the finding was in *an* accepted review. It does
not prove that review was ever *posted* — and the commander is acting on a comment
they read.

So the command additionally requires a comment the harness owns
(`post.find_own_comment`: the marker on line 1 **and** `resolve_bot_login`)
carrying `post.sha_stamp(head_sha)` for the commanded SHA. That is GitHub's own
authorship record, which only the harness's credential could have produced, and it
reuses both helpers unchanged rather than parsing the comment's markdown.

Parsing was considered and refused. The comment is the commander's exact view,
which nothing else can claim, but recovering a finding from it means a markdown
round-trip whose injection surface is contributor-influenced: finding bodies quote
contributor code, and a fenced block can contain a literal `####` heading. The
witness is therefore existence-and-SHA only. It answers "was a review posted for
this SHA?", which is the question the artifact cannot answer, and it does not try
to answer the one the artifact answers better.

## Consequences

- `check_commanded_finding` is gone rather than kept alongside. A shape check that
  no longer bounds anything is a check that reads as enforcement while enforcing
  something the caller already established.
- The bundle contract changed, so the plan eval scenarios carry `review.json` and
  `commanded_index.json`. Their fixture test now asserts that each scenario's
  review is one `verify()` accepts — load-bearing rather than advisory, because a
  scenario whose artifact could not have been posted now fails before the model is
  invoked, and the eval would otherwise report a generator failure for a fixture
  defect.
- The executor passes the diff pair it fetched itself into the derivation. An
  artifact accepted against the bundle's own copy of the diff would be checked
  against input from the job that process distrusts, which is the provenance
  posture `post.py` established and the first addendum restated.
- `review.json` has to reach the `/fix` run from the review run's Actions artifact,
  so the workflow needs `actions: read`. Retention is finite: a command on a
  review whose artifact has expired is refused, not guessed at.
