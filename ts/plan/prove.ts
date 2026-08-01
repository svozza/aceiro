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
import type { Plan, Step } from './schema.js';
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
  /** The solver returned `unknown`: nothing was decided, so the policy is not
   * held — but neither was it disproved. Distinct from a counterexample because
   * the two mean different things to whoever reads the audit log: a
   * counterexample is evidence about the PLAN, an undecided query is an
   * operational failure of the RUN. */
  readonly undecided?: true;
}

/** Knobs the corpus needs and the policy cannot express.
 *
 * `resourceLimit` bounds solver work (z3's `rlimit`), which is the only way to
 * reach the `unknown` branch deterministically — every admissible plan decides in
 * milliseconds. proveTaint's `bindings` sets the precedent: the prover is tested
 * beyond what the schema can produce, because a branch no test reaches is a
 * branch nobody knows the behaviour of.
 *
 * Unset in production: a real timeout would trade a fail-closed answer for a
 * fail-closed non-answer, and the queries here are small by construction. */
export interface ProofOptions {
  readonly resourceLimit?: number;
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

/** Apply the corpus's solver knobs, if any. */
function configure(solver: { set(key: string, value: unknown): void }, options: ProofOptions | undefined): void {
  if (options?.resourceLimit !== undefined) solver.set('rlimit', options.resourceLimit);
}

/**
 * The undecided result, for a `check()` that returned neither sat nor unsat.
 *
 * A verdict other than `unsat` must never be read as "holds", and `unknown` must
 * not be read as "violated" either: the model does not exist, so extracting a
 * counterexample from it throws, and the only honest report is that nothing was
 * decided. The solver's own reason is carried through, because "why" is the part
 * an operator can act on.
 */
function undecidedResult(policy: string, reason: string, ms: number): ProofResult {
  return {
    holds: false,
    undecided: true,
    policy,
    ms,
    counterexample: {
      policy,
      path: [
        `${policy}: UNDECIDED — the solver returned unknown, so this policy was neither ` +
          'proved nor disproved',
        `solver reason: ${reason === '' ? '(none given)' : reason}`,
      ],
    },
  };
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
 * THE THEOREM THIS PROVES, exactly: for the plan's own concrete order, no
 * declared rule ({before, after}) is violated by any pair of its steps. NOT "on
 * no ordering of these steps" — positions are pinned to the plan's indices with
 * `eq(index)`, because a plan IS an order and the question is whether THIS order
 * violates a rule, not whether some permutation could. The pinning is therefore
 * load-bearing, and dropping it to "restore the quantified reading" would change
 * what is being proved: a plan whose steps could be rearranged into a violation
 * would start failing, and the legal chain patch -> push_branch -> open_pr is
 * exactly such a plan.
 *
 * It stays a solver query rather than an index comparison because the encoding
 * has to be the shape a future policy grows into, and because a rule's violation
 * is stated once here instead of once per reader.
 *
 * NOTE what this is NOT: ADR-0001 dropped §20's dominance policy, so there is no
 * "run_tests must dominate every write" obligation here and no `passed` claim to
 * bind. Ordering is only about the mutating actions' sequence among themselves.
 */
export async function proveOrdering(
  plan: Plan,
  policy: PlanPolicy,
  options?: ProofOptions,
): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Int, Or } = Z3;

  const solver = new Solver();
  configure(solver, options);
  const position = plan.steps.map((step) => Int.const(`pos_${step.id}`));

