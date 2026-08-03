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

## Addendum: the checked GRAMMAR is the rendered grammar

The same defect had a second form, in the parser rather than in the string. The
verifier parsed with markdown-it's bare `commonmark` preset, under which

    | Status | Verdict |
    |---|---|
    | Security review | APPROVED |

is a single paragraph of text. Every node it produces — `paragraph`, `inline`,
`text` — is on `allowed_nodes`, so it verified clean, and GitHub then rendered an
authoritative-looking table inside a comment whose structure is supposed to come
only from `post.py`'s fixed template. That is `TestImpersonation`'s threat exactly
— the fake `## SYSTEM NOTICE` heading and the fake `> [!WARNING]` alert box —
reached by a route the parser could not see.

The equality this ADR enforces is between the checked text and the posted text.
The parser is where that equality is decided, so it has to be an equality about
the *renderer*: **a construct GitHub renders structurally must be a construct this
parser produces a node for.** A rule the parser does not implement is not a
construct that cannot appear; it is a construct that appears unchecked.

So GFM extensions are enabled *in order to reject them*, which reads backwards
until the direction is stated. `table` is enabled and `table_open` is not on the
allowlist and never will be. Enabling `strikethrough` was the same decision
resolved the other way — the node exists, and `s` is on the allowlist as
**allowed**. Both are policy decisions about a rendered construct; neither is a
parser-configuration detail.

The rule that follows: adding a GFM extension to the parser requires asking both
halves — does GitHub render it, and is the resulting node allowed or refused? A
GFM construct in neither the parser nor the allowlist is the gap this addendum
closes, and it is silent, because the text verifies as prose.

Rejecting rather than escaping, for this ADR's own reason: escaping the pipes
would post something the verifier never saw. The calibration cost is bounded and
tested — a table needs its delimiter row, so a shell pipeline, a `int | None`
union and a regex alternation in prose are all still accepted.

### The constructs with no node at all

The addendum's rule was applied to the table and stopped there, and two GFM
constructs answer its first half — *does GitHub render it structurally?* — with
yes while the parser produces nothing to allowlist:

- **Footnotes.** `[^ok]` becomes a superscript link, and GitHub appends a
  `Footnotes` section under a horizontal rule. Under `commonmark` the reference
  and its definition are both ordinary `paragraph`/`inline`/`text`, so
  `Overall fine.[^ok]` plus a definition in a finding body verified clean and
  rendered a section the template never emitted, carrying whatever the definition
  said. That is the table's route exactly, and the section is where a reader looks
  for provenance.
- **Task lists.** `- [x]` becomes a checkbox. Its node stream is identical to an
  ordinary list item — the marker is consumed by the list parser and does not
  survive into the tree — so no allowlist decision can distinguish the two.

**Both are refused.** For footnotes, the reference as well as the definition:
`post.py` composes every field into one document, so a reference in the summary
resolves against a definition in a finding body, and neither field's in-isolation
check sees the pair. That is the same argument link reference definitions are
refused under, reached independently.

A checked box is refused for the verdict table's reason rather than the
footnote's: it reads as a gate that passed, which is `TestImpersonation`'s threat
whatever the surrounding text says. `bullet_list` and `ordered_list` stay
allowed — enumerating findings is what a reviewer does.

**Where the rule cannot be a parser question, it is a source question.** Neither
construct can be refused by `allowed_nodes`, because neither produces a node, and
adding a plugin would make the verifier *render* them when the goal is refusal.
So these two are matched on text — the only such rules here, and the reason is
that the parser has nothing to say about them.

The corpus each is matched against is not the same, and the difference is not
incidental. Footnotes are matched on `extract_prose`, which drops code spans and
fenced blocks, so a review quoting `` `[^a-z]:` `` as a regex still verifies.
The checkbox marker is absent from that prose, so it is matched on the source
line with `code_lines` skipping code. A per-source-line scan cannot see an inline
code span, which is what the first rule needs and the second does not.

Calibration is tested in both directions: a caret inside a code span, a plain
bullet list, an ordered list, `matrix[i][j]`, and a bare `[x]` in prose all still
verify. `a[^b]` in prose does **not**, and that is correct rather than a
false positive — GFM renders it as a reference as soon as any definition exists in
the composed comment.
