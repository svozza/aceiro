# Code review: suggestion delivery (chunk B), 1e2bc25..a1bd348

Two engines (Codex via MCP, Claude via the Agent tool) over nine narrow slices,
one file region and one question each. Every finding below was reproduced by
running the real modules; the verification actually run is stated per finding.
Fourteen candidates survived merge and dedupe; **six were discarded** as
non-defects (listed at the end).

Suite status at HEAD, run once after the review, working tree clean:

- `pytest tests/ -q` — **1473 passed**
- `npm test` — **165 pass, 0 fail**; `npm run typecheck` — clean
- `npm run build` + `pytest tests/test_plan_gate_differential.py -q` — **97 passed**

All four green. Nothing in this review is a test failure; every finding is a
property no test asserts.

---

## The shape of what went wrong

Eight of the fourteen findings are one family: **the verifier and GitHub's
suggestion applier do not model the same effect.** `plan_verify` proves a
property about `original.replace(old, new)` — a byte-substring substitution. A
GitHub suggestion block replaces *the addressed line range* with *the block's
lines*. Those two operations agree only for the case both were built around
(a single-line `old`, a `new` ending in `\n`), and the policy admits several
shapes where they diverge. ADR-0005's human gate survives — the contributor
still clicks — but what the click commits is not what any checker proved.

---

## F1 — CRITICAL: a verified multi-line `old` posts a single-line-addressed comment, so applying it commits bytes nothing proved

`src/smtithy/suggest.py:439-441` (the POST payload) against
`src/smtithy/plan_verify.py:698-707`

`plan_verify` deliberately supports a multi-line `old`: lines 698-707 derive its
extent from the anchor and provenance-check *every line it spans*, and four
tests pin the behaviour (`test_plan_verify.py:652,657,662,688`). But the payload
`reconcile_suggestions` builds carries only `path`, `line`, `side` — no
`start_line`. `grep -rn "start_line" src/smtithy/` finds it **only** inside
`plan_verify`, never in a request. A review comment with no `start_line`
addresses exactly one line, so GitHub replaces one line with the block's
content.

Failure scenario — the ordinary "collapse these three lines" fix:

```
f.py at head:            old (verified, 3 lines):      new:
1 def f():               "    x = compute()\n          "    return compute()\n"
2     x = compute()       　  log(x)\n
3     log(x)              　  return x\n"
4     return x
```

**Verification (run):** `verify_plan` returns with no Rejection; the POST payload
is `path='f.py' line=2 side='RIGHT'`, keys `['body','line','path','side']`.

- What the verifier proved the applied file to be:
  `b'def f():\n    return compute()\n'`
- What GitHub commits (block's one line replaces line 2 only):
  `b'def f():\n    return compute()\n    log(x)\n    return x\n'`

The contributor clicks once and gets `return compute()` followed by the two
orphaned lines it was meant to absorb — broken code, from a plan the harness
verified, with no signal anywhere. Bounding, denylist and secret checks were all
proved over bytes that are not the bytes committed.

Nothing in `tests/test_suggest.py` mentions a multi-line `old`; the gap lives
exactly between the two files' test suites.

---

## F2 — CRITICAL: `note` may carry its own ```suggestion fence, so unanchored model text gets an Apply button

`src/smtithy/suggest.py:290` (the `args["note"]` insertion), claim at `:263-265`

`fence` is on `policy.json`'s markdown allowlist, so `check_markdown_field`
admits a `note` containing a fenced block — including one whose info string is
`suggestion`. `note` is bound by *none* of the checks that bind `new`: no
anchoring, no `old` byte-match, no hunk containment, no bounding, no per-path
cardinality. `render_suggestion` inserts it verbatim above the real block.

**Verification (run):** `verify_plan` returns with no Rejection for

```python
note = ("This needs a default.\n\n```suggestion\n"
        "def load(path=None):\n"
        "    subprocess.run(BACKDOOR, shell=True)\n```")
