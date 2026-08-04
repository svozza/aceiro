"""Credential-free delivery router: which job should deliver this plan?

The remediation lane has two deliveries needing two different credentials —
suggestion comments need `pull-requests: write`, a stacked follow-up pull request
also needs `contents: write` — and ADR-0009 records with approval that suggestions
made the write scope SHRINK for the common case. Keeping that shrink means the
scope cannot live in one job that handles both.

So the routing decision happens HERE, in a job holding `permissions: {}`, no model
credential and no write scope, and its output selects which delivery job runs.

WHAT THIS TRADES, STATED PLAINLY. The plan it reads has not been verified: the
gate runs in the delivery job, not before it. So unverified model output decides
which credential gets minted. That concession is bounded to exactly that — which
job STARTS — by two things:

- each delivery job is the SOLE full gate for its own path (re-fetch, re-derive the
  commanded finding, re-verify, re-prove, TOCTOU), so the process holding the token
  is still the process that verified what it writes; and
- each passes `--allow`, naming the mode it may deliver, so a plan misrepresenting
  its own mode reaches a job that refuses it after verification. The credential is
  minted and then not used: a self-inflicted denial of service with no benefit.

The alternatives were weighed and rejected. Widening the existing job loses
ADR-0009's shrink for every suggestion run unconditionally, on every repository,
rather than only for a plan that lies about itself. Routing from the PLAN job would
let the job holding a model credential choose which credential the next job mints.
Hoisting the gate into one job and making the delivery jobs thin would mean the
token holder is no longer the process that verified what it writes, which is the
posture execute_plan.py is built on.

Because the input is unverified, everything here fails closed and NOTHING is
emitted unless a mode was genuinely derived: an absent or malformed plan, a step
that is not an object, a missing `kind`, a shape decide_delivery refuses. A router
that guessed a mode on bad input would mint a credential for a plan no gate would
have passed.

The decision itself is decide_delivery, imported rather than reimplemented: this
must route to whatever the executor will DECIDE, or a plan is routed to a job whose
--allow then refuses it.

Arguments: --artifact-dir (plan.json). Environment: GITHUB_OUTPUT. No token.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from canonicalize import read_harness_text
from execute_plan import Refusal, decide_delivery
from github_api import fail

# Every arg decide_delivery reads off a step, per kind. Checked here because the
# schema gate has NOT run: decide_delivery indexes args["path"] for a suggestion,
# and a plan is free to omit it at this point in the lane.
REQUIRED_ARGS = {"suggest": ("path",)}


def routed_mode(plan_path: Path) -> str:
    """The delivery mode for the plan at `plan_path`, or exit non-zero.

    Every shape check here exists because this input is unverified. The schema
    gate that would make these impossible runs inside the delivery job, after the
    routing decision has already been made.
    """
    try:
        plan = json.loads(read_harness_text(plan_path))
    except FileNotFoundError:
        fail(f"no plan at {plan_path}; nothing to route")
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"plan at {plan_path} is unreadable ({exc}); nothing to route")

    if not isinstance(plan, dict):
        fail(f"plan is a {type(plan).__name__}, not an object; nothing to route")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        fail(f"plan.steps is {type(steps).__name__}, not a list; nothing to route")

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            fail(f"plan.steps[{index}] is a {type(step).__name__}, not an object; nothing to route")
        kind = step.get("kind")
        if not isinstance(kind, str):
            fail(f"plan.steps[{index}].kind is {kind!r}, not a string; nothing to route")
        args = step.get("args")
        for name in REQUIRED_ARGS.get(kind, ()):
            if not isinstance(args, dict) or name not in args:
                fail(
                    f"plan.steps[{index}] is a {kind} step with no args.{name}, which the "
                    "delivery decision reads; nothing to route"
                )

    try:
        return decide_delivery(steps).mode
    except Refusal as exc:
        # A refused plan routes NOWHERE. Picking a mode anyway would start a job
        # that mints a credential and then refuses the plan itself — pointless
        # work, and a credential in a log for a delivery that never happens.
        fail(f"plan carries no deliverable shape ({exc}); nothing to route")
    raise AssertionError("unreachable")  # fail() exits; keeps the type checker honest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True, type=Path)
    args = parser.parse_args()

    mode = routed_mode(args.artifact_dir / "plan.json")
    print(f"routed delivery: {mode}")
    # Written only once a mode was derived: every failure path above exits before
    # reaching this, so a downstream job never sees a mode this process guessed.
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"mode={mode}\n")


if __name__ == "__main__":
    main()
