"""Runs Claude Code via the Agent SDK to produce a remediation PLAN artifact.

The second cc_loop-style session (ADR-0007: remediation is commanded per
finding; ADR-0013: the command names a SET of them). Input is the ACCEPTED review
artifact plus the ordinals the command named — the commanded findings are derived
from the two, never supplied, so their membership in an accepted review is
structural (read_commanded_findings) — plus
the same review context the reviewer saw; output is plan.json, arrived at
through an in-process
`submit_plan` tool whose handler runs verify_plan() in this process — the
same recovery-channel pattern as submit_review, and literally the same code:
make_submit_tool, build_review_server and drive_session are cc_loop's, with
this module supplying only what differs (the schema, the prompt, the
verifier, and the content source for ADR-0005 anchoring).

The session reads the same two roots the reviewer does, and pr_root plays a
second role here: it IS the reviewed head, so it is also the content source
verify_plan anchors `old` against. The model copying `old` from the tree it
can Read is the honest path being the easy path.

Writes plan.json and transcript.jsonl; exits non-zero without an artifact on
any failure. No step is executed here — the plan is inert data for the
executor, which re-verifies it (the posture post.py takes toward the review
job).

Usage:
    python plan_loop.py --context-dir "$CONTEXT_DIR" --pr-root "$PR_QUARANTINE" \
        --base-root "$GITHUB_WORKSPACE" --output-dir "$OUTPUT_DIR"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from artifact import (
    POLICY_PATH,
    Transcript,
    _scalar_to_json_schema,
    apply_project_description,
    build_user_message,
    fence,
    rendered_findings,
    severity_ranks,
    sha256,
)
from canonicalize import read_contributor_text, read_harness_text
from cc_loop import (
    MAX_SUBMISSIONS,
    configured_model,
    drive_session,
    fail,
    make_submit_tool,
    tool_guidance,
)
from plan_verify import build_plan_schema, tree_content_source, verify_plan
from verify import Rejection, verify

SUBMIT_TOOL = "mcp__plan__submit_plan"

_HARNESS_ROOT = Path(__file__).resolve().parent
PLAN_PROMPT_PATH = _HARNESS_ROOT.parent.parent / "prompts" / "ai-pr-plan.md"


def render_plan_constraints(policy: dict) -> str:
    """The enforced plan constraints from policy.json as a system prompt
    section, so the prose the model reads can never drift from what
    verify_plan enforces — render_constraints' rule, applied to plans."""
    plan = policy["plan"]
    kinds = ", ".join(f"`{kind}`" for kind in sorted(plan["step_kinds"]))
    denylist = ", ".join(f"`{p}`" for p in plan["path_denylist"])
    # Rendered from policy.ordering, not restated: the rule is enforced by both
    # gates, so the prose has to move when an operator edits the policy object.
    # An empty ordering list renders NOTHING rather than a sentence with a hole
    # in it — render_constraints' rule for the empty allowlist, same reasoning.
    if plan["ordering"]:
        pairs = "; ".join(f"every `{rule['before']}` before every `{rule['after']}`" for rule in plan["ordering"])
        ordering_rule = (
            f"- Step order is enforced: {pairs}. A step out of that order rejects "
            "the whole plan, however far apart the pair sits.\n"
        )
    else:
        ordering_rule = ""
    return (
        "\n\n## Enforced plan constraints (verifier-rejected if violated)\n\n"
        f"- Step kinds: {kinds}. Nothing else exists; an unknown kind rejects "
        "the whole plan.\n"
        f"- At most {plan['max_steps']} steps, at most {plan['max_patched_files']} "
        f"distinct patched files, at most {plan['max_changed_lines']} changed "
        "lines (old plus new) per patch or suggest step.\n"
        f"- Never these paths, even if the PR touched them: {denylist}.\n"
        "- Every patch/suggest `path` must be a file this PR changed. Every "
        "patch/suggest `old` must byte-match the file at the PR head exactly "
        "— copy it verbatim from the PR head root, never retype it — and must "
        "occur exactly once in the file, so include enough surrounding lines "
        "to make it unique.\n"
        "- A `suggest` step's `line` must be a line number inside a diff hunk "
        "for that file; read it off the annotated diff verbatim, never "
        "compute it.\n"
        f"{ordering_rule}"
        "- `suggest.note` and `open_pr.body` are markdown with the same "
        "restrictions as a review comment: no links unless allowlisted, no "
        "images, no raw HTML, no @-mentions, every fence closed.\n"
        "- Arguments are literal values only. There is no way to reference "
        "another step's output, no conditional, and no loop.\n"
    )


