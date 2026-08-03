/**
 * The prover's corpus. ADR-0003: "the encoding layer — plan to constraints — is
 * new trusted code, exactly as §2.5 warns: the solver's answer is no more
 * trustworthy than the encoding behind it. It needs its own adversarial corpus."
 *
 * Two things every case here is written against:
 *
 * 1. **An encoding that is accidentally unsatisfiable approves everything.** The
 *    policies are asserted negated, so `unsat` means "holds". A bug that makes the
 *    constraints contradictory yields `unsat` for every plan — a prover that
 *    always says yes, and looks green doing it. So for each policy there is a
 *    case that MUST come back sat with a counterexample. Those are the load-
 *    bearing tests; the passing ones only show the prover is not uselessly strict.
 *
 * 2. **Rejecting is not enough; the reason must match.** Same gap ADR-0003 names
 *    for the eventual port. Every violation case asserts the counterexample names
 *    the offending step or path, not merely that `holds` is false.
 */

import assert from 'node:assert/strict';
import { after, describe, it } from 'node:test';
import { checkPlanPolicy, writeClassKinds, type PlanPolicy } from './policy.js';
import { checkPlanSchema, type Plan } from './schema.js';
import {
  globToRegExp,
  proveBounds,
  proveCardinality,
  proveFrame,
  proveOrdering,
  proveTaint,
  proveWriteTargets,
  shutdown,
} from './prove.js';

after(async () => {
  // WASM threads keep the process alive otherwise.
  await shutdown();
});

const POLICY: PlanPolicy = checkPlanPolicy({
  max_steps: 20,
  control_flow: [],
  argument_forms: ['literal'],
  step_kinds: {
    patch: {
      write_class: false,
      args: {
        path: { type: 'string', min_length: 1, max_length: 500 },
        old: { type: 'string', min_length: 1, max_length: 20000 },
        new: { type: 'string', min_length: 0, max_length: 20000 },
      },
    },
    push_branch: { write_class: true, args: { name: { type: 'string', min_length: 1, max_length: 240 } } },
    open_pr: {
      write_class: true,
      args: {
        branch: { type: 'string', min_length: 1, max_length: 240 },
        title: { type: 'string', min_length: 1, max_length: 120 },
        body: { type: 'string', min_length: 1, max_length: 4000 },
      },
    },
    label: { write_class: true, args: { name: { type: 'string', min_length: 1, max_length: 50 } } },
    suggest: {
      write_class: false,
      args: {
        path: { type: 'string', min_length: 1, max_length: 500 },
        line: { type: 'integer', minimum: 1 },
        old: { type: 'string', min_length: 1, max_length: 20000 },
        new: { type: 'string', min_length: 0, max_length: 20000 },
        note: { type: 'string', min_length: 1, max_length: 1000 },
      },
    },
    // Not in the shipped policy: there is no read_pr_file kind, because the
    // generator reads at generation time (ADR-0004). Declared HERE so the taint
    // cases can build the plan the shipped policy cannot express.
    read_pr_file: { write_class: false, args: { path: { type: 'string', min_length: 1, max_length: 500 } } },
  },
  ordering: [
    { before: 'patch', after: 'push_branch' },
    { before: 'push_branch', after: 'open_pr' },
  ],
  max_patched_files: 3,
  max_changed_lines: 120,
  max_changed_bytes: 8000,
  max_plan_changed_bytes: 16000,
  path_denylist: ['.github/**', '**/*.pem', '**/*.key'],
  branch_prefix: 'smtithy/',
  label_allowlist: ['needs-tests'],
});

function plan(...steps: readonly { kind: string; args: Record<string, string | number> }[]): Plan {
  return checkPlanSchema(
    { steps: steps.map((step, index) => ({ id: `s${index}`, kind: step.kind, args: step.args })) },
    POLICY,
  );
}

const patch = (path: string) => ({ kind: 'patch', args: { path, old: 'a', new: 'b' } });
const suggest = (path: string) => ({ kind: 'suggest', args: { path, line: 1, old: 'a', new: 'b', note: 'n' } });
const pushBranch = (name = 'smtithy/fix-x') => ({ kind: 'push_branch', args: { name } });
const openPr = (branch = 'smtithy/fix-x') => ({ kind: 'open_pr', args: { branch, title: 't', body: 'b' } });
const readPrFile = (path = 'src/a.py') => ({ kind: 'read_pr_file', args: { path } });

