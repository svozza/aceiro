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
import { globToRegExp, proveFrame, proveOrdering, proveTaint, shutdown } from './prove.js';

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
  path_denylist: ['.github/**', '**/*.pem', '**/*.key'],
});

function plan(...steps: readonly { kind: string; args: Record<string, string> }[]): Plan {
  return checkPlanSchema(
    { steps: steps.map((step, index) => ({ id: `s${index}`, kind: step.kind, args: step.args })) },
    POLICY,
  );
}

const patch = (path: string) => ({ kind: 'patch', args: { path, old: 'a', new: 'b' } });
const pushBranch = (name = 'fix/x') => ({ kind: 'push_branch', args: { name } });
const openPr = () => ({ kind: 'open_pr', args: { branch: 'fix/x', title: 't', body: 'b' } });
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
