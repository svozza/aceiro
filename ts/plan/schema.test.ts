/**
 * The plan schema's adversarial corpus.
 *
 * Same posture as the artifact verifier's: every plan here MUST be rejected
 * whole, and a case that starts passing is a regression in the safe grammar. The
 * schema runs before the solver, so anything that slips through here is reasoned
 * about by an encoding that assumed a shape it never had.
 *
 * The three ADR-0004 closures each get a section, because they are the parts most
 * likely to be "simplified" by someone who does not know what they are reserving.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { checkPlanPolicy, PolicyError, Rejection, type PlanPolicy } from './policy.js';
import { checkPlanSchema } from './schema.js';

const POLICY: PlanPolicy = checkPlanPolicy({
  max_steps: 3,
  control_flow: [],
  argument_forms: ['literal'],
  step_kinds: {
    patch: {
      write_class: false,
      args: {
        path: { type: 'string', min_length: 1, max_length: 500, pattern: '\\.?[A-Za-z0-9][A-Za-z0-9._/-]*' },
        old: { type: 'string', min_length: 1, max_length: 100 },
        new: { type: 'string', min_length: 0, max_length: 100 },
      },
    },
    push_branch: { write_class: true, args: { name: { type: 'string', min_length: 1, max_length: 20 } } },
    label: {
      write_class: true,
      args: { name: { type: 'enum', values: ['automated', 'needs-review'] } },
    },
  },
  ordering: [{ before: 'patch', after: 'push_branch' }],
  max_patched_files: 3,
  max_changed_lines: 120,
  path_denylist: [],
  branch_prefix: 'smtithy/',
  label_allowlist: [],
});

const validStep = { id: 'p1', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } };
const valid = { steps: [validStep] };

function rejected(candidate: unknown, match?: RegExp): void {
  assert.throws(() => checkPlanSchema(candidate, POLICY), match ? { name: 'Rejection', message: match } : Rejection);
}

describe('a well-formed plan', () => {
  it('passes and returns the typed plan', () => {
    // Fail-closed is right but useless if nothing passes.
    const plan = checkPlanSchema(valid, POLICY);
    assert.equal(plan.steps.length, 1);
    assert.equal(plan.steps[0]?.kind, 'patch');
    assert.equal(plan.steps[0]?.args['path'], 'src/a.py');
  });

  it('accepts a multi-step plan up to max_steps', () => {
    const plan = checkPlanSchema(
      {
        steps: [
          validStep,
          { id: 'b1', kind: 'push_branch', args: { name: 'fix/x' } },
          { id: 'l1', kind: 'label', args: { name: 'automated' } },
        ],
      },
      POLICY,
    );
    assert.equal(plan.steps.length, 3);
  });
});

describe('ADR-0004 closure 1: steps are typed records with explicit ids', () => {
  it('rejects a step with no id', () => {
    rejected({ steps: [{ kind: 'patch', args: { path: 'a.py', old: 'a', new: 'b' } }] }, /missing id/);
  });

  it('rejects duplicate ids, which would make a reference ambiguous', () => {
    rejected({ steps: [validStep, { ...validStep }] }, /duplicate id/);
  });

  it('rejects an id that is not a short lowercase identifier', () => {
    for (const id of ['', 'A1', 'has space', '1leading', 'x'.repeat(41), '../x', 'p-1']) {
      rejected({ steps: [{ ...validStep, id }] }, /id/);
    }
  });

  it('rejects a plan that is a bare array rather than {steps}', () => {
    rejected([validStep], /expected a JSON object/);
  });
});

describe('ADR-0004 closure 2: argument_forms admits only literals', () => {
  it('rejects a $ref binding where a string is expected, and says why', () => {
    // The reserved shape. An execution-time binding is discriminated by SHAPE
    // rather than by a wrapper on every argument, so this is the case that proves
    // the reservation is real.
    assert.throws(
      () =>
        checkPlanSchema(
          { steps: [{ ...validStep, args: { path: { $ref: 'step0.output' }, old: 'a', new: 'b' } }] },
          POLICY,
        ),
      /expected a literal string, got object.*argument_forms admits only/s,
    );
  });

  it('rejects an array where a string is expected', () => {
    rejected({ steps: [{ ...validStep, args: { path: ['a.py'], old: 'a', new: 'b' } }] }, /expected a literal string/);
  });

  it('rejects null where a string is expected', () => {
    rejected({ steps: [{ ...validStep, args: { path: null, old: 'a', new: 'b' } }] }, /got null/);
  });

  it('rejects a policy that admits any other argument form', () => {
    // The prover only implements literals. Accepting the flag without teaching
    // the encoding would mean bindings pass a check that never looked at them.
    assert.throws(
      () => checkPlanPolicy({ ...POLICY, argument_forms: ['literal', 'ref'] }),
      { name: 'PolicyError', message: /only implements/ },
    );
  });
});

describe('ADR-0004 closure 3: no version field in the artifact', () => {
  it('rejects a model-supplied version like any other unexpected key', () => {
    // A model-supplied schema version is a model-SELECTED policy. Not
    // special-cased, because the policy owns its version.
    rejected({ ...valid, version: 1 }, /unexpected keys.*version/);
  });

  it('rejects any extra top-level key', () => {
    for (const key of ['auto_merge', 'metadata', 'notes', '__proto__']) {
      rejected({ ...valid, [key]: true }, /unexpected keys/);
    }
  });

  it('rejects an extra key on a step', () => {
    rejected({ steps: [{ ...validStep, force: true }] }, /unexpected keys.*force/);
  });

  it('rejects an undeclared arg', () => {
    rejected(
      { steps: [{ ...validStep, args: { path: 'a.py', old: 'a', new: 'b', mode: '0777' } }] },
      /unexpected keys.*mode/,
    );
  });
});

describe('step kinds are allowlisted', () => {
  it('rejects an unknown kind and lists what is allowed', () => {
    // An unknown kind is not a no-op the executor can skip. It is a request the
    // harness does not understand.
    assert.throws(() => checkPlanSchema({ steps: [{ ...validStep, kind: 'run_shell' }] }, POLICY), {
      message: /run_shell.*not a declared step kind.*label, patch, push_branch/s,
    });
  });

  it('rejects a kind that differs only in case', () => {
    rejected({ steps: [{ ...validStep, kind: 'Patch' }] }, /not a declared step kind/);
  });

  // A plain-object lookup resolves names inherited from Object.prototype, so an
  // untrusted kind of 'toString' used to reach Object.keys(kindSpec.args) and
  // throw a TypeError past the Rejection path. The class of inherited names is
  // what matters, not the three famous ones.
  for (const kind of ['toString', 'constructor', '__proto__', 'valueOf', 'hasOwnProperty']) {
    it(`rejects the prototype-inherited kind ${kind} as a Rejection, not a TypeError`, () => {
      assert.throws(
        () => checkPlanSchema({ steps: [{ id: 's0', kind, args: {} }] }, POLICY),
        (error: unknown) => {
          assert.ok(error instanceof Rejection, `expected Rejection, got ${(error as Error).constructor.name}`);
          assert.match((error as Error).message, /not a declared step kind/);
          return true;
        },
      );
    });
  }

  it('still rejects an inherited name carrying otherwise-valid patch args', () => {
    // args that WOULD satisfy patch, so nothing downstream can be blamed.
    rejected(
      { steps: [{ id: 's0', kind: 'toString', args: { path: 'a.py', old: 'a', new: 'b' } }] },
      /not a declared step kind/,
    );
  });

  it('rejects a missing required arg', () => {
    rejected({ steps: [{ ...validStep, args: { path: 'a.py', old: 'a' } }] }, /missing new/);
  });
});

describe('scalar bounds', () => {
  it('rejects a string over max_length', () => {
    rejected({ steps: [{ id: 'b', kind: 'push_branch', args: { name: 'x'.repeat(21) } }] }, /exceeds max_length/);
  });

  it('rejects an empty string where min_length is 1', () => {
    rejected({ steps: [{ ...validStep, args: { path: 'a.py', old: '', new: 'b' } }] }, /min_length/);
  });

  it('accepts an empty string where min_length is 0', () => {
    // A patch that deletes content has an empty `new`, and must stay expressible.
    const plan = checkPlanSchema({ steps: [{ ...validStep, args: { path: 'a.py', old: 'a', new: '' } }] }, POLICY);
    assert.equal(plan.steps[0]?.args['new'], '');
  });

  it('measures length on NFC, so decomposed forms cannot buy extra budget', () => {
    // 20 e-acute as base+combining is 40 code units raw, 20 after NFC. The
    // artifact verifier does the same, and the two must agree since policy.json
    // is shared and a length has to mean one thing.
    const decomposed = 'e\u0301'.repeat(20);
    assert.equal(decomposed.length, 40);
    const plan = checkPlanSchema({ steps: [{ id: 'b', kind: 'push_branch', args: { name: decomposed } }] }, POLICY);
    assert.equal(plan.steps[0]?.args['name'], decomposed);
  });

  it('anchors patterns at both ends', () => {
    // An unanchored pattern accepts anything with a matching substring -- the
    // shape of the §17 dotfile defect.
    rejected({ steps: [{ ...validStep, args: { path: 'src/a.py\nrm -rf /', old: 'a', new: 'b' } }] }, /pattern/);
    rejected({ steps: [{ ...validStep, args: { path: '../../etc/passwd', old: 'a', new: 'b' } }] }, /pattern/);
  });

  it('rejects a value outside an enum', () => {
    rejected({ steps: [{ id: 'l', kind: 'label', args: { name: 'ship-it' } }] }, /not in/);
  });
});

describe('plan-level bounds', () => {
  it('rejects an empty plan, which would do nothing silently', () => {
    rejected({ steps: [] }, /empty/);
  });

  it('rejects more steps than max_steps', () => {
    const steps = Array.from({ length: 4 }, (_unused, index) => ({ ...validStep, id: `p${index}` }));
    rejected({ steps }, /exceeds max_steps/);
  });

  it('rejects steps that is not an array', () => {
    rejected({ steps: { '0': validStep } }, /expected an array/);
  });

  it('rejects a non-object step', () => {
    for (const step of ['patch', 42, null, ['patch']]) {
      rejected({ steps: [step] }, /expected an object/);
    }
  });
});

describe('the policy itself is validated', () => {
  it('rejects an ordering rule naming an undeclared kind', () => {
    // A rule that can never fire reads as enforcement while enforcing nothing.
    assert.throws(() => checkPlanPolicy({ ...POLICY, ordering: [{ before: 'patch', after: 'deploy' }] }), {
      name: 'PolicyError',
      message: /deploy.*not a declared step kind/,
    });
  });

  it('rejects an ordering rule naming a prototype-inherited kind', () => {
    // The `in` test that validates a rule's kinds was the second reader of
    // step_kinds able to resolve an inherited name.
    assert.throws(() => checkPlanPolicy({ ...POLICY, ordering: [{ before: 'toString', after: 'patch' }] }), {
      name: 'PolicyError',
      message: /toString.*not a declared step kind/,
    });
  });

  it('gives step_kinds a null prototype, so no reader can resolve an inherited name', () => {
    // The structural guarantee the two cases above rely on, asserted directly:
    // a future reader inherits it without knowing to guard.
    const policy = checkPlanPolicy(POLICY);
    assert.equal(Object.getPrototypeOf(policy.step_kinds), null);
    assert.equal(policy.step_kinds['toString'], undefined);
    assert.equal('toString' in policy.step_kinds, false);
  });

  it('rejects a string arg with no max_length', () => {
    assert.throws(
      () =>
        checkPlanPolicy({
          ...POLICY,
          step_kinds: { patch: { write_class: false, args: { path: { type: 'string' } } } },
          ordering: [],
        }),
      { name: 'PolicyError', message: /must declare max_length/ },
    );
  });

  it('rejects an unexpected key in the plan policy', () => {
    assert.throws(() => checkPlanPolicy({ ...POLICY, allow_everything: true }), PolicyError);
  });

  it('rejects an invalid regex in a pattern', () => {
    assert.throws(
      () =>
        checkPlanPolicy({
          ...POLICY,
          step_kinds: { patch: { write_class: false, args: { p: { type: 'string', max_length: 5, pattern: '[' } } } },
          ordering: [],
        }),
      { name: 'PolicyError', message: /not a valid regex/ },
    );
  });
});
