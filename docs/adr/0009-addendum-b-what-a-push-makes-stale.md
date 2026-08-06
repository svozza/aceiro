# Second addendum to ADR-0009: what a push makes stale, and what may claim otherwise

Asked of the suggestion lane after it ran on a real pull request: what should a
whole prior remediation look like once the head moves? Three artefacts go stale
three different ways, and only one of them was getting it right.

Measured on `svozza/artel` #61, four `/fix` commands over a moving head:

| Artefact | On a push | Who does it |
| --- | --- | --- |
| suggestion comment | marked Outdated, `line` → `null` | GitHub, from the `commit_id` `submit_review` sends |
| review wrapper | claimed currency it had not established | nobody — only a later run |
| stacked follow-up pull request | branch cut from a dead head; still open | nothing acts on it |

## A stale suggestion is not a pending one

The tempting reading — "the outdated comments are still real issues, they should
still show" — was tested against the pull request and does not hold. Of the two
suggestions GitHub had marked outdated:

- the `client.rs` one was reviewed at `cd8fa363` where the line read
  `EVENTS_QUEUE_CAPACITY: usize = 64`, and at head it reads `256`. That is the
  suggestion's own replacement text: **the defect was fixed.**
- the `version.rs` one suggested `self.0 == client.0`, arguing from a doc contract
  that was same-version-only. At head, `supports` takes an `asker: Peer` and reads
  `Peer::Daemon => self.0 >= other.0` / `Peer::Client => self.0 <= other.0`, with
  documentation stating the asymmetry deliberately. The `client` binding the
  suggestion names no longer exists, and applying its text now would **destroy the
  intended behaviour**. The defect was redesigned away and the fix decayed from
  correct into harmful.

So the Outdated marker is load-bearing rather than cosmetic, and this is the
anchoring rule from ADR-0005 doing its job: a suggestion is a claim about specific
bytes at a specific SHA, and it self-extinguishes when they change (`old` stops
matching). "Still visible because it might still apply" would mean re-asserting an
unverified patch against a tree no gate ever checked it against. Nothing is owed
here; GitHub already tells the truth, and a re-command on the new head is how a
still-live defect gets a still-valid suggestion.

## The wrapper was claiming what its run never established

`supersede_previous_reviews(repo, pr_number, bot_login)` takes no scope. It marks
EVERY prior wrapper of ours superseded as soon as a later run posts any fresh
suggestion — including a run commanded on an unrelated finding in another file. The
body it writes then says:

> A later run has posted updated suggestions on this pull request; any from this one
> that still apply are in the current suggestion comments.

On #61 that sentence was false. The `version.rs` wrapper was superseded by a run
scoped to `server.rs`, which never looked at `version.rs`, never re-derived that
finding, and could not have re-posted its suggestion.

**This is the defect `reconcile_suggestions` already guards against, one artefact
over.** Retraction takes `commanded_path` precisely because "one run delivers one
commanded finding (ADR-0007), while the comment listing is the whole pull request",
so a run must be told what it speaks for or it withdraws another command's live
work. Retraction is scoped; superseding was not. Same mistake — one run's process
acting on another command's artefact — caught for comments and missed for wrappers.

## Decision: the wrapper stops making the claim

