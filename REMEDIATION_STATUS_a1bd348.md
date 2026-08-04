# Remediation of CODE_REVIEW_a1bd348.md

Fix pass over the fourteen findings from the two-engine review of chunk B.
**Thirteen fixed, one accepted as-is.** Nine commits, 1e2bc25..HEAD grows by
nine; every fix was written test-first and each test was checked by reverting the
code it covers and confirming it fails.

Suite status after the pass, all four run at the end:

- `pytest tests/ -q` — **1538 passed** (was 1473; +65)
- `npm test` — **165 pass, 0 fail**; `npm run typecheck` — clean
- `npm run build` + `pytest tests/test_plan_gate_differential.py -q` — **97 passed**

No gate was weakened and no test was relaxed to make a finding go away. Two
findings needed a decision that was not the fix pass's to make; both were put to
the user and are recorded below with the option chosen.

---

## The family, and what closed it

Eight of the fourteen were one defect wearing different clothes: **`plan_verify`
proves a property about `original.replace(old, new)` while GitHub replaces the
addressed line range with the suggestion block's lines.** Those agree only for a
single-line `old` whose `new` ends in `\n`. Rather than patch instances, the pass
made the two models agree at each point they could diverge:

| where | rule now |
|---|---|
| the POST payload | addresses the range `old` REPLACES (`start_line`/`start_side`) |
| `new`'s terminator | may not drop the one `old` carried, except at an unterminated EOF |
| `new`'s bytes | no CR, no NUL — CommonMark rewrites both before the applier sees them |
| `note` | may not carry a ```suggestion fence of its own |

`replaced_line_count` in suggest.py is a deliberate twin of
`plan_verify._line_count`, named as such in both docstrings: the range addressed
has to be the range anchored, or the gates disagree about what is replaced.

---

## Per finding

### F1 CRITICAL — fixed (`1625079`)

`comment_anchor` derives the addressed range from `old`'s extent and sends
`start_line`/`start_side`. `start_line` is omitted for a single-line anchor
because GitHub requires `start_line < line` and 422s on the degenerate range.

Verified end-to-end: the verified three-line collapse now posts
`start_line=2 line=4`, and the bytes GitHub commits equal the bytes the verifier
proved (previously `b'def f():\n    return compute()\n'` proved against
`b'def f():\n    return compute()\n    log(x)\n    return x\n'` committed).

Three reverts checked: addressing only `line` fails 3 tests; always sending
`start_line` fails the single-line test; dropping the no-final-newline case from
the count fails its own.

### F2 CRITICAL — fixed (`d23886a`)

`check_note_carries_no_suggestion` refuses a note whose fence GitHub would offer
to apply. The info string comes from markdown-it's own `token.info`, not a source
scan — the ways to spell one opener are the fence rules again (tilde fence,
longer run, padding, mixed case) and every hand-rolled version of those rules in
this harness has been wrong. `verify.fence_info_strings` is the new helper.

Scoped to `suggest.note`. A suggestion fence in a pull-request body applies to
nothing, so refusing `open_pr.body` would refuse prose for a hazard that surface
does not have — pinned by a test.

The original exploit now rejects with
`plan.steps[0].args.note: contains a suggestion block, which GitHub offers to apply`.

### F3 HIGH — fixed (`7a2bf75`)

Both halves in `plan_verify`. The terminator rule sits with the other placement
checks (it is the same question about what gets replaced); the CR/NUL rule is
`check_suggestion_new_survives_markdown`. Refused rather than normalised, per
ADR-0011: the checked bytes must BE the delivered bytes, and folding a CRLF
file's endings to LF would rewrite its convention on the model's behalf.

`test_a_crlf_line_keeps_its_carriage_return` asserted the renderer preserves a CR
into the block. That is true and no longer reachable, so it was rewritten to pin
where the shape is actually refused — noted here because it is the one test whose
assertion changed rather than being added to.

### F4 HIGH — fixed (`b1a04ce`) — **user decision**

Chosen: scope to the commanded finding's anchor, over carrying a finding id in
the marker (which would orphan every comment posted before the change).

`reconcile_suggestions` takes `commanded_path`, keyword-only with no default, and
retracts only comments on that file. The path comes from the commanded finding
rather than the plan's steps, because scope is a fact about the COMMAND — and
`check_plan_scope` has already refused a plan that does not touch it.

Fail closed on both sides, and **neither absence is an identity**: an unknown
scope withdraws nothing, and a comment GitHub lists without a path is left
standing rather than deleted on a guess. That last case needed its own test —
the first version of the guard passed a revert, because only the
both-are-None combination distinguishes it.

### F5 HIGH — fixed (`d5f01ad`, `a547e31`)

Three mechanisms, two commits (one property each).

`normalize_signature_line` stripped with a bare `str.strip()`, folding every
Unicode whitespace code point — so `_INDENT_RE`'s deliberate narrowness, whose
own comment records why `str.split()` is wrong, ended at the first and last
character. Now `strip(" ")`, and the module comment says so. A revert to
`strip(" \t")` exposed that the tab half was dead (the fold has already run), so
the docstring says a space is the whole class needed rather than implying
otherwise.

The fingerprint gained `old`. It is model-supplied but not model-CHOSEN —
containment requires it to byte-match the reviewed tree exactly once — and it is
canonicalized the way the window is, so a reindentation is still the same
suggestion and `test_nothing_the_model_authors_reaches_the_key` still holds.

### F6, F7, F8 MEDIUM — fixed (`25bd7b4`)

- The containment test's hostile input had an EVEN backtick-run count, so it
  passed with `fence_marker` replaced by `return "```"`. Parametrised over
  odd-count inputs; now also asserts the AI notice, not just the policy hash.
- The empty-`bot_login` guard is now pinned in **both** places, including the
  `is_our_review` half that gates a body overwrite of a human's review summary.
  The old test passed `user: None`, which the second clause already caught.
- The fetched-diff test compared against a bundle file `main()` never reads.
  It now compares the signature map's KEY SET against each diff's own, and fails
  when the map is keyed on the bundle's copy.

### F9 MEDIUM — fixed (`41a3e1d`)

A post-write `pr_moved` re-check, the posture post.py already took and the one
`submit_review`'s docstring already claimed existed.

**It withdraws nothing**, which is where it differs from post.py's single upsert:
the comments are bound to the reviewed SHA by `commit_id`, so GitHub marks them
outdated rather than misplacing them — the fail-visible behaviour ADR-0009 leans
on. Deleting them would destroy correctly-outdated suggestions and any human
thread beneath them. The run fails so the commander sees it; the comments stay.

### F10 LOW — fixed (`9a08ada`)

`strike_through` now wraps in `<s>`, not `~~…~~`. The wrapper must not be able to
become SYNTAX: a line already beginning with `~` became `~~~…`, a tilde-fence
opener, and swallowed the suggestion block and the `<sub>` attribution line into
one code block — the transform manufacturing a fence that captured ADR-0005's
disclosure. Raw HTML is refused in model text while remaining ours to emit.

`test_the_visible_prose_is_struck` asserted the `~~` spelling; it now asks the
renderer, so the property survives a change of wrapper.

### F11, F12, F13 LOW and the ValueError — fixed (`9a08ada`)

- `replied_ids` is re-read immediately before the DELETEs. The window cannot be
  closed (the read and the delete are not atomic) but it can be made as small as
  the last read, and the costs are asymmetric: a reply seen late is an
  unnecessary strike, a reply missed is a severed thread. A failing re-read keeps
  the earlier snapshot rather than losing the retraction, and the extra call only
  happens when something is actually stale.
- `review.get("id")`, bound once and used in both the try and the handler.
- `comment_content` normalises line endings. Nothing normalised is ever POSTED;
  this decides only whether two bodies say the same thing. `verify.NEWLINES_RE`
  was made public for it rather than reaching across for a private name.
- `head_content_lines` catches `(OSError, ValueError)`.

### F14 LOW — **accepted, not fixed**

A line prepended to one of our comments makes it permanently invisible to the
reconciler. Reading the marker from line 1 only is load-bearing containment —
model text can legally contain the marker inside a fence, and `new` is not
markdown-checked — and it outweighs a hazard that requires a deliberate human
edit by someone who already has write access. Recorded rather than closed.

---

## What the pass changed beyond the findings

- `verify.fence_info_strings` and `verify.NEWLINES_RE` (renamed from
  `_NEWLINES_RE`) are new shared helpers, each documented at its definition with
  why it is shared.
- `docs/architecture.html` carries the new properties on three nodes and its
  per-node test counts moved with them (63 and 102), per the living-diagram rule.
- One pre-existing test's assertion changed (F3's CRLF renderer test) and one was
  rewritten to ask the renderer instead of the syntax (F10). Both are called out
  above; no other existing assertion was touched.

## Not done

Nothing from the review is outstanding except F14, which is a decision rather
than a gap. `git log` is nine commits, one property each, and nothing has been
pushed — the branch is local, as it was before this pass.
