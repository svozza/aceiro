# A decline is a reply from the command channel, not an artifact and not a step

The decline channel has been owed since the remediation lane was designed: the honest
exit for a commanded fix the vocabulary cannot express. The open question was whether
a decline is a **new artifact kind** or a **plan step kind**.

It is neither. A decline is a refusal the **command channel** reports, before any
model runs, to the person who issued the command.

## What made this due

ADR-0013 is what turns the decline from owed-eventually into load-bearing. `/fix 1,3`
on a **fork** pull request names two paths, so `decide_delivery` routes `stacked_pr`,
and `execute_plan` then refuses: a fork's head branch does not exist in the base
repository for a pull request to be based on (ADR-0009's addendum records that fork
asymmetry as accepted).

That refusal is correct. Where it lands is not. The commander has by then spent the
approval gate, a full model session, both gates, the prover and a `contents: write`
job, and their receipt is a red run with an `::error::` line in a log they must click
into. The refusal was knowable at **command time** — `prepare_fix_context` already
fetches the pull-request object, and two commanded findings on distinct paths
guarantee the stacked route.

Before ADR-0013 this path was unreachable: a single `/fix N` is one file, always
suggestions, never fork-blocked. It is now the only reachable case where a
well-formed, fully-verified plan for a legitimately-scoped command has **no possible
delivery**, which is the decline channel's definition.

## Why not a plan step kind, and why not an artifact kind

ADR-0004's reservation machinery invites a `decline` step, and it is the wrong shape
on three counts.

`check_plan_cardinality` refuses a plan with no fix step precisely so that a fixless
plan cannot verify — protecting "the one shape that reaches `contents: write` while
remediating nothing". A `decline` step is a fix step that fixes nothing, so it needs
an exemption carved into the rule guarding that shape.

It spends a **model session** to learn a fact the harness established before the
session began. And it puts the decline's reason in model-authored text, so the
harness would be posting a model's explanation of a **harness** limitation — a fork
having no branch to base a pull request on is not a fact the generator knows or
should narrate.

A new artifact kind carries every one of those objections and adds a third generator
output to maintain.

## The refusal already exists; what was missing is an addressee

`prepare_fix_context.Refused` states the contract exactly:

> Distinct from producing nothing: a body that is not a command is not a refusal,
> because there is nobody to tell. Every `Refused` here is a case where someone DID
> command a fix and the harness will not perform it.

ADR-0007's third addendum found the same gap from the opposite direction, as a reason
auto-posted suggestions cannot ship: "`decide_delivery`'s Refusal has NO addressee
without a commander." Under a command there is one, by construction.

So the decline channel is not new vocabulary. It is giving that refusal a reply.

## Which refusals reply: exactly two

A command can be refused for a dozen reasons. Only two get a comment.

- **Undeliverable by construction** — the fork plus multi-path scope above, and
  anything else the harness knows at command time cannot be delivered on this pull
  request at all.
- **Already delivered** — a repeat command whose deduplication key matches an
  existing follow-up pull request. Added deliberately: it is the refusal a maintainer
  is most likely to want an answer to, and the message already names and links the
  pull request that answers it.

Everything else stays a red run, for a reason that is a security property rather than
a preference. **The untrusted-commander refusal must never be replied to.** Trust is
resolved as `prepare_fix_context`'s second step, so everything before it runs for an
untrusted commenter; replying there would let any passer-by make the harness post a
comment naming them. That is the shape `parse_fix_command` already refuses when it
declines to report malformed commands — "there is nothing to report to a commander
who did not issue one, and every non-command body on a busy pull request would
otherwise produce noise."

Replying to every `Refused` would therefore need a hand-maintained exemption for that
one case, and a hand-maintained security exemption list is what §2's silently
unasserted gate-lane list already cost. Two named refusals keep the decline
**derivable from the command's own shape**: a command the channel cannot express gets
a reply, a run that failed gets a failed run.

## The posting job, and why `command` does not do it

The `command` job's design property is stated in the workflow: it "holds NO
credential and no write scope. Everything that decides whether this command is
honoured at all happens here, before any model or write token exists in any job."

Granting it `pull-requests: write` breaks that sentence in the job reached directly
from `issue_comment`, the channel anyone can write to, upstream of the trust check. A
write scope there is one minted for anybody who types `/fix 1`, whatever it is used
for.

So a **fourth job, `decline`**, holds `pull-requests: write` and posts. The
credential-free job decides and the scoped job acts — `route` → `execute`/`stack`
reused, with the credential minted only when a refusal was genuinely derived,
downstream of the trust check.

**It has two producers, because the two refusals are knowable in different places.**
`fix_key` needs `anchor_signatures` over the quarantine tree, which `command` never
fetches (the quarantine is fetched in `plan` and re-fetched in `execute`/`stack`). So
`command` emits the decline for the undeliverable case, `stack` emits one when it
catches `AlreadyDelivered`, and `decline` fires on either. One posting job, one
reason format, two producers; `stack` gains an output and no new scope.

