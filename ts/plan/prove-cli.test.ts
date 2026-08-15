/**
 * The prover CLI, exercised as the executor will invoke it: a subprocess whose
 * exit code is the verdict. The three-way split matters to the caller —
 * 0 proved, 1 disproved (counterexample on stdout), 2 nothing was proved at
 * all — because the executor treats 1 and 2 as rejection but must log them
 * differently: a counterexample is an audit record, an unreadable input is an
 * operational failure.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CLI = join(HERE, 'prove-cli.js');
const POLICY_PATH = join(HERE, '..', '..', 'src', 'smtithy', 'policy.json');

function run(plan: unknown, changedFiles: unknown, policyPath?: string): ReturnType<typeof spawnSync> {
  return runText(JSON.stringify(plan), changedFiles, policyPath);
}

/** The plan as TEXT, for the cases where its JSON spelling is the point. */
function runText(
  planText: string,
  changedFiles: unknown,
  policyPath: string = POLICY_PATH,
): ReturnType<typeof spawnSync> {
  const dir = mkdtempSync(join(tmpdir(), 'prove-cli-'));
  writeFileSync(join(dir, 'plan.json'), planText);
  writeFileSync(join(dir, 'changed.json'), JSON.stringify(changedFiles));
  return spawnSync(
    process.execPath,
    [CLI, '--plan', join(dir, 'plan.json'), '--changed-files', join(dir, 'changed.json'), '--policy', policyPath],
    { encoding: 'utf8' },
  );
}

