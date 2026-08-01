/**
 * CLI over the plan prover, for the executor's re-verification.
 *
 * The executor is Python and holds the write token; ADR-0003 put the prover in
 * TypeScript. The executor re-proves rather than trusting the generator job's
 * claim (the posture post.py takes toward the review job), so it needs the
 * prover invocable as a subprocess: read plan + changed files + policy, run
 * every policy, print verdicts, exit non-zero on any violation.
 *
 * Exit codes: 0 every policy holds, 1 some policy fails (counterexample on
 * stdout), 2 inputs unreadable or malformed (including a plan the schema gate
 * rejects — the caller treats both as "not verified", but a 2 means nothing
 * was proved at all).
 */

import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { checkPlanSchema } from './schema.js';
import { loadPlanPolicy, Rejection } from './policy.js';
import { proveOrdering, proveFrame, proveTaint, proveWriteTargets, shutdown } from './prove.js';

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
    plan = checkPlanSchema(readJson(values.plan), policy);
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
    // --head-branch is optional: the executor knows the reviewed PR's head
    // branch from live context, a standalone invocation may not, and its absence
    // only removes the one check the namespace prefix cannot express.
    proveWriteTargets(plan, policy, values['head-branch']),
  ];

  let failed = false;
  for (const result of results) {
    console.log(`${result.policy}: ${result.holds ? 'holds' : 'VIOLATED'} (${result.ms.toFixed(1)}ms)`);
    if (!result.holds) {
      failed = true;
      for (const line of result.counterexample?.path ?? []) console.log(`  ${line}`);
    }
  }
  return failed ? 1 : 0;
}

process.exitCode = await main();
await shutdown();
