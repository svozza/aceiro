/**
 * Structural check on a plan, before the solver sees it.
 *
 * This is the shape gate, and it carries ADR-0004's three reserved closures. It
 * runs first for a reason the spike's own README makes: the solver's answer is
 * only as good as the encoding behind it, and an encoding built from a plan whose
 * shape was never checked is reasoning about a structure it assumed.
 *
 * Fail-closed, whole-plan, first violation wins — the same posture as the
 * artifact verifier. No partial acceptance, and no repair.
 */

// .js, not .ts: this resolves against the emitted output, which is what Node
// loads. Standard for NodeNext ESM.
import { Rejection, type PlanPolicy, type ScalarSpec } from './policy.js';

export interface Step {
  readonly id: string;
  readonly kind: string;
  readonly args: Readonly<Record<string, string | number>>;
}

export interface Plan {
  readonly steps: readonly Step[];
}

/** Ids exist so steps can be referred to; a duplicate makes a reference
 * ambiguous, and a counterexample naming a step becomes unactionable. Kept
 * conservative: this is what appears in audit output. */
const ID_RE = /^[a-z][a-z0-9_]{0,39}$/;

const PLAN_KEYS = ['steps'] as const;
const STEP_KEYS = ['id', 'kind', 'args'] as const;

function checkScalar(value: unknown, spec: ScalarSpec, where: string): string | number {
  switch (spec.type) {
    case 'string': {
      if (typeof value !== 'string') {
        // ADR-0004's second closure lands exactly here. An execution-time
        // binding would arrive as {"$ref": "step1.output"} — an object where a
        // string is expected — so it rejects today with no per-argument wrapper.
        // Named explicitly in the message because a bare "expected string" would
        // send someone looking for a typo instead of reading ADR-0004.
        const shape = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
        throw new Rejection(
          `${where}: expected a literal string, got ${shape}` +
            (shape === 'object' ? ' — argument_forms admits only ["literal"], so bindings are not accepted' : ''),
        );
      }
      // NFC before measuring, so decomposed forms cannot smuggle extra budget.
      // Same rule as the artifact verifier's check_scalar; the two must agree,
      // since policy.json is shared and a length means one thing.
      const length = value.normalize('NFC').length;
      if (length < (spec.min_length ?? 0)) throw new Rejection(`${where}: shorter than min_length ${spec.min_length}`);
      if (spec.max_length !== undefined && length > spec.max_length) {
        throw new Rejection(`${where}: exceeds max_length ${spec.max_length}`);
      }
      if (spec.pattern !== undefined) {
        // Anchored on both ends: an unanchored pattern would accept anything
        // with a matching substring, which is how the dotfile-path defect in
        // §17 worked — enforced exactly as written, and written wrong.
        const anchored = new RegExp(`^(?:${spec.pattern})$`, 'u');
        if (!anchored.test(value)) {
          throw new Rejection(`${where}: does not match required pattern ${JSON.stringify(spec.pattern)}`);
        }
      }
      return value;
    }
    case 'integer': {
      if (typeof value !== 'number' || !Number.isInteger(value)) {
        throw new Rejection(`${where}: expected an integer, got ${typeof value}`);
      }
      if (spec.minimum !== undefined && value < spec.minimum) {
        throw new Rejection(`${where}: below minimum ${spec.minimum}`);
      }
      return value;
    }
    case 'enum': {
      if (typeof value !== 'string' || !(spec.values ?? []).includes(value)) {
        throw new Rejection(`${where}: ${JSON.stringify(value)} not in ${JSON.stringify(spec.values)}`);
      }
      return value;
    }
  }
}