def render_plan_rejection_guidance(policy: dict) -> str:
    """The retry message appended to every rejected submission."""
    kinds = ", ".join(sorted(policy["plan"]["step_kinds"]))
    return (
        "Nothing was saved. Return one complete, self-contained plan: a "
        f"single `steps` array of {{id, kind, args}} objects (kinds: {kinds}), "
        "no extra keys anywhere. Partial or incremental submissions are not "
        "supported."
    )


def read_commanded_indices(context_dir: Path) -> list[int]:
    """The ordinals the command named, sorted, as plain non-negative ints.

    The one fact about the command that cannot be derived from anything else, so
    it is the only thing the remediation lane's context adds to the review's own.
    A SET, because `/fix 3,1` and `/fix 1,3` are one command (ADR-0013); returned
    sorted so every downstream reader sees one canonical order and cannot make
    ordering part of an identity.

    Every per-element bound the single-ordinal version had still applies, per
    element. Every non-int is refused rather than coerced, and `bool` is excluded
    explicitly because it is an `int` in Python — `True` would resolve
    findings[1], a real finding on a file nobody commanded. A negative value is
    refused for the same reason and it is the sharper case: Python indexes from
    the end, so -1 is the only out-of-range ordinal that silently resolves to a
    finding at all.

    Three bounds are the SET's own. The list must be a list, because a bare string
    is iterable and `"12"` would read as two ordinals nobody typed. It must not be
    empty: there is no command naming no finding, and an empty set would make every
    scope check pass vacuously — the same fixless-plan shape check_plan_cardinality
    refuses. And a repeated ordinal collapses rather than naming a finding twice.
    """
    raw = json.loads(read_harness_text(context_dir / "commanded_index.json"))
    if not isinstance(raw, dict):
        raise Rejection("commanded_index.json: expected a JSON object")
    indices = raw.get("indices")
    if not isinstance(indices, list):
        raise Rejection(
            f"commanded_index.json: indices must be a list, got {type(indices).__name__}; "
            "a bare string is iterable and would read each character as an ordinal"
        )
    if not indices:
        raise Rejection(
            "commanded_index.json: indices is empty; a command names at least one finding, "
            "and an empty set would leave the plan's scope unconstrained"
        )
    for index in indices:
        if isinstance(index, bool) or not isinstance(index, int):
            raise Rejection(f"commanded_index.json: index must be an integer, got {index!r}")
        if index < 0:
            raise Rejection(
                f"commanded_index.json: index {index} is negative, which would address a "
                "finding from the end of the review rather than none at all"
            )
    return sorted(set(indices))