describe('proveOrdering', () => {
  it('holds when patch precedes push_branch precedes open_pr', async () => {
    const result = await proveOrdering(plan(patch('src/a.py'), pushBranch(), openPr()), POLICY);
    assert.equal(result.holds, true);
    assert.equal(result.counterexample, undefined);
  });

  it('CATCHES push_branch before patch, and names the order', async () => {
    // The load-bearing direction. If this ever passes, the encoding is
    // contradictory and every plan is being approved.
    const result = await proveOrdering(plan(pushBranch(), patch('src/a.py')), POLICY);
    assert.equal(result.holds, false);
    assert.deepEqual(result.counterexample?.path, ['0: push_branch (s0)', '1: patch (s1)']);
  });

  it('CATCHES open_pr before push_branch', async () => {
    const result = await proveOrdering(plan(openPr(), pushBranch()), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path[0]?.includes('open_pr'));
  });

  it('catches a violation across unrelated steps between the pair', async () => {
    // The rule is about relative order, not adjacency: a step in between must
    // not hide the violation.
    const result = await proveOrdering(plan(openPr(), patch('src/a.py'), pushBranch()), POLICY);
    assert.equal(result.holds, false);
  });

  it('holds for a plan with no orderable pair', async () => {
    const result = await proveOrdering(plan({ kind: 'label', args: { name: 'automated' } }), POLICY);
    assert.equal(result.holds, true);
  });

  it('holds for repeated patches, which no rule orders against each other', async () => {
    const result = await proveOrdering(plan(patch('a.py'), patch('b.py'), pushBranch()), POLICY);
    assert.equal(result.holds, true);
  });

  it('judges the plan\'s OWN order, not the orders it could be rearranged into', async () => {
    // What the eq(index) pinning buys, stated as a case. The legal chain can be
    // permuted into a violation — push_branch before patch — so a prover that
    // quantified over orderings would reject it. Dropping the pinning to "restore
    // the quantified reading" turns this green case red, which is what makes the
    // pinning load-bearing rather than an optimisation.
    const legal = await proveOrdering(plan(patch('src/a.py'), pushBranch(), openPr()), POLICY);
    assert.equal(legal.holds, true);
    const permuted = await proveOrdering(plan(pushBranch(), openPr(), patch('src/a.py')), POLICY);
    assert.equal(permuted.holds, false);
  });
});

