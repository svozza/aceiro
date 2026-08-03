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
import { checkPlanSchema, parsePlanJson, reviveJsonNumber } from './schema.js';

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
  max_changed_bytes: 8000,
  max_plan_changed_bytes: 16000,
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

describe('ADR-0004 closure 1: control_flow is reserved, so a policy declaring it is refused', () => {
  // argument_forms already refuses a widened value above; control_flow did not,
  // and it is the same reservation. Every proof here reasons about a
  // straight-line plan — proveOrdering pins positions to the plan's own indices,
  // proveFrame quantifies over a closed file set — so a `branch` kind admitted
  // by the schema would be proved about as an ordinary sequential step, and the
  // branches nobody modelled would be the part no policy covered.

  const withBranch = {
    ...POLICY,
    control_flow: ['branch'],
    step_kinds: {
      ...POLICY.step_kinds,
      branch: { write_class: false, args: { cond: { type: 'string', min_length: 1, max_length: 100 } } },
    },
  };

  it('rejects a policy declaring any control flow', () => {
    assert.throws(() => checkPlanPolicy(withBranch), { name: 'PolicyError', message: /control_flow/ });
  });

  it('reports it as a PolicyError, never as a bad plan', () => {
    // The fault is the deployment's. A Rejection here would send a reader to the
    // generator for a rule the prover cannot implement.
    assert.throws(() => checkPlanPolicy(withBranch), { name: 'PolicyError' });
  });

  it('still accepts the reserved empty value', () => {
    assert.deepEqual(checkPlanPolicy({ ...POLICY, control_flow: [] }).control_flow, []);
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

  it('counts an astral character once, as the artifact verifier does', () => {
    // `String.length` is UTF-16 units, so an emoji costs 2 there and 1 in
    // Python. A max_length of 20 admitted 10 emoji in one gate and 20 in the
    // other — the same policy number meaning two things, which is what the
    // shared policy.json exists to prevent.
    const emoji = '🙂'.repeat(20);
    assert.equal(emoji.length, 40);
    const plan = checkPlanSchema({ steps: [{ id: 'b', kind: 'push_branch', args: { name: emoji } }] }, POLICY);
    assert.equal(plan.steps[0]?.args['name'], emoji);
  });

  it('rejects one code point over max_length, astral or not', () => {
    // The complement: the cap still binds, it is simply counted in the metric
    // both gates share.
    rejected({ steps: [{ id: 'b', kind: 'push_branch', args: { name: '🙂'.repeat(21) } }] }, /exceeds max_length/);
  });

  it('rejects an unpaired surrogate, which no UTF-8 encoder will take', () => {
    // JSON permits \ud800 and both parsers accept it, so a plan can carry a
    // string that is not encodable text. The Python twin's containment phase
    // encodes and raises UnicodeEncodeError rather than a Rejection, so the two
    // gates disagreed on whether such a plan is even well-formed -- the prover
    // held all six policies while the verifier crashed. Twin of
    // test_plan_verify's test_an_unpaired_surrogate_is_a_shape_violation.
    rejected({ steps: [{ id: 'b', kind: 'push_branch', args: { name: '\ud800' } }] }, /unpaired surrogate/);
    rejected({ steps: [{ id: 'b', kind: 'push_branch', args: { name: 'a\udfffb' } }] }, /unpaired surrogate/);
    rejected({ steps: [{ id: 'b', kind: 'push_branch', args: { name: 'x\ud83d' } }] }, /unpaired surrogate/);
  });

  it('accepts a PAIRED surrogate, which is an ordinary astral character', () => {
    // The calibration: an emoji IS a surrogate pair in UTF-16, so a check
    // reading code units would refuse it.
    const plan = checkPlanSchema(
      { steps: [{ id: 'b', kind: 'push_branch', args: { name: '🎉' } }] },
      POLICY,
    );
    assert.equal(plan.steps[0]?.args['name'], '🎉');
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

describe('an integer is an integer LEXEME, as it is in Python', () => {
  // `1.0` and `1` parse to the same double, so Number.isInteger cannot tell them
  // apart and the schema gate accepted `"line": 1.0` where plan_verify rejected
  // it as a float. The distinction survives only in the source text, so this is
  // decided at the parse boundary — parsePlanJson — not in checkScalar.
  const INTEGER_POLICY: PlanPolicy = checkPlanPolicy({
    ...POLICY,
    step_kinds: {
      suggest: {
        write_class: false,
        args: {
          path: { type: 'string', min_length: 1, max_length: 500 },
          line: { type: 'integer', minimum: 1 },
        },
      },
    },
    ordering: [],
  });

  function parsed(text: string): unknown {
    return parsePlanJson(text);
  }

  it('accepts a plain integer literal', () => {
    const plan = checkPlanSchema(parsed('{"steps":[{"id":"s","kind":"suggest","args":{"path":"a.py","line":7}}]}'),
      INTEGER_POLICY);
    assert.equal(plan.steps[0]?.args['line'], 7);
  });

  it('rejects a decimal-point spelling of a whole number', () => {
    assert.throws(
      () => parsed('{"steps":[{"id":"s","kind":"suggest","args":{"path":"a.py","line":1.0}}]}'),
      { name: 'Rejection', message: /1\.0.*not an integer/ },
    );
  });

  it('rejects exponent notation, which Python also reads as a float', () => {
    assert.throws(
      () => parsed('{"steps":[{"id":"s","kind":"suggest","args":{"path":"a.py","line":1e2}}]}'),
      { name: 'Rejection', message: /not an integer/ },
    );
  });

  it('rejects an integer literal too large to survive as a double', () => {
    // Python keeps 9007199254740993 exactly; the double rounds it to ...992, so
    // the two gates would be checking different numbers. Refused rather than
    // silently re-spelled.
    assert.throws(
      () => parsed('{"steps":[{"id":"s","kind":"suggest","args":{"path":"a.py","line":9007199254740993}}]}'),
      { name: 'Rejection', message: /exactly/ },
    );
  });

  it('rejects rather than admits when no source text is reported', () => {
    // Unreachable through JSON.parse on any supported runtime — every one of them
    // reports source text for a number — so this is a direct call, pinning the
    // DIRECTION the predicate fails in rather than a scenario. If a runtime ever
    // stopped reporting it, the whole-numbered float this boundary exists to catch
    // must not become admissible.
    assert.throws(() => reviveJsonNumber('line', 1, undefined), Rejection);
    assert.throws(() => reviveJsonNumber('line', 1, {}), Rejection);
  });

  it('decides integer-ness from the source text when the runtime reports it', () => {
    // The other bound: the supported shape must not reject, or the line above
    // would be a gate on every plan carrying a number.
    assert.equal(reviveJsonNumber('line', 7, { source: '7' }), 7);
  });

  it('leaves strings holding a decimal spelling alone', () => {
    // The rule is about JSON numbers. A string is a string, and its own spec
    // decides it.
    const plan = checkPlanSchema(
      parsed('{"steps":[{"id":"s","kind":"suggest","args":{"path":"1.0","line":1}}]}'),
      INTEGER_POLICY,
    );
    assert.equal(plan.steps[0]?.args['path'], '1.0');
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

  it('rejects a scalar spec key no gate reads', () => {
    // `maximum` has no reader in EITHER gate, so {minimum:1, maximum:2} read as
    // a cap to a reviewer while admitting 100. Same fail-closed rule the plan
    // policy's own top-level keys get: a key nobody reads is the worst outcome
    // for a reviewable policy file.
    assert.throws(
      () =>
        checkPlanPolicy({
          ...POLICY,
          step_kinds: {
            patch: { write_class: false, args: { n: { type: 'integer', minimum: 1, maximum: 2 } } },
          },
          ordering: [],
        }),
      { name: 'PolicyError', message: /maximum/ },
    );
  });

  it('rejects a scalar spec key belonging to another type', () => {
    // max_length on an integer, minimum on a string: each is a bound the reader
    // for that type never consults, so it reads as enforcement and enforces
    // nothing.
    for (const args of [
      { n: { type: 'integer', minimum: 1, max_length: 5 } },
      { s: { type: 'string', max_length: 5, minimum: 1 } },
      { e: { type: 'enum', values: ['a'], max_length: 5 } },
    ]) {
      assert.throws(
        () => checkPlanPolicy({ ...POLICY, step_kinds: { patch: { write_class: false, args } }, ordering: [] }),
        PolicyError,
      );
    }
  });

  it('accepts exactly the keys each scalar type declares', () => {
    // The complement: the shipped spellings must keep loading, or the rule has
    // cost the policy its expressiveness.
    const policy = checkPlanPolicy({
      ...POLICY,
      step_kinds: {
        patch: {
          write_class: false,
          args: {
            path: { type: 'string', min_length: 1, max_length: 500, pattern: 'a+' },
            body: { type: 'string', max_length: 10, markdown: true },
            line: { type: 'integer', minimum: 1 },
            tag: { type: 'enum', values: ['x', 'y'] },
          },
        },
      },
      ordering: [],
    });
    assert.equal(policy.step_kinds['patch']?.args['line']?.minimum, 1);
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

  it('rejects a pattern only the ENFORCER would refuse', () => {
    // The loader compiled the pattern plainly while checkScalar compiles it
    // anchored and with `u`, which is stricter: `a{,3}` and `\p{Foo}` both load
    // under plain new RegExp and throw SyntaxError at enforcement time, so the
    // gate raised out of the middle of a check instead of returning a verdict.
    // Both also compile under Python's re, so the policy looked loadable
    // everywhere it was tried.
    for (const pattern of ['a{,3}', '\\p{Foo}']) {
      assert.throws(
        () =>
          checkPlanPolicy({
            ...POLICY,
            step_kinds: {
              patch: { write_class: false, args: { p: { type: 'string', max_length: 5, pattern } } },
            },
            ordering: [],
          }),
        { name: 'PolicyError', message: /not a valid regex/ },
        `loader admitted ${pattern}, which checkScalar cannot compile`,
      );
    }
  });

  it('rejects a spec VALUE the enforcer cannot use', () => {
    // A key with no reader is already refused; these are keys whose VALUE the
    // reader cannot use. `{"type":"integer","minimum":"bogus"}` loaded, and
    // `value < "bogus"` is `false` in JS, so a plan carrying line -9999 verified
    // against a policy that reads as bounding it at 1. Python raises TypeError on
    // the same comparison — one policy, two behaviours, neither a verdict.
    for (const args of [
      { n: { type: 'integer', minimum: 'bogus' } },
      { s: { type: 'string', max_length: '5' } },
      { t: { type: 'string', max_length: 5, min_length: true } },
      { e: { type: 'enum', values: 'automated' } },
    ]) {
      assert.throws(
        () => checkPlanPolicy({ ...POLICY, step_kinds: { patch: { write_class: false, args } }, ordering: [] }),
        PolicyError,
        `loader admitted ${JSON.stringify(args)}`,
      );
    }
  });
});
