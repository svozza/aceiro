/**
 * CLI over the plan prover, for the executor's re-verification.
 *
 * The executor is Python and holds the write token; ADR-0003 put the prover in
 * TypeScript. The executor re-proves rather than trusting the generator job's
 * claim (the posture post.py takes toward the review job), so it needs the
 * prover invocable as a subprocess: read plan + changed files + policy, run
 * every policy, print verdicts, exit non-zero on any violation.
 *
 * Exit codes: 0 every policy holds, 1 some policy is DISPROVED (counterexample
 * on stdout), 2 nothing was proved — inputs unreadable or malformed (including a
 * plan the schema gate rejects), or a query the solver could not decide. The
 * caller treats 1 and 2 both as "not verified", but only a 1 is evidence about
 * the plan.
 */

import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { checkPlanSchema, parsePlanJson } from './schema.js';
import { loadPlanPolicy, Rejection } from './policy.js';
import {
  proveBounds,
  proveCardinality,
  proveFrame,
  proveOrdering,
  proveTaint,
  proveWriteTargets,
  shutdown,
} from './prove.js';

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf8'));
}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: {
      plan: { type: 'string' },
      'changed-files': { type: 'string' },
      policy: { type: 'string' },
      'head-branch': { type: 'string' },
    },
  });
  if (!values.plan || !values['changed-files'] || !values.policy) {
    console.error('usage: prove-cli --plan plan.json --changed-files changed_files.json --policy policy.json');
    return 2;
  }

  let plan, policy, changedFiles: string[];
  try {
    policy = loadPlanPolicy(values.policy);
    // parsePlanJson, not readJson: an integer's spelling only survives in the
    // source text, and `1.0` must not pass a check the Python gate fails.
    plan = checkPlanSchema(parsePlanJson(readFileSync(values.plan, 'utf8')), policy);
    const files: unknown = readJson(values['changed-files']);
    if (!Array.isArray(files) || !files.every((f) => typeof f === 'string')) {
      throw new Rejection('changed-files: expected an array of strings');
    }
    changedFiles = files;
  } catch (error) {
    console.error(`prove-cli: ${error instanceof Error ? error.message : String(error)}`);
    return 2;
  }

  const results = [
    await proveOrdering(plan, policy),
    await proveFrame(plan, policy, changedFiles),
    await proveTaint(plan, policy),
    // --head-branch is optional; its absence only removes the one check the
    // namespace prefix cannot express.
    proveWriteTargets(plan, policy, values['head-branch']),
    proveCardinality(plan, policy),
    proveBounds(plan, policy),
  ];

  let disproved = false;
  let undecided = false;
  for (const result of results) {
    const verdict = result.holds ? 'holds' : result.undecided ? 'UNDECIDED' : 'VIOLATED';
    console.log(`${result.policy}: ${verdict} (${result.ms.toFixed(1)}ms)`);
    if (!result.holds) {
      if (result.undecided) undecided = true;
      else disproved = true;
      for (const line of result.counterexample?.path ?? []) console.log(`  ${line}`);
    }
  }
  // An undecided policy is exit 2, not 1: exit 1 means DISPROVED, which the
  // executor logs as an audit record about the plan. A query the solver could not
  // decide is an operational failure of this run, and reporting it as a disproof
  // would blame the model for the solver giving up. A disproof alongside it still
  // wins — that IS evidence about the plan.
  if (disproved) return 1;
  return undecided ? 2 : 0;
}

process.exitCode = await main();
await shutdown();
