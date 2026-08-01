/**
 * The plan half of policy.json, typed.
 *
 * policy.json is shared data with two readers in two languages (ADR-0003), and
 * it is the reviewable security object: its sha256 is stamped into the
 * transcript. So this module only describes and loads it — the policy is never
 * constructed in code, and there are no defaults. A missing field is a policy
 * error, not something to fill in, because a default here would be a rule nobody
 * reviewed.
 */

import { readFileSync } from 'node:fs';

/** A plan violates policy. The message states which check and why. */
export class Rejection extends Error {
  override readonly name = 'Rejection';
}

/** The policy itself is malformed. Distinct from Rejection: this is our bug or a
 * bad deployment, not a misbehaving generator, and it must never be reported as
 * "the model produced something invalid". */
export class PolicyError extends Error {
  override readonly name = 'PolicyError';
}

export interface ScalarSpec {
  readonly type: 'string' | 'integer' | 'enum';
  readonly min_length?: number;
  readonly max_length?: number;
  readonly minimum?: number;
  readonly pattern?: string;
  readonly values?: readonly string[];
  readonly markdown?: boolean;
}

export interface StepKindSpec {
  /** Whether this kind performs an effect outside the harness. What the ordering
   * and frame-condition policies quantify over (CONTEXT.md: write-class step). */
  readonly write_class: boolean;
  readonly args: Readonly<Record<string, ScalarSpec>>;
}

export interface OrderingRule {
  readonly before: string;
  readonly after: string;
}

export interface PlanPolicy {
  readonly max_steps: number;
  /** Empty today. ADR-0004's first reserved shape: admitting `branch` later is an
   * entry here plus one in step_kinds, not a change to the plan's shape. */
  readonly control_flow: readonly string[];
  /** `["literal"]` today. ADR-0004's second: an execution-time binding would
   * arrive as a distinguishable shape ({"$ref": ...}), so an object where a
   * string is expected rejects now, with no wrapper on every argument. */
  readonly argument_forms: readonly string[];
  readonly step_kinds: Readonly<Record<string, StepKindSpec>>;
  readonly ordering: readonly OrderingRule[];
  readonly max_patched_files: number;
  readonly max_changed_lines: number;
  readonly path_denylist: readonly string[];
  /** The harness-owned namespace every branch a plan pushes must sit under. A
   * prefix rather than a denylist of protected names, so "not the default branch"
   * is a property of the name (ADR-0009 addendum). */
  readonly branch_prefix: string;
  /** Labels a plan may apply, matched EXACTLY. Ships empty: a label is a control
   * surface (this repo's evals workflow triggers on one), so a consumer names the
   * ones it accepts. */
  readonly label_allowlist: readonly string[];
}

function requireKeys(object: Record<string, unknown>, keys: readonly string[], where: string): void {
  for (const key of keys) {
    if (!(key in object)) throw new PolicyError(`${where}: missing ${key}`);
  }
  const extra = Object.keys(object).filter((k) => !keys.includes(k));
  if (extra.length > 0) {
    // Fail closed on a key nobody reads, exactly as the artifact verifier does:
    // a misspelled "path_denylist" that silently denies nothing is the worst
    // possible outcome for a policy file.
    throw new PolicyError(`${where}: unexpected keys ${JSON.stringify(extra.sort())}`);
  }
}

const PLAN_KEYS = [
  'max_steps',
  'control_flow',
  'argument_forms',
  'step_kinds',
  'ordering',
  'max_patched_files',
  'max_changed_lines',
  'path_denylist',
  'branch_prefix',
  'label_allowlist',
] as const;

