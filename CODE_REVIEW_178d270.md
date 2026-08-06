# Code review — a1bd348..178d270

Range: `a1bd348..178d270` — 33 commits, 53 files, +6778/−394. `a1bd348` was the
last reviewed tip (`CODE_REVIEW_a1bd348.md`, chunk B, 2026-08-04). Everything
since was unreviewed: the whole `/fix` command channel, the stacked follow-up
pull request, and the four defects the production run found.

Reviewed 2026-08-06. Two engines, independently, on every slice: **Codex** (GPT,
via the codex MCP server) and **Claude** (via the Agent tool). Neither saw the
other's findings. Every candidate then went to an independent adversarial
refuter instructed to refute by default and to look for the guard the finder
missed.

Out of scope, and not sent to any reviewer: `docs/adr/**`, `CONTEXT.md`,
`README.md`, `docs/architecture.html`, `CODE_REVIEW_*.md`,
`REMEDIATION_STATUS_*.md`, and today's docs commits `dabc2ef`, `1ca7d59`,
`178d270`. ADRs were read as the source of a property to test, never reported on.

## Shape of the round

| | Codex | Claude |
|---|---|---|
| Slices answered | 14 of 14 | 14 of 14 |
| Candidates filed | 11 | 43 |
| Survived refutation | 4 | 21 (14 distinct defects) |
| Clean slices | S1 (ordinal), S9c (injection), S7a, S7b | S1, S9c |

**Discard rate: 43 of 54 candidates did not survive** — chunk B discarded 6 of
20, so this is the expected shape, steeper because two engines filed
independently against the same regions.

The two engines converged independently on four defects: **F1** (artifact
identity), **F2** (the SHA witness), **F3** (retraction scope) and **F5**
(`fix_key` collision). Independent convergence is the strongest signal in this
document; each of those four is reproduced twice, by two engines, with different
scripts.

### Slice table as executed

Two departures from the plan, both because a slice carried more than one
question. Slice 4 split into 4a (commanded scope) / 4b (committed bytes); slice 9
split into 9a (token scope) / 9b (approval gate) / 9c (contributor text). Slice
10 split per test file — +2500 lines is far past one slice.

