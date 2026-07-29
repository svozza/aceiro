// Spike: can z3-solver (WASM) express smtithy's plan-level taint policy?
//
// Policy (design doc §20): data derived from read_pr_file must never flow into
// push_branch.name / open_pr.branch / any label argument. Encode the plan plus
// the NEGATION of the policy; unsat = policy holds on all paths, sat = a
// concrete leaking path for the audit log.
//
// Plan modelled as a bounded sequence of steps with branches: each step has a
// tool, a "tainted output" bit, and an argument-source binding to an earlier
// step. Branch reachability is a Bool per step.

const t0 = Date.now();
const { init } = await import('z3-solver');
const { Context, em } = await init();
const tLoad = Date.now() - t0;

const Z3 = Context('main');
const { Solver, Bool, Int, ForAll, Exists, Implies, And, Or, Not, Distinct } = Z3;

const N = 6; // plan steps

// Tools, as ints
const READ_PR_FILE = 0, RUN_TESTS = 1, PATCH = 2, PUSH_BRANCH = 3, OPEN_PR = 4, LABEL = 5;
const WRITE_CLASS = [PUSH_BRANCH, OPEN_PR, LABEL];

function mkPlan(name) {
  const steps = [];
  for (let i = 0; i < N; i++) {
    steps.push({
      tool: Int.const(`${name}_tool_${i}`),
      reachable: Bool.const(`${name}_reach_${i}`),
      tainted: Bool.const(`${name}_tainted_${i}`),
      // argSrc[i] = j means step i's argument is bound to step j's output (-1 = literal)
      argSrc: Int.const(`${name}_argsrc_${i}`),
    });
  }
  return steps;
}

const steps = mkPlan('p');
const s = new Solver();

// --- well-formedness of the plan encoding ---
for (let i = 0; i < N; i++) {
  s.add(steps[i].tool.ge(0), steps[i].tool.le(5));
  s.add(steps[i].argSrc.ge(-1), steps[i].argSrc.lt(i)); // bindings point backwards only
}

// --- taint propagation: transitive dataflow ---
// A step is tainted if it reads PR content, or if its argument is bound to a
// tainted earlier step. This is the transitive closure, unrolled over the
// bounded plan.
for (let i = 0; i < N; i++) {
  const readsPr = steps[i].tool.eq(READ_PR_FILE);
  const inheritedClauses = [];
  for (let j = 0; j < i; j++) {
    inheritedClauses.push(And(steps[i].argSrc.eq(j), steps[j].tainted));
  }
  const inherited = inheritedClauses.length ? Or(...inheritedClauses) : Bool.val(false);
  s.add(steps[i].tainted.eq(Or(readsPr, inherited)));
}

// --- the policy, NEGATED: does any reachable write-class step take tainted data? ---
const violations = [];
for (let i = 0; i < N; i++) {
  const isWrite = Or(...WRITE_CLASS.map((w) => steps[i].tool.eq(w)));
  violations.push(And(steps[i].reachable, isWrite, steps[i].tainted));
}
s.add(Or(...violations));

// Case 1: unconstrained plan space -> expect sat (a leak is possible in general)
const t1 = Date.now();
const r1 = await s.check();
const tCheck1 = Date.now() - t1;

let witness = null;
if (r1 === 'sat') {
  const m = s.model();
  witness = [];
  for (let i = 0; i < N; i++) {
    witness.push({
      i,
      tool: Number(m.eval(steps[i].tool).toString()),
      reachable: m.eval(steps[i].reachable).toString(),
      tainted: m.eval(steps[i].tainted).toString(),
      argSrc: Number(m.eval(steps[i].argSrc).toString()),
    });
  }
}

// Case 2: add the enforcement the verifier would require -- write-class steps
// may only bind to literals or untainted sources -> expect unsat (policy holds
// on ALL paths, including ones nobody enumerated)
const s2 = new Solver();
for (let i = 0; i < N; i++) {
  s2.add(steps[i].tool.ge(0), steps[i].tool.le(5));
  s2.add(steps[i].argSrc.ge(-1), steps[i].argSrc.lt(i));
}
for (let i = 0; i < N; i++) {
  const readsPr = steps[i].tool.eq(READ_PR_FILE);
  const cl = [];
  for (let j = 0; j < i; j++) cl.push(And(steps[i].argSrc.eq(j), steps[j].tainted));
  s2.add(steps[i].tainted.eq(Or(readsPr, cl.length ? Or(...cl) : Bool.val(false))));
}
// enforcement
for (let i = 0; i < N; i++) {
  const isWrite = Or(...WRITE_CLASS.map((w) => steps[i].tool.eq(w)));
  s2.add(Implies(And(steps[i].reachable, isWrite), Not(steps[i].tainted)));
}
// negated policy again
const v2 = [];
for (let i = 0; i < N; i++) {
  const isWrite = Or(...WRITE_CLASS.map((w) => steps[i].tool.eq(w)));
  v2.push(And(steps[i].reachable, isWrite, steps[i].tainted));
}
s2.add(Or(...v2));

const t2 = Date.now();
const r2 = await s2.check();
const tCheck2 = Date.now() - t2;

// Case 3: a quantified frame condition, to test ForAll ergonomics.
// "the plan modifies only files already touched by the PR"
const s3 = new Solver();
const FileSort = Z3.Int.sort();
const touchedByPr = Z3.Function.declare('touched_by_pr', FileSort, Z3.Bool.sort());
const modifiedByPlan = Z3.Function.declare('modified_by_plan', FileSort, Z3.Bool.sort());
const f = Int.const('f');
// enforcement: every modified file is a touched file
s3.add(ForAll([f], Implies(modifiedByPlan.call(f), touchedByPr.call(f))));
// negated policy: some modified file is not touched
const g = Int.const('g');
s3.add(Exists([g], And(modifiedByPlan.call(g), Not(touchedByPr.call(g)))));
const t3 = Date.now();
const r3 = await s3.check();
const tCheck3 = Date.now() - t3;

console.log(JSON.stringify({
  node: process.version,
  wasmLoadMs: tLoad,
  taint_unconstrained: { result: r1, ms: tCheck1 },
  taint_with_enforcement: { result: r2, ms: tCheck2, expected: 'unsat' },
  frame_condition_quantified: { result: r3, ms: tCheck3, expected: 'unsat' },
  counterexample: witness,
}, null, 2));

em.PThread.terminateAllThreads();
