# Addendum to ADR-0003: a shared policy number means one thing

**Historical (see [ADR-0016](0016-retire-the-typescript-prover-consolidate-on-python.md)):**
the seam this addendum polices closed when the prover was retired; the scalar
lexeme rules it produced (code-point lengths, integer lexemes, surrogate
refusal) stay in the Python gate on their own merits.

ADR-0003 accepts a two-language verification tier "for now" and names what keeps
the two gates honest while the seam is open: `policy.json` "becomes shared data
with two readers in two languages… now it is also the only thing keeping the two
verifiers describing the same policy." The port section adds the mechanism —
a differential oracle comparing rejection *kinds*, both runners in CI for the
whole duration.

The mechanism was built (`tests/test_plan_gate_differential.py`, plus a
policy-coverage assertion). What neither the ADR nor the corpus said is that
sharing the policy *file* does not by itself share the policy. Three fields were
read by both gates, from the same file, and meant different things:

- **`max_length`.** `check_scalar` measures Python string length — code points.
  `checkScalar` measured `String.length` — UTF-16 units. So `max_length: 4000` on
  `open_pr.body` admitted 4000 emoji in one gate and 2000 in the other, and the
  same number in the same reviewed file was two different rules.
- **`type: "integer"`.** `1.0` and `1` parse to one double, so `Number.isInteger`
  cannot tell them apart, while Python's `json` reads the first as a `float` and
  `check_scalar` rejects it. A `suggest` step with `"line": 1.0` was admitted by
  the prover and rejected by the executor's verifier.
- **`maximum`.** Read by *neither* gate. An integer spec of
  `{"minimum": 1, "maximum": 2}` loaded, reviewed as a cap, and admitted 100.

Each is a plan one gate admits and the other rejects, which `plan_verify.py`'s own
docstring calls "a defect in one of them". None was exploitable: the divergences
land on caps rather than on the frame, the denylist or the branch namespace. But
they are the failure the seam is supposed to be guarded against, and they
survived a corpus, a coverage assertion and two green suites.

## What the metric has to be

**A policy number's metric is part of the policy, so it is written down once and
implemented identically.** ADR-0005's byte budget already had this right —
`max_changed_bytes` is UTF-8 bytes *precisely because* the two gates' string
lengths differed, and it was defined that way to sidestep a disagreement rather
than to resolve it. This addendum resolves it.

- **String lengths are Unicode code points**, in both gates, after NFC. Code
  points rather than UTF-8 bytes because that is what the artifact verifier has
  always measured and what the policy's numbers were chosen against; changing the
  Python side would have silently retightened every shipped cap. The TypeScript
  gate iterates the string instead of reading `.length`.
- **An integer is an integer LEXEME.** The distinction survives only in the source
  text, so it is decided at the parse boundary (`parsePlanJson`) rather than in
  the scalar check: no decimal point, no exponent, and refused outright if the
  value cannot survive as a double, since Python keeps `9007199254740993` exactly
  and a double does not. Two gates checking different numbers is the same defect
  in a quieter form than two gates reaching different verdicts.
- **A spec key with no reader is a policy error.** Exactly the keys each scalar
  type's reader consults, enforced in both gates — the rule the plan policy's own
  top-level keys already had, applied one level down. A bound nobody enforces is
  worse than an absent one, because a reviewer reads it as present.

## A third verdict, which is not the other two

ADR-0003's whole argument for the negated direction is that `unsat` means "no
execution can violate this, including paths nobody enumerated". `sat` is the
disproof, and it carries the counterexample. The prover collapsed everything that
was not `unsat` into the `sat` branch and went straight to `solver.model()`.

`unknown` is the third answer. It is reachable — a resource limit, a future
quantified encoding, plain solver incompleteness — and on it there is no model:
`solver.model()` throws `there is no current model`, which out of `prove-cli`'s
`main()` is an unhandled rejection, a stack trace, and WASM threads never
terminated. Verified against the real query shapes, not assumed.

**`unknown` is reported as UNDECIDED, and the CLI exits 2 rather than 1.** The
exit code is the point. Exit 1 means DISPROVED, and `execute_plan.run_prover`
already logs that as "an audit record about the plan"; exit 2 it logs as "an
operational failure of this run, not evidence about the plan". A solver that gave
up is the second. Reporting it as a disproof would blame the model for the
solver's incompleteness, and reporting it as `holds` would be the fail-open
version of the same mistake.

Reaching the branch needs a seam the policy cannot express, so `proveOrdering`,
`proveFrame` and `proveTaint` take an optional `resourceLimit`. That is
`proveTaint`'s own precedent — synthetic `bindings` exist so the taint encoding
can be shown to catch a leak rather than reporting `unsat` because nothing was
ever tainted. A branch no test reaches is a branch nobody knows the behaviour of.

## Consequences

- The differential corpus grows a second dimension: the plan travels to both gates
  as **text**, not as a re-serialized object, because `json.dumps` spells `1.0` as
  `1` and would erase the difference under test. A corpus that normalizes its
  inputs cannot see a disagreement about spelling.
- Both directions get a case. The astral-length cases assert an admitted plan and a
  rejected one, so the fix is shown to have made the gates agree rather than to
  have widened one of them.
- The policy-coverage assertion catches a key no gate *mentions*; it could not
  catch `maximum`, which no gate mentioned and no gate needed to. Refusing unknown
  spec keys is the structural version of the same guard, and it is the one that
  fails closed on a key that was never added to either reader.
- **On the TypeScript side that assertion was a tautology, and the mention proxy
  is only as good as the file set it reads.** `ts/plan/policy.ts` was counted as a
  gate file, and it is the *loader*: `PLAN_KEYS` and the `PlanPolicy` interface
  enumerate every plan key by construction, and `requireKeys` refuses a policy
  carrying any key outside `PLAN_KEYS`. So a key that loaded at all was a key the
  set mentioned, and one with a Python reader and no enforcement anywhere in
  `prove.ts` or `schema.ts` passed — the bound-nobody-enforces case this addendum
  calls worse than an absent one, in the one place it claimed to be guarded. The
  file set is now the enforcing files only, and the exclusion carries its own
  assertion so re-adding the loader goes red.
- This does not narrow the port's remaining work. It removes three of the
  divergences the port would otherwise have had to re-derive, and it leaves the
  ones ADR-0003 already flags as empirical — `markdown-it`'s rendering behaviour
  above all — untouched.