  // A plan IS an order, so positions are pinned to the plan's own indices. The
  // solver is not searching for an order; it is being asked whether THIS order
  // violates a rule. Distinctness follows from the pinning and is not asserted
  // separately: a Distinct over variables already fixed to 0..n-1 is a tautology,
  // so it would read as a constraint no test could ever fail without.
  for (const [index, variable] of position.entries()) {
    solver.add(variable.eq(index));
  }

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
        violations.push(posJ.lt(posI));
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
  // sat, unsat and unknown are the three cases, and only sat has a model to
  // extract. Guarding here rather than at the model call, so the branch that
  // decides the verdict is the branch that reports it.
  if (verdict !== 'sat') return undecidedResult('ordering', solver.reasonUnknown(), ms);

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
 * BOTH obligations are asserted, not just the frame: a counterexample may only
 * report a check the query actually made.
 */
export async function proveFrame(
  plan: Plan,
  policy: PlanPolicy,
  changedFiles: readonly string[],
  options?: ProofOptions,
): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Int, Bool, Function: Fn, ForAll, Implies, Not, And, Or } = Z3;

  const solver = new Solver();
  configure(solver, options);
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
  // The step, not just its path: a counterexample naming "patch src/evil.py" for
  // a suggest step misidentifies the violating kind, and the audit record is what
  // someone reads to find the step to fix.
  const modifying = plan.steps
    .filter((step) => step.kind === 'patch' || step.kind === 'suggest')
    .map((step) => ({ step, path: step.args['path'] }))
    .filter((entry): entry is { step: Step; path: string } => typeof entry.path === 'string');
  const patchedPaths = modifying.map(({ path }) => path);

  for (const path of changedFiles) solver.add(touchedByPr.call(idOf(path)));
  for (const path of patchedPaths) solver.add(modifiedByPlan.call(idOf(path)));

  // Facts over the SAME intern table, so both obligations are decided against one
  // identity per file. Every interned path is pinned either way, or the solver
  // could pick a value and invent a violation no plan expresses.
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

  // Negated: some modified file is untouched by the PR, OR is denylisted.
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
  if (verdict !== 'sat') return undecidedResult('frame', solver.reasonUnknown(), ms);

  // The witness is an int, so names come from the intern table. Escaping paths
  // FIRST, matching plan_verify.check_plan_containment's frame-then-denylist
  // order, so both gates give one reason for one plan. Each line carries the
  // step's own kind and id, so the record names the step that violated rather
  // than a kind it guessed.
  const changed = new Set(changedFiles);
  const escaping = modifying.filter(({ path }) => !changed.has(path));
  const denied = modifying
    .map(({ step, path }) => ({ step, path, pattern: firstMatch(path, policy.path_denylist) }))
    .filter((entry): entry is { step: Step; path: string; pattern: string } => entry.pattern !== undefined);
  return {
    holds: false,
    policy: 'frame',
    ms,
    counterexample: {
      policy: 'frame',
      path: [
        ...escaping.map(({ step, path }) => `${step.kind} ${step.id} ${path}: not a file this PR touched`),
        ...denied.map(
          ({ step, path, pattern }) =>
            `${step.kind} ${step.id} ${path}: on the policy path denylist (${pattern})`,
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
  options?: ProofOptions,
): Promise<ProofResult> {
  const { Context } = await z3();
  const Z3 = Context(CONTEXT_NAME);
  const { Solver, Bool, Or, Not } = Z3;

  const solver = new Solver();
  configure(solver, options);
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
  if (verdict !== 'sat') return undecidedResult('taint', solver.reasonUnknown(), ms);

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

/**
 * Cardinality: at most one of each write-class kind, no chain on a suggest plan.
 *
 * Ordering constrains the chain's ORDER, this its COUNT — one commanded finding
 * produces one effect. Twin of plan_verify.check_plan_cardinality. A ground check
 * for the same reason proveWriteTargets is one.
 */
export function proveCardinality(plan: Plan, policy: PlanPolicy): ProofResult {
  const started = performance.now();
  const kinds = plan.steps.map((step) => step.kind);
  const writeKinds = writeClassKinds(policy);
  const violations: string[] = [];

  for (const kind of [...writeKinds].sort()) {
    const count = kinds.filter((k) => k === kind).length;
    if (count > 1) violations.push(`${count} ${kind} steps: a write-class kind may appear at most once`);
  }
  if (kinds.includes('suggest')) {
    const chain = [...writeKinds].sort().filter((kind) => kind !== 'label' && kinds.includes(kind));
    if (chain.length > 0) {
      violations.push(`a suggest plan carries ${chain.join(', ')}: a suggestion is applied in place`);
    }
  }
  if (kinds.includes('open_pr') && !kinds.includes('push_branch')) {
    violations.push('open_pr with no push_branch: there is no branch to open it from');
  }

  const ms = performance.now() - started;
  if (violations.length === 0) return { holds: true, policy: 'cardinality', ms };
  return { holds: false, policy: 'cardinality', ms, counterexample: { policy: 'cardinality', path: violations } };
}

/** The kinds whose args name file content the executor would write. Twin of
 * plan_verify.ANCHORED_KINDS; suggest joins patch per ADR-0009. */
const ANCHORED_KINDS = ['patch', 'suggest'];

/** Lines in a patch fragment: a trailing newline ends the last line rather than
 * starting an empty new one, and the empty string is zero lines. Twin of
 * plan_verify._line_count — the two gates must count a fragment the same way. */
function lineCount(text: string): number {
  if (text.length === 0) return 0;
  return (text.match(/\n/gu)?.length ?? 0) + (text.endsWith('\n') ? 0 : 1);
}

/** UTF-8 byte length. NOT `text.length`, which counts UTF-16 units and would put
 * this gate's budget at a different number from plan_verify's for exactly the
 * astral input the two length metrics already disagree on. The budget bounds what
 * reaches the file, and a file holds bytes. */
function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * Bounding (ADR-0005): distinct patched files, and changed lines and bytes per
 * step plus bytes over the plan.
 *
 * A ground check, for proveWriteTargets' reason: counting a closed set is not a
 * ∀-shaped claim, and ∀-encoding it would add an encoding layer to trust for no
 * reachability reasoning gained. It is here because it was NOWHERE in TypeScript —
 * a plan over max_patched_files or max_changed_lines was admitted by this gate and
 * rejected by plan_verify.py, and a plan one gate admits and the other rejects is
 * a defect in one of them.
 *
 * Two dimensions, because a line count bounds nothing about line LENGTH: a
 * minified or generated line is one line carrying arbitrary content. The plan
 * total is separate from the per-step cap because several steps may share one
 * file, so max_patched_files does not bound the sum.
 */
export function proveBounds(plan: Plan, policy: PlanPolicy): ProofResult {
  const started = performance.now();
  const violations: string[] = [];
  const anchored = [...plan.steps.entries()].filter(([, step]) => ANCHORED_KINDS.includes(step.kind));

  const paths = new Set(
    anchored
      .map(([, step]) => step.args['path'])
      .filter((path): path is string => typeof path === 'string'),
  );
  if (paths.size > policy.max_patched_files) {
    violations.push(`${paths.size} patched files exceeds max_patched_files ${policy.max_patched_files}`);
  }

  let planBytes = 0;
  for (const [index, step] of anchored) {
    const old = step.args['old'];
    const replacement = step.args['new'];
    if (typeof old !== 'string' || typeof replacement !== 'string') continue; // the schema gate owns shape

    // diff --stat's reading on both counts: the old side plus the new side, so a
    // rewrite cannot spend the whole budget twice.
    const changedLines = lineCount(old) + lineCount(replacement);
    if (changedLines > policy.max_changed_lines) {
      violations.push(
        `${index}: ${step.kind} (${step.id}) changes ${changedLines} lines, ` +
          `exceeding max_changed_lines ${policy.max_changed_lines}`,
      );
    }
    const changedBytes = utf8Length(old) + utf8Length(replacement);
    if (changedBytes > policy.max_changed_bytes) {
      violations.push(
        `${index}: ${step.kind} (${step.id}) changes ${changedBytes} bytes, ` +
          `exceeding max_changed_bytes ${policy.max_changed_bytes}`,
      );
    }
    planBytes += changedBytes;
  }
  if (planBytes > policy.max_plan_changed_bytes) {
    violations.push(
      `${planBytes} changed bytes across all steps exceeds ` +
        `max_plan_changed_bytes ${policy.max_plan_changed_bytes}`,
    );
  }

  const ms = performance.now() - started;
  if (violations.length === 0) return { holds: true, policy: 'bounds', ms };
  return { holds: false, policy: 'bounds', ms, counterexample: { policy: 'bounds', path: violations } };
}

/** Which argument names a write-class kind's target. Twin of
 * plan_verify.BRANCH_ARGS. */
const BRANCH_ARGS: Readonly<Record<string, string>> = { push_branch: 'name', open_pr: 'branch' };

/**
 * Write-class targets: every branch a plan pushes is inside the harness-owned
 * namespace, and every label it applies is on the allowlist. Where the frame
 * bounds which FILES change, this bounds where the write-class steps act.
 *
 * Not a solver query — a membership test over closed sets is a ground check
 * (CONTEXT.md), and ∀-encoding it would add an encoding layer to trust for no
 * reachability reasoning gained. Returns the same ProofResult shape so prove-cli
 * treats it like the rest.
 *
 * `headBranch` optional: undefined means unknown, which refuses nothing extra
 * since the namespace still bounds the target.
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
      // Segment-wise: `smtithy-evil/x` must not pass as `smtithy/`, and a `..`
      // segment must not climb out of the namespace it matched.
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

/** The first pattern `path` matches, or undefined. Twin of
 * plan_verify.matches_denylist, so both gates name the same pattern. */
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