Scoping the supersede pass the way retraction is scoped was considered and
rejected. It would make a wrapper superseded only by a run for the same commanded
path, which with per-finding commands means wrappers accumulate roughly one per
distinct file ever remediated, none of them ever tidied — reviving the
wrapper-accumulation problem this ADR's first addendum recorded from live
measurement (PR #512: nine wrappers). That pays real timeline noise to preserve a
claim the wrapper should not be making at all.

**The wrapper is a delivery vehicle, not a record of findings.** It exists only
because the reviews API has no upsert for creation, and the substance was never in
it. So every prior wrapper is still superseded — the timeline stays collapsed — but
the superseded body states only what that run can know:

- that this wrapper is **spent**, not that anything was carried forward;
- **which SHA it delivered for**, and which paths;
- that each suggestion's own currency is shown on the suggestion, where GitHub
  marks it.

It must not say "any that still apply are in the current suggestion comments",
because the run rewriting it did not evaluate them.

**And the live wrapper is self-dating.** `REVIEW_BODY` carries no SHA — alone among
everything the harness posts, since the suggestion comment's footer, the reviewer's
sticky comment and the follow-up pull-request body all carry `reviewed SHA`. It says
"see the suggestion comments below" with nothing recording what it spoke for, so
after a push it is a live undated claim pointing at comments GitHub has marked
outdated. Stamping it with the reviewed SHA is not merely symmetry with the other
artefacts: an artefact that never claims currency **never needs a later run to
correct it**, and this lane may never run again. `/fix` is commanded, so there is no
guarantee of a subsequent run, and `supersede_previous_reviews` only executes inside
`if fresh:` — a wrapper is tidied only when a later command posts new suggestions.
Self-dating is what makes that acceptable rather than a gap.

Moving `supersede` out of the `if fresh:` guard was therefore not adopted. It still
depends on a later run arriving, which is the assumption that fails; once the
wrapper no longer over-claims, there is little left for it to buy.

## The stale stacked pull request is accepted, and the close stays human

The third row is the only artefact that goes stale as a durable WRITE. A suggestion
self-extinguishes and a wrapper is text; a stacked pull request is a branch and an
open pull request the harness already created. Because `fix_key` includes
`head_sha` — deliberately, since "this delivery's premise dies with the head" — a
re-command of the same finding on a new head computes a different key and is
honoured. The first pull request stays open, anchored to a dead head, targeting a
base branch that has moved past it. Two open fix pull requests for one defect.

That is accepted. The follow-up pull-request body already carries `reviewed SHA`, so
each states which head it spoke for, and the artefact already satisfies the
self-dating rule above with no change. Reconciling them is the human's.

**Having the new command close the prior pull request was considered, including as a
convenience command (`/close-fix N`) mirroring `/fix N`. It is refused, and
`find_existing_fix` already contains the reason:**

> Scoped to pull requests opened against the reviewed head branch [...] and spanning
> every state: a maintainer who CLOSED a fix has made a decision, and a repeat
> command must not overrule it by opening a second one. Reopening is theirs.

A human's close is therefore already load-bearing INPUT: the harness reads it and
obeys it. A harness-issued close makes the harness both the author and the reader of
that signal — it would perform a closure whose existence then refuses a later
command for the same key, authoring its own veto. That is the "recognise and exempt
its own writes" trust judgment this ADR's first addendum rejected push-to-branch
for, arriving somewhere new.

The lookup makes it worse rather than better. Since the key carries `head_sha`, a
close command issued on the new head cannot compute the stale pull request's key. It
would need either a second, weaker `(pr, finding)` key shadowing the real one — in a
module whose `unanchored`/`anchored` tagging exists specifically to make two key
cases unaliasable — or a resolution of the ordinal against a review for the OLD
head, which needs the compensating witness for a SHA that is no longer current and
whose Actions artifact may have expired, a case ADR-0007's second addendum already
refuses outright. Both add a lookup path that exists only to enable a close.

Against that: closing a pull request is one click, for the same maintainer who typed
the command, on a pull request the refusal message already names and links. Paying a
new write scope, a second key and a self-authored veto to remove that click is the
trade this ADR's first addendum priced and refused for the Apply click.

**This row has never run in production** — `stack` was skipped on all four commands
of the #61 test — so anything beyond accept-and-record would be designing on
unmeasured ground. **Revisit trigger:** stacked delivery running for real, on a pull
request whose head moves between commands, showing maintainers confused about which
of two fix pull requests is current.

## Consequences

- The superseded body and the live body both become SHA-bearing, so the two are
  distinguished by content rather than by the presence of a stamp.
  `supersede_previous_reviews` currently skips a wrapper whose body already equals
  `SUPERSEDED_REVIEW_BODY` — that comparison is how it avoids rewriting the one it
  just posted, and a body carrying a per-run SHA is no longer a constant, so the
  skip needs a different discriminator. `REVIEW_MARKER` stays the ownership half;
  what changes is only how "already spent" is recognised.
- Nothing here weakens a gate. Both changes are to executor-authored text on
  artefacts the harness owns and already authenticates by marker plus resolved bot
  login.
- The reviewer lane gains nothing and is not involved. Having the review lane tidy
  stale wrappers, since it runs on every push, was rejected on the same ground this
  addendum's first section rests on: the reviewer's whole effect is one idempotent
  issue-comment upsert, and giving it the reviews API purely to tidy another lane's
  artefact is the shape this ADR's first addendum already refused for
  push-to-branch ("the pipeline would have to recognise and exempt its own pushes, a
  trust judgment the path currently doesn't make").