describe('proveFrame', () => {
  it('holds when every patched file is a changed file', async () => {
    const result = await proveFrame(plan(patch('src/a.py'), patch('src/b.py')), POLICY, ['src/a.py', 'src/b.py']);
    assert.equal(result.holds, true);
  });

  it('CATCHES a patch outside the changed set, and names the file', async () => {
    const result = await proveFrame(plan(patch('src/evil.py')), POLICY, ['src/a.py']);
    assert.equal(result.holds, false);
    assert.ok(
      result.counterexample?.path.some((line) => line.includes('src/evil.py')),
      `counterexample should name the escaping file, got ${JSON.stringify(result.counterexample?.path)}`,
    );
  });

  it('CATCHES one escaping file among several legitimate ones', async () => {
    // A frame check that only looked at the first patch would pass this.
    const result = await proveFrame(
      plan(patch('src/a.py'), patch('src/escapes.py'), patch('src/b.py')),
      POLICY,
      ['src/a.py', 'src/b.py'],
    );
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('src/escapes.py')));
  });

  it('CATCHES a patch when the PR changed nothing at all', async () => {
    const result = await proveFrame(plan(patch('src/a.py')), POLICY, []);
    assert.equal(result.holds, false);
  });

  it('holds for a plan that patches nothing', async () => {
    // Vacuous, and it must be: "every modified file is touched" over an empty set
    // is true. Asserted so a later change cannot make emptiness a violation and
    // break every label-only plan.
    const result = await proveFrame(plan(pushBranch(), openPr()), POLICY, ['src/a.py']);
    assert.equal(result.holds, true);
  });

  it('does not accept a path that merely shares a prefix with a changed file', async () => {
    const result = await proveFrame(plan(patch('src/a.py.bak')), POLICY, ['src/a.py']);
    assert.equal(result.holds, false);
  });

  it('holds when a suggestion targets a changed file', async () => {
    const result = await proveFrame(plan(suggest('src/a.py')), POLICY, ['src/a.py']);
    assert.equal(result.holds, true);
  });

  it('CATCHES a suggestion outside the changed set (ADR-0009: suggest binds to the frame)', async () => {
    // An applied suggestion modifies the file exactly as a patch would. A frame
    // check that only counted `patch` steps would pass this plan — the vacuous
    // acceptance ADR-0009's consequences call out.
    const result = await proveFrame(plan(suggest('src/evil.py')), POLICY, ['src/a.py']);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('src/evil.py')));
  });

  // The denylist was REPORTED in the counterexample and never asserted, so it
  // could only appear when the frame had already failed for some other path. A
  // denylisted path that IS in changed_files made the query unsat, and the
  // prover printed 'frame: holds' for the file the Python gate rejects.

  it('holds when the PR touched a denylisted file the plan leaves alone', async () => {
    // The denylist binds what the PLAN modifies, not what the PR changed. Any PR
    // that edits a workflow or a dependabot config has a denylisted path in its
    // changed set, and remediating some other file in it is legal -- so this
    // admitting case is what keeps such PRs remediable at all.
    const result = await proveFrame(
      plan(patch('src/a.py'), pushBranch(), openPr()),
      POLICY,
      ['src/a.py', '.github/dependabot.yml'],
    );
    assert.equal(result.holds, true, `unexpected ${JSON.stringify(result.counterexample?.path)}`);
  });

  it('holds when the PR touched a denylisted file and the plan suggests on another', async () => {
    const result = await proveFrame(plan(suggest('src/a.py')), POLICY, ['src/a.py', 'deploy/key.pem']);
    assert.equal(result.holds, true, `unexpected ${JSON.stringify(result.counterexample?.path)}`);
  });

  it('CATCHES a denylisted path that IS a changed file', async () => {
    const result = await proveFrame(
      plan(patch('.github/workflows/ai-pr-review.yml')),
      POLICY,
      ['.github/workflows/ai-pr-review.yml'],
    );
    assert.equal(result.holds, false);
    assert.ok(
      result.counterexample?.path.some(
        (line) => line.includes('.github/workflows/ai-pr-review.yml') && line.includes('denylist'),
      ),
      `counterexample should name the denied path, got ${JSON.stringify(result.counterexample?.path)}`,
    );
  });

  it('CATCHES a denylisted suggestion target that IS a changed file', async () => {
    const result = await proveFrame(plan(suggest('deploy/key.pem')), POLICY, ['deploy/key.pem']);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('denylist')));
  });

  it('CATCHES a denylisted path among otherwise legitimate ones', async () => {
    const result = await proveFrame(
      plan(patch('src/a.py'), patch('secrets/tls.key'), patch('src/b.py')),
      POLICY,
      ['src/a.py', 'secrets/tls.key', 'src/b.py'],
    );
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('secrets/tls.key')));
  });

  it('reports the escaping reason first when a path both escapes and is denied', async () => {
    // Evaluation ORDER matches plan_verify.check_plan_containment, which checks
    // the frame before the denylist, so the two gates give the same reason for
    // the same plan rather than two defensible different ones.
    const result = await proveFrame(plan(patch('.github/workflows/x.yml')), POLICY, ['src/a.py']);
    assert.equal(result.holds, false);
    assert.match(result.counterexample?.path[0] ?? '', /not a file this PR touched/);
  });

  it('holds for a path that merely resembles a denylist pattern', async () => {
    // False-positive guard: `.github` as a path SEGMENT is denied, but a file
    // whose name merely contains it is an ordinary source file.
    const result = await proveFrame(plan(patch('docs/.github-notes.md')), POLICY, ['docs/.github-notes.md']);
    assert.equal(result.holds, true);
  });

  it('names the violating step KIND, so a suggestion is not reported as a patch', async () => {
    // The counterexample is the audit record. Reading "patch src/evil.py" for a
    // suggest step sends whoever reads it looking for a step the plan does not
    // contain.
    const result = await proveFrame(plan(suggest('src/evil.py')), POLICY, ['src/a.py']);
    assert.equal(result.holds, false);
    assert.deepEqual(result.counterexample?.path, ['suggest s0 src/evil.py: not a file this PR touched']);
  });

  it('names the kind on a denied path too', async () => {
    const result = await proveFrame(plan(suggest('deploy/key.pem')), POLICY, ['deploy/key.pem']);
    assert.equal(result.holds, false);
    assert.deepEqual(result.counterexample?.path, [
      'suggest s0 deploy/key.pem: on the policy path denylist (**/*.pem)',
    ]);
  });

  it('distinguishes two steps of different kinds on the same path', async () => {
    // Without the id, two steps naming one file give two identical lines and the
    // audit record cannot say which step to fix.
    const result = await proveFrame(
      checkPlanSchema(
        {
          steps: [
            { id: 'a', kind: 'patch', args: { path: 'src/evil.py', old: 'a', new: 'b' } },
            { id: 'b', kind: 'suggest', args: { path: 'src/evil.py', line: 1, old: 'a', new: 'b', note: 'n' } },
          ],
        },
        POLICY,
      ),
      POLICY,
      ['src/a.py'],
    );
    assert.equal(result.holds, false);
    assert.deepEqual(result.counterexample?.path, [
      'patch a src/evil.py: not a file this PR touched',
      'suggest b src/evil.py: not a file this PR touched',
    ]);
  });
});