export function checkPlanSchema(candidate: unknown, policy: PlanPolicy): Plan {
  if (typeof candidate !== 'object' || candidate === null || Array.isArray(candidate)) {
    throw new Rejection('plan: expected a JSON object');
  }
  const plan = candidate as Record<string, unknown>;

  const extra = Object.keys(plan).filter((key) => !PLAN_KEYS.includes(key as 'steps'));
  if (extra.length > 0) {
    // ADR-0004's third closure: no version field in the artifact. A
    // model-supplied schema version is a model-selected policy, and the policy
    // owns its version. So "version" is not special-cased — it is simply an
    // unexpected key, like anything else the model invents.
    throw new Rejection(`plan: unexpected keys ${JSON.stringify(extra.sort())}`);
  }
  if (!('steps' in plan)) throw new Rejection('plan: missing steps');

  const steps = plan['steps'];
  if (!Array.isArray(steps)) throw new Rejection('plan.steps: expected an array');
  if (steps.length === 0) {
    // An empty plan is not a safe no-op to wave through: something asked for a
    // remediation and nothing would happen, which is a failure the caller has to
    // see rather than a success with no effect.
    throw new Rejection('plan.steps: empty, so the plan does nothing');
  }
  if (steps.length > policy.max_steps) {
    throw new Rejection(`plan.steps: ${steps.length} steps exceeds max_steps ${policy.max_steps}`);
  }

  const seenIds = new Set<string>();
  const checked: Step[] = [];

  for (const [index, raw] of steps.entries()) {
    const where = `plan.steps[${index}]`;
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
      throw new Rejection(`${where}: expected an object`);
    }
    const step = raw as Record<string, unknown>;

    const stepExtra = Object.keys(step).filter((key) => !STEP_KEYS.includes(key as 'id' | 'kind' | 'args'));
    if (stepExtra.length > 0) throw new Rejection(`${where}: unexpected keys ${JSON.stringify(stepExtra.sort())}`);
    for (const key of STEP_KEYS) {
      if (!(key in step)) throw new Rejection(`${where}: missing ${key}`);
    }

    // ADR-0004's first closure: {id, kind, args} even while straight-line,
    // because a straight-line plan is the degenerate case of a program. Steps
    // carry identity even when nothing refers to them yet.
    const id = step['id'];
    if (typeof id !== 'string' || !ID_RE.test(id)) {
      throw new Rejection(`${where}.id: expected a short lowercase identifier, got ${JSON.stringify(id)}`);
    }
    if (seenIds.has(id)) throw new Rejection(`${where}.id: duplicate id ${JSON.stringify(id)}`);
    seenIds.add(id);

    const kind = step['kind'];
    if (typeof kind !== 'string') throw new Rejection(`${where}.kind: expected a string`);
    const kindSpec = policy.step_kinds[kind];
    if (kindSpec === undefined) {
      // Allowlist, not denylist: an unknown kind is not a no-op the executor can
      // skip. It is a request the harness does not understand, and the only safe
      // reading of it is to reject the whole plan.
      throw new Rejection(
        `${where}.kind: ${JSON.stringify(kind)} is not a declared step kind ` +
          `(${Object.keys(policy.step_kinds).sort().join(', ')})`,
      );
    }

    const args = step['args'];
    if (typeof args !== 'object' || args === null || Array.isArray(args)) {
      throw new Rejection(`${where}.args: expected an object`);
    }
    const argRecord = args as Record<string, unknown>;
    const declared = Object.keys(kindSpec.args);
    const argExtra = Object.keys(argRecord).filter((key) => !declared.includes(key));
    if (argExtra.length > 0) throw new Rejection(`${where}.args: unexpected keys ${JSON.stringify(argExtra.sort())}`);

    const argsOut: Record<string, string | number> = {};
    for (const [argName, argSpec] of Object.entries(kindSpec.args)) {
      if (!(argName in argRecord)) throw new Rejection(`${where}.args: missing ${argName}`);
      argsOut[argName] = checkScalar(argRecord[argName], argSpec, `${where}.args.${argName}`);
    }

    checked.push({ id, kind, args: argsOut });
  }

  return { steps: checked };
}