```

and the rendered body contains **two** ```suggestion openers, the note's first
(`body.count("```suggestion") == 2`). The unanchored one is the one a reader
reaches first, and GitHub renders every suggestion fence in a review-comment
body as independently applicable.

This is the one containment hole in the diff: model text that passed only the
prose grammar becomes committable file bytes under the harness's authenticated
identity. The docstring at `:265` — "`new` goes inside the suggestion fence,
which is what GitHub applies" — is false as written; `new` is not the only thing
GitHub will apply.

---

## F3 — HIGH: a `new` that omits its terminator proves one file and commits another

`src/smtithy/suggest.py:284`

`args["new"][:-1] if args["new"].endswith("\n") else args["new"]` treats a
missing terminator as "a line ending the file". The policy admits `new` with no
`\n` anywhere, and `plan_verify.py:663` models the result as a plain
`bytes.replace` — which **joins** the replacement to the following line. GitHub
terminates the line it replaced, so it does not join.

**Verification (run):** `old="y = 2\n"`, `new="y = 99"` on `x = 1\ny = 2\nz = 3\n`
verifies (`verify_plan` returns), and:

- verifier's model of the applied file: `b'x = 1\ny = 99z = 3\n'`
- what GitHub commits: `b'x = 1\ny = 99\nz = 3\n'`

Also in this family, same line, same root cause (both reported by Codex,
verified by me against `markdown_it`): a `new` carrying `\r\n` is served to
CommonMark, which normalises CRLF to LF before the applier sees it, so
`a\r\n` commits `a\n`; and a `new` containing NUL renders as U+FFFD
(`'a\x00b'` → `'a�b'`), committing a byte the plan never named.
`test_a_crlf_line_keeps_its_carriage_return` (tests/test_suggest.py:119) asserts
the `\r` survives *into the body* — which it does — but the body is not the
commit.

The empty-vs-`"\n"` distinction the docstring defends is genuinely correct:
`""` → `['```suggestion','```']` (deletion), `"\n"` → one blank line. Verified.

---

## F4 — HIGH: a second `/fix` retracts the first finding's live suggestion, with a note that is false

`src/smtithy/suggest.py:478-480` (and `425`), scoped by `execute_plan.py:357`

`wanted` is built from **one commanded finding's** plan. `ours` is **every**
owned suggestion comment on the pull request. Any comment whose fingerprint is
absent from this one plan is retracted. ADR-0007 makes "the command names one
finding" the design, so two commands on one PR is the normal flow, not an edge.

**Verification (run):** run 1 delivers a suggestion for finding A on `a.py:2`
and a reviewer replies; run 2 is `/fix` on finding B in `b.py`. Output:

```
posted review with 1 suggestion comment(s)
struck through suggestion comment 11 (has a human reply)
```

Comment 11 is A's still-valid suggestion. It is PATCHed with `WITHDRAWN_NOTE`
— "no longer in the latest AI remediation" — which is untrue: A was never
withdrawn, it simply was not this command's subject. With no human reply it is
DELETEd outright. This is the reconciler treating a per-finding plan as if it
were the PR's complete suggestion set.

This is *not* the deliberately-absent ADR-0007 dedup key. It is a scoping
question the addendum does not settle, and it needs a decision before a fix:
either the reconciler's retraction set is narrowed to the commanded finding, or
comments carry which finding they came from.

---

## F5 — HIGH: distinct suggestions collide on one fingerprint whenever the window is not unique

`src/smtithy/suggest.py:92`, with `src/smtithy/diff_map.py:203`

The key is `path` + the window=1 signature of the anchored line. Both engines
found collisions independently, by three different mechanisms; all three are
reachable by a plan that verifies, because `old` can be byte-unique while the
three-line window is not.

**Verification (run), verified-plan-reachable case** — a multi-line `old` reaching
past the window, which is what makes `old` unique:

```
lines 2,3,4 == lines 7,8,9; line 5 ("return A") != line 10 ("return B")
old at line 3 spans 3..5 and is unique in the file -> verify_plan returns
sig(3) == sig(8) == 'if flag:\x00risky()\x00pass'
fp(3) == fp(8) == bd97a9add2218762   COLLIDE
```

Also demonstrated (Claude): periodic content — a `ci.yml` of
`timeout: 30` / `retry: 0` repeated — collides lines 2 and 4. And a distinct
third mechanism at `diff_map.py:203`: `normalize_signature_line`'s trailing
`.strip()` folds **every** Unicode whitespace code point, defeating the
narrowness `_INDENT_RE` is documented (`diff_map.py:109-115`) as deliberately
preserving. Verified: U+2028, NBSP, NEL, VT and U+3000 are all folded at a line
end, and two byte-different anchors (`  go(a); ` vs `  go(a);`) collide on
`4f04cceb6de1a507`.

**What a collision costs, verified end-to-end:** driving `reconcile_suggestions`
with run 1's comment live on one anchor and run 2's step on the colliding one:

```
no new suggestions to post
updated suggestion comment 900 (its suggestion changed)
```

The second finding's note and replacement are PATCHed into the **first
anchor's** comment, which stays anchored where it was. One click applies the
wrong line's fix to the wrong line, and the real defect never gets a comment.
The fingerprint gates every DELETE and PATCH, so a collision is also an
ownership-scope error, not only churn.

Related, same key, no collision needed: the key ignores `old`'s **extent**.
Verified — a 1-line `old` and a 3-line `old` at the same `line` hash identically
(`d5c3beb42376a3f9`). A later run that broadens the same finding takes the PATCH
branch, and `patch_review_comment` cannot move the anchored range, so the
comment keeps a one-line anchor while carrying a three-line replacement. That is
F1's failure reached by a second route.

---

## F6 — MEDIUM: the test named for fence containment passes with `fence_marker` gutted

`tests/test_suggest.py:86` (`test_the_content_cannot_reach_outside_the_block`)

**Verification (run):** I replaced `fence_marker`'s body with `return "```"` —
deleting the entire length arithmetic — and ran the file. The test **passed**;
only three others failed. Its hostile input
`"escape\n```\n</sub>\n```suggestion\nrm -rf /\n"` carries an *even* number of
three-backtick runs, so the content re-opens and re-closes and the policy hash
lands outside a fence regardless of whether the opener was widened.

The property is real and is covered by the sibling
`test_every_line_of_the_content_is_inside_the_block` (which did fail). But the
test named for the containment property enforces nothing; an odd-count input
(`new="```\n"`) would make it bite.

## F7 — MEDIUM: the fail-closed empty-`bot_login` guard is unpinned in both places it appears

`tests/test_suggest.py:354` (`test_an_unresolved_bot_login_owns_nothing`)

The test passes `{"user": None}`, which the *second* clause already rejects
(`None != ""`). The `not bot_login` guard is never reached.

**Verification (run):** I removed `not bot_login or` from **both**
`owned_fingerprint` (`suggest.py:144`) and `is_our_review` (`suggest.py:332`) and
ran `tests/test_suggest.py tests/test_execute_plan.py` — **133 passed**. Under
the weakened code, `owned_fingerprint({"user": {"login": ""}}, "")` returns the
fingerprint and `is_our_review(..., "")` returns True. The `is_our_review` half
gates a body **overwrite of a human's review summary**, which is the mis-scope
its own docstring calls out.

Working tree restored and verified clean (`git status --porcelain` empty).

## F8 — MEDIUM: `test_the_signatures_are_computed_from_the_fetched_diff` half-asserts

`tests/test_execute_plan.py:762`

The forged `diff.patch` the test writes into the bundle is never read by
`main()` at all — the executor reads only `plan.json` and `finding.json` from
there. The `assert "forged" not in ...` half therefore holds under any wiring,
including a deliberately forged one (the window comes from the tree, so the
string never appears in a signature value). Only the key-presence assertion does
work. The named property — identity keyed on the fetched diff, not the bundle's
copy — is the one that matters most for F5's threat model, and it is half-tested.

---

## F9 — MEDIUM: no post-write drift check, contradicting the contract `submit_review` states

`src/smtithy/execute_plan.py:359-360`

`pr_snapshot` (line 320) is the only drift check on this path. `grep` confirms
`pr_moved` appears once in the file. Between the snapshot and the POST the
executor makes several live API calls (`resolve_bot_login`, the `review_comments`
listing, `supersede_previous_reviews`); after `reconcile_suggestions` returns,
`main` prints and exits 0.

`post.py:432-434` does the opposite for the same window: re-runs
`check_pr_unmoved`, calls `withdraw_own_review`, and fails. And
`github_api.submit_review`'s own docstring (lines 137-139) asserts "The pre- and
post-write drift checks stay — this bounds the window they cannot close." On
this path the post-write half does not exist, so `commit_id` is load-bearing
alone rather than a bound on a closed check. Because the remediator is
command-triggered, no later run corrects it: the run reports
`delivered N suggestion(s)` green having already deleted the previous run's
comments.

Verified by reading both paths; the inconsistency is textual and certain, the
cost is a race I could not trigger offline.

## F10 — LOW: a retracted body whose `note` begins with `~` swallows the disclosure footer

`src/smtithy/suggest.py:205`, used at `:255`. **Both engines found this
independently.**

`strike_through` wraps a line as `~~line~~`. A `note` line whose first character
is `~` therefore becomes `~~~…`, which CommonMark reads as a tilde-fence opener.

**Verification (run):** `note = "~~Deprecated~~ - use the new helper instead."`
passes `check_markdown_field` (`s` is on the allowlist — confirmed). Retracted
with a human reply, the body's tail is `~~~~Deprecated~~ - …~~`, and rendering
the result shows the ```suggestion block **and** the
`<sub>🤖 model · policy · reviewed SHA · [run]</sub>` line inside one
`<pre><code>` — I asserted both directly (`policy hash inside <pre><code>: True`,
`suggestion block swallowed too: True`). `close_open_fence` then appends `~~~~`
*after* the footer, confirming the fence rather than closing it before it.

ADR-0005's visibility requirement is the casualty: the attribution and policy
hash render as literal code in a retracted comment. The docstring's "BEST
EFFORT" caveat covers a note that *defeats* our span; here the transform
manufactures a fence that captures executor-authored text. Recovery is intact
(`owned_fingerprint` and `is_struck` still read line 1 — verified).

## F11 — LOW: `replied_ids` is snapshotted before the write and reused for retraction

`src/smtithy/suggest.py:421`, used at `:478`

The single listing at line 419 happens before the POST, the supersede loop and
the re-render PATCHes. A human reply landing in that window is absent from the
snapshot, so `retract` reads "no human discussion" and DELETEs the parent —
the orphaned-reply outcome its own docstring gives as the reason DELETE is
avoided. Narrow, and unreachable offline; reported as a race, not a demonstrated
failure.

## F12 — LOW: the except handlers in `supersede_previous_reviews` can themselves raise

`src/smtithy/suggest.py:371` and `:377`. **Both engines found this.**

`review["id"]` appears inside the `except Exception` handler's f-string. The
subscript at 368 is guarded; the one in the recovery print is not.

**Verification (run):** a review dict without `"id"` that passes `is_our_review`
makes `KeyError: 'id'` escape `supersede_previous_reviews` — and it runs *before*
`submit_review`, so cosmetic tidying costs the whole delivery, which is exactly
what the "best effort, never run-failing" docstring forbids. Low only because
GitHub's payload always carries `id`. `review.get('id')` in both handlers closes
it.

Otherwise that function is sound, and both engines agree: nothing else in the
repo creates reviews as the bot, `except Exception` does catch everything
`github_api` raises on those paths, and no call reaches `fail()`/`SystemExit`.

## F13 — LOW: `comment_content` may churn every comment every run if GitHub returns CRLF

`src/smtithy/suggest.py:396, 474`

`comment_content` joins on `"\n"` and `.strip()`s, so interior `\r` survive.
This repo's own live-measured note asserts GitHub returns comment bodies with
CRLF (`post.py:221`: "the strip absorbs the CRLF GitHub returns").

**Verification (run):** `comment_content(body) != comment_content(body with \n→\r\n)`
is True, and driving the reconciler with a CRLF-served body on a byte-identical
plan prints `updated suggestion comment 7 (its suggestion changed)` — unbounded
churn, since each run re-PATCHes LF and GitHub re-serves CRLF.
`test_an_unchanged_plan_re_run_writes_nothing` cannot catch it: the fake echoes
bodies verbatim in LF. **PLAUSIBLE** — it rests on GitHub's serialisation, which
this repo asserts but which I could not confirm offline.

## F14 — LOW: a line prepended to one of our comments makes it permanently invisible

`src/smtithy/suggest.py:127`

Ownership is read from line 1 only — deliberate and correct against model text.
But anyone with write access can edit another account's comment, and prepending
one line (or leaving a BOM) makes `owned_fingerprint` return None. **Verified:**
`"NOTE: tracked in #123\n<!-- smtithy:suggest:… -->\n…"` → `None`. The
consequence is a duplicate on the next run (the fingerprint is absent from
`live`, so the same suggestion posts again) plus an original no run can ever
strike or delete. `.strip()` covers leading spaces and CRLF, not a preceding
line. **PLAUSIBLE**: requires a deliberate human edit, and reading line 1 only
is load-bearing for a reason that outweighs this.

Also noted, not ranked: `diff_map.py:143` catches only `OSError`, while
`tree_content_source` can raise `ValueError` on a path with an embedded NUL —
which would fail the whole delivery the fallback exists to protect. A NUL cannot
appear in a real filename, so reachability is doubtful; widening to
`(OSError, ValueError)` is free.

---

## Discarded (6)

- **64-bit birthday collision on the fingerprint.** Needs ~2^32 hashes aimed at
  the attacker's own pull request. F5 supplies collisions for free, so this adds
  nothing.
- **Shared bot identity (`github-actions[bot]`) permits false ownership.** True
  of any `GITHUB_TOKEN` workflow and not introduced here; the marker-plus-author
  gate is as strong as the identity the platform offers.
- **The model stamp is unauthenticated.** `MODEL_STAMP_RE.fullmatch` is
  fail-closed and rejects backticks, newlines, `<sub>`, fences and over-long
  values — I verified the charset gate. That the arm could *lie* about which
  model ran is inherent to self-reporting, not a defect in this diff. Both
  engines independently confirmed nothing model-supplied reaches the comment
  unchecked.
- **Minimize-without-rewrite** (line 372 outside the rewrite's `try`). A
  collapsed wrapper with a stale body is tidier than an expanded one; each
  mutation is independently permission-gated by design.
- **CRLF and NUL normalisation in `new`** as separate findings — folded into F3,
  which is the same defect (block content ≠ committed bytes) at the same line.
- **"The window comes from the diff"** and the ADR's unmet-contract text — a
  standing fact; `content_source` closes it, and the head-window slice came back
  clean from both engines on CRLF, missing final newline, U+2028 and every
  unreadable-path case.

## Where the engines disagreed

- **`is_struck` false positives.** Codex hypothesised the substring test could
  misfire; Claude traced that nothing model- or human-controlled reaches line 1
  except the marker and `STRUCK_MARKER` that `retract` itself appends. Claude is
  right — I confirmed `note` and `new` cannot reach line 1.
- **Reply loss via `in_reply_to_id`.** Codex said replies reference the thread
  root and `review_comments` flattens all pages, so no loss. Claude agreed on
  pagination but found the pre-write snapshot race (F11). Both are right about
  their own question; F11 is the narrower, real residue.
- **The head-content window.** Codex: no findings. Claude: two, of which the
  `.strip()` over-fold is real (folded into F5) and the `ValueError` is
  near-unreachable (noted). Claude's extra coverage was worth having.