Two alternatives were rejected:

- **`stack` posts its own reply.** It already holds `pull-requests: write`, so this
  costs no scope at all — but it makes the job holding the lane's broadest credential
  the one that talks to humans, and it splits the decline into two implementations
  that must agree on their text. ADR-0009's second addendum's entire finding was an
  artefact whose text claimed something its run had not established.
- **Hoist the deduplication check into `command`** by fetching a quarantine there.
  Tempting, because it would refuse a duplicate before the approval gate and the
  model session. But it puts `find_existing_fix`'s live pull-request listing and a
  computed dedup key in the credential-free job, and the answer would be
  **advisory**: `stack` must re-check regardless, since the posture is that the token
  holder re-establishes everything itself. Two readers of one property is the defect
  class three misleading comments about `create_ref` came from.

Both are still worth doing in their cheap half: the fork-plus-multi-path check
belongs in `prepare_fix_context`, where the refusal costs seconds instead of a model
session. Cheap **and** visible, not one or the other.

## The comment is marker-keyed and upserted

Every other artefact the harness posts is reconciled — found again, edited,
superseded. The decline is the first that is purely additive in intent: it reports a
fact about one command and nothing later needs to revise it.

It still needs a marker, for accumulation rather than reconciliation. The lane sets
`cancel-in-progress: false` so that no maintainer's command is discarded, which means
every retry runs, which means N retried commands would leave N identical comments —
the wrapper-accumulation problem ADR-0009's first addendum measured at nine, in a
channel with no upsert. So `post.upsert_comment` is reused unchanged: marker on line
1, ownership by marker **and** `resolve_bot_login`, edit in place.

**The marker must not be `post.MARKER`.** The reviewer's sticky comment owns that
one, and sharing it would make the two lanes fight over a single comment — the
reviewer's next push overwriting the decline, or the decline overwriting the review.
That is `supersede_previous_reviews`' unscoped-authority defect waiting to happen
somewhere new.

## Consequences

- An upsert destroys the previous decline's text, so a commander who was declined
  twice for different reasons sees only the latest. Accepted: a decline is a statement
  about the current state of the channel, not a log. It satisfies ADR-0009 addendum
  B's self-dating rule by carrying the head SHA and the ordinals it spoke for, so it
  never claims a currency a later run must correct.
- The reason text is **harness-authored**, so nothing model-controlled reaches the
  comment. The decline names a limitation of the channel; there is no field in it a
  generator writes.

  **Amended 2026-08-10.** The second sentence read "there is no field in it a
  generator writes" and was true of the *generator* and false of everything else,
  which is the distinction the sentence elided. The undeliverable reason
  interpolates the commanded **paths**, and a finding's path must name a file the
  pull request touched (`path_must_be_changed_file`) — so a **contributor** authors
  that text. Schema-constrained and verified as provenant, but not harness-derived.

  The consequence was measured, not theoretical: `emit` refuses a value carrying
  the `GITHUB_OUTPUT` heredoc delimiter, and the policy path pattern admitted
  `SMTITHY_DECLINE_EOF` as a substring, so a contributor who named a file after it
  suppressed their own decline on a fork — the "honest refusal nobody is notified
  of" this ADR exists to prevent, reached through the mechanism built to prevent
  it, and self-serve since the contributor controls both the fork-ness and the
  filename. Suppression only: closing the heredoc needs a line consisting solely of
  the delimiter, the path pattern forbids newlines, and a backtick cannot leave the
  code span, so nothing untrusted ever reached a trusted effect.

  Fixed by making the delimiter inexpressible in the path grammar (it carries `+`
  and `=`), **not** by switching the guard from refusing to escaping: an escape is
  a fail-open answer to a value that should not exist. The durable half is this
  amendment — a future author reasoning from the old sentence would conclude the
  delimiter guard is defence in depth, when it is load-bearing on contributor
  content. Recorded in addendum D's shape: the claim is corrected rather than the
  code stretched to make it true.
- A red run on `issue_comment` appears in the Actions tab and **not** on the pull
  request's timeline — there is no check-run surface for a comment-triggered workflow
  the way there is for a push. That is why failing cheaply is not sufficient on its
  own: an honest refusal nobody is notified of is the "silent — the harness declined
  to fix something and told nobody" case ADR-0007's third addendum forbids.
- ADR-0007's third addendum said auto-posted suggestions are "strictly downstream of
  the decline channel" because `decide_delivery`'s refusals would have no addressee.
  That blocker is now answered for commanded runs only: an uncommanded lane still has
  no addressee, so the revisit trigger there is unchanged.
- The fork asymmetry itself is not fixed and is not fixable here. A multi-file fix on
  a fork pull request still has no automated delivery (ADR-0009's addendum, accepted).
  What changes is that the commander is told so, in seconds, in the place they are
  looking.