export function checkPlanPolicy(candidate: unknown): PlanPolicy {
  if (typeof candidate !== 'object' || candidate === null || Array.isArray(candidate)) {
    throw new PolicyError('policy.plan: expected an object');
  }
  const plan = candidate as Record<string, unknown>;
  requireKeys(plan, PLAN_KEYS, 'policy.plan');

  for (const numeric of ['max_steps', 'max_patched_files', 'max_changed_lines'] as const) {
    const value = plan[numeric];
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
      throw new PolicyError(`policy.plan.${numeric}: expected a positive integer`);
    }
  }

  for (const list of ['control_flow', 'argument_forms', 'path_denylist', 'label_allowlist'] as const) {
    const value = plan[list];
    if (!Array.isArray(value) || value.some((entry) => typeof entry !== 'string')) {
      throw new PolicyError(`policy.plan.${list}: expected an array of strings`);
    }
  }

  // An empty prefix would confine nothing, so it is a policy error rather than a
  // permissive setting. No default: a default here is a rule nobody reviewed.
  if (typeof plan['branch_prefix'] !== 'string' || plan['branch_prefix'].length === 0) {
    throw new PolicyError('policy.plan.branch_prefix: expected a non-empty string');
  }

  const forms = plan['argument_forms'] as readonly string[];
  if (forms.length !== 1 || forms[0] !== 'literal') {
    // The encoding below assumes every argument is a literal. If a policy ever
    // admits another form, the prover has to learn it FIRST — silently accepting
    // the flag would mean bindings pass a check that never looked at them.
    throw new PolicyError(
      `policy.plan.argument_forms: this prover only implements ["literal"], got ${JSON.stringify(forms)}`,
    );
  }

  const kinds = plan['step_kinds'];
  if (typeof kinds !== 'object' || kinds === null || Array.isArray(kinds)) {
    throw new PolicyError('policy.plan.step_kinds: expected an object');
  }
  const kindEntries = Object.entries(kinds as Record<string, unknown>);
  if (kindEntries.length === 0) throw new PolicyError('policy.plan.step_kinds: no kinds declared');

  for (const [name, spec] of kindEntries) {
    if (typeof spec !== 'object' || spec === null || Array.isArray(spec)) {
      throw new PolicyError(`policy.plan.step_kinds.${name}: expected an object`);
    }
    const kind = spec as Record<string, unknown>;
    requireKeys(kind, ['write_class', 'args'], `policy.plan.step_kinds.${name}`);
    if (typeof kind['write_class'] !== 'boolean') {
      throw new PolicyError(`policy.plan.step_kinds.${name}.write_class: expected a boolean`);
    }
    const args = kind['args'];
    if (typeof args !== 'object' || args === null || Array.isArray(args)) {
      throw new PolicyError(`policy.plan.step_kinds.${name}.args: expected an object`);
    }
    for (const [argName, argSpec] of Object.entries(args as Record<string, unknown>)) {
      const where = `policy.plan.step_kinds.${name}.args.${argName}`;
      if (typeof argSpec !== 'object' || argSpec === null) throw new PolicyError(`${where}: expected an object`);
      const scalar = argSpec as Record<string, unknown>;
      const type = scalar['type'];
      if (type !== 'string' && type !== 'integer' && type !== 'enum') {
        throw new PolicyError(`${where}: unknown type ${JSON.stringify(type)}`);
      }
      if (type === 'string' && typeof scalar['max_length'] !== 'number') {
        // Mirrors the artifact verifier's rule that every string must declare how
        // it is bounded. An unbounded string reaches a rendered PR body.
        throw new PolicyError(`${where}: a string arg must declare max_length`);
      }
      if (type === 'enum' && !Array.isArray(scalar['values'])) {
        throw new PolicyError(`${where}: an enum arg must declare values`);
      }
      if (typeof scalar['pattern'] === 'string') {
        try {
          new RegExp(scalar['pattern'] as string);
        } catch (cause) {
          throw new PolicyError(`${where}: pattern is not a valid regex: ${String(cause)}`);
        }
      }
    }
  }

  const ordering = plan['ordering'];
  if (!Array.isArray(ordering)) throw new PolicyError('policy.plan.ordering: expected an array');
  for (const [index, rule] of ordering.entries()) {
    const where = `policy.plan.ordering[${index}]`;
    if (typeof rule !== 'object' || rule === null) throw new PolicyError(`${where}: expected an object`);
    const entry = rule as Record<string, unknown>;
    requireKeys(entry, ['before', 'after'], where);
    for (const side of ['before', 'after'] as const) {
      const value = entry[side];
      if (typeof value !== 'string') throw new PolicyError(`${where}.${side}: expected a string`);
      if (!(value in (kinds as Record<string, unknown>))) {
        // An ordering rule naming a kind that does not exist is a rule that can
        // never fire, which reads as enforcement while enforcing nothing.
        throw new PolicyError(`${where}.${side}: ${JSON.stringify(value)} is not a declared step kind`);
      }
    }
  }

  return plan as unknown as PlanPolicy;
}

export function loadPlanPolicy(policyPath: string): PlanPolicy {
  const parsed: unknown = JSON.parse(readFileSync(policyPath, 'utf8'));
  if (typeof parsed !== 'object' || parsed === null) throw new PolicyError('policy: expected an object');
  const plan = (parsed as Record<string, unknown>)['plan'];
  if (plan === undefined) throw new PolicyError('policy: no "plan" section');
  return checkPlanPolicy(plan);
}

export function writeClassKinds(policy: PlanPolicy): readonly string[] {
  return Object.entries(policy.step_kinds)
    .filter(([, spec]) => spec.write_class)
    .map(([name]) => name);
}