const WELL_FORMED = {
  steps: [
    { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
    { id: 'push', kind: 'push_branch', args: { name: 'smtithy/fix-x' } },
    { id: 'pr', kind: 'open_pr', args: { branch: 'smtithy/fix-x', title: 't', body: 'b' } },
  ],
};

describe('prove-cli', () => {
  it('exits 0 when every policy holds', () => {
    const result = run(WELL_FORMED, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stdout), /ordering: holds/);
    assert.match(String(result.stdout), /frame: holds/);
  });

  it('reports taint as n/a under the shipped policy, and still exits 0', () => {
    // The shipped policy declares no read_pr_file, so no plan under it can taint
    // anything. That is not the same claim as `holds`, and must not print as one:
    // ADR-0004's opening argument is that every literal in a plan is ALREADY
    // PR-derived, so `holds` is the reassuring misreading of a vacuous query.
    const result = run(WELL_FORMED, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stdout), /taint: n\/a — no read_pr_file kind in this policy/);
    assert.doesNotMatch(String(result.stdout), /taint: holds/);
  });

  it('reports taint as holds when the policy DOES declare the source kind', () => {
    // The other side of the same coin: with a source kind declared the query is
    // real, so the verdict is a proof about this plan and says so. Guards against
    // `n/a` becoming the permanent answer if the vocabulary ever grows.
    const policy = JSON.parse(readFileSync(POLICY_PATH, 'utf8'));
    policy.plan.step_kinds['read_pr_file'] = {
      write_class: false,
      args: { path: { type: 'string', min_length: 1, max_length: 500 } },
    };
    const dir = mkdtempSync(join(tmpdir(), 'prove-cli-policy-'));
    const path = join(dir, 'policy.json');
    writeFileSync(path, JSON.stringify(policy));

    const result = run(WELL_FORMED, ['src/a.py'], path);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stdout), /taint: holds/);
    assert.doesNotMatch(String(result.stdout), /taint: n\/a/);
  });

  it('exits 1 with a counterexample when the frame is violated', () => {
    const result = run(WELL_FORMED, ['other.py']);
    assert.equal(result.status, 1);
    // The counterexample is the audit record, not a bare verdict (ADR-0003).
    assert.match(String(result.stdout), /frame: VIOLATED/);
    assert.match(String(result.stdout), /src\/a\.py: not a file this PR touched/);
  });

  it('exits 2 on a plan the schema gate rejects', () => {
    const result = run({ steps: [] }, ['src/a.py']);
    assert.equal(result.status, 2);
    assert.match(String(result.stderr), /empty/);
  });

  it('exits 2 on malformed changed-files', () => {
    const result = run(WELL_FORMED, { not: 'an array' });
    assert.equal(result.status, 2);
    assert.match(String(result.stderr), /array of strings/);
  });

  it('exits 2 when the plan file is unreadable', () => {
    const result = spawnSync(
      process.execPath,
      [CLI, '--plan', '/nonexistent/plan.json', '--changed-files', '/nonexistent/c.json', '--policy', POLICY_PATH],
      { encoding: 'utf8' },
    );
    assert.equal(result.status, 2);
  });

  it('exits 1 for a denylisted path that IS a changed file', () => {
    // The whole plan is otherwise legitimate — the path is in changed_files, so
    // the frame condition holds for it. Until the denylist became a solver
    // assertion this printed 'frame: holds' and exited 0, while
    // plan_verify.check_plan_containment rejected the identical plan.
    const plan = {
      steps: [
        { id: 's0', kind: 'patch', args: { path: '.github/workflows/ai-pr-review.yml', old: 'x', new: 'y' } },
      ],
    };
    const result = run(plan, ['.github/workflows/ai-pr-review.yml']);
    assert.equal(result.status, 1, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stdout), /frame: VIOLATED/);
    assert.match(String(result.stdout), /denylist/);
  });

  it('exits 2 on a line spelled 1.0, which plan_verify reads as a float', () => {
    // Reached only through the file, because JSON.stringify(1.0) is "1". The
    // divergence is in the spelling, so the CLI has to parse the bytes it was
    // given rather than a round-tripped copy of them.
    const result = runText(
      '{"steps":[{"id":"fix","kind":"suggest","args":' +
        '{"path":"src/a.py","line":1.0,"old":"a","new":"b","note":"n"}}]}',
      ['src/a.py'],
    );
    assert.equal(result.status, 2);
    assert.match(String(result.stderr), /not an integer/);
  });

  it('names the violating step kind in the counterexample, not a guessed one', () => {
    // The audit record has to name a step the plan contains. A suggest step
    // reported as "patch" sends a reader looking for one that is not there.
    const plan = {
      steps: [
        { id: 'fix', kind: 'suggest', args: { path: 'src/a.py', line: 1, old: 'a', new: 'b', note: 'n' } },
      ],
    };
    const result = run(plan, ['other.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /suggest fix src\/a\.py: not a file this PR touched/);
  });

  it('a suggestion-only plan proves clean (vacuous ordering, ADR-0009)', () => {
    const plan = {
      steps: [
        { id: 'fix', kind: 'suggest', args: { path: 'src/a.py', line: 1, old: 'a', new: 'b', note: 'n' } },
      ],
    };
    const result = run(plan, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
  });

  // Exit 1 is a claim about the PLAN. A fault in the invocation or the runtime
  // says nothing about the plan, so every one of these is a 2.
  // Ported from the red-team sweep of 2026-08-14 (20 crafted plans, all refused).
  // prove.test.ts already covers each policy's logic against a synthetic policy;
  // these run the SHIPPED one, where the values themselves are the assertion —
  // `branch_prefix: "smtithy/"` and an EMPTY `label_allowlist` are facts about
  // production that a synthetic fixture cannot pin.
  it('exits 1 when a push_branch precedes the patch it delivers', () => {
    const result = run({
      steps: [
        { id: 'push', kind: 'push_branch', args: { name: 'smtithy/fix-x' } },
        { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /ordering: VIOLATED/);
  });

  it('exits 1 for a branch outside the shipped branch_prefix', () => {
    const result = run({
      steps: [
        { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
        { id: 'push', kind: 'push_branch', args: { name: 'main' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /write_targets: VIOLATED/);
  });

  it('exits 1 for ANY label, because the shipped allowlist is empty', () => {
    // Not "the wrong label": the shipped policy permits none at all, so the
    // whole kind is unreachable in production. A synthetic policy with one
    // allowed label would pass this plan and prove nothing about the real one.
    const result = run({ steps: [{ id: 'lab', kind: 'label', args: { name: 'approved' } }] }, ['src/a.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /write_targets: VIOLATED|cardinality: VIOLATED/);
  });

  it('exits 1 when a write-class kind appears twice', () => {
    const result = run({
      steps: [
        { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
        { id: 'push_a', kind: 'push_branch', args: { name: 'smtithy/a' } },
        { id: 'push_b', kind: 'push_branch', args: { name: 'smtithy/b' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /cardinality: VIOLATED/);
  });

  it('exits 1 when a suggest is mixed with a write chain', () => {
    const result = run({
      steps: [
        { id: 's1', kind: 'suggest', args: { path: 'src/a.py', line: 1, old: 'a', new: 'b', note: 'n' } },
        { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
        { id: 'push', kind: 'push_branch', args: { name: 'smtithy/x' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /cardinality: VIOLATED/);
  });

  it('exits 1 for more patched files than the shipped max_patched_files', () => {
    const paths = ['src/a.py', 'src/b.py', 'src/c.py', 'src/d.py'];
    const result = run({
      steps: paths.map((path, index) => ({
        id: `fix${index}`, kind: 'patch', args: { path, old: 'a', new: 'b' },
      })),
    }, paths);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /bounds: VIOLATED/);
  });

  // KNOWN GAP, pinned rather than fixed: two patches to one file whose `old`
  // texts are identical, and a cascade whose second patch consumes the first's
  // output, both prove CLEAN. `ordering` constrains patch-before-push_branch,
  // never patch-versus-patch, and no policy models the file's state between
  // steps. Neither is a hole — plan_verify.count_occurrences refuses an ambiguous
  // write downstream — but the prover's silence here is a property worth failing
  // on if it changes, in either direction.
  it('does NOT catch two patches to one file with identical old text', () => {
    const result = run({
      steps: [
        { id: 'p1', kind: 'patch', args: { path: 'src/a.py', old: 'alpha\n', new: 'ALPHA\n' } },
        { id: 'p2', kind: 'patch', args: { path: 'src/a.py', old: 'alpha\n', new: 'OTHER\n' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
  });

  it('does NOT catch a cascade where patch 2 consumes patch 1 output', () => {
    const result = run({
      steps: [
        { id: 'p1', kind: 'patch', args: { path: 'src/a.py', old: 'alpha\n', new: 'BETA\n' } },
        { id: 'p2', kind: 'patch', args: { path: 'src/a.py', old: 'BETA\n', new: 'GAMMA\n' } },
      ],
    }, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
  });

  it('exits 2 on an unknown option', () => {
    const result = spawnSync(process.execPath, [CLI, '--bogus'], { encoding: 'utf8' });
    assert.equal(result.status, 2);
    assert.match(String(result.stderr), /prove-cli:/);
  });

  it('exits 2 on an unexpected positional argument', () => {
    const result = spawnSync(process.execPath, [CLI, 'plan.json'], { encoding: 'utf8' });
    assert.equal(result.status, 2);
    assert.match(String(result.stderr), /prove-cli:/);
  });

  it('exits 2 when the solver cannot start', () => {
    // --jitless leaves WebAssembly undefined, so z3 throws on init: the plan is
    // well formed and every input readable, and nothing about it was decided.
    const dir = mkdtempSync(join(tmpdir(), 'prove-cli-'));
    writeFileSync(join(dir, 'plan.json'), JSON.stringify(WELL_FORMED));
    writeFileSync(join(dir, 'changed.json'), JSON.stringify(['src/a.py']));
    const result = spawnSync(
      process.execPath,
      ['--jitless', CLI, '--plan', join(dir, 'plan.json'),
       '--changed-files', join(dir, 'changed.json'), '--policy', POLICY_PATH],
      { encoding: 'utf8' },
    );
    assert.equal(result.status, 2, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stderr), /prove-cli:/);
  });
});