describe('a solver that decides nothing is not a proof', () => {
  // `unsat` means the policy holds; every other verdict must not be read as one.
  // `unknown` is the third: the model does not exist, so extracting it throws,
  // and an unhandled rejection out of the CLI would abort the process with a
  // stack trace where a structured fail-closed result belongs. Reached via the
  // resourceLimit seam, on proveTaint's precedent — the corpus constructs what
  // the policy cannot.

  it('proveOrdering reports unknown as not holding, with the reason', async () => {
    const result = await proveOrdering(plan(pushBranch(), patch('src/a.py')), POLICY, { resourceLimit: 1 });
    assert.equal(result.holds, false);
    assert.match(result.counterexample?.path.join('\n') ?? '', /UNDECIDED/);
    assert.match(result.counterexample?.path.join('\n') ?? '', /resource limit/);
  });

  it('proveFrame reports unknown as not holding, with the reason', async () => {
    const result = await proveFrame(plan(patch('src/evil.py')), POLICY, ['src/a.py'], { resourceLimit: 1 });
    assert.equal(result.holds, false);
    assert.match(result.counterexample?.path.join('\n') ?? '', /UNDECIDED/);
  });

  it('proveTaint reports unknown as not holding, with the reason', async () => {
    const result = await proveTaint(plan(readPrFile(), pushBranch()), POLICY, [{ from: 0, to: 1 }], {
      resourceLimit: 1,
    });
    assert.equal(result.holds, false);
    assert.match(result.counterexample?.path.join('\n') ?? '', /UNDECIDED/);
  });

  it('does not claim the policy was violated when nothing was decided', async () => {
    // The distinction the executor logs differently: a counterexample is an
    // audit record about the plan, an undecided query is an operational failure
    // of the run. Saying "VIOLATED" for the second would blame the model.
    const result = await proveOrdering(plan(patch('src/a.py'), pushBranch()), POLICY, { resourceLimit: 1 });
    assert.equal(result.holds, false);
    assert.equal(result.undecided, true);
    assert.ok(!result.counterexample?.path.some((line) => line.includes('violat')));
  });

  it('a limit generous enough to decide still returns the real verdict', async () => {
    // The seam must not turn every proof into an unknown, or the tests above
    // would pass against a prover that decides nothing at all.
    const result = await proveOrdering(plan(patch('src/a.py'), pushBranch()), POLICY, { resourceLimit: 100_000_000 });
    assert.equal(result.holds, true);
    assert.equal(result.undecided, undefined);
  });
});

