/**
 * The plan prover: policies asserted NEGATED, so unsat means the policy holds.
 *
 * The direction is the whole point. Asking "is this plan fine?" and getting `sat`
 * tells you one model satisfies your constraints. Asking "can this plan violate
 * the policy?" and getting `unsat` tells you no execution can, including paths
 * nobody enumerated. §2.5's threshold for a solver is a ∀-shaped claim, and both
 * policies here are ∀-shaped: ordering quantifies over pairs of steps, the frame
 * condition over all files.
 *
 * Encoded in the general form from day one, per ADR-0004's first consequence:
 * taint propagates transitively over argument bindings even though
 * argument_forms admits only literals, so the machinery is exercised rather than
 * dormant. That leaves taint trivially unsat on every admissible plan, which is a
 * check with no signal — so proveTaint accepts a synthetic bindings argument that
 * the schema can never produce, and the corpus uses it to assert `sat` with the
 * expected counterexample. The prover is tested beyond what the policy admits.
 *
 * This module is the risk ADR-0003 names: "the solver's answer is no more
 * trustworthy than the encoding behind it". Every function here returns the
 * counterexample on sat, because a bare verdict is not an audit record.
 */

import { init } from 'z3-solver';
import type { Plan } from './schema.js';
import { writeClassKinds, type PlanPolicy } from './policy.js';

export interface Counterexample {
  readonly policy: string;
  /** The concrete violating path, as human-readable lines. CONTEXT.md's
   * definition: the audit log's evidence for a rejection, not the bare verdict. */
  readonly path: readonly string[];
}

export interface ProofResult {
  readonly holds: boolean;
  readonly policy: string;
  readonly ms: number;
  readonly counterexample?: Counterexample;
}

/** An execution-time binding: step `from` feeds an argument of step `to`.
 *
 * The schema cannot produce these — argument_forms is ["literal"] — so this is
 * only reachable from tests. It exists so the taint encoding can be shown to
 * catch a leak, rather than reporting unsat because nothing was ever tainted.
 */
export interface SyntheticBinding {
  readonly from: number;
  readonly to: number;
}

/** Every Context is named 'main', and it has to be.
 *
 * z3-solver's SortToExprMap decides what an expression's static type is by
 * testing `S extends BoolSort` — against BoolSort's DEFAULT name parameter,
 * which is 'main'. So under any other context name a Bool-sorted function
 * application types as Expr<BoolSort> instead of Bool, losing not/and/or/implies
 * and making the quantified frame encoding uncompilable without casts.
 *
 * Verified rather than guessed: the identical declaration narrows correctly under
 * Context('main') and fails under Context('frame'). Upstream typing bug in
 * z3-solver 5.0.0. The name is cosmetic at runtime — contexts are still separate
 * objects, one per proof — so complying costs nothing and buys full type
 * checking in the layer ADR-0003 says needs it most.
 *
 * Do not "improve" this by naming contexts after their policies.
 */
const CONTEXT_NAME = 'main';

let cached: Awaited<ReturnType<typeof init>> | undefined;

/** The WASM module costs ~85ms to load and is stateless across solvers, so it is
 * loaded once per process. */
async function z3(): Promise<Awaited<ReturnType<typeof init>>> {
  cached ??= await init();
  return cached;
}

/** Release the WASM threads. Node will not exit while they are alive, so a CLI
 * or a test run has to call this. */
export async function shutdown(): Promise<void> {
  if (cached !== undefined) {
    cached.em.PThread.terminateAllThreads();
    cached = undefined;
  }
}

/**
 * Ordering: no write-class step may precede a step it depends on.
 *
 * Stated over the policy's declared rules ({before, after}), and quantified over
 * every PAIR of steps rather than checked pairwise in a loop, so the claim is
 * "on no ordering of these steps does an `after` precede a `before`".
 *
 * NOTE what this is NOT: ADR-0001 dropped §20's dominance policy, so there is no
 * "run_tests must dominate every write" obligation here and no `passed` claim to
 * bind. Ordering is only about the mutating actions' sequence among themselves.
 */
