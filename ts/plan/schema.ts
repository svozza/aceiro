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

/** The reviver's third argument, carrying the raw source text of the value being
 * revived. Declared here rather than taken from the lib types, which do not
 * describe it yet.
 *
 * Optional at the parameter, not just in the property: Node 20 passes no third
 * argument at all, so a runtime below the `engines` floor gives `undefined`
 * rather than an object with no `source`. `engines` is advisory and the executor
 * spawns whatever `node` is on PATH, so both spellings of "no source text" are
 * reachable and neither may be dereferenced. */
interface JsonParseContext {
  readonly source?: string;
}

/** A JSON number spelled as an integer: no decimal point, no exponent. Both the
 * sign and the digits, so `-3` passes and `-3.0` does not. */
const INTEGER_LEXEME_RE = /^-?(?:0|[1-9][0-9]*)$/;

/**
 * A JSON.parse reviver deciding a number's integer-ness from its SOURCE TEXT,
 * and rejecting the plan when it cannot.
 *
 * This exists because `1.0` and `1` parse to the same double, so
 * `Number.isInteger` cannot distinguish them — while Python's json reads the
 * first as a float and the second as an int, and check_scalar rejects the float.
 * A plan one gate admits and the other rejects is a defect in one of them, and
 * the information needed to agree survives only in the text.
 *
 * A whole number too large for a double is refused rather than rounded: Python
 * keeps 9007199254740993 exactly, a double does not, and two gates checking
 * different numbers is the same defect in a quieter form.
 *
 * Exported so the no-source-text refusal is testable at all: no supported
 * runtime can reach it through JSON.parse, so the only way to exercise it is to
 * call this directly.
 */
export function reviveJsonNumber(_key: string, value: unknown, context?: JsonParseContext): unknown {
  if (typeof value !== 'number') return value;
  const source = context?.source;
  if (source === undefined) {
    // No source text means the runtime does not carry it, and integer-ness
    // cannot be decided. Fail closed rather than silently falling back to the
    // check that admits 1.0.
    throw new Rejection(
      'plan: this runtime does not report JSON source text, so an integer cannot be ' +
        'told from a whole-numbered float; Node 22 or newer is required',
    );
  }
  if (!INTEGER_LEXEME_RE.test(source)) {
    throw new Rejection(
      `plan: ${source} is not an integer — a decimal point or exponent makes it a float, ` +
        'which the Python gate rejects, so it is not accepted here either',
    );
  }
  if (!Number.isSafeInteger(value)) {
    throw new Rejection(
      `plan: ${source} cannot be represented exactly, so the two gates would check ` +
        'different numbers',
    );
  }
  return value;
}

/**
 * Parse plan JSON from text, through the integer-lexeme reviver.
 *
 * Callers holding already-parsed data (tests, and any in-process caller) may
 * still use checkPlanSchema directly; this is the boundary for plan JSON that
 * arrived as text, which is every production path.
 */
export function parsePlanJson(text: string): unknown {
  // The lib types do not describe the reviver's context argument yet.
  const parseWithSource = JSON.parse as unknown as (
    input: string,
    reviver: (this: unknown, key: string, value: unknown, context: JsonParseContext) => unknown,
  ) => unknown;
  return parseWithSource(text, reviveJsonNumber);
}

/** Length in Unicode code points, which is what `len()` measures on the Python
 * side of the same policy number. NOT `text.length`: that counts UTF-16 units,
 * so every astral code point costs two and a max_length admits half as much text
 * here as it does there. The metric is part of the policy, so it can only mean
 * one thing. Twin of plan_verify's reliance on Python's own string length. */
function codePointLength(text: string): number {
  let count = 0;
  for (const _codePoint of text) count += 1;
  return count;
}

/** The index of the first unpaired surrogate, or -1.
 *
 * JSON permits `\ud800` and both parsers accept it, so a plan can carry a string
 * that is not encodable text — and the Python twin's later phases encode it, for
 * the anchor and for the audit record, raising UnicodeEncodeError rather than a
 * Rejection. The two gates have to agree on whether such a plan is well-formed.
 *
 * Pairing is checked rather than the range alone, because a JS string keeps UTF-16
 * units: an ordinary emoji IS a high surrogate followed by a low one. The Python
 * twin needs no pairing logic, since `json.loads` combines a valid pair into one
 * code point. */
function unpairedSurrogateAt(text: string): number {
  for (let i = 0; i < text.length; i += 1) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = i + 1 < text.length ? text.charCodeAt(i + 1) : 0;
      if (next >= 0xdc00 && next <= 0xdfff) {
        i += 1; // a well-formed pair
        continue;
      }
      return i;
    }
    if (unit >= 0xdc00 && unit <= 0xdfff) return i; // a low surrogate with no high
  }
  return -1;
}

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
      // Before the bounds, which are all total on a surrogate: the length is
      // measurable and NFC is a no-op, so the value would satisfy every declared
      // constraint and reach a gate that has to encode it.
      const surrogate = unpairedSurrogateAt(value);
      if (surrogate !== -1) {
        const code = value.charCodeAt(surrogate).toString(16).toUpperCase().padStart(4, '0');
        throw new Rejection(
          `${where}: contains an unpaired surrogate (U+${code}) at position ${surrogate}, which is ` +
            'not encodable text; a plan argument must be a string this gate can write to a file ' +
            'and to the audit log',
        );
      }
      // NFC before measuring, so decomposed forms cannot smuggle extra budget.
      // Same rule as the artifact verifier's check_scalar; the two must agree,
      // since policy.json is shared and a length means one thing.
      const length = codePointLength(value.normalize('NFC'));
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
    // checkPlanPolicy gives step_kinds a null prototype, so this cannot resolve
    // an inherited name like "toString".
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
