# The command channel replies with the command's terminal state

Amends [ADR-0014](0014-a-decline-is-a-reply-from-the-command-channel.md) — its
"exactly two" membership, its posting job's name, and its marker — and leaves
its security property untouched. Driven by finding 2 of
[0002-real-pr-testbed-results](../findings/0002-real-pr-testbed-results.md):
the stacked lane reports **neither** of its terminal states to the commander.

## What made this due

Both halves were observed live on the testbed, not hypothesised:

- **A post-push refusal is silence plus an orphan.** `/fix 1,2` on testbed
  PR #17 verified, routed `stacked_pr`, and pushed the fix branch — then the
  403 (the repository setting that gates `POST /pulls` independently of the
  token's scope) refused. `execute_plan`'s `except Refusal` arm calls `fail()`
  with no emit, so the posting job's `if:` is false and it skips. The
  commander got a red run and a pushed branch bearing their fix that nothing
  told them exists. The refusal message names the branch, the commit, and the
  remedy; it is delivered where only someone who opens the Actions log reads
  it. The 422 beside it (branch already exists) has the same shape.
- **A stacked success is a `print` in a job log.** The re-issued command
  delivered PR #18 end to end, and PR #17 got no reply and no timeline
  cross-reference — GitHub does not link a pull request to the one whose head
  branch it targets, and the follow-up's body names no issue number. The
  suggestion lane has no such gap: its delivery lands on the commanding pull
  request itself.

One pass for both, because they are one defect: the stacked lane's terminal
state is invisible from the place the command was typed.

## Decision

### 1. One channel, and a criterion instead of an enumeration

ADR-0014 named its members ("exactly two"). This pass replaces the enumeration
with the test those members were already passing:

**The channel replies when the harness has a terminal answer to the command
and that answer is not already surfaced on the commanding pull request. A run
whose machinery failed stays a red run.**

Four cases pass the test — *undeliverable by construction*, *already
delivered*, *stranded* (a post-push refusal, below), and *delivered* (the
receipt). All four are facts about the channel or the repository, reached with
the command legitimate and the plan verified or knowably undeliverable. What
stays silent is a malfunction: a gate failure, a crash, a refusal whose shape
is documented unreachable for a verified plan. The reply is the command's
answer; a red run is the machinery's failure report, addressed to the
operator, not the commander.

The untrusted-commander case is excluded **by construction, not by
criterion**: every reply producer runs downstream of trust resolution, so a
refusal reached before trust resolves has no addressee and no reply path.
ADR-0014's security sentence — the untrusted-commander refusal must never be
replied to, because everything before trust resolution runs for any passer-by
— survives verbatim and is not weakened by anything here. The
`Refused`/`Undeliverable` split in `prepare_fix_context` is untouched.

The alternative — a separate receipt mechanism beside the decline channel —
was rejected: it doubles the posting surface ADR-0014 built once (marker,
upsert, ownership, heredoc guard, posting job) and adds a fourth writer to the
workflow for no property the one channel lacks.

### 2. The two post-push stack refusals join; pre-push refusals stay silent

Derived from the criterion rather than chosen. `stack.py` raises the 422
(branch already exists at `create_ref`) and the 403 (Actions may not open pull
requests) **after a fix branch stands**, and both messages are already written
for the commander — they name the branch, the commit, and the remedy. Those
are answers about channel state.

Two boundary readings, recorded so the criterion is honest:

- **The 422 reports prior-run state, not this run's write.** When it fires,
  this run's `create_ref` failed and its commit is a dangling object; the
  branch the commander must clean up was left by an earlier run, in the
  deliberately-open window between a ref existing and its pull request
  carrying the marker. The reply *reports* orphaned state rather than having
  created it — a first cousin of AlreadyDelivered.
- **The no-content refusal stays silent on the stateless ground, not the
  unreachable ground.** A plan whose patch steps change no bytes (`old` equal
  to `new`) could plausibly verify, so "unreachable" would be a stretched
  claim. It fires before any write and leaves nothing behind; a red run is an
  adequate receipt for a command that changed nothing and left nothing. The
  other pre-push refusals (mixed step counts, push/open mismatch) are
  documented unreachable for a verified plan — reaching one means a gate is
  unwired, which is a machinery failure.

### 3. Success replies on the stacked lane only

The suggestion lane's delivery lands **on the commanding pull request** —
inline suggestions where the commander is already looking — so it fails the
criterion's visibility clause: the review is its own receipt. The stacked
lane's artifact is a separate pull request that GitHub will never cross-link
(measured: PR #17/#18), so the receipt exists exactly there. This is a derived
consequence of the criterion, not a per-lane exception.

**The criterion also admits a case this pass deliberately defers.** The
no-review-for-current-head refusal (`/fix` just after a push — finding 0002's
C4) fires downstream of trust resolution and is an answer about the channel,
not a malfunction. It is not enrolled here: it leaves no state behind,
re-issuing costs seconds, and the head-moved half self-corrects when the
re-review updates the sticky. If commanders are observed hitting it repeatedly
(the C4 legibility gap), enrolling it is a one-producer addition this
criterion already licenses — no new argument needed. Recording the admission
rather than defining it away keeps the criterion testable: a rule that quietly
excludes a case it plainly covers is a stretched claim.

### 4. The full rename: `decline` becomes `reply`

ADR-0014's own title already names the channel — "a decline is **a reply**
from the command channel". The receipt is the second message kind of the same
noun, so the artifacts that said `decline` while meaning the channel are
renamed, all the way down:

- the workflow job `decline` → `reply`;
- the module `decline.py` → `reply.py`, one `emit` implementation as before;
- the outputs `decline_reason`/`decline_head_sha`/`decline_ordinals` →
  `reply_reason`/`reply_head_sha`/`reply_ordinals`, plus a new **`reply_kind`**
  (`declined` | `delivered`) the poster renders the lead line from;
- the flag `declined=true` → `replied=true`, still written **last** so the
  job's `if:` is only true once every value it needs exists;
- `if: always() && (needs.command.outputs.replied == 'true' ||
  needs.stack.outputs.replied == 'true')` — equality, not negation, as before.

The name is pinned in roughly fifteen places in `test_workflow_shape.py`
(the writers list, the job block/needs/condition assertions, the output
names, `import decline`); every pin is updated in the same change, each still
asserting the property it always asserted. That walk is the pins doing their
job, not friction to route around. A shallow rename (job only) was rejected:
the outputs are the interface between producer and poster, and an output
named `decline_reason` carrying a delivery receipt is the same lie one layer
down. A job named `decline` posting receipts, kept for cheapness, would be a
claim stretched to fit the code — the correction discipline runs the other
way here.

ADR-0014 stays as written: it documented the channel's first kind, and this
ADR records the widening. Only its membership sentence ("exactly two") and its
marker consequence (superseded by decision 6) no longer describe the code.

### 5. `StrandedDelivery`, a sibling of `AlreadyDelivered` — not a `Refusal`

The two post-push raises move from `Refusal` to a new exception:

- **Not a subclass, for a semantic reason:** `Refusal`'s docstring promises
  "raised before any write, so a refused plan leaves nothing behind", and
  these two raises were the only ones breaking that promise. Moving them out
  makes the docstring true again. `AlreadyDelivered`'s own taxonomy applies:
  nothing is wrong with the plan — the channel's state blocked delivery — and
  answers in this file are non-`Refusal` exceptions.
- **Not a subclass, for a mechanical reason:** if the dedicated `except` arm
  were ever lost or reordered, a subclass would be silently swallowed by
  `except Refusal` — regressing to exactly the defect this pass fixes,
  invisibly. A sibling propagates as a loud traceback instead: same red run,
  but screaming.
- **No structured attributes.** The raise sites already build
  commander-addressed messages, and they differ in a way that matters: the
  403's commit is the branch tip, the 422's is this run's dangling object.
  Structured `branch`/`commit` fields would invite a second renderer that
  must agree with the message — the two-implementations defect ADR-0009's
  addendum B measured. The exception's text is the reason; `emit` needs
  nothing more.

Caught in `execute_plan` beside `AlreadyDelivered`, emitting kind `declined`
with the same ordinals derivation, then `fail()` — **the run stays red**. The
receipt is emitted beside the existing `delivered:` print, kind `delivered`,
on a green run; the reply job's `always()` reasoning already covers a red
producer, and a green one is the simpler case.

### 6. Idempotency needs no new mechanism; the marker becomes per-command

One command → one pull request (`fix_key` dedup) → one receipt. A re-run of a
delivered command lands on `AlreadyDelivered`, which already names and links
the pull request — so the reply comment stays truthful under retry with zero
new machinery. Stated here so nobody builds a receipt-dedup that the dedup key
already provides.

The comment's identity does change: **the marker is keyed per-command**
(reviewed head SHA + ordinals) rather than one per pull request. ADR-0014's
accepted consequence — "a commander declined twice for different reasons sees
only the latest" — was fine when every message was transient guidance; a
receipt is a cross-link to an artifact GitHub refuses to surface, and letting
a second command's receipt upsert away the first's pointer would recreate,
for the first command, the exact invisible-delivery gap this pass closes. With
the per-command key:

- a **retry of the same command** still upserts one comment — accumulation
  only ever came from retries, so the wrapper-accumulation problem ADR-0009's
  first addendum measured stays solved;
- a **distinct command** gets its own comment, each carrying distinct real
  information issued by a trusted maintainer;
- each comment converges to **that command's current terminal state**:
  declined → (setting fixed, re-issued) → the receipt replaces the decline;
  re-run after delivery → `AlreadyDelivered` replaces the receipt with a
  pointer to the same pull request. Latest-per-command, not
  latest-per-channel.

Both producers already hold both marker inputs — they are `emit`'s required
values today. The ordinals grammar is digits and commas and the SHA is hex,
so nothing new is expressible inside the marker and the heredoc-delimiter
guard's alphabet is unchanged.

## Consequences

- The workflow comment that deliberately makes adding a producer hard is
  rewritten to state the criterion instead of the count. It keeps doing its
  job; this ADR is the front door it points to.
- A stranded delivery's reply and the receipt are **harness-authored** end to
  end: branch names are plan-authored but prefix-constrained, and the pull
  request number and URL come from GitHub. Nothing new joins the
  contributor-authored alphabet ADR-0014's amendment documented for the
  undeliverable reason.
- The receipt outlives the run's artifacts: a comment is permanent where the
  bundle's 90-day retention is not, so the pointer to the follow-up pull
  request survives the evidence of the run that opened it.
- Every path this pass adds is owner-measurable (post-push decline, receipt,
  AlreadyDelivered all fire for a trusted commander). The untrusted-commander
  negative path — a passer-by's `/fix` getting silence — remains the declared
  gap it was for the testbed (one GitHub account); nothing upstream of trust
  resolution changes, and the refusal there is unit-pinned.
- A green `stack` now writes outputs the reply job reads. The posting job's
  `if:` already tolerated a red producer via `always()`; tolerating a green
  one is strictly simpler, and the flag-written-last ordering keeps a partial
  emit unpostable in both cases.