describe('ordering with suggest steps (ADR-0009)', () => {
  it('holds for a suggestion-only plan, vacuously — no write-class steps at all', async () => {
    const result = await proveOrdering(plan(suggest('src/a.py')), POLICY);
    assert.equal(result.holds, true);
  });

  it('CATCHES push_branch before patch in a plan that also carries suggestions', async () => {
    // Vacuous-pass is this policy's known failure mode: suggest steps must not
    // dilute the ordering obligation on the write chain they sit beside.
    const result = await proveOrdering(plan(suggest('src/a.py'), pushBranch(), patch('src/b.py')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('push_branch')));
  });
});

describe('proveWriteTargets', () => {
  // The Python twin is tests/test_plan_verify.py TestWriteClassTargets, case for
  // case. Containment binds only patch and suggest, so these arguments used to be
  // constrained by nothing but a permissive regex — and push_branch.name decides
  // where the executor's `contents: write` credential is pointed.

  it('holds for a branch inside the harness namespace', () => {
    const result = proveWriteTargets(plan(patch('src/a.py'), pushBranch('smtithy/fix-1')), POLICY);
    assert.equal(result.holds, true);
    assert.equal(result.counterexample, undefined);
  });

  it('CATCHES a push to the default branch', () => {
    const result = proveWriteTargets(plan(patch('src/a.py'), pushBranch('main')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('branch_prefix')));
  });

  it('CATCHES an unprefixed contributor branch', () => {
    const result = proveWriteTargets(plan(pushBranch('feature/theirs')), POLICY);
    assert.equal(result.holds, false);
  });

  it('CATCHES a namespace lookalike', () => {
    // `smtithy-evil/` merely starts with the same characters.
    const result = proveWriteTargets(plan(pushBranch('smtithy-evil/fix')), POLICY);
    assert.equal(result.holds, false);
  });

  it('CATCHES a traversal out of the namespace', () => {
    const result = proveWriteTargets(plan(pushBranch('smtithy/../main')), POLICY);
    assert.equal(result.holds, false);
  });

  it('CATCHES open_pr.branch, the same target under another arg name', () => {
    const result = proveWriteTargets(plan(pushBranch('smtithy/ok'), openPr('main')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('open_pr')));
  });

  it('CATCHES an open_pr that opens from a branch the plan did not push', () => {
    // Both inside the namespace, both confined -- and different. The executor
    // would push the verified patch to one and open the follow-up PR from the
    // other, whose content no step of this plan described.
    const result = proveWriteTargets(plan(pushBranch('smtithy/a'), openPr('smtithy/b')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(
      result.counterexample?.path.some(
        (line) => line.includes('smtithy/a') && line.includes('smtithy/b'),
      ),
      `counterexample should name both branches, got ${JSON.stringify(result.counterexample?.path)}`,
    );
  });

  it('holds when open_pr opens from exactly the pushed branch', () => {
    const result = proveWriteTargets(plan(pushBranch('smtithy/fix-1'), openPr('smtithy/fix-1')), POLICY);
    assert.equal(result.holds, true);
  });

  it('holds for a push with no open_pr, which has no relation to check', () => {
    const result = proveWriteTargets(plan(pushBranch('smtithy/fix-1')), POLICY);
    assert.equal(result.holds, true);
  });

  it('reports the prefix violation, not the mismatch, when a branch is off-namespace', () => {
    const result = proveWriteTargets(plan(pushBranch('smtithy/ok'), openPr('main')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path[0]?.includes('branch_prefix'));
  });

  it("CATCHES a push to the reviewed PR's own head branch even when prefixed", () => {
    // The prefix cannot express this: a contributor could name their branch
    // inside the namespace. ADR-0009's addendum decided against this mode.
    const result = proveWriteTargets(plan(pushBranch('smtithy/theirs')), POLICY, 'smtithy/theirs');
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes("own head branch")));
  });

  it('holds for that same plan when the head branch is unknown', () => {
    // A standalone invocation may not know it; the namespace still confines.
    const result = proveWriteTargets(plan(pushBranch('smtithy/theirs')), POLICY);
    assert.equal(result.holds, true);
  });

  it('holds for an allowlisted label', () => {
    const result = proveWriteTargets(plan({ kind: 'label', args: { name: 'needs-tests' } }), POLICY);
    assert.equal(result.holds, true);
  });

  it('CATCHES a label off the allowlist', () => {
    // A label is a control surface: this repo's evals workflow triggers on one.
    const result = proveWriteTargets(plan({ kind: 'label', args: { name: 'run-evals' } }), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('label_allowlist')));
  });

  it('matches labels exactly, not by prefix', () => {
    const result = proveWriteTargets(
      plan({ kind: 'label', args: { name: 'needs-tests-urgently' } }),
      POLICY,
    );
    assert.equal(result.holds, false);
  });

  it('holds vacuously for a plan with no write-class step', () => {
    const result = proveWriteTargets(plan(patch('src/a.py')), POLICY);
    assert.equal(result.holds, true);
  });
});

