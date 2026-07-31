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
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CLI = join(HERE, 'prove-cli.js');
const POLICY_PATH = join(HERE, '..', '..', 'src', 'smtithy', 'policy.json');

function run(plan: unknown, changedFiles: unknown): ReturnType<typeof spawnSync> {
  const dir = mkdtempSync(join(tmpdir(), 'prove-cli-'));
  writeFileSync(join(dir, 'plan.json'), JSON.stringify(plan));
  writeFileSync(join(dir, 'changed.json'), JSON.stringify(changedFiles));
  return spawnSync(
    process.execPath,
    [CLI, '--plan', join(dir, 'plan.json'), '--changed-files', join(dir, 'changed.json'), '--policy', POLICY_PATH],
    { encoding: 'utf8' },
  );
}

const WELL_FORMED = {
  steps: [
    { id: 'fix', kind: 'patch', args: { path: 'src/a.py', old: 'a', new: 'b' } },
    { id: 'push', kind: 'push_branch', args: { name: 'fix/x' } },
    { id: 'pr', kind: 'open_pr', args: { branch: 'fix/x', title: 't', body: 'b' } },
  ],
};

describe('prove-cli', () => {
  it('exits 0 when every policy holds', () => {
    const result = run(WELL_FORMED, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
    assert.match(String(result.stdout), /ordering: holds/);
    assert.match(String(result.stdout), /frame: holds/);
    assert.match(String(result.stdout), /taint: holds/);
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

  it('a suggestion-only plan proves clean (vacuous ordering, ADR-0009)', () => {
    const plan = {
      steps: [
        { id: 'fix', kind: 'suggest', args: { path: 'src/a.py', line: 1, old: 'a', new: 'b', note: 'n' } },
      ],
    };
    const result = run(plan, ['src/a.py']);
    assert.equal(result.status, 0, String(result.stdout) + String(result.stderr));
  });
});