`S9b-workflow-gate` stalled and was retried 4 times on the Claude side (the
harness's own retry, not a reviewer loop); it completed. No slice was abandoned.

## Confirmed findings

Severity: **critical** = a credential escapes, or untrusted content reaches a
trusted effect. **high** = a gate fails open. **medium** = a gate fails closed
wrongly, or a real correctness bug. **low** = hardening.

---

### F1 — The artifact a commanded finding is derived from is selected by name and highest id, so its identity is never established
Severity: **high**
Location: `src/smtithy/prepare_fix_context.py:89-99`
Property broken: Conjunct (a) of ADR-0007's second addendum — the commanded
finding is an element of an **accepted** artifact, which the addendum reads as
"the review job's OUTPUT … the only copy that exists". The code establishes
membership in *some* artifact this verifier accepts, not in the artifact this
harness's generator produced and its executor posted. The module docstring's
claim that "there is deliberately no forgeable finding input anywhere in this
lane" is unenforced.

Stated as artifact **identity**, not provenance: `CONTEXT.md` reserves
Provenance for a finding pointing at a file and line the diff touched, which
`check_provenance` does enforce and which a forged artifact must still satisfy.

Failure scenario: `fetch_reviewed_artifact` lists
`/repos/{repo}/actions/artifacts?name=ai-review-{pr}-{head_sha}` — a
repository-wide listing — filters only on `expired`, and resolves multiplicity
with `max(artifacts, key=lambda a: a["id"])`. No field identifying the producing
run is read: not `workflow_run.id`, not `head_repository_id` vs `repository_id`,
not the workflow's path. Any artifact in the base repository carrying that
predictable name can therefore become the trust anchor. Its findings need only
satisfy `verify()` against the real fetched diff — trivial for a contributor,
who authored that diff. What the attacker gains is control of **which** defect a
commander's `/fix N` addresses, of the text placed inside the
`<commanded_finding>` fence for the plan session, and of the `commanded_path`
that `check_commanded_scope` then pins the patch set to. The delivered effect
stays inert and the patch bytes stay bounded by containment, the denylist and
`old`-must-byte-match.

Verification RUN: reproduced by both engines, independently, against a real
loopback `http.server` on `127.0.0.1` with `github_api.API_ROOT` repointed — real
`urllib`, real `zipfile`, no stub of the module under test. The listing served
two non-expired artifacts of the same name: the genuine review run's, and a
second with a higher id.

```
$ .venv/bin/python /tmp/repro_s3.py
  GET /repos/o/r/actions/artifacts?name=ai-review-7-abc
  GET /repos/o/r/actions/artifacts/200/zip
derived from: {"summary": "forged review", "findings": [{"file": "b.py", "line": 9,
  "severity": "critical", "title": "attacker-chosen defect", ...}]}
VERDICT: FORGED artifact selected
```

A second engine drove the full chain through `prepare()` and
`plan_loop.read_commanded_finding`:

```
artifact chosen  : no reviewer ever found this
verify() accepted the forged artifact: True
commanded finding: critical | no reviewer ever found this
MAINTAINER READ  : low | env read without a default
```

Confidence: **reproduced**. That a fork-added workflow's upload appears in the
base repository's artifact listing is reasoned from the event model, not measured
against live GitHub; the selection defect itself is measured. Codex reached the
same conclusion by the same-SHA re-run route (C10) without the fork premise, so
the defect does not depend on it.

---

### F2 — The posted-review witness matches the SHA stamp as a bare substring, so generator text can satisfy it
Severity: **high**
Location: `src/smtithy/post.py:194` (same pattern at `post.py:370`)
Property broken: Conjunct (b) of ADR-0007's second addendum — the compensating
witness, which must prove the accepted artifact was actually **posted** for the
commanded head, and which is also the drift refusal. The addendum states the
witness is "existence-and-SHA only" precisely to keep contributor-influenced
markdown out of the trust path; a bare substring test puts it back in.

Failure scenario: the check is
`sha_stamp(reviewed_sha) not in (existing.get("body") or "")` — an unanchored
substring search over the **whole** comment body, not an anchored match on the
executor-authored footer. `render()` places findings *above* the footer, so
finding bodies, the summary and the residual risk are all inside the searched
region. A contributor plants the literal text `reviewed SHA: \`B\`` in a line of
commit A, where B is a sibling commit they hold locally but have not pushed (so
there is no hash circularity). The reviewer quotes that line in a finding body;
`verify()` accepts it, because a backticked token is an ordinary code span. The
executor splices it verbatim into the sticky comment for A. The contributor
pushes B. The sticky comment is still A's review, but the witness for B now
returns an id, so the "a review was posted for the current head" precondition
passes for a head no review was ever posted for.

Verification RUN: reproduced by both engines and independently by the reviewer
of record, with the real verifier and real `policy.json`:

```
$ .venv/bin/python /tmp/witness_probe.py
stamp being forged: 'reviewed SHA: `bbbb…bbbb`'
verifier: ACCEPTED the artifact carrying the forged stamp
rendered for POSTED_SHA=aaaaaaaa...
  footer line: <sub>model: `m` · … · reviewed SHA: `aaaa…aaaa` · [run](u)</sub>
  does body contain the TARGET stamp? True
witness for TARGET_SHA (never reviewed): 4242
witness for POSTED_SHA (really reviewed): 4242
RESULT: the witness is SATISFIED for a head no review was posted for.
```

Confidence: **reproduced**.

---

### F3 — Retraction is scoped to the commanded finding's path, not to the finding, so one command withdraws another's suggestion
Severity: **high**
Location: `src/smtithy/suggest.py:582`
Property broken: Retraction scope — "a comment on another finding's file is not
withdrawn by a command that was never about it" (ADR-0007's one-command-one-
finding; ADR-0009 addendum C's "two commands on one pull request is the designed
flow"). The scope predicate is per **file**; the unit a command speaks for is per
**finding**. The fingerprint that correctly distinguishes them is computed for
`stale` and then discarded at the scope predicate.

This is distinct from the accepted `supersede_previous_reviews`-takes-no-scope
item: that is about the review wrapper, this is about the per-comment retraction
predicate.

Failure scenario: an accepted artifact holds two findings on one file — which the
schema, provenance and the reviewer prompt all admit, and which ADR-0009
addendum C measured the reviewer actually doing. A commander runs `/fix 1`; a
suggestion is posted. They then run `/fix 2` for the other finding in the same
file, the designed flow. Command 2's `wanted` holds only finding 2's fingerprint,
so finding 1's live comment lands in `stale`; the only scope test is
`comment.get("path") != commanded_path`, which passes because both are the same
file. The first command's suggestion is then **deleted** outright, or — where a
human has replied — struck through and stamped with `WITHDRAWN_NOTE`'s claim
that it is "no longer in the latest AI remediation", a claim command 2 never
evaluated. `check_plan_cardinality` permits at most one suggestion per file per
command, so a second command on the same file *always* finds the first
command's comment stale-and-in-scope.

Verification RUN: reproduced by both engines with the real
`reconcile_suggestions`; only the HTTP helpers were replaced by recorders, never
the function under test.

```
$ .venv/bin/python /tmp/exploit_s8.py
fingerprints differ: True 1cfc03bd0ffbdbcc 9a3988b66a1c3900
--- now command 2: /fix 2, a DIFFERENT finding in the same file ---
deleted suggestion comment 1001
calls issued by command 2: [('POST_REVIEW', ['src/app.py']), ('DELETE', 1001)]
finding 1's comment 1001 destroyed by command 2: True
=== variant: finding 1's comment carries a HUMAN reply ===
struck through suggestion comment 1001 (has a human reply)
```

Confidence: **reproduced**.

---

### F4 — A `/fix` on a closed or merged pull request passes every gate and still delivers
Severity: **medium**
Location: `src/smtithy/execute_plan.py:254` (inside `pr_snapshot`, where the
precondition is applied; reached from `:407`)
Property broken: ADR-0009-addendum-b's premise for the stacked delivery — "this
delivery's premise dies with the head" — and ADR-0009's framing that the thing
being fixed is a pull request that is not merged yet. `pr_snapshot`'s
precondition is `pr_moved` alone, which compares head SHA and base ref and reads
neither `state` nor `merged`, so a dead premise that is not a *moved* head passes
the gate before the first effect.

Failure scenario: a maintainer merges PR #61 with auto-delete-branch on, then
types `/fix 2` on it. The review comment is still there and the witness passes,
because the merged pull request's head SHA is unchanged. `pr_moved` returns
`None`, so `pr_snapshot` accepts. On the stacked path `base = pr["head"]["ref"]`
is the deleted branch: `create_ref` — the first visible mutation — runs before
`POST /pulls`, so the run leaves a real `smtithy/…` branch behind and no
follow-up pull request. `head.repo` stays non-null on a merged pull request, so
`is_fork` does not refuse; the reviewed commit and tree stay readable and the
head SHA stays fetchable for the quarantine.

Verification RUN:
```
$ .venv/bin/python -c "… from github_api import pr_moved; merged = {'state':'closed',
  'merged':True, 'head':{'sha':'aaa',…}, 'base':{'ref':'main',…}} …"
pr_moved on a MERGED pr with the reviewed head: None

$ grep -n "state" src/smtithy/execute_plan.py
246:    fork-ness used for the delivery MUST describe the same PR state the
248:    check with one state and deliver against another.
(no pr['state']/pr['merged'] read anywhere)
```
End-to-end through the real `main()` against a loopback server serving that
merged pull request, with a patch+push+open_pr plan and `--allow stacked_pr`:
`delivery decision: stacked PR based on 'feature'` /
`created 'smtithy/fix-a' at objsha (1 file(s) patched)`.

Confidence: **reproduced**. Severity raised from the filed `low` by the refuter,
which established reachability on a real merged pull request.

---

### F5 — `fix_key` drops the line number on its anchored branch, so two findings on repeated code share one deduplication key
Severity: **medium**
Location: `src/smtithy/stack.py:120-123` (the `anchored` branch)
Property broken: ADR-0007's `(pull request, head SHA, finding)` deduplication key
is not injective over findings. The key's own docstring claims "the finding, as
its PATH plus the anchor signature of its line" identifies the finding, and
ADR-0009's addendum justifies window=1 as what keeps "two identical `return True`
lines" distinct. It does not: the line number appears only in the `unanchored`
fallback, so two anchors with byte-identical ±1 windows are one key.
`suggest.suggestion_fingerprint` faces the identical hazard and answers it by
folding `old` in — its docstring records exactly this ("a window=1 signature is
not unique for periodic code"). `fix_key` has no equivalent term.

Failure scenario: a pull request adds two structurally identical blocks — any
repeated boilerplate. The reviewer emits one finding per block. Their windows are
byte-identical, so the keys collide. A commander runs `/fix` on the first;
follow-up pull request #42 opens carrying that key on line 1 of its body. They
then run `/fix` on the second: `find_existing_fix` matches #42 — marker *and*
author both genuine, nothing spoofed — `deliver_stacked_pr` raises
`AlreadyDelivered` before any write, and the run fails pointing the commander at
a pull request that fixes a different defect. No `/fix` for the second finding
can ever succeed until the head SHA moves, and `find_existing_fix` spans
`state=all`, so closing #42 does not help. `head_sha` is already in the key, so
including the line would cost no anchor stability.

Verification RUN: reproduced by both engines and by the refuter, on both window
sources, through the genuine `render_pr_body` → `marker_line` → `owned_fix_key`
read-back:

```
key(finding A @ line 4) = 6c82d42c3dccb731
key(finding B @ line 11)= 6c82d42c3dccb731
COLLIDE: True
/fix on finding B -> AlreadyDelivered: a follow-up pull request for this finding
  at this head already exists: #42. ADR-0007 deduplicates on (pull request,
  head SHA, finding), so this command is already delivered.
```

The refuter measured the contrast directly: the same two anchors give
`suggestion_fingerprint` `9a54f0fa3d6f5ca7` vs `639a27f5c16ad6d4` — distinct —
while `fix_key` collides. Ownership itself is sound: a contributor pasting the
marker into their own pull request body reads as `None`, a null author reads as
`None`, an empty `bot_login` matches nothing, and a marker on line 2 is not read.

Confidence: **reproduced**.

---

### F6 — The approval gate's position inside the gated job is unasserted
Severity: **medium**
Location: `.github/workflows/ai-pr-fix.yml:274` (and the same hole in
`ai-pr-review.yml`'s `review` job)
Property broken: the approval gate is asserted in code (ADR-0006) — "the
assertion must run *inside* the gated job" and, per its own comment, "before any
credential exists in it". The evals lane has `TestEvalsApprovalGate` pinning
gate-before-untrusted-checkout and gate-before-credential; the agent lanes have
no equivalent, so ADR-0006's mutation-verified discipline does not cover the
**ordering** half of the property.

Failure scenario: someone moves the "Assert the approval gate was real" step
below the quarantine fetch and `configure-aws-credentials` while refactoring. CI
stays green. On the next command against an untrusted author's pull request, with
a consumer whose `ai-pr-review` environment was auto-created without protection
rules, the contributor-authored head is on disk and the Bedrock session
credential is in the job environment before the gate refuses. Deleting the step
outright *is* caught by `test_every_gated_lane_is_listed`; silently relocating it
is not.

Verification RUN: the workflows were copied to `/tmp/gatemut/wf`, the gate step
relocated, and the suite re-pointed at the mutant via a pytest plugin:

```
276:      - name: Quarantine-fetch PR head (bytes only, never executed)
306:        uses: aws-actions/configure-aws-credentials@254c19bd… # v6.2.1
328:      - name: Assert the approval gate was real
335:      - name: Run plan agent
$ PYTHONPATH=/tmp/gatemut .venv/bin/python -m pytest tests/test_workflow_shape.py -p patchwf -q
```
All 56 workflow-shape tests green with the gate behind both the credential and
the untrusted head. The stronger mutation — relocating the gate to run *after*
"Run plan agent", so the generator has already consumed contributor content with
the model credential in scope — is also fully green across the whole 1735-test
suite.

Confidence: **reproduced**.

---

### F7 — `create_ref` is not the compare-and-swap three comments claim it is
Severity: **low**
Location: `src/smtithy/github_api.py:375`, `src/smtithy/stack.py:279-280`
Property broken: write-class step / delivery deduplication. `github_api.py:282`
asserts "create_ref 422s on a ref that already exists, so GitHub itself is the
compare-and-swap" *for the dedup key*, and `stack.py:279` repeats it. The atomic
object is `refs/heads/<push_branch.name>`; the deduplicated object is
`fix_key(pr, head_sha, finding)`. Nothing binds them — `deliver_stacked_pr` takes
`key` and `branch` as independent parameters and never derives one from the
other. The invariant as written is false; what actually deduplicates is
`find_existing_fix`'s line-1 marker search plus the lane's per-pull-request
concurrency group.

Failure scenario: the false invariant is load-bearing documentation on a
credential-adjacent path — a future change that removes `find_existing_fix`, or
copies these jobs without the concurrency block, would be reasoning from a
guarantee `create_ref` does not provide. A serialized re-run carrying the same
key *is* refused by `AlreadyDelivered` even when the branch is reworded, so
"two commands with the same key both open a pull request" does not happen in
this workflow.

Verification RUN: against a real loopback server implementing GitHub's documented
422 "Reference already exists", driving unmodified `deliver_stacked_pr` with
`find_existing_fix` genuinely returning nothing (the read-then-write window):
```
one dedup key for both commands: efdf361cdfeb3b2f
created 'smtithy/fix-null-check' … opened follow-up pull request #1
created 'smtithy/fix-npe'        … opened follow-up pull request #2
refs now in refs/heads: ['refs/heads/smtithy/fix-null-check', 'refs/heads/smtithy/fix-npe']
dedup keys carried by both bodies: ['efdf361cdfeb3b2f', 'efdf361cdfeb3b2f']
```
Confidence: **reproduced** for the mechanism; the two-pull-request outcome
requires a schedule the concurrency group forbids, which is why this is low.

---

### F8 — A 422 from `create_ref` escapes as an untyped traceback, so the refusal the docstring promises is unimplemented
Severity: **low**
Location: `src/smtithy/stack.py:325`
Property broken: rejection / delivery refusal reporting. `stack.py:287` states "a
re-run refuses at `create_ref` with a message naming that branch", and
`execute_plan.py:473` handles exactly `AlreadyDelivered` and `StackRefusal`. A
422 is neither, and `grep` for `HTTPError` in both files returns nothing, so the
promised message — and the branch name it should carry — never exists.

Failure scenario: reachable where the dedup key cannot see the prior effect —
specifically the deliberately-open `create_ref`/`open_pull_request` window, where
a ref exists with no pull request carrying the marker. The run then ends in
`urllib.error.HTTPError: HTTP Error 422: Unprocessable Content` with no
`::error::` annotation, the branch name absent, and GitHub's own "Reference
already exists" body dropped by `HTTPError.__str__`. Fail-closed is intact —
nothing further is written — so this is a defect in the executor's failure
reporting and audit record, not in delivery.

Verification RUN:
```
run 1: opened #1
run 2: raised urllib.error.HTTPError: HTTP Error 422: Unprocessable Content
   caught by execute_plan? AlreadyDelivered: False  Refusal: False
$ grep -rn "urllib|HTTPError" src/smtithy/execute_plan.py src/smtithy/stack.py -> exit=1
```
Confidence: **reproduced**.

---

### F9 — A suggestion whose `old` stops before a mid-file terminator proves different bytes than it commits
Severity: **low**
Location: `src/smtithy/plan_verify.py:719` (the `at_line_end` middle clause) with
`plan_verify.py:755` (the terminator guard)
Property broken: ADR-0005's containment identity as ADR-0009's suggestion
delivery reaches it — "one function means 'verified' and 'delivered' cannot come
apart". The placement phase deliberately admits an `old` that ends at a line end
*without* consuming the terminator, but the terminator-equivalence rule fires
only when `old` carried a terminator and `new` dropped it. The complementary
shape is one-directional and unguarded.

Failure scenario: for `src/a.py` = `b"foo\nbar\n"` and a step
`{line: 1, old: "foo", new: "x\n"}`, `verify_plan` accepts. `apply_patch_steps` —
the single applier, and the bytes the stacked-PR delivery commits — yields
`b"x\n\nbar\n"`, modelling a fabricated blank line. But the suggestion block
addresses line 1 alone, so a contributor's click commits `b"x\nbar\n"`. The two
differ by one byte the verifier's model never described. No unchecked content
reaches the repository: the region GitHub commits is byte-for-byte exactly `new`,
which bounds, the denylist and the secret scan all covered.

Verification RUN: real `verify_plan` + `apply_patch_steps` + `render_suggestion`
on the real policy; applier proves `b"x\n\nbar\n"` while the rendered block
replaces line 1 only. Reproduced independently by both engines with different
fixtures (the second used `old="def load(path):"`, `new="def load(path=None):\n"`).

Confidence: **reproduced**. Both engines filed this at medium; both refuters
independently corrected it to low, because the divergence is in the applier's
*model* rather than in bytes committed without checking.

---

### F10 — A head branch name beginning with `-` makes every `/fix` on that pull request fail
Severity: **low**
Location: `src/smtithy/execute_plan.py:215`
Property broken: the write-class target check is re-proved by passing the
reviewed head branch as a subprocess argument. A contributor-controlled value
argv cannot express turns re-proof into an unconditional operational failure.
ADR-0006's fail-closed posture is preserved — nothing fails open; what breaks is
the ability to deliver at all.

Failure scenario: a contributor opens a pull request from a branch named
`-evil`. git accepts it (`git check-ref-format refs/heads/-evil` exits 0).
`prepare_fix_context` forwards it verbatim as the `head_ref` step output, and
`execute_plan` builds `["--head-branch", "-evil"]`. Node's `parseArgs` treats it
as ambiguous and throws; the outer guard maps it to exit 2 and `run_prover`
fails the run. Every `/fix N` on that pull request ends red with a message
blaming the harness rather than naming the branch. Fixed by `--head-branch=<v>`
single-argument form, which `parseArgs` accepts.

Verification RUN:
```
$ HEAD_REF='-evil' .venv/bin/python -c "… execute_plan.run_prover(…) …"
::error::prover proved nothing (exit 2); operational failure, not evidence about the plan:
prove-cli: Option '--head-branch' argument is ambiguous.
To specify an option argument starting with a dash use '--head-branch=-XYZ'.
required_env(HEAD_REF) = '-evil'
executor exited 1 -> no delivery
```
Confidence: **reproduced**.

---

### F11 — Two refusals on the routing path leave a traceback instead of an audit record
Severity: **low**
Location: `src/smtithy/route_delivery.py:90` (presence-not-type) and
`route_delivery.py:73` (the `except` tuple)
Property broken: rejection is fail-closed **and audited** — `route_delivery.py:32`
states "everything here fails closed and NOTHING is emitted unless a mode was
genuinely derived", and `github_api.fail` is the emit path that makes a refusal
an audit record.

Failure scenario: the shape checks test argument *presence* but not *type*, so a
`suggest` step whose `args.path` is a list reaches `decide_delivery`'s set
comprehension and dies on `TypeError: unhashable type: 'list'`. Separately,
`except (OSError, json.JSONDecodeError)` does not cover `UnicodeDecodeError`, so
a non-UTF-8 `plan.json` escapes the same way. Fail-closed is preserved in both
cases (exit 1, no `GITHUB_OUTPUT`, no mode, no credential minted); the loss is
the audited *form* of the refusal.

Verification RUN:
```
$ … route_delivery.py --artifact-dir $D   # args.path = ["a.py"]
TypeError: unhashable type: 'list'
exit=1
(and: bad_utf8 exit=1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff")
```
Confidence: **reproduced**.

---

### F12 — `MAX_REDIRECTS = 5` is dead code, so the declared bound is not the enforced one
Severity: **low**
Location: `src/smtithy/github_api.py:29`
Property broken: none. This is a dead constant in the credential-boundary region
that misstates the enforced bound to any future reader.

Failure scenario: `c4cd183` replaced the hand-rolled redirect loop — whose guard
was `redirects >= MAX_REDIRECTS` — with the `_StripAuthOnCrossOriginRedirect`
handler, and left the constant behind. Nothing reads it. The enforced depth is
urllib's `max_redirections = 10`. Either delete it or pass it as
`max_redirections` so the constant is what the test measures.

Verification RUN: measured against real loopback servers by the reviewer of
record —
```
effective max_redirections: [10]
MAX_REDIRECTS constant: 5
$ grep -rn MAX_REDIRECTS src/  ->  only the definition line
$ .venv/bin/python /tmp/loop_probe.py
raised HTTPError: HTTP Error 302: … would lead to an infinite loop.
same-origin hops made before it stopped: 11
```
Confidence: **reproduced**.

---

## The redirect handler is correct — measured, not assumed

Slice 7a is the one place where a stub already fooled this project, so the
handler was driven against **real loopback servers** on two ports, with real
`urllib`, by the reviewer of record. `MAX_REDIRECTS` aside (F12), it holds:

| hop | Authorization on the second request |
|---|---|
| 301 / 302 / 303 / 307 / 308 cross-origin | **dropped** (all five) |
| same-origin | retained, correctly |
| port-only change | dropped |
| relative `Location` | retained (stays same-origin) |
| protocol-relative `//host` | dropped |
| chain A→B→A | dropped on the hop **and** on the return to the API host |
| uppercase host, same origin | retained |

```
$ .venv/bin/python /tmp/redirect_probe.py
=== 302 cross-origin A -> B
    server A /start   -> TOKEN SENT
    server B /moved   -> no auth header
=== 302 same-origin A -> A
    server A /start   -> TOKEN SENT
    server A /elsewhere -> TOKEN SENT
=== 302 chain A -> B -> A (token back on return?)
    server A /start -> TOKEN SENT
    server B /hop   -> no auth header
    server A /back  -> no auth header
```

The `(scheme, netloc)` tuple comparison — never a prefix test — is what makes
`api.github.com.evil.test` fail closed, and `tests/test_github_api.py` asserts
that directly against the handler.

## Slice 10 — mutation testing

The one question: **which new test passes when the property it names is
removed?** Each of 11 test files was mutated in its **own git worktree** so
concurrent source mutations could not collide; no file under `tests/` was ever
edited, and every worktree was verified clean and removed. The main tree was
confirmed clean before and after.

| test file | added tests | survivors |
|---|---|---|
| `test_execute_plan.py` | 97 (28 mutations) | **0** |
| `test_workflow_shape.py` | 18 | **0** |
| `test_post.py` | 10 | **0** |
| `test_stack.py` | 44 (28 mutations) | 3 |
| `test_route_delivery.py` | 17 | 4 |
| `test_prepare_fix_context.py` | 20 | 2 |
| `test_fix_command.py` | 14 | 2 |
| `test_suggest.py` | ~30 | 1 |
| `test_plan_verify.py` | 17 | 1 |
| `test_plan_loop.py` | 15 | 1 |
| `test_github_api.py` | 14 | 1 |

Method note, and a warning for the next round: one agent's first pass produced
two **spurious** survivors from stale `__pycache__` — equal-length mutations
written inside the same mtime second. All results above are from re-runs with
`PYTHONDONTWRITEBYTECODE=1` and `__pycache__` purged. Any future mutation run
must do the same or it will report false survivors.

### T1 — `test_an_empty_bot_login_matches_nothing` does not exercise the guard it names
Severity: **medium** (credential boundary, and the guard is genuinely unprotected)
Location: `tests/test_stack.py:183`
Names: "`resolve_bot_login` fails closed upstream; an unresolved identity must
not become ownership."
Revert: deleted the `not bot_login` fail-closed clause from
`stack.owned_fix_key`. **PASSED** — `1 passed, 43 deselected`.
Why: the fixture is `{"user": None}`, so `(None or {}).get("login")` is `None`
and `None != ""` is already true — the author comparison alone returns `None` and
the `not bot_login` clause is never reached. Exploited against the mutant: a
pull request whose author GitHub reports as `{"login": ""}` with an unresolved
bot login yields `owned_fix_key(...) == key`, i.e. an unresolved identity claims
ownership. Killing it needs `pr={"body": marker, "user": {"login": ""}}`,
`bot_login=""` → expect `None`.

### T2 — `test_a_different_finding_is_a_different_key` never pins `path` independently
Severity: **low**
Location: `tests/test_stack.py:76`
Revert: dropped `path` from `fix_key`'s key parts. **PASSED**.
Why: the test's "other" finding changes path *and* line together, so the
`anchored` component alone distinguishes them. Exploited: two findings at the
same line in different files whose anchor text is identical (`def handle(request):`
in both) collide under the mutant. Needs a case varying *only* the path. This is
the same non-injectivity as F5, from the other side.

### T3 — `test_nothing_is_emitted_empty` cannot reach the guard it names
Severity: **medium**
Location: `tests/test_prepare_fix_context.py`, `TestTheEmittedOutputsMatchWhatPrepareReturned`
Names: "an empty value is worse than a missing one … an empty one disables the
gate that reads it" — i.e. `main()` must refuse to emit an empty gate input.
Revert: deleted the `if not value: … return 1` guard at
`prepare_fix_context.py:252-255`. **PASSED**; whole file `20 passed`.
Why: the test iterates the lines the writer *already wrote* and asserts
`value != ""`. The guard exists to stop an empty value being written, so with it
gone the happy-path file is byte-identical. No added test drives an empty ref
through `main()`. This is the exact shape of the production defect this range was
fixing (`2665ff0`, "every ref a delivery gate reads is emitted") — the sibling
`test_all_four_refs_are_emitted` *is* protected, but the empty-value half is not.

### T4 — `test_a_missing_review_artifact_is_refused` asserts against its own stub
Severity: **medium** (this is F1's region, and it has no real coverage)
Location: `tests/test_prepare_fix_context.py`, `TestThePreconditions`
Revert: deleted the `if not artifacts: raise Refused(...)` block. **PASSED**;
also survives inverting the condition.
Why: the test monkeypatches `fetch_reviewed_artifact` with its own stub that
raises, then asserts the stub raised what it was told to raise. The real
`fetch_reviewed_artifact` and `download_review` — the artifact listing, the
expiry filter, the newest-wins selection at the heart of **F1**, and the
`BadZipFile`/`KeyError` refusal — have **zero** coverage in this file, and none
elsewhere (`tests/test_github_api.py:182` notes it "was stubbed in"). F1 and T4
are the same hole seen from two sides: the selection is both unverified and
untested.

### T5 — `test_the_anchor_guarantee_travels_with_the_helper` leaves the reviewed-SHA guard entirely unprotected
Severity: **medium**
Location: `tests/test_plan_verify.py`, `TestTheApplierIsSharedWithTheDelivery`
Revert: deleted `if at_reviewed_sha > 1: raise Rejection(…)` from
`apply_patch_steps`. **PASSED — and no test in the whole 194-test file fails.**
Why: the test asserts `match="ambiguous"`, a substring both ambiguity messages
share, against a tree where the anchor is duplicated at the reviewed SHA *and*
in pending content, so either guard alone satisfies it. Deleting the *other*
guard is caught by two pre-existing tests; the reviewed-SHA guard — the one the
docstring calls provenance, "`old` is proof the model read the file" — is
protected by nothing. Needs a case where the two counts differ, plus distinct
`match=` strings.

### T6 — `test_a_non_integer_ordinal_is_refused` does not protect the `bool` exclusion it is named for
Severity: **medium**
Location: `tests/test_plan_loop.py:397`
Revert: `if isinstance(index, bool) or not isinstance(index, int):` →
`if not isinstance(index, int):`. **PASSED**; whole file `50 passed`.
Why: `write_context` seeds a **one-finding** review, so `True` coerces to index 1,
falls off the end, and is caught by the past-the-end guard — whose message
happens to contain the substring `match="index"` looks for. With the bool arm
removed, `read_commanded_index` on `{"index": true}` returns `index: True`
unnoticed. On a review with two or more findings — the ordinary case — `/fix` with
a `true` ordinal would silently resolve `findings[1]`, a finding nobody
commanded. The test is loose twice over: `match="index"` matches every
`Rejection` in the module, and the fixture size makes the bool case
indistinguishable from the range case. Note slice 1 found the ordinal handling
itself **clean**; this is the test, not the code.

### T7 — `test_an_unknown_commanded_path_withdraws_nothing` is vacuous
Severity: **low** (but it is F3's region)
Location: `tests/test_suggest.py`, `TestRetractionScope`
Revert: deleted the entire scope guard; and separately inverted its `None` half
so an unknown scope retracts everything. **PASSED** both times.
Why: the test passes `steps=[step()]` together with a comment rendered from that
same step, so the comment's fingerprint is in `wanted`, it is never `stale`, and
the retraction loop is never reached. Its sibling passes `steps=[]` — that is the
shape this one needed.

### T8 — `test_a_redirect_loop_terminates` measures the stdlib, not this module
Severity: **low**
Location: `tests/test_github_api.py:309`
Revert: deleted `MAX_REDIRECTS = 5`; then, maximally, set it to `100000` **and**
replaced the whole `redirect_request` override with `pass`. **PASSED** both times.
Why: `MAX_REDIRECTS` is dead code (**F12**), so the bound the test observes is
urllib's. The test cannot fail for any change to this file. Either delete it or
give the constant teeth.

### T9 — `test_the_cap_is_the_policys_finding_limit` asserts the current value, not the derivation
Severity: **low**
Location: `tests/test_fix_command.py:21`
Revert: `MAX_ORDINAL = 10` in place of the policy read. **PASSED**.
Why: `max_items` is 10 today, so a hardcoded literal satisfies the assertion —
precisely the drift the class exists to prevent. Control: the literal `3` does
fail, confirming it detects only a *wrong* constant, never a *restated* one.
Companion: `test_a_negative_ordinal_is_not_a_command` is fully subsumed by the
regex character-class tests and pins nothing of its own.

### T10 — four `test_route_delivery.py` tests pass for the wrong reason
Severity: **low**
Location: `tests/test_route_delivery.py`
- `test_it_needs_no_token` — the file's only credential-boundary assertion. It
  only does `monkeypatch.delenv` and asserts a mode was emitted, so it detects a
  *hard* dependency but not an opportunistic read, log, or use of a token.
  Adding `token = os.environ.get("GITHUB_TOKEN", ""); print(len(token))` passes.
- `test_it_reads_only_the_plan` — the fixture never creates `review.json`, so a
  router that reads it *whenever the bundle carries one* — the real CI condition —
  passes. Only an *unconditional* read fails.
- `test_the_decision_is_decide_deliverys` — its input yields `stacked_pr` under
  any naive reimplementation, so it cannot distinguish "imports `decide_delivery`"
  from "guesses". Caught collectively by the file.
- `test_steps_that_are_not_a_list_are_refused` — its input `{"steps": "suggest"}`
  is iterable, so the *step-is-an-object* guard refuses instead and the test
  passes for the wrong reason.

### What mutation testing confirmed as genuinely protected

Worth recording, because it is most of the range. `test_execute_plan.py` killed
all 28 mutations — including moving the `--allow` gate to *after* the write
(ordering, not just the refusal, is pinned) and restoring the forgeable
`finding.json` input, whose mutant printed a delivered forged remediation.
`test_workflow_shape.py` killed all 18, including `contents: write` leaking into
`execute`, the comment body interpolated into a `run:`, and
`cancel-in-progress: true`. `test_post.py` killed all 10, including both halves
of the composite guard at `post.py:194` separately — each half is killed by a
distinct test, so the `or` carries no untested branch. On `test_github_api.py`,
the security-load-bearing trio (cross-origin strip, same-origin retention,
lookalike host) all die when reverted.

## Discarded candidates

43 of 54 did not survive. The instructive ones:

**Refuted on the ADR that explicitly declines the stronger property.** Both
engines filed `check_commanded_scope` as testing membership rather than "the fix
touches the commanded file", at high. Refuted: ADR-0007's addendum states
"deliberately not stronger … 'every path is justified by the finding' is not
checkable from a finding and a plan", and the refuters showed the implied fix
(`old != new`) enforces nothing — a one-byte comment edit, or one trailing space,
satisfies it identically. Reporting it would have pushed a maintainer toward
enforcement-shaped non-enforcement. Same for `routed_mode` not counting regions:
on the accepted list, and the rule wanted is not derivable from a step list.

**Refuted by tracing the step the finder skipped.** Codex filed the `plan` job's
`id-token: write` and `pull-requests: read` as unused (C3, C4), and the gate as
merely assumed (C6). All three refuted: `id-token: write` is required by
`configure-aws-credentials` on the `use-bedrock=true` path and Actions
permissions are static, so it cannot be granted conditionally; and the `plan` job
*does* resolve trust from the collaborator-permission API on every run, in a step
the finder never traced.

**Refuted for depending on editing the shipped policy.** One finder's repro of a
dropped `label` step set `policy['plan']['label_allowlist']` itself — the
verifier refuses that input with the real policy. A finding that requires
modifying the security object it is testing is not a finding.

**Refuted on the concurrency group.** Codex's C2 (two concurrent commands both
open a pull request) reproduced only under a `threading.Barrier` that stubbed
`find_existing_fix`, `create_ref` and `open_pull_request` — the very functions in
question. The lane's `concurrency: group: ai-pr-fix-${{ … issue.number }}` with
`cancel-in-progress: false` serialises runs per pull request, and the refuter
confirmed on a real loopback server that the serialised second run *is* refused.
Surviving remnant: F7, the false invariant.

**Refuted as fail-closed in the safe direction.** Draft pull requests never
reaching `approve`; a same-origin hop whose `Location` differs textually. Both
mechanically real, both refusing rather than permitting.

## Residual risk

What this round could not establish, and the reader must carry:

- **Live GitHub behaviour is modelled, not measured.** F1's premise — that a
  fork- or branch-added workflow's `upload-artifact` lands in the base
  repository's artifact listing — is reasoned from the event model. The
  *selection* defect is measured on a loopback server. Confirming the premise
  needs a live repository. Note F1 does not depend on it: the same-SHA re-run
  route reaches it without any fork.
- **Codex could not bind loopback sockets** (`PermissionError: Operation not
  permitted` in its read-only sandbox), so its answers on slices 7a/7b are
  necessarily reasoned-only. The real-server work on those slices is Claude's and
  the reviewer of record's. This is why slice 7a shows Codex `INCOMPLETE`.
- **`stack.py` still has no reachable trigger** through `/fix` (recorded state,
  not a defect). F5, F7 and F8 are all in that lane, so they bite the first time
  it runs rather than today. Its unit tests are the only thing behind it, and T1
  and T2 are holes in exactly those tests.
- **The four `run:`-step and token-scope questions came back clean** from both
  engines. That is an absence of findings under a ~15-call budget per slice, not
  a proof of absence: 86 interpolations were enumerated and classified, and the
  comment body reaches the harness through `COMMENT_BODY` → `parse_fix_command`
  and is never parsed by a shell.
- **F6 means the gate's ordering is only as good as review discipline.** A
  workflow-shape test asserting gate-before-credential and
  gate-before-quarantine in both agent lanes — the assertions the evals lane
  already has — would close it.

## Known and accepted, re-confirmed not re-reported

F14 (a prepended line makes one of our comments invisible to the reconciler
forever; marker-from-line-1 is load-bearing containment);
`supersede_previous_reviews` taking no scope and `REVIEW_BODY` carrying no SHA
(decided 2026-08-06, implementation owed); `decide_delivery` not counting regions
(judged unreachable-as-prompted); the plan prompt being DRAFT (pinned by a test);
`stack.py` never having run in production. Candidates restating these were
discarded by the refute pass, which was given the list.