export async function proveOrdering(plan: Plan, policy: PlanPolicy): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Int, Or, And, Distinct } = Z3;

  const solver = new Solver();
  const position = plan.steps.map((step) => Int.const(`pos_${step.id}`));

  // A plan IS an order, so positions are pinned to the plan's own indices. The
  // solver is not searching for an order; it is being asked whether THIS order
  // violates a rule.
  for (const [index, variable] of position.entries()) {
    solver.add(variable.eq(index));
  }
  if (position.length > 1) solver.add(Distinct(...position));

  // Negated: some declared rule is violated by some pair.
  const violations = [];
  for (const rule of policy.ordering) {
    for (const [i, first] of plan.steps.entries()) {
      for (const [j, second] of plan.steps.entries()) {
        if (i === j) continue;
        if (first.kind !== rule.before || second.kind !== rule.after) continue;
        // `after` appears before `before`.
        const posJ = position[j];
        const posI = position[i];
        if (posJ === undefined || posI === undefined) continue;
        violations.push(And(posJ.lt(posI)));
      }
    }
  }

  const started = performance.now();
  if (violations.length === 0) {
    // No pair of steps could violate any rule, so there is nothing to solve. Say
    // so rather than asserting Or() of nothing, which is false and would look
    // like a proof.
    return { holds: true, policy: 'ordering', ms: performance.now() - started };
  }
  solver.add(Or(...violations));
  const verdict = await solver.check();
  const ms = performance.now() - started;

  if (verdict === 'unsat') return { holds: true, policy: 'ordering', ms };

  const model = solver.model();
  const ordered = plan.steps
    .map((step, index) => ({ step, at: Number(model.eval(position[index]!).toString()) }))
    .sort((a, b) => a.at - b.at);
  return {
    holds: false,
    policy: 'ordering',
    ms,
    counterexample: {
      policy: 'ordering',
      path: ordered.map(({ step, at }) => `${at}: ${step.kind} (${step.id})`),
    },
  };
}

/**
 * Frame condition: every file the plan modifies is a file the PR touched, AND no
 * modified file is on the policy's path denylist.
 *
 * Quantified over all files via an uninterpreted function, which is the shape the
 * spike proved ergonomic. ADR-0005 is what makes this worth proving: patch
 * CONTENT is unverifiable, so containment is the property on offer — the frame
 * bounds WHERE the agent can write, and this is that bound as a ∀-claim over a
 * closed set the verifier already holds.
 *
 * BOTH obligations are asserted. The denylist used to be computed for the
 * counterexample only and never turned into a constraint, so `denied` could be
 * non-empty only when the frame had already failed for some other path — a
 * denylisted path that IS in changedFiles made the query unsat and this returned
 * holds:true, while src/smtithy/plan_verify.py rejected the identical plan.
 * matchesAny/globToRegExp read as enforcement while enforcing nothing, which is
 * the evaluation-order-sensitive shape ADR-0005 calls security-relevant.
 */
export async function proveFrame(
  plan: Plan,
  policy: PlanPolicy,
  changedFiles: readonly string[],
): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Int, Bool, Function: Fn, ForAll, Implies, Not, And, Or } = Z3;

  const solver = new Solver();
  const touchedByPr = Fn.declare('touched_by_pr', Int.sort(), Bool.sort());
  const modifiedByPlan = Fn.declare('modified_by_plan', Int.sort(), Bool.sort());
  const deniedByPolicy = Fn.declare('denied_by_policy', Int.sort(), Bool.sort());

  // Files are interned to ints: the solver reasons about identity, not text, and
  // an int keeps the encoding small. Both sides use the same table, so a path
  // that appears in one and not the other is a genuinely different file rather
  // than an artefact of two spellings.
  const intern = new Map<string, number>();
  const idOf = (path: string): number => {
    const existing = intern.get(path);
    if (existing !== undefined) return existing;
    const next = intern.size;
    intern.set(path, next);
    return next;
  };

  // suggest joins patch here (ADR-0009): a suggestion the contributor applies
  // modifies the file exactly as a patch would, so the frame condition binds
  // both. GitHub's own diff-anchoring makes an out-of-frame suggestion
  // unpostable anyway, but the policy must hold before mechanics are trusted.
  const patchedPaths = plan.steps
    .filter((step) => step.kind === 'patch' || step.kind === 'suggest')
    .map((step) => step.args['path'])
    .filter((path): path is string => typeof path === 'string');

  for (const path of changedFiles) solver.add(touchedByPr.call(idOf(path)));
  for (const path of patchedPaths) solver.add(modifiedByPlan.call(idOf(path)));

  // The denylist as facts over the SAME intern table, so the solver decides both
  // obligations against one identity for each file. Every interned path is pinned
  // either way — an unconstrained predicate would let the solver choose a value
  // and invent a violation no plan expresses, the same closed-world reason the
  // quantifier below exists.
  for (const [path, id] of intern) {
    const denied = matchesAny(path, policy.path_denylist);
    solver.add(denied ? deniedByPolicy.call(id) : Not(deniedByPolicy.call(id)));
  }

  // Everything outside the two sets is pinned false, so the quantifier ranges
  // over a closed world. Without this, the solver may invent a file that is
  // modified and untouched and report a violation that no plan expresses.
  const known = [...intern.values()];
  const f = Int.const('f');
  if (known.length > 0) {
    const isKnown = Or(...known.map((id) => f.eq(id)));
    solver.add(ForAll([f], Implies(Not(isKnown), Not(modifiedByPlan.call(f)))));
  } else {
    solver.add(ForAll([f], Not(modifiedByPlan.call(f))));
  }

  // Negated policy: some modified file is not touched by the PR, OR some modified
  // file is on the denylist. unsat now covers both obligations, so 'frame: holds'
  // cannot be printed for a denylisted path.
  const g = Int.const('g');
  solver.add(
    Or(
      ...known.map((id) => And(modifiedByPlan.call(id), Not(touchedByPr.call(id)))),
      ...known.map((id) => And(modifiedByPlan.call(id), deniedByPolicy.call(id))),
      // Retained so an empty known set still produces a well-formed query.
      And(modifiedByPlan.call(g), Not(touchedByPr.call(g))),
    ),
  );

  const started = performance.now();
  const verdict = await solver.check();
  const ms = performance.now() - started;
  if (verdict === 'unsat') return { holds: true, policy: 'frame', ms };

  // Report the offending path by name. The solver's witness is an int, so the
  // useful answer comes from the intern table rather than the model.
  //
  // Escaping paths come FIRST, matching the phase order in
  // plan_verify.check_plan_containment (frame, then denylist): a path that both
  // escapes the frame and is denied gets the same reason from both gates rather
  // than two defensible different ones.
  const changed = new Set(changedFiles);
  const escaping = patchedPaths.filter((path) => !changed.has(path));
  const denied = patchedPaths
    .map((path) => ({ path, pattern: firstMatch(path, policy.path_denylist) }))
    .filter((entry): entry is { path: string; pattern: string } => entry.pattern !== undefined);
  return {
    holds: false,
    policy: 'frame',
    ms,
    counterexample: {
      policy: 'frame',
      path: [
        ...escaping.map((path) => `patch ${path}: not a file this PR touched`),
        ...denied.map(
          ({ path, pattern }) => `patch ${path}: on the policy path denylist (${pattern})`,
        ),
      ],
    },
  };
}