def read_commanded_findings(context_dir: Path, policy: dict, *,
                            diff_text: str | None = None,
                            changed_files: list[str] | None = None) -> list[dict]:
    """The commanded findings, DERIVED from the accepted artifact and the ordinals.

    Membership rather than shape, which is the property the previous contract
    could not state. finding.json used to be an input: a well-shaped finding no
    accepted artifact contained passed every check, so a forged one could direct a
    real remediation at a defect no reviewer ever found, on any file in the pull
    request. Both review engines raised it independently, and it was closed on
    reachability — the remediation lane had no workflow, so nothing
    contributor-reachable composed context_dir. Wiring that workflow is what made
    reachability stop bounding it.

    So the finding is no longer supplied. The context carries the artifact and an
    ordinal; this verifies the artifact with the review verifier — the same gate
    that accepted it, provenance and markdown and secret scan included — and then
    indexes it. "An element of an accepted artifact" becomes structural: there is
    no separate finding to forge, and no second copy for two readers to disagree
    about. No signing key is needed, because neither gate is comparing its input
    against another job's copy; each verifies the artifact it holds.

    Each ordinal is a RENDERED position (artifact.rendered_findings), because
    `/fix 3` means the third finding of the comment the commander read and
    review.json holds model order.

    Returned in ordinal order, which is the sorted order read_commanded_indices
    established. That makes the list a function of the SET the commander named and
    not of how they typed it — the property stack.fix_key needs so `/fix 3,1` and
    `/fix 1,3` cannot open two follow-up pull requests.

    One reader for the two things that need it: the prompt (which quotes the
    findings) and verify_plan (which checks the plan's scope against them,
    ADR-0007 and ADR-0013).
    Shared with the executor for the same reason — two readers would be two
    chances to disagree about which finding was commanded — which is why the
    provenance inputs are parameters. The plan session reads them from the
    SHA-anchored context it was given; the executor passes the pair it fetched
    itself, because an artifact accepted against the bundle's own copy of the diff
    would be checked by the job that process distrusts.
    """
    review = json.loads(read_harness_text(context_dir / "review.json"))
    if diff_text is None:
        diff_text = read_contributor_text(context_dir / "diff.patch")
    if changed_files is None:
        changed_files = json.loads(read_harness_text(context_dir / "changed_files.json"))
    # The full review verifier, not a per-finding subset: an artifact that could
    # not have been accepted cannot command a fix, and provenance is the arm no
    # per-finding check ever had — it is what refuses a finding on a file this
    # pull request never touched.
    verify(review, diff_text, changed_files, policy)

    indices = read_commanded_indices(context_dir)
    findings = rendered_findings(review, severity_ranks(policy))
    # Every ordinal, and one past the end rejects the whole command: the scope the
    # commander asserted is the set they named, so resolving the subset that happens
    # to exist would verify a plan against a scope nobody asked for.
    if past_the_end := [index for index in indices if index >= len(findings)]:
        raise Rejection(
            f"commanded_index.json: index/indices {past_the_end} but the accepted review has "
            f"{len(findings)} finding(s); the command names no finding of it"
        )
    return [findings[index] for index in indices]


def build_plan_user_message(context_dir: Path, policy: dict) -> str:
    """The review context the reviewer saw, plus the commanded findings.

    The findings are the ones the maintainer's command names (ADR-0007, ADR-0013),
    derived by read_commanded_findings from the accepted artifact rather than read
    from a file that merely claims to hold elements of one; they are fenced anyway
    because they quote contributor code. The diff and PR description arrive through
    build_user_message unchanged: the plan is anchored against the same
    SHA-anchored context the review was, or the anchor and the review can
    disagree.

    Each finding is fenced SEPARATELY rather than as one JSON array, so the fence
    is per untrusted payload exactly as it is for a single command: one block whose
    content is several findings would let text quoted inside the first appear to a
    reader (and to the model) to be structure between them.

    Naming several findings asserts they take ONE remediation. That assertion is
    the commander's, and the message says so — the model is not asked to judge it,
    and nothing in the plan lane checks it (ADR-0005's content question).
    """
    findings = read_commanded_findings(context_dir, policy)
    review_context = build_user_message(context_dir)
    # The reviewer's closing instruction is the one review-specific sentence
    # in an otherwise reusable context block; swap it rather than duplicate
    # the block's assembly. Anchored exactly so a reworded build_user_message
    # fails the swap-landed test instead of silently shipping both sentences.
    closing = "Investigate with your tools as needed, then return your review."
    if closing not in review_context:
        raise ValueError("build_user_message no longer ends with the expected instruction; cannot substitute")
    review_context = review_context.replace(
        closing, "Investigate with your tools as needed, then return your plan."
    )
    fenced = "\n".join(
        fence(json.dumps(finding, indent=2, ensure_ascii=False), "commanded_finding")
        for finding in findings
    )
    if len(findings) == 1:
        preamble = (
            "A maintainer has commanded a fix for ONE finding of an accepted "
            "review. Plan the remediation for this finding and no other:"
        )
    else:
        preamble = (
            f"A maintainer has commanded a fix for {len(findings)} findings of an "
            "accepted review, and by naming them together has asserted that they "
            "take ONE remediation. Plan that remediation for these findings and no "
            "others — your fix must touch every file they name:"
        )
    return (
        f"{preamble}\n"
        f"{fenced}\n\n"
        "The review context below is what the reviewer saw, for the same PR "
        "at the same head SHA.\n\n"
        f"{review_context}"
    )