describe('proveCardinality', () => {
  // Twin of tests/test_plan_verify.py TestWriteChainCardinality.

  it('holds for the legal single chain', () => {
    const result = proveCardinality(plan(patch('src/a.py'), pushBranch(), openPr()), POLICY);
    assert.equal(result.holds, true);
  });

  it('CATCHES nine chains for one patch', () => {
    const steps: { kind: string; args: Record<string, string | number> }[] = [patch('src/a.py')];
    for (let i = 0; i < 9; i += 1) steps.push(pushBranch(`smtithy/b${i}`));
    for (let i = 0; i < 9; i += 1) steps.push(openPr(`smtithy/b${i}`));
    const result = proveCardinality(plan(...steps), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('at most once')));
  });

  it('CATCHES two push_branch steps', () => {
    const result = proveCardinality(
      plan(patch('src/a.py'), pushBranch(), pushBranch('smtithy/other'), openPr()),
      POLICY,
    );
    assert.equal(result.holds, false);
  });

  it('CATCHES a plan with no fix step', () => {
    // A fixless write chain: legally ordered, every write-class kind within its
    // count, and it proved clean on both gates. The frame quantifies over the
    // paths a plan modifies and there are none, so every containment obligation
    // holds vacuously -- leaving the executor's delivery refusal as the only
    // guard on the one shape that reaches `contents: write` remediating nothing.
    const result = proveCardinality(plan(pushBranch(), openPr()), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('no fix step')));
  });

  it('CATCHES a label-only plan', () => {
    const result = proveCardinality(plan({ kind: 'label', args: { name: 'needs-tests' } }), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('no fix step')));
  });

  it('CATCHES open_pr with no push_branch', () => {
    const result = proveCardinality(plan(patch('src/a.py'), openPr()), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('no push_branch')));
  });

  it('CATCHES a write chain on a suggest plan', () => {
    const result = proveCardinality(plan(suggest('src/a.py'), pushBranch(), openPr()), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('applied in place')));
  });

  it('holds for a suggestion-only plan', () => {
    assert.equal(proveCardinality(plan(suggest('src/a.py')), POLICY).holds, true);
  });

  it('CATCHES duplicate labels', () => {
    const result = proveCardinality(
      plan(patch('src/a.py'), { kind: 'label', args: { name: 'needs-tests' } },
           { kind: 'label', args: { name: 'needs-tests' } }),
      POLICY,
    );
    assert.equal(result.holds, false);
  });

  it('CATCHES two suggestions on one file', () => {
    // ADR-0009: a suggestion is independently applicable, so two on one file can
    // be half-applied — the state its atomicity argument refuses. The
    // counterexample names the path and both steps, since a count alone leaves a
    // reader hunting for which two.
    const result = proveCardinality(plan(suggest('src/a.py'), suggest('src/a.py')), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes("'src/a.py'")));
    assert.ok(result.counterexample?.path.some((line) => line.includes('s0') && line.includes('s1')));
  });

  it('holds for one suggestion each on two files', () => {
    // Cardinality bounds per PATH. decide_delivery is what refuses a multi-file
    // suggestion plan, and pre-empting it here would move a rule with its own
    // reason and its own message.
    assert.equal(proveCardinality(plan(suggest('src/a.py'), suggest('src/b.py')), POLICY).holds, true);
  });

  it('holds for several patches on one file', () => {
    // The asymmetry is the point: patch steps become ONE atomic commit on the
    // stacked branch, so coordinated hunks in a file are what that delivery is
    // for. Only suggestions are independently applicable.
    const result = proveCardinality(
      plan(patch('src/a.py'), patch('src/a.py'), pushBranch(), openPr()),
      POLICY,
    );
    assert.equal(result.holds, true);
  });
});

