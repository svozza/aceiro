/**
 * The SHIPPED policy.json, read through the TypeScript loader.
 *
 * ADR-0003: "policy.json becomes shared data with two readers in two languages.
 * It was already the reviewable security object; now it is also the only thing
 * keeping the two verifiers describing the same policy."
 *
 * Every other test here builds a policy inline, which says nothing about the file
 * that actually ships. These read it off disk, so the plan section cannot drift
 * into a shape this prover rejects without a test noticing — the failure would
 * otherwise surface at the first real remediation, as a PolicyError in the job
 * holding the write credential.
 *
 * The Python side has the mirror of this in tests/test_policy_defaults.py.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadPlanPolicy, writeClassKinds } from './policy.js';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const POLICY_PATH = join(REPO_ROOT, 'src', 'smtithy', 'policy.json');

describe('the shipped policy.json', () => {
  it('loads and validates through the TypeScript reader', () => {
    // The whole point: the file the Python verifier reads is a file this prover
    // can also read. If loadPlanPolicy throws, the two readers have diverged.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.ok(policy.max_steps > 0);
  });

  it('declares the step-kind universe: §2.5\'s four plus ADR-0009\'s suggest', () => {
    // "more than one mutating action?" is what routes to a solver at all.
    // suggest (ADR-0009) joins as a NON-mutating kind: it becomes a review
    // comment the contributor applies, so it adds no write-class action and
    // does not change the §2.5 count.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.deepEqual(
      Object.keys(policy.step_kinds).sort(),
      ['label', 'open_pr', 'patch', 'push_branch', 'suggest'],
    );
  });

  it('marks exactly the effects-outside-the-harness kinds as write-class', () => {
    // patch is NOT write-class: it edits the quarantined tree, and nothing leaves
    // the harness until push_branch. Getting this wrong would either exempt a real
    // effect from the ordering policy or subject a local edit to it.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.deepEqual([...writeClassKinds(policy)].sort(), ['label', 'open_pr', 'push_branch']);
  });

  it('open_pr has no base argument — the base is never model-suppliable', () => {
    // ADR-0009 addendum: the follow-up PR is STACKED on the reviewed PR's own
    // head branch, and the executor sets that base from PR context. A `base`
    // arg appearing here would make the merge target model-suppliable — the
    // same banned move as a model-selected policy version — so its absence is
    // pinned exactly, not implied by the arg list happening to be short.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.deepEqual(Object.keys(policy.step_kinds['open_pr']!.args).sort(), ['body', 'branch', 'title']);
  });

  it('reserves control_flow as empty and argument_forms as literal-only', () => {
    // ADR-0004's first and second closures, as shipped. Both are reservations
    // that refuse their shape TODAY, so a non-empty control_flow here would mean
    // the policy admits branches the prover cannot reason about.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.deepEqual(policy.control_flow, []);
    assert.deepEqual(policy.argument_forms, ['literal']);
  });

  it('orders patch before push_branch before open_pr', () => {
    const policy = loadPlanPolicy(POLICY_PATH);
    const rules = policy.ordering.map((rule) => `${rule.before}<${rule.after}`);
    assert.ok(rules.includes('patch<push_branch'));
    assert.ok(rules.includes('push_branch<open_pr'));
  });

  it('does NOT order anything against run_tests', () => {
    // ADR-0001 dropped §20's dominance policy: the executor never runs tests, so
    // there is no run_tests kind and nothing may order against one. A rule
    // mentioning it would be a rule that can never fire, and would quietly
    // reintroduce the obligation whose removal is the credential split.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.ok(!('run_tests' in policy.step_kinds));
    for (const rule of policy.ordering) {
      assert.notEqual(rule.before, 'run_tests');
      assert.notEqual(rule.after, 'run_tests');
    }
  });

  it('bounds patches, per ADR-0005', () => {
    // Patch content is unverifiable, so containment is the property on offer:
    // bounding is half of it (anchoring, in the Python verifier, is the other).
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.ok(policy.max_patched_files >= 1);
    assert.ok(policy.max_changed_lines >= 1);
  });

  it('denies paths that must never be patched whatever the PR touched', () => {
    // A narrowing of the already-closed changed_files frame, which is why a
    // denylist is acceptable here despite allowlisting being the rule elsewhere.
    const policy = loadPlanPolicy(POLICY_PATH);
    assert.ok(policy.path_denylist.includes('.github/**'), 'workflow files must be undeniably out of reach');
    assert.ok(policy.path_denylist.length >= 1);
  });
});