def run(base_root: Path, pr_root: Path, context_dir: Path, output_dir: Path,
        verify_fn=verify_plan) -> int:
    """Return 0 with a verified plan.json written, or non-zero with none.

    verify_fn is the eval harness's fault-injection seam (cc_loop's pattern);
    it receives verify_plan's five arguments, content source included.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    policy_text = read_harness_text(POLICY_PATH)
    policy = json.loads(policy_text)
    transcript = Transcript(output_dir / "transcript.jsonl", policy)

    schema = build_plan_schema(policy)
    # The SAME description seam the review session uses (cc_loop.run). A consumer
    # setting SMTITHY_PROJECT_DESCRIPTION as documented had it read by the
    # reviewer and ignored here, so their planner was told its patch paths must be
    # files this PR changed while being shown an example rooted in another
    # project's tree. Absent, the assembled prompt is byte-identical, so the
    # shipped default keeps whatever eval history it has.
    system_prompt = (
        apply_project_description(
            read_harness_text(PLAN_PROMPT_PATH), os.environ.get("SMTITHY_PROJECT_DESCRIPTION")
        )
        + render_plan_constraints(policy)
        + tool_guidance(base_root.resolve(), pr_root.resolve())
    )

    transcript.log(
        "run_start",
        generator="claude-agent-sdk",
        artifact_kind="plan",
        model_id=configured_model(),
        prompt_sha256=sha256(system_prompt),
        policy_sha256=sha256(policy_text),
        max_rounds=MAX_SUBMISSIONS,
    )

    # Rejection joins the caught set: a commanded finding that is not the shape it
    # claims to be is a context this run cannot assemble, and it fails closed with
    # the reason logged rather than reaching the model.
    try:
        user_message = build_plan_user_message(context_dir, policy)
        commanded_findings = read_commanded_findings(context_dir, policy)
    except (OSError, ValueError, UnicodeError, Rejection) as exc:
        return fail(transcript, f"cannot assemble the plan context: {exc}")
    transcript.log("context", sha256=sha256(user_message), bytes=len(user_message.encode()))

    diff_text = read_contributor_text(context_dir / "diff.patch")
    changed_files = json.loads(read_harness_text(context_dir / "changed_files.json"))
    guidance = render_plan_rejection_guidance(policy)

    # The content source is pinned to pr_root HERE, not inside the tool: the
    # anchor tree is this process's trust decision, never a submission's.
    content_source = tree_content_source(pr_root.resolve())

    # The reviewed head branch, so a plan targeting it is refused in-session with
    # a reason the model can act on rather than surviving to the executor. Read
    # with a default, unlike execute_plan's os.environ["HEAD_REF"]: this gate is a
    # PRE-CHECK feeding the generator, and the executor re-verifies with the value
    # required. An eval scenario is a fixture with no pull request, so there is no
    # head branch to supply and nothing to fail closed about.
    head_branch = os.environ.get("HEAD_REF") or None

    # The commanded findings are pinned HERE for the same reason the content source
    # is: which findings were commanded is this process's trust decision, read from
    # the context the maintainer's command produced, never from a submission.
    def checked(artifact, diff, files, pol):
        verify_fn(artifact, diff, files, pol, content_source,
                  head_branch=head_branch, commanded_findings=commanded_findings)

    return drive_session(
        transcript=transcript,
        policy=policy,
        system_prompt=system_prompt,
        user_message=user_message,
        base_root=base_root,
        pr_root=pr_root,
        output_dir=output_dir,
        make_tool=lambda state: make_submit_tool(
            schema, state, transcript, checked, diff_text, changed_files, policy, guidance,
            tool_name="submit_plan", noun="plan", note_fn=None,
        ),
        verify_fn=verify_fn,
        server_name="plan",
        submit_tool_name=SUBMIT_TOOL,
        artifact_filename="plan.json",
        tool_display_name="submit_plan",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--pr-root", required=True, type=Path)
    parser.add_argument("--context-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    return run(args.base_root, args.pr_root, args.context_dir, args.output_dir)


if __name__ == "__main__":
    sys.exit(main())
