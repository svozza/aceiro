# Third addendum to ADR-0007: why a suggestion is commanded too

ADR-0007 settles that remediation happens on a command. Asked of the suggestion
lane specifically after it ran in production, the question is fair and the ADR's
own reasons are the weaker half of the answer: `/fix` and a suggestion have
different **consent shapes**. A suggestion is inert until someone clicks Apply, so
the human is already the gate and their action is ACCEPTANCE. Requiring a command
first makes a maintainer opt in twice — once to ask for the fix, once to take it.
Posted alongside the review, the only human action would be to accept or ignore:
the inverse of `/fix`, rejecting a fix rather than initiating one.

Three of ADR-0007's four justifications do genuinely weaken here, and saying so is
the point of recording this:

- **Deduplication is already void for suggestions.** `suggest.py` holds no
  `(pr, head_sha, finding)` key; `reconcile_suggestions` is idempotent on
  `suggestion_fingerprint`, which is derived from the anchored code. "Two
  maintainers typing `/fix 3` must not produce two branches" is an argument about
  the stacked pull request, whose premise dies with the head. ADR-0009's addendum
  says as much already.
- **Trust follows the commander** — with no command there is no commander. There is
  also no write beyond a review comment, which the reviewer posts unprompted today.
- **Drift means refuse** — that window exists only because `issue_comment` carries
  no SHA. On the review event the SHA is the event's, so the window closes rather
  than needing a guard.

The command stays anyway, for two reasons the consent-shape argument does not
reach.

## The decisive one is mechanical: batch-apply skips the gate

ADR-0009's addendum rests the whole suggestions-first case on one click:

> under ADR-0005 the patch content is unverified by construction, so that click is
> the last point where a human looks at the actual bytes before they join a merge
> candidate.

GitHub also ships **"Add suggestion to batch"** and **"Apply N suggestions"** — one
commit, N diffs, one click. Under `/fix` that path is nearly unreachable: one
command delivers one commanded finding, so N is 1 by construction and the click the
addendum relies on is the click that happens. Auto-posting a suggestion beside
every suggestible finding **manufactures the batch**. It is not that unbidden
suggestions might erode "the pull request is the gate" as a matter of reading
habits; it is that they hand the contributor a button which applies unverified
model output without displaying it. ADR-0005's containment argument is that
nothing executes until a human looks; a batch of size N is that human looking N-1
times too few.

This is the argument to keep, because it is checkable against GitHub's mechanics
rather than a claim about how endorsement reads.

## The other is that the reviewer would become the remediator

`CONTEXT.md` defines the **Reviewer** as the application that posts findings, whose
"effect is one idempotent comment upsert". A suggestion cannot be posted that way:
there is no upsert for review CREATION (ADR-0009's addendum), so the lane would
acquire `submit_review`, `patch_review_comment`, DELETE, and GraphQL
`minimize_review` — and upstream of those, a model session per suggestible finding,
the prover, and `decide_delivery`. A lane holding all of that is not a reviewer
with an extra output; it is the **Remediator** under another name, and the
vocabulary's split would have to be dissolved rather than kept.

Two consequences make that concrete:

- **The refusal path loses its addressee.** `decide_delivery` refuses mixed fix
  kinds, multi-file suggestions, and an incomplete write chain. A commanded refusal
  is reported to the person who asked. An uncommanded one is either silent — the
  harness declined to fix something and told nobody — or unrequested noise. So
  auto-posting cannot ship before the decline channel exists, which makes it
  strictly downstream of that design rather than parallel to it.
- **Retraction scope has no source.** `reconcile_suggestions`'s `commanded_path` is
  the scope, and its contract is that `None` retracts NOTHING, precisely so one
  command does not withdraw another finding's live suggestion. Uncommanded, the
  scope must widen to the whole artifact — a much broader deletion authority over
  human-visible threads, for a lane whose worst case today is a wrong comment.

Cost — a model session per suggestible finding on every push — is real but is the
weakest of the arguments and is deliberately not load-bearing here. A consumer
could flag it away; it cannot flag away either of the two above.

## The middle path, and why a flag does not help yet

"Auto-post behind a policy flag, defaulting off" was considered. It does not reduce
what has to be built: the flag's ON branch still needs the reviewer's full write
surface, an addressee for refusals, and a whole-artifact retraction scope. A flag
whose enabled path is unbuilt is not a smaller decision, it is the same decision
with a switch in front of it — and shipping the switch first would put the
harness's least-verified delivery behind its least-visible configuration.

## Revisit trigger

Sustained real usage in which maintainers command `/fix` on most findings of most
reviews, so the command is measurably a formality rather than a judgement. That
evidence would justify designing an uncommanded lane — and it would have to arrive
together with the decline channel, and with an answer for whether a batch-applied
suggestion still satisfies ADR-0005. Until then, remediation is commanded in both
delivery modes, and ADR-0007 is unchanged.
