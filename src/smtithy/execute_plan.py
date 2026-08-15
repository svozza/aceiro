"""Trusted plan executor: re-verify, decide the delivery, deliver.

Runs in the execute job (the one holding `pull-requests: write`). Trusts
nothing from the plan job: re-fetches the SHA-anchored diff and changed-file
list rather than reading the bundle's copies, re-runs verify_plan() in this
process against the quarantine-fetched PR head (the same tree that anchored the
plan), and only then decides how the fix is delivered — and delivers it.

The plan is the one thing that must come from the bundle, being the plan job's
output. Everything the plan is CHECKED against is first-party.

The delivery decision is the EXECUTOR's, computed from checkable structure of
the verified plan, never the model's (ADR-0009):

- the fix is ONE `suggest` step -> suggestion comments. One file and one
  REGION: two suggestions on one path would post as independently applicable
  comments, which is the atomicity rule's other half and is refused here as
  well as at cardinality (ADR-0009's addendum C);
- every fix step is `patch` and the write chain push_branch -> open_pr is
  present -> a stacked follow-up PR whose base is the reviewed PR's own head
  BRANCH, taken from the live PR context, never from the plan (`open_pr`
  deliberately has no base argument — ADR-0009 addendum);
- a fork PR cannot take a stacked PR (its head branch does not exist in the
  base repository), so that combination is refused;
- everything else — mixed kinds, suggestions spanning files, an incomplete
  write chain, a plan with no fix step at all — is refused. Some of these are
  unreachable for a verified plan; they are refused anyway, because "the
  verifier must have caught it" is not a delivery mechanism.

Any rejection or refusal: nothing is posted, exit non-zero.

Both deliveries are built and they are ALTERNATIVES, never a fallback chain:
suggest.py reconciles suggestion comments (ported from the extraction source),
stack.py opens the stacked follow-up pull request. A plan deciding one is never
delivered as the other — the atomicity a pull request's merge has is the whole
reason that mode was chosen, and per-file suggestions of a coordinated fix can be
half-applied.

The stacked path additionally carries ADR-0007's deduplication key on
(pr, head_sha, findings), which the suggestion path deliberately does not: a
command is not idempotent the way a push is, and two maintainers typing `/fix 3`
must not produce two branches and two pull requests. The key is computed over the
SET of commanded findings (ADR-0013), so `/fix 3,1` and `/fix 1,3` are one command
while `/fix 1` and `/fix 1,3` are two — the second being a widening, which is a
different fix and a different artefact.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA, BASE_SHA
(the diff anchor), BASE_REF (the reviewed base BRANCH, which is what a retarget
changes — ADR-0012), HEAD_REF (the reviewed head BRANCH, the one push target both
gates refuse — ADR-0009 addendum), RUN_URL (the delivered comment's footer).
Arguments: --artifact-dir (plan.json, plus review.json and commanded_index.json —
the accepted artifact and the ordinal, from which the commanded finding is
DERIVED rather than taken on trust; the bundle's diff.patch and
changed_files.json travel as evidence only, since the gate's provenance inputs
are re-fetched here), --pr-root (the quarantine-fetched reviewed head, the anchor
tree), and --policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import NamedTuple, cast

from artifact import redact_line
from diff_map import anchor_signatures
from github_api import api_json, fail, is_fork, pr_moved
from canonicalize import decode_contributor_bytes, read_harness_text
from decline import emit as emit_decline
from decline import ordinals_of
from plan_loop import read_commanded_findings, read_commanded_indices
from plan_verify import apply_patch_steps, tree_content_source, verify_plan
from post import read_model_stamp, resolve_bot_login
from prepare_context import fetch_anchored_pair
from stack import AlreadyDelivered, deliver_stacked_pr, fix_key
from stack import Refusal as StackRefusal
from suggest import finding_identity, reconcile_suggestions
from verify import Rejection

# The step kinds that express the fix itself; everything else is delivery
# plumbing (push_branch, open_pr) or side effect (label). Mirrors
# plan_verify.ANCHORED_KINDS but named for what it means here: the delivery
# decision is made from these steps' shape.
FIX_KINDS = ("suggest", "patch")


def required_env(name: str) -> str:
    """An environment variable that must be present AND non-empty.

    Empty is refused, not just absent, and the distinction is what a production
    failure turned up: the upstream step emitted two of its four outputs, so
    BASE_REF and HEAD_REF arrived as empty strings. `os.environ[name]` succeeds for
    those, so the fail-closed KeyError this module relied on never fired.

    One of the two failures was loud — the retarget check compared a live base ref
    against "" and refused every command. The other was silent and is the reason
    this helper exists: HEAD_REF is what makes the reviewed-head-branch push
    refusal reachable, and with an empty value `head_branch is not None` still holds
    while `branch == ""` matches no real branch, so the check ran and enforced
    nothing. An empty gate input is worse than a missing one precisely because it
    looks like a value.

    Checked here rather than trusted from the workflow because this process trusts
    no other job — the posture the whole module is built on.
    """
    value = os.environ[name]
    if not value:
        fail(
            f"{name} is set but empty; it is a gate input and an empty one disables the "
            "check that reads it, so nothing was executed"
        )
    return value


class Refusal(Exception):
    """The plan verified, but no delivery path can carry it.

    Distinct from verify.Rejection on purpose: a Rejection means the plan is
    outside the safe grammar, a Refusal means it is inside the grammar but
    its structure matches no delivery the executor is willing to perform.
    Both fail closed; the audit trail should not conflate them.
    """


class Delivery(NamedTuple):
    mode: str  # "suggestions" | "stacked_pr"
    path: str | None  # the single suggestion target, None for a stacked PR


def decide_delivery(steps: list[dict]) -> Delivery:
    """The executor's delivery decision, from the verified plan's structure.

    Pure and total over verified plans: every input either returns a Delivery
    or raises Refusal. Nothing here trusts the model beyond what verify_plan
    already proved — the decision reads only step kinds and paths, both of
    which the schema gate pinned to the policy's vocabulary.
    """
    kinds = [step["kind"] for step in steps]
    fix_kinds = {kind for kind in kinds if kind in FIX_KINDS}
    pushes = kinds.count("push_branch")
    opens = kinds.count("open_pr")

    if not fix_kinds:
        # Both gates' cardinality now reject this shape, so it should not arrive
        # here. Kept because the executor re-decides rather than trusting that a
        # gate ran: a remediation that fixes nothing must fail where the commander
        # sees it, not no-op.
        raise Refusal("no fix step (suggest or patch): the plan delivers nothing")

    if len(fix_kinds) > 1:
        # Should be unreachable for a plan a model followed the prompt to
        # write, but "should be unreachable" is not a delivery mechanism.
        raise Refusal("mixed suggest and patch steps: no single delivery carries both")

    if fix_kinds == {"suggest"}:
        if pushes or opens:
            raise Refusal("a write chain (push_branch/open_pr) with no patch steps has nothing to push")
        suggestions = [step for step in steps if step["kind"] == "suggest"]
        paths = {step["args"]["path"] for step in suggestions}
        if len(paths) > 1:
            # ADR-0009's atomicity rule: per-file suggestions of a multi-file
            # fix can be HALF-applied, leaving the branch broken in a way
            # nobody intended. A coordinated fix must never ship as
            # independently applicable pieces.
            raise Refusal(
                f"suggestions span {len(paths)} files; a multi-file fix must be a stacked PR "
                "(patch steps), never independently applicable pieces"
            )
        # The same rule counted over REGIONS rather than paths, which is the half
        # ADR-0009 claimed was "checkable from the verified plan's step list" while
        # nothing checked it: this decision routed on distinct paths and never
        # counted steps, so two `suggest` steps on ONE path returned suggestions and
        # posted both as independently applicable comments — the precise harm the
        # atomicity rule exists to prevent.
        #
        # check_plan_cardinality refuses the same shape, and this is deliberately not
        # a second READER of that property but the same re-decision every other arm
        # here already is: "should be unreachable" is not a delivery mechanism, and
        # the module's posture is that the process holding the write token decides
        # for itself rather than trusting that a gate ran. The two refusals also
        # serve different purposes — a Rejection at the gate is feedback a session can
        # retry against, while a Refusal here is the last thing between a verified
        # plan and a write.
        if len(suggestions) > 1:
            raise Refusal(
                f"{len(suggestions)} suggest steps on {next(iter(paths))!r}; a suggestion is "
                "applied on its own, so two on one file can be half-applied — coordinated "
                "regions go to the stacked PR as patch steps"
            )
        return Delivery("suggestions", paths.pop())

    # All patch. The chain must be complete and unambiguous: a patch with no
    # push_branch/open_pr verifies (the ordering policy is vacuous without
    # them) but has no delivery; two chains would be two PRs for one finding.
    if pushes != 1 or opens != 1:
        raise Refusal(
            f"patch steps need exactly one push_branch and one open_pr "
            f"(got {pushes} and {opens})"
        )
    return Delivery("stacked_pr", None)


def pr_snapshot(repo: str, pr_number: int, reviewed_head: str, reviewed_base_ref: str) -> dict:
    """Fetch the live PR, enforce the TOCTOU precondition, and return the
    delivery context in one call.

    One fetch serves both purposes on purpose: the head branch name and the
    fork-ness used for the delivery MUST describe the same PR state the
    unmoved check accepted, or a retarget between two fetches could pass the
    check with one state and deliver against another.

    Two preconditions, both before the first effect.

    github_api.pr_moved is shared with post.py: the two executors must not
    disagree about what "moved" means.

    OPEN is checked here rather than in pr_moved, because it is not a fact about
    drift and the reviewer does not need it — a review of a merged pull request is
    a stale hint, while a remediation of one is a write whose premise is gone
    (ADR-0009 addendum B: this delivery's premise dies with the head). A merged
    pull request keeps the reviewed head SHA, so pr_moved returns None and every
    other gate passes: head.repo stays non-null so is_fork does not refuse, and
    the commit and tree stay readable. On the stacked path the base is the head
    branch, which merging may have deleted, and create_ref runs before POST /pulls
    — so the run would leave a branch behind and no follow-up pull request.
    Anything that is not exactly "open" is refused, so an unexpected state reads
    as not-open rather than as permission.
    """
    pr = cast("dict", api_json(f"/repos/{repo}/pulls/{pr_number}"))
    if (state := pr.get("state")) != "open":
        fail(
            f"pull request {pr_number} is {state or 'in an unknown state'}, not open, so there is "
            "no branch this fix belongs on; nothing executed"
        )
    if moved := pr_moved(pr, reviewed_head, reviewed_base_ref):
        fail(f"{moved}; nothing executed")
    return pr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--pr-root", required=True, type=Path,
                        help="Quarantine-fetched tree of the reviewed head SHA — the anchor source.")
    parser.add_argument("--policy", required=True, type=Path)
    # Which delivery THIS JOB may perform, declared by the job rather than derived
    # from the plan. The lane routes on plan structure to choose between a job
    # holding pull-requests: write and one also holding contents: write, and the
    # routing job holds no credential and reads a plan nothing has verified yet —
    # so unverified structure decides which job STARTS. This flag is what bounds
    # the concession to exactly that: a plan misrepresenting its mode reaches a job
    # whose --allow refuses it after verification, so the credential is minted and
    # then not used. A self-inflicted denial of service with no benefit, rather
    # than a delivery performed under a scope it was not routed for.
    #
    # `choices` so a typo in the workflow is an argparse error, not a silently
    # empty or silently total allowance — one of those two readings is dangerous
    # and neither is what was meant.
    #
    # Empty means UNCONFINED, deliberately: the flag confines a job, and an
    # operator running this by hand names no confinement. The workflow always
    # passes it, which test_workflow_shape asserts where the workflow is the
    # subject.
    parser.add_argument("--allow", action="append", default=[],
                        choices=["suggestions", "stacked_pr"],
                        help="Delivery mode this job may perform; repeatable. "
                             "Omitted, every mode is allowed.")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    reviewed_sha = required_env("HEAD_SHA")
    # Two roles, deliberately separate (ADR-0012): the SHA anchors the diff, the
    # ref detects a retarget. BASE_SHA is unusable for the second because the
    # live base.sha tracks the branch tip.
    reviewed_base_sha = required_env("BASE_SHA")
    reviewed_base_ref = required_env("BASE_REF")
    # The reviewed head BRANCH, which is the push target both gates must refuse
    # (ADR-0009 addendum: the harness never pushes to the contributor's branch).
    # From the event, not from pr_snapshot: that fetch is deliberately single and
    # happens after both gates, and hoisting it to get this value would widen the
    # TOCTOU window it exists to close.
    reviewed_head_ref = required_env("HEAD_REF")

    plan_path = args.artifact_dir / "plan.json"
    plan = json.loads(read_harness_text(plan_path))
    policy = json.loads(read_harness_text(args.policy))

    # Both halves of the command are INPUTS to the scope gate rather than
    # evidence, so both are read from the bundle and fail-closed like plan.json.
    # The ordinal cannot be re-derived here — which finding a maintainer commanded
    # is a fact about the COMMAND — and ADR-0007's "the command names one finding"
    # is only a property if the process holding the write credential checks it.
    for name in ("review.json", "commanded_index.json"):
        if not (args.artifact_dir / name).is_file():
            fail(
                f"no {name} in the bundle: the commanded finding cannot be derived, so the "
                f"plan's scope cannot be verified and nothing is executed "
                f"({args.artifact_dir / name})"
            )

    # The provenance inputs are re-fetched, not read from the bundle. The plan
    # must come from the plan job — it IS that job's output — but the diff and
    # the changed-file list are facts about the PR that this job's own token can
    # establish, and the frame condition is only as strong as the list it
    # quantifies over. post.py takes the same posture toward the review job.
    #
    # Hoisted above the commanded finding, which the review verifier now needs:
    # deriving the finding means accepting the artifact, and an artifact accepted
    # against the bundle's own copy of the diff would be checked by the job this
    # process distrusts.
    diff_bytes, changed_files = fetch_anchored_pair(repo, reviewed_base_sha, reviewed_sha)
    diff_text = decode_contributor_bytes(diff_bytes)

    # DERIVED, not supplied: the accepted artifact is re-verified here with the
    # review verifier — against the provenance inputs just fetched — and the
    # ordinal indexes it. So "an element of an accepted review" is structural at
    # the gate holding the write token, not a claim about a file the plan job
    # wrote. A forged finding cannot authorise a fix because there is no forgeable
    # finding input left, and no key is needed: this process accepts the artifact
    # itself rather than comparing two copies of one.
    try:
        commanded_findings = read_commanded_findings(args.artifact_dir, policy,
                                                     diff_text=diff_text,
                                                     changed_files=changed_files)
    except Rejection as exc:
        fail("the bundle's commanded finding(s) are not an accepted review's findings: "
             f"{redact_line(str(exc), policy)}")

    # Re-verification happens HERE, where the write token lives. The plan
    # job's claim to have verified anything is not trusted — the posture
    # post.py takes toward the review job. The anchor tree is the quarantine
    # fetch of the reviewed head, so anchoring reads the same bytes the plan
    # session read.
    try:
        parsed_steps = verify_plan(
            plan, diff_text, changed_files, policy, tree_content_source(args.pr_root),
            head_branch=reviewed_head_ref, commanded_findings=commanded_findings,
        )
    except Rejection as exc:
        # Redacted at the caller, which is where the policy is: a Rejection
        # interpolates the value it refused, github_api.fail is policy-free by
        # design, and this print is the emit path the transcript redaction never
        # covered.
        fail(f"plan rejected, nothing executed: {redact_line(str(exc), policy)}")

    try:
        delivery = decide_delivery(plan["steps"])
    except Refusal as exc:
        fail(f"plan verified but refused: {exc}")

    # The job's own declaration of what it may deliver, checked after verification
    # and before any live call. This is what makes routing on unverified plan
    # structure safe: the routing job chooses which credential is minted, and this
    # decides whether that credential may be USED for the mode the verified plan
    # actually decided. A mismatch is a run that fails having written nothing.
    if args.allow and delivery.mode not in args.allow:
        fail(
            f"delivery ({delivery.mode}) is not allowed in this job (--allow "
            f"{' '.join(args.allow)}); the plan routed to a job that may not deliver it, "
            "so nothing was executed"
        )

    # TOCTOU precondition and delivery context in one fetch. For a stacked PR
    # the base is the reviewed PR's own head BRANCH — from this context, never
    # from the plan (open_pr has no base argument, and both gates pin that).
    pr = pr_snapshot(repo, pr_number, reviewed_sha, reviewed_base_ref)
    if delivery.mode == "stacked_pr" and is_fork(pr):
        fail(
            "stacked PR refused: the reviewed PR is from a fork, so its head branch "
            "does not exist in the base repository to base a PR on (ADR-0009 addendum)"
        )

    # Announced BEFORE anything delivery needs can fail. The decision is the
    # commander's explanation of what this run chose to do, so it must survive a
    # later failure — an ownership resolution that fails closed should read as
    # "decided this, then could not proceed", not as a run that never decided.
    if delivery.mode == "stacked_pr":
        # The base is the reviewed pull request's own head BRANCH, from this live
        # context and never from the plan: open_pr has no base argument, and both
        # gates pin its argument set exactly (ADR-0009 addendum). The fix therefore
        # merges INTO the open pull request and broken code never lands on the
        # default branch.
        base = pr["head"]["ref"]
        print(f"delivery decision: stacked PR based on {base!r}")
    else:
        print(f"delivery decision: suggestion comments on {delivery.path!r}")

    # Resolved before the first write: ownership decides which comments and which
    # follow-up pull requests this token may edit or delete, so it must come from
    # the credential in hand and fail closed rather than be guessed
    # (post.resolve_bot_login). Both deliveries need it.
    bot_login = resolve_bot_login()

    metadata = {
        "model": read_model_stamp(args.artifact_dir),
        "policy": hashlib.sha256(read_harness_text(args.policy).encode()).hexdigest()[:12],
        "sha": reviewed_sha,
        "run_url": os.environ["RUN_URL"],
    }

    # Identity keyed on the FETCHED diff, never the bundle's copy: a tampered
    # bundle diff would let the plan job choose which existing comment its
    # suggestion — or which existing follow-up pull request its fix — collides
    # with. The window comes from the quarantine tree — the reviewed head, the same
    # bytes that anchored the plan — which is what makes the key independent of
    # where the hunk boundaries happen to fall (ADR-0009 addendum).
    signatures = anchor_signatures(diff_text, content_source=tree_content_source(args.pr_root))

    if delivery.mode == "stacked_pr":
        try:
            delivered = deliver_stacked_pr(
                repo,
                plan["steps"],
                # The bytes the VERIFIER produced, from the same function its
                # anchoring phase ran. Re-deriving them here would be a second
                # model of what patching means, which is the divergence eight of
                # the chunk B review's fourteen findings came from. Over the
                # steps verify_plan parsed: the applier takes Steps, and only
                # the parser constructs them (ADR-0017).
                apply_patch_steps(
                    [(index, step) for index, step in enumerate(parsed_steps)
                     if step.kind in FIX_KINDS],
                    tree_content_source(args.pr_root),
                ),
                base=base,
                reviewed_sha=reviewed_sha,
                # ADR-0007's (pr, head_sha, findings), over the DERIVED findings, so
                # nothing the plan job wrote can steer which existing fix this
                # command dedups against. Over the SET (ADR-0013), so `/fix 3,1` and
                # `/fix 1,3` are one command and cannot open two pull requests.
                key=fix_key(pr_number, reviewed_sha, commanded_findings, signatures),
                metadata=metadata,
                bot_login=bot_login,
            )
        except AlreadyDelivered as exc:
            # The decline channel's second producer (ADR-0014). This refusal is
            # knowable only here: fix_key needs anchor_signatures over the
            # quarantine tree, which `command` never fetches, and find_existing_fix
            # needs a live pull-request listing. Deliberately included — it is the
            # refusal a maintainer is most likely to want an answer to, and the
            # message already names and links the pull request that answers it.
            #
            # `stack` emits an OUTPUT rather than posting: it holds this lane's
            # broadest credential, and making that job the one that talks to humans
            # would split the decline into two implementations that must agree on
            # their text. One posting job, one reason format, two producers.
            emit_decline(
                f"This command has already been delivered. {exc}",
                head_sha=reviewed_sha,
                # The ordinals come from the bundle's commanded_index.json, which is
                # already a gate input here — the commanded findings are DERIVED from
                # it — so the decline names the same command the scope gate checked.
                ordinals=ordinals_of(read_commanded_indices(args.artifact_dir)),
            )
            fail(f"command already delivered: {exc}")
        except StackRefusal as exc:
            fail(f"plan verified but refused at delivery: {exc}")
        # The commander's receipt. A delivered remediation whose only trace was a
        # green run would leave them hunting for the pull request.
        print(f"delivered: opened follow-up pull request #{delivered.get('number')} "
              f"({delivered.get('html_url')}) from the fix branch into {base!r}")
        # No post-write drift re-check, and that is the difference from the
        # suggestion path. A suggestion's value depends on the head it is anchored
        # to, so a push landing mid-delivery has to fail the run. This delivery's
        # artifact is a PULL REQUEST: if the head moved while it was being created,
        # GitHub itself now shows the fix branch as behind and the merge as needing
        # attention, which is the same fail-visible signal, in the place the human
        # is already looking. Failing here would report a red run for a pull request
        # that exists and is correct.
        return

    steps = [step for step in plan["steps"] if step["kind"] == "suggest"]
    # The retraction scope. One command names one or more findings (ADR-0007,
    # ADR-0013), so this run speaks only for THOSE findings: without the scope the
    # reconciler would read every OTHER finding's live suggestion as withdrawn and
    # take it down. Derived from the commanded findings rather than from the plan's
    # steps, because scope is a fact about the COMMAND. The findings, not their
    # files — two findings of one accepted artifact routinely share a file, and a
    # path is the set two commands speak for.
    #
    # A tuple, not a set: membership is what scope needs, and the ORDER is what the
    # reconciler records on a comment (the marker stays per finding). Canonical
    # because read_commanded_findings returns ordinal order, so the same command
    # records the same representative on every run.
    reconcile_suggestions(repo, pr_number, steps, signatures, metadata,
                          bot_login=bot_login, head_sha=reviewed_sha,
                          commanded_finding_keys=tuple(
                              finding_identity(finding, signatures)
                              for finding in commanded_findings))
    print(f"delivered {len(steps)} suggestion(s) on {delivery.path!r}")

    # TOCTOU guard, second half — the posture post.py takes after ITS write, and
    # what submit_review's docstring means by "the pre- and post-write drift checks
    # stay". The pre-check and the write are not atomic and several live calls sit
    # between them, so a push (or a retarget) can land mid-delivery.
    #
    # Nothing is withdrawn, which is where this differs from post.py's single
    # upsert: the comments are bound to the reviewed SHA by commit_id, so GitHub
    # marks them OUTDATED against the new head rather than misplacing them — the
    # fail-visible behaviour ADR-0009 leans on. Deleting them would destroy
    # correctly-outdated suggestions and any human thread beneath them. So the run
    # FAILS, because a commander must see that the fix was delivered against a head
    # that has since moved, and the comments stay for them to read.
    if moved := pr_moved(cast("dict", api_json(f"/repos/{repo}/pulls/{pr_number}")),
                         reviewed_sha, reviewed_base_ref):
        fail(f"{moved} while delivering; the suggestions were posted against "
             f"{reviewed_sha} and GitHub marks them outdated against the new head")


if __name__ == "__main__":
    main()