/**
 * Taint: no write-class step may take data derived from PR content.
 *
 * Trivially unsat on every admissible plan, because argument_forms is
 * ["literal"] — there is no `read_pr_file` node to taint from once the generator
 * has already read everything at generation time (ADR-0004's opening argument).
 * Encoded anyway, in its general transitive form, so the ∀-paths machinery is
 * exercised from the first commit; `bindings` lets the corpus construct the
 * violation the schema forbids and check that this reports it.
 */
export async function proveTaint(
  plan: Plan,
  policy: PlanPolicy,
  bindings: readonly SyntheticBinding[] = [],
): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Bool, Or, And, Not } = Z3;

  const solver = new Solver();
  const writeKinds = new Set(writeClassKinds(policy));
  const tainted = plan.steps.map((step) => Bool.const(`tainted_${step.id}`));

  // Transitive closure, unrolled over the bounded plan: a step is tainted if it
  // reads PR content, or if any argument binds to a tainted earlier step.
  for (const [index, step] of plan.steps.entries()) {
    const readsPr = step.kind === 'read_pr_file';
    const inherited = bindings
      .filter((binding) => binding.to === index)
      .map((binding) => tainted[binding.from])
      .filter((variable): variable is NonNullable<typeof variable> => variable !== undefined);

    const self = tainted[index];
    if (self === undefined) continue;
    if (readsPr) {
      solver.add(self);
    } else if (inherited.length > 0) {
      solver.add(self.eq(Or(...inherited)));
    } else {
      solver.add(Not(self));
    }
  }

  // Negated policy: some write-class step is tainted.
  const violations = plan.steps
    .map((step, index) => ({ step, variable: tainted[index] }))
    .filter(({ step, variable }) => writeKinds.has(step.kind) && variable !== undefined)
    .map(({ variable }) => variable!);

  const started = performance.now();
  if (violations.length === 0) {
    return { holds: true, policy: 'taint', ms: performance.now() - started };
  }
  solver.add(Or(...violations));
  const verdict = await solver.check();
  const ms = performance.now() - started;
  if (verdict === 'unsat') return { holds: true, policy: 'taint', ms };

  const model = solver.model();
  const leaking = plan.steps
    .map((step, index) => ({ step, index, isTainted: model.eval(tainted[index]!).toString() === 'true' }))
    .filter(({ isTainted }) => isTainted);
  return {
    holds: false,
    policy: 'taint',
    ms,
    counterexample: {
      policy: 'taint',
      path: leaking.map(({ step, index }) => {
        const sources = bindings.filter((b) => b.to === index).map((b) => plan.steps[b.from]?.id ?? '?');
        const via = sources.length > 0 ? `, bound to ${sources.join(', ')}` : '';
        const marker = writeKinds.has(step.kind) ? '  <- the leak' : '';
        return `${index}: ${step.kind} (${step.id}) tainted${via}${marker}`;
      }),
    },
  };
}