describe('proveTaint', () => {
  it('holds for every plan the schema admits, because arguments are literals', async () => {
    // Expected to be green forever while argument_forms is ["literal"]. Recorded
    // as a fact about the policy, not as evidence the encoding works — the cases
    // below are that evidence.
    const result = await proveTaint(plan(readPrFile(), patch('src/a.py'), pushBranch(), openPr()), POLICY);
    assert.equal(result.holds, true);
  });

  it('CATCHES a binding from a PR read into a write-class step', async () => {
    // ADR-0004's consequence made executable: "the encoding's corpus must include
    // synthetic binding-bearing plans that the schema forbids, asserting sat with
    // the expected counterexample. The prover is tested beyond what the policy
    // admits." Without this case, taint is a check with no signal.
    const subject = plan(readPrFile(), pushBranch());
    const result = await proveTaint(subject, POLICY, [{ from: 0, to: 1 }]);
    assert.equal(result.holds, false);
    const path = result.counterexample?.path ?? [];
    assert.ok(path.some((line) => line.includes('read_pr_file')), 'names the source');
    assert.ok(path.some((line) => line.includes('the leak')), 'marks the leaking write step');
  });

  it('CATCHES taint laundered through an intermediate step', async () => {
    // Transitivity is the part most likely to be got wrong: a one-hop check would
    // pass this, since push_branch binds to patch rather than to the read.
    const subject = plan(readPrFile(), patch('src/a.py'), pushBranch());
    const result = await proveTaint(subject, POLICY, [
      { from: 0, to: 1 },
      { from: 1, to: 2 },
    ]);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('push_branch')));
  });

  it('holds when a binding carries untainted data into a write', async () => {
    // The complement of the case above. If this failed, the encoding would be
    // marking everything tainted and its unsat verdicts would be meaningless.
    const subject = plan(patch('src/a.py'), pushBranch());
    const result = await proveTaint(subject, POLICY, [{ from: 0, to: 1 }]);
    assert.equal(result.holds, true);
  });

  it('holds when tainted data reaches only a non-write step', async () => {
    const subject = plan(readPrFile(), patch('src/a.py'));
    const result = await proveTaint(subject, POLICY, [{ from: 0, to: 1 }]);
    assert.equal(result.holds, true);
  });
});

