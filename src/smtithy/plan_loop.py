"""Runs Claude Code via the Agent SDK to produce a remediation PLAN artifact.

The second cc_loop-style session (ADR-0007: remediation is commanded per
finding). Input is the commanded finding plus the same review context the
reviewer saw; output is plan.json, arrived at through an in-process
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
    build_user_message,
    fence,
    sha256,
)
from canonicalize import read_contributor_text, read_harness_text
from cc_loop import MAX_SUBMISSIONS, drive_session, make_submit_tool, tool_guidance
from plan_verify import tree_content_source, verify_plan

SUBMIT_TOOL = "mcp__plan__submit_plan"

_HARNESS_ROOT = Path(__file__).resolve().parent
PLAN_PROMPT_PATH = _HARNESS_ROOT.parent.parent / "prompts" / "ai-pr-plan.md"


def build_plan_schema(policy: dict) -> dict:
    """Translate policy.json's plan section into a JSON Schema for the
    generator's submit_plan tool input. One oneOf branch per step kind, so
    the schema the model reads names each kind's exact argument set — but the
    MCP layer never validates against it (build_review_server disables that
    on purpose); verify_plan is the gate, and this is documentation."""
    plan = policy["plan"]
    step_branches = []
    for kind, spec in plan["step_kinds"].items():
        step_branches.append({
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"},
                "kind": {"const": kind},
                "args": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        name: _scalar_to_json_schema(arg) for name, arg in spec["args"].items()
                    },
                    "required": list(spec["args"]),
                },
            },
            "required": ["id", "kind", "args"],
        })
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": plan["max_steps"],
                "items": {"oneOf": step_branches},
            },
        },
        "required": ["steps"],
    }


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


def build_plan_user_message(context_dir: Path) -> str:
    """The review context the reviewer saw, plus the one commanded finding.

    finding.json is the finding the maintainer's command names (ADR-0007) —
    an element of the ACCEPTED review artifact, so it has already passed the
    review verifier; it is fenced anyway because it quotes contributor code.
    The diff and PR description arrive through build_user_message unchanged:
    the plan is anchored against the same SHA-anchored context the review
    was, or the anchor and the review can disagree.
    """
    finding = json.loads(read_harness_text(context_dir / "finding.json"))
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
    return (
        "A maintainer has commanded a fix for ONE finding of an accepted "
        "review. Plan the remediation for this finding and no other:\n"
        f"{fence(json.dumps(finding, indent=2, ensure_ascii=False), 'commanded_finding')}\n\n"
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
    system_prompt = (
        read_harness_text(PLAN_PROMPT_PATH)
        + render_plan_constraints(policy)
        + tool_guidance(base_root.resolve(), pr_root.resolve())
    )

    transcript.log(
        "run_start",
        generator="claude-agent-sdk",
        artifact_kind="plan",
        model_id=os.environ.get("CC_MODEL", "default"),
        prompt_sha256=sha256(system_prompt),
        policy_sha256=sha256(policy_text),
        max_rounds=MAX_SUBMISSIONS,
    )

    try:
        user_message = build_plan_user_message(context_dir)
    except (OSError, ValueError, UnicodeError) as exc:
        return fail(transcript, f"cannot assemble the plan context: {exc}")
    transcript.log("context", sha256=sha256(user_message), bytes=len(user_message.encode()))

    diff_text = read_contributor_text(context_dir / "diff.patch")
    changed_files = json.loads(read_harness_text(context_dir / "changed_files.json"))
    guidance = render_plan_rejection_guidance(policy)

    # The content source is pinned to pr_root HERE, not inside the tool: the
    # anchor tree is this process's trust decision, never a submission's.
    content_source = tree_content_source(pr_root.resolve())

    def checked(artifact, diff, files, pol):
        verify_fn(artifact, diff, files, pol, content_source)

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
