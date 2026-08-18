# smtithy

A harness for agents that are never trusted, only verified: a model proposes,
a deterministic checker finds no counterexample, and a trusted executor acts.
The AI PR reviewer is the first application; review-and-remediate, where the
verified object is a plan rather than a flat record, is the second.

## Experiment result ownership

Evaluation arms own their native redacted result records. Smtithy owns the
canonical fixtures, shared schema, and cross-arm aggregation. See
`docs/adr/0020-arm-repositories-own-native-experiment-results.md` and
`src/smtithy/evals/arm_result.schema.json` before adding or moving experiment
results.

Smtithy's own committed native records live under `results/`; records from
other arms remain in their respective repositories.

## Language

### The pipeline

**Generator**:
The component that invokes a model and produces a candidate artifact. Holds no
write credential and calls no write API.
_Avoid_: the agent, the reviewer, the LLM

**Artifact**:
The structured record a generator emits — the untrusted proposal, treated as
data rather than instructions.
_Avoid_: response, output, review object

**Finding**:
One defect claim in an artifact, anchored to a file and line the diff touched.
Provenance-constrained, and the only part of an artifact that becomes an inline
comment. A defect in unchanged code is still a finding when the change makes it
reachable — anchored to the changed line responsible, not to the defect. One
anchor, always: it is the property that makes provenance checkable, so a finding
never names a second location however related.
_Avoid_: issue, comment, problem

**Group**:
A finding's claim that it and other findings sharing its value are one defect.
Advisory: it is addressed to a human choosing what to command, never read by the
remediator, because whether two findings are one defect is not a property the
verifier can check. What a group buys is that a commander can see the split.
_Avoid_: cluster, related, duplicate

**Residual risk**:
What the reviewer could not establish, and the reader must therefore carry: a
suspicion the available tools could not confirm, a claim needing a capability the
generator lacks (running tests, reaching the network), or an attempted
manipulation in the reviewed content that the reader should know about. Free
prose, not provenance-constrained, and never a place to put a defect that IS
established — a finding demoted here loses its anchor and its inline comment, so
the test is "could I confirm this?", not "could I anchor it?".
_Avoid_: caveats, notes, limitations, disclaimer

**Verifier**:
The deterministic component that decides whether an artifact satisfies the
policy. The security boundary; it interprets policy rather than encoding it.
_Avoid_: validator, checker, linter

**Executor**:
The trusted component that renders a verified artifact and performs the effect.
Re-verifies independently rather than trusting the verifier that ran before it.
_Avoid_: poster, publisher, writer

**Policy**:
The declarative safety rules an artifact must satisfy, held as data
(`policy.json`) so it can be reviewed and hashed rather than read as code.
_Avoid_: rules, config, schema

### Verification

**Rejection**:
The verdict that an artifact violates policy. Whole-artifact and fail-closed —
never partial acceptance.
_Avoid_: failure, error, invalid

**Provenance**:
The property that a finding points at a file and line the diff actually
touched, proving the artifact describes the reviewed change.
_Avoid_: grounding, attribution

**Ground check**:
A policy question answerable by direct interpretation in one pass — set
membership, range, enum, AST-node allowlist. What today's flat artifacts need.
_Avoid_: simple check, basic validation

**Canonicalization**:
Establishing what a target environment will actually render a piece of text as,
before deciding whether it is safe. Where the system's real defects have lived.
_Avoid_: normalization, sanitization, escaping

**Plan**:
A verified object made of typed steps with literal arguments. Straight-line
today; the vocabulary reserves control flow so branches and bindings can be
admitted later without a shape change.
_Avoid_: script, workflow, remediation steps

**Step**:
One typed record in a plan — an id, a kind, and its arguments. A straight-line
plan is the degenerate case of a program, so steps carry identity even when
nothing refers to them.
_Avoid_: action, instruction, command

**Write-class step**:
A step whose kind performs an effect outside the harness: `push_branch`,
`open_pr`, `label`. What the ordering and frame-condition policies quantify over.
_Avoid_: mutation, side effect

**Counterexample**:
The concrete violating path a solver returns when a policy fails — the audit
log's evidence for a rejection, as opposed to the bare verdict.
_Avoid_: model, witness, sat result

### Trust

**Trusted author**:
A pull-request author holding write-or-above on the repository, resolved from
the collaborator-permission API. Anything unresolvable is untrusted.
_Avoid_: collaborator, member, maintainer

**Commander**:
The person who issues a remediation command on a pull request. Must hold
write-or-above; their trust is what authorises a fix, independently of whether
the pull request's author is trusted. A command names one or more findings, and
naming several asserts they take one remediation — the only source of a fix's
scope that is neither the model's nor the harness's.
_Avoid_: requester, invoker, maintainer

**Quarantine**:
The directory holding pull-request head content, read as bytes and never
executed, with version-control metadata removed.
_Avoid_: sandbox, workspace, checkout

**Fence**:
The delimiter that marks untrusted content as data in a prompt, with embedded
closing sequences neutralised so content cannot terminate its own fence.
_Avoid_: delimiter, wrapper, tag

### Applications

**Reviewer**:
The application that posts findings about a pull request. Its effect is one
idempotent comment upsert.

**Remediator**:
The application that proposes a fix for the commanded findings. Its effect is
inert and human-applied either way: suggestion comments by default, a stacked
follow-up pull request where a suggestion cannot carry the fix. Never runs
tests; see ADR-0001.

**Delivery**:
Which effect a remediation takes — suggestions or a stacked pull request. The
executor's decision, read from the verified plan's step list, never the
generator's. Not a size judgement: a fix that could be half-applied is not
deliverable as suggestions. Atomicity is a property of one plan, so two
commands can still deliver one defect in pieces.
_Avoid_: mode, output, channel, strategy

**Reply**:
The command channel reporting a command's terminal state to its commander — a
decline or a receipt. Posted only when the harness has an answer to the command
itself, never when its own machinery failed, and only where that answer is not
already visible on the commanding pull request. One reply per command, holding
that command's current terminal state.
_Avoid_: notification, status comment, decline channel

**Decline**:
The reply kind that tells a commander their fix was not performed: the channel
cannot express it, it has already been delivered, or delivery halted leaving
state they must know about. No model runs to produce one and its text is the
harness's. Distinct from a rejection, which is a verdict about an artifact, and
from a run that merely failed.
_Avoid_: refusal, error, rejection

**Receipt**:
The reply kind that tells a commander their fix was delivered, naming where.
Owed only where the delivery's artifact does not land on the commanding pull
request itself — a delivery the commander can already see is its own receipt.
_Avoid_: confirmation, success comment

**Stranded delivery**:
A delivery halted with a fix branch standing and no pull request carrying it.
The one terminal state that leaves something a commander must clean up, which
is what makes its silence more expensive than any other refusal's.
_Avoid_: partial delivery, half-delivered, orphan branch