describe('proveBounds', () => {
  // ADR-0005's bounding half, which this gate did not enforce at all: a plan
  // over max_patched_files or max_changed_lines was admitted here and rejected
  // by plan_verify.py, and a plan one gate admits and the other rejects is a
  // defect in one of them. The Python twin is test_plan_verify.py TestBounding.
  const lines = (n: number) => 'x\n'.repeat(n);
  const bigPatch = (path: string, old: string, replacement: string) => ({
    kind: 'patch',
    args: { path, old, new: replacement },
  });

  it('holds for a plan inside every bound', () => {
    const result = proveBounds(plan(patch('src/a.py'), pushBranch()), POLICY);
    assert.equal(result.holds, true);
    assert.equal(result.counterexample, undefined);
  });

  it('holds at exactly max_patched_files', () => {
    const paths = ['src/a.py', 'src/b.py', 'src/c.py'];
    assert.equal(paths.length, POLICY.max_patched_files);
    assert.equal(proveBounds(plan(...paths.map(patch)), POLICY).holds, true);
  });

  it('CATCHES one file over max_patched_files, and names the count', () => {
    const paths = ['src/a.py', 'src/b.py', 'src/c.py', 'src/d.py'];
    const result = proveBounds(plan(...paths.map(patch)), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('max_patched_files')));
  });

  it('counts distinct paths, not steps', () => {
    // Several patches into one file are one file: this must stay legal, or the
    // bound would forbid the two-hunk fix ADR-0005 expects.
    const result = proveBounds(
      plan(bigPatch('src/a.py', 'a', 'b'), bigPatch('src/a.py', 'c', 'd'), bigPatch('src/a.py', 'e', 'f')),
      POLICY,
    );
    assert.equal(result.holds, true);
  });

  it('CATCHES a step over max_changed_lines, counting both sides', () => {
    // diff --stat's reading: removed plus added, so a rewrite costs twice what
    // its longer side alone would.
    const half = lines(POLICY.max_changed_lines / 2 + 1);
    const result = proveBounds(plan(bigPatch('src/a.py', half, half)), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('max_changed_lines')));
  });

  it('holds at exactly max_changed_lines', () => {
    const atCap = lines(POLICY.max_changed_lines - 1);
    const result = proveBounds(plan(bigPatch('src/a.py', 'a', atCap)), POLICY);
    assert.equal(result.holds, true);
  });

  it('CATCHES a long single-line rewrite the line count cannot see', () => {
    // The whole reason the byte cap exists: two lines, 16 KB of substitution.
    const long = 'x'.repeat(POLICY.max_changed_bytes);
    const result = proveBounds(plan(bigPatch('src/a.py', long, long)), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('max_changed_bytes')));
  });

  it('measures UTF-8 bytes, not UTF-16 units or code points', () => {
    // The metric has to be the one Python uses, or the caps diverge for exactly
    // the astral input the schema's length metric already differs on. A 4-byte
    // emoji is 2 UTF-16 units and 1 code point; only bytes agree across gates.
    const emoji = '🙂'.repeat(POLICY.max_changed_bytes / 4 + 1);
    const result = proveBounds(plan(bigPatch('src/a.py', 'a', emoji)), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('max_changed_bytes')));
  });

  it('CATCHES a plan whose steps are each in bounds but whose total is not', () => {
    // Per-step alone bounds nothing about the plan: several steps may share one
    // file, so max_patched_files does not bound the sum.
    const chunk = 'x'.repeat(POLICY.max_changed_bytes - 100);
    const steps = [0, 1, 2].map((i) => bigPatch('src/a.py', `anchor${i}`, chunk));
    const result = proveBounds(plan(...steps), POLICY);
    assert.equal(result.holds, false);
    assert.ok(result.counterexample?.path.some((line) => line.includes('max_plan_changed_bytes')));
  });

  it('binds suggest exactly as it binds patch', () => {
    // ADR-0009 applies ADR-0005's caps per suggestion: an applied suggestion
    // changes the file exactly as a patch would.
    const long = 'x'.repeat(POLICY.max_changed_bytes);
    const result = proveBounds(
      plan({ kind: 'suggest', args: { path: 'src/a.py', line: 1, old: long, new: long, note: 'n' } }),
      POLICY,
    );
    assert.equal(result.holds, false);
  });

  it('ignores kinds that carry no file content', () => {
    // push_branch/open_pr/label have no old/new; a bound tripping over them
    // would reject every complete plan.
    assert.equal(proveBounds(plan(pushBranch(), openPr()), POLICY).holds, true);
  });
});

describe('writeClassKinds', () => {
  it('is derived from the policy, not hardcoded', () => {
    assert.deepEqual([...writeClassKinds(POLICY)].sort(), ['label', 'open_pr', 'push_branch']);
  });
});

describe('globToRegExp', () => {
  // The §17 dotfile defect was a pattern enforced exactly as written where the
  // written pattern was wrong, so the glob semantics are pinned rather than
  // assumed.
  it('** spans separators', () => {
    assert.equal(globToRegExp('.github/**').test('.github/workflows/ci.yml'), true);
  });

  it('**/ also matches zero directories', () => {
    assert.equal(globToRegExp('**/*.pem').test('key.pem'), true);
    assert.equal(globToRegExp('**/*.pem').test('certs/deep/key.pem'), true);
  });

  it('* does not span separators', () => {
    assert.equal(globToRegExp('src/*.py').test('src/a.py'), true);
    assert.equal(globToRegExp('src/*.py').test('src/nested/a.py'), false);
  });

  it('a dot is a literal dot, not any character', () => {
    assert.equal(globToRegExp('**/*.pem').test('keyXpem'), false);
    assert.equal(globToRegExp('.github/**').test('Xgithub/x.yml'), false);
  });

  it('anchors both ends', () => {
    assert.equal(globToRegExp('src/*.py').test('prefix/src/a.py'), false);
    assert.equal(globToRegExp('.github/**').test('vendor/.github/x'), false);
  });
});