/** Which argument names a write-class kind's target. The Python twin is
 * plan_verify.BRANCH_ARGS. */
const BRANCH_ARGS: Readonly<Record<string, string>> = { push_branch: 'name', open_pr: 'branch' };

/**
 * Write-class targets: every branch a plan pushes is inside the harness-owned
 * namespace, and every label it applies is on the allowlist.
 *
 * The frame condition bounds where the agent can WRITE FILES; this bounds where
 * the write-class steps ACT. Containment binds only patch and suggest, so these
 * arguments used to be constrained by nothing but a permissive regex — and
 * push_branch.name is the single argument deciding where the executor's
 * `contents: write` credential is pointed.
 *
 * Not a solver query. It is a per-step membership test over closed sets — a
 * ground check in CONTEXT.md's terms, decidable by direct interpretation in one
 * pass, so encoding it as ∀-claims would add an encoding layer to trust for no
 * reachability reasoning gained (ADR-0003's own threshold for reaching for a
 * solver). Reported in the same ProofResult shape so prove-cli treats it
 * identically, and the counterexample names the offending step as every other
 * policy here does.
 *
 * `headBranch` is a plan input, so it is optional: undefined means "unknown",
 * which refuses nothing extra because the namespace still confines the target.
 */
export function proveWriteTargets(
  plan: Plan,
  policy: PlanPolicy,
  headBranch?: string,
): ProofResult {
  const started = performance.now();
  const violations: string[] = [];

  for (const [index, step] of plan.steps.entries()) {
    const argName = BRANCH_ARGS[step.kind];
    if (argName !== undefined) {
      const branch = step.args[argName];
      if (typeof branch !== 'string') continue; // the schema gate owns shape
      // Segment-wise, so `smtithy-evil/x` cannot pass as `smtithy/` and a `..`
      // segment cannot climb out of the namespace it matched.
      if (!branch.startsWith(policy.branch_prefix) || branch.split('/').includes('..')) {
        violations.push(
          `${index}: ${step.kind} (${step.id}) targets ${branch}: not under branch_prefix ${policy.branch_prefix}`,
        );
      } else if (headBranch !== undefined && branch === headBranch) {
        violations.push(
          `${index}: ${step.kind} (${step.id}) targets ${branch}: the reviewed PR's own head branch`,
        );
      }
    } else if (step.kind === 'label') {
      const name = step.args['name'];
      if (typeof name !== 'string') continue;
      if (!policy.label_allowlist.includes(name)) {
        violations.push(
          `${index}: label (${step.id}) applies ${name}: not on the policy label_allowlist`,
        );
      }
    }
  }

  const ms = performance.now() - started;
  if (violations.length === 0) return { holds: true, policy: 'write_targets', ms };
  return {
    holds: false,
    policy: 'write_targets',
    ms,
    counterexample: { policy: 'write_targets', path: violations },
  };
}

function matchesAny(path: string, patterns: readonly string[]): boolean {
  return patterns.some((pattern) => globToRegExp(pattern).test(path));
}

/** The first pattern `path` matches, or undefined — the twin of
 * plan_verify.matches_denylist, so the counterexample names the same pattern the
 * Python rejection message does. */
function firstMatch(path: string, patterns: readonly string[]): string | undefined {
  return patterns.find((pattern) => globToRegExp(pattern).test(path));
}

/** Minimal glob for the path denylist: ** spans separators, * does not.
 *
 * Hand-rolled rather than pulled in, so the semantics are visible next to the
 * check that depends on them — the §17 dotfile defect was a pattern enforced
 * exactly as written where the written pattern was wrong. Escapes everything
 * else, so a `.` in a pattern is a literal dot rather than "any character". */
export function globToRegExp(pattern: string): RegExp {
  let out = '';
  for (let i = 0; i < pattern.length; i += 1) {
    const char = pattern[i]!;
    if (char === '*') {
      if (pattern[i + 1] === '*') {
        // `**/` should also match zero directories, so `.github/**` catches
        // `.github/x` and `**/*.pem` catches a top-level `k.pem`.
        if (pattern[i + 2] === '/') {
          out += '(?:.*/)?';
          i += 2;
        } else {
          out += '.*';
          i += 1;
        }
      } else {
        out += '[^/]*';
      }
    } else {
      out += char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }
  }
  return new RegExp(`^${out}$`, 'u');
}
