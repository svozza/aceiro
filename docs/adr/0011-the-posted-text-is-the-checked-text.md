# The posted text is the checked text

`check_markdown_field` used to open with `text = unicodedata.normalize("NFC", text)`
and a comment claiming "the checked text is the posted text". It was not: the
normalization applied to a local copy, and `post.render` inserted
`artifact["summary"]`, `finding["title"]` and `finding["body"]` — the original
strings — into the comment. Two consequences followed.

**Bidi controls and invisibles reached the reader.** Nothing rejected U+202A..202E,
U+2066..2069 or zero-width joiners, so `summary = "Reviewed ‮kcatta na si sihT‬ safe
code"` verified clean and posted verbatim. A right-to-left override reverses the
run, so the rendered review reads differently from the bytes any downstream tool
or reviewer sees — Trojan-source deception in a comment whose entire purpose is to
be trusted *because* it was verified. The asymmetry was the tell: the input side
of the harness already treats these code points as a live threat and strips them
(`artifact.escape_fence` via `canonicalize.strip_invisible`), while the output
side — the side a human reads and acts on — did not.

**Length and grammar were checked against a different string than was posted.**
The cap was measured on NFC and the AST walked on NFC, then the NFD original went
out. The two agreed only by coincidence.

## The decision

The verifier **rejects** artifact text that is not already canonical, rather than
normalizing it:

- any code point `canonicalize.is_invisible` reports (general category Cf/Cc plus
  the Default_Ignorable table, retaining `\n`, `\t`, `\r` as visible separation);
- any text not equal to its own NFC normalization.

So the claim becomes an equality the verifier enforces, not a transformation it
performs on a copy.

## Why reject and not strip

Stripping is the tempting option and it is wrong here in two ways.

Stripping in `post.py` recreates the same split one layer down: the executor
would post something the verifier never saw, which is the defect restated with a
different owner. Stripping in `verify.py` and posting the stripped form would
work, but it means the artifact that passed is not the artifact the generator
submitted, so the transcript's record and the posted comment diverge — and a
silent repair is exactly the "partial acceptance" the module's fail-closed,
whole-artifact posture rules out everywhere else.

Rejection is also what "allowlist a safe grammar" already means in this codebase.
An invisible code point in a *review comment* has no legitimate use: the harness
is not a text editor, and prose describing a change does not need a zero-width
joiner. The generator retries with plain text, which is the outcome we want.

## Consequences

- **The generator is told.** `render_constraints` gains the rule, so this is not
  a constraint the model discovers by burning a submission — the same reason every
  other enforced rule is rendered from policy into the prompt.

- **A reviewer cannot quote an invisible character it found.** This is the real
  cost, and it is accepted. A PR that smuggles a zero-width space into an
  identifier is a genuine finding, and the reviewer must now *describe* it
  ("the identifier contains a U+200B between `x` and `y`") rather than paste it.
  Describing it is better output anyway: pasted invisibles are invisible in the
  comment, so the reader could not have seen the evidence. The
  `zero_width_fence_breakout` eval scenario already expects a description in
  `residual_risk` rather than a quotation, so the shipped corpus is unaffected.

- **This is not a policy knob.** Like the mention, raw-HTML and image rules, it is
  an unconditional verifier invariant: nothing in `policy.json` can re-enable
  invisible code points in posted text. A consumer who needs them needs a
  different tool.

- **One table, three readers.** The invisible set is `canonicalize.is_invisible`,
  shared with the input fence and both secret scans. A code point added there
  closes the hole in all of them at once; that module exists because the gap
  between two spellings of "invisible" was a confirmed secret-scan bypass.

- **Plan text inherits it.** `check_plan_markdown` reuses `check_markdown_field`,
  so `suggest.note` and `open_pr.body` get the same rule. Patch `old`/`new` do
  NOT: they are file bytes, anchored raw per ADR-0005, and an anchor that
  normalized would stop proving the model read the file.
