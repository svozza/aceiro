"""Deterministic, fail-closed verifier for remediation PLAN artifacts.

The Python twin of ts/plan/schema.ts, for the executor's side of the boundary:
the prover (TypeScript, ADR-0003) decides whether a plan satisfies the ordering
and frame policies, but the process that holds the write token is Python, and
it re-verifies rather than trusting a claim from another job — the same posture
post.py takes toward the review job. The two implementations read the same
policy.json, so a plan the prover admitted and this module rejects (or the
reverse) is a defect in one of them, and the differential is worth a test.

This module carries ADR-0004's three reserved closures, same as the TS gate:
steps are {id, kind, args} typed records; argument_forms admits only literals,
so an execution-time binding ({"$ref": ...}) is an object where a scalar is
expected and rejects today; and there is no version field — a model-supplied
schema version is a model-selected policy, so "version" is just an unexpected
key like anything else the model invents.

Fail-closed, whole-plan, first violation wins. No partial acceptance, no
repair.

Checks, in order (mirroring verify.py's phase order):
1. Strict structural schema (this module's check_plan_schema).
2. ADR-0005's containment: anchoring, bounding, frame (arrives next; see the
   ADR — anchoring grows a content source, the largest interface change).
3. Markdown allowlist on markdown-bearing args, secret scan (reused from
   verify.py; the policy marks which args are markdown).
"""

from __future__ import annotations

import re

from verify import Rejection, check_scalar

# Ids exist so steps can be referred to; a duplicate makes a reference
# ambiguous, and a counterexample naming a step becomes unactionable. Kept
# conservative: this is what appears in audit output. Same expression as
# ts/plan/schema.ts's ID_RE — the two must agree or a plan can pass one gate
# and fail the other on shape alone.
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")

PLAN_KEYS = frozenset({"steps"})
STEP_KEYS = frozenset({"id", "kind", "args"})


def check_plan_schema(candidate, policy_plan: dict) -> None:
    """Raise Rejection on the first structural violation; return None if the
    plan is well-shaped. Shape only: containment (ADR-0005) and markdown are
    separate phases, same as verify.py's schema/provenance split."""
    if not isinstance(candidate, dict):
        raise Rejection("plan: expected a JSON object")

    extra = set(candidate) - PLAN_KEYS
    if extra:
        raise Rejection(f"plan: unexpected keys {sorted(extra)}")
    if "steps" not in candidate:
        raise Rejection("plan: missing steps")

    steps = candidate["steps"]
    if not isinstance(steps, list):
        raise Rejection("plan.steps: expected an array")
    if not steps:
        # An empty plan is not a safe no-op to wave through: something asked
        # for a remediation and nothing would happen, which is a failure the
        # commander has to see rather than a success with no effect.
        raise Rejection("plan.steps: empty, so the plan does nothing")
    if len(steps) > policy_plan["max_steps"]:
        raise Rejection(f"plan.steps: {len(steps)} steps exceeds max_steps {policy_plan['max_steps']}")

    step_kinds = policy_plan["step_kinds"]
    seen_ids = set()

    for index, step in enumerate(steps):
        where = f"plan.steps[{index}]"
        if not isinstance(step, dict):
            raise Rejection(f"{where}: expected an object")

        extra = set(step) - STEP_KEYS
        if extra:
            raise Rejection(f"{where}: unexpected keys {sorted(extra)}")
        missing = STEP_KEYS - set(step)
        if missing:
            raise Rejection(f"{where}: missing keys {sorted(missing)}")

        step_id = step["id"]
        if not isinstance(step_id, str) or not ID_RE.fullmatch(step_id):
            raise Rejection(f"{where}.id: expected a short lowercase identifier, got {step_id!r}")
        if step_id in seen_ids:
            raise Rejection(f"{where}.id: duplicate id {step_id!r}")
        seen_ids.add(step_id)

        kind = step["kind"]
        if not isinstance(kind, str):
            raise Rejection(f"{where}.kind: expected a string")
        if kind not in step_kinds:
            # Allowlist, not denylist: an unknown kind is not a no-op the
            # executor can skip. It is a request the harness does not
            # understand, and the only safe reading of it is to reject the
            # whole plan.
            raise Rejection(
                f"{where}.kind: {kind!r} is not a declared step kind ({', '.join(sorted(step_kinds))})"
            )

        args = step["args"]
        if not isinstance(args, dict):
            raise Rejection(f"{where}.args: expected an object")
        declared = step_kinds[kind]["args"]
        extra = set(args) - set(declared)
        if extra:
            raise Rejection(f"{where}.args: unexpected keys {sorted(extra)}")
        missing = set(declared) - set(args)
        if missing:
            raise Rejection(f"{where}.args: missing keys {sorted(missing)}")

        for arg_name, arg_spec in declared.items():
            value = args[arg_name]
            arg_where = f"{where}.args.{arg_name}"
            # ADR-0004's second closure lands exactly here. An execution-time
            # binding would arrive as {"$ref": "step1.output"} — an object
            # where a scalar is expected — so it rejects today with no
            # per-argument wrapper. Named explicitly because check_scalar's
            # bare "expected string" would send someone hunting a typo
            # instead of reading ADR-0004.
            if isinstance(value, (dict, list)):
                raise Rejection(
                    f"{arg_where}: expected a literal, got {type(value).__name__} — "
                    "argument_forms admits only [\"literal\"], so bindings are not accepted"
                )
            check_scalar(value, arg_spec, arg_where)
