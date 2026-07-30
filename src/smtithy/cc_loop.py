"""Runs the Claude Code CLI to produce a review artifact.

Writes review.json and transcript.jsonl; exits non-zero without an artifact on
any failure. Everything derived from policy.json lives in artifact.py.

Tool calls are not mediated: the CLI's Read/Grep/Glob run in its own sandbox
scoped by --add-dir, and the transcript records them after the fact for audit
only.

Usage:
    python cc_loop.py --context-dir "$CONTEXT_DIR" --pr-root "$PR_QUARANTINE" \
        --base-root "$GITHUB_WORKSPACE" --output-dir "$OUTPUT_DIR"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from artifact import (
    MAX_REPEATED_REJECTIONS,
    POLICY_PATH,
    PROMPT_PATH,
    Transcript,
    build_artifact_schema,
    build_user_message,
    redact_text,
    rejection_fingerprint,
    render_constraints,
    render_rejection_guidance,
    sha256,
)
from diff_map import split_diff_lines as split_ndjson_lines
from verify import Rejection, verify

ALLOWED_TOOLS = "Read,Grep,Glob"

# --allowedTools does NOT restrict the surface to the tools it names: probing the
# agent showed Workflow, Skill, ToolSearch, ReportFindings and the deferred
# Task*/Cron*/Worktree set all still reachable. Workflow spawns subagents with
# their own Bash/Write; ReportFindings looks like the reviewer's own reporting
# tool but writes to the CLI's UI, not to the artifact, and the agent that used
# it then omitted `findings` entirely. Both must be denied by name.
DISALLOWED_TOOLS = ",".join(
    [
        "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
        "Task", "Agent", "TodoWrite", "ReportFindings",
        "Workflow", "Skill", "ToolSearch", "SendMessage",
        "TaskCreate", "TaskUpdate", "TaskStop", "TaskGet", "TaskList", "TaskOutput",
        "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        "EnterWorktree", "ExitWorktree",
    ],
)

# Both bounds fail closed: exceeding either yields no structured_output, so
# run() returns non-zero with no artifact. --max-turns is absent from
# `claude --help` on 2.1.220 but is documented and does enforce.
MAX_TURNS = int(os.environ.get("CC_MAX_TURNS", "30"))

# PER ATTEMPT, and it must leave room for MAX_ATTEMPTS of them plus the backoff
# inside the workflow's timeout-minutes. When this matched the job timeout, a
# slow run was killed by GitHub mid-step and every diagnostic was lost with it:
# the transcript is only useful if the process lives long enough to write it.
WALL_CLOCK_SECONDS = int(os.environ.get("CC_WALL_CLOCK_SECONDS", "150"))

MAX_ATTEMPTS = 4

# Doubled per attempt, so the budget spans 1s + 2s + 4s of waiting.
API_ERROR_BACKOFF_SECONDS = float(os.environ.get("CC_API_ERROR_BACKOFF_SECONDS", "1"))


# Errors that cannot succeed on a later attempt: a missing IAM action, a denied
# model, a bad credential. Retrying one wastes the whole budget AND buries the
# reason under identical failures — a missing bedrock action once turned a
# 30-second diagnosis into a 12-minute one with an empty job log.
PERMANENT_API_ERROR_MARKERS = (
    "not authorized to perform",
    "accessdenied",
    "unrecognizedclient",
    "invalidsignature",
    "expiredtoken",
    "could not be found",
    "you don't have access to the model",
)


def is_permanent_api_error(detail: str) -> bool:
    """Whether an upstream error is a misconfiguration rather than a blip.

    Matched on the message because the CLI reports every upstream failure as the
    same `terminal_reason: api_error`, with the HTTP status and body only in the
    human-readable text.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in PERMANENT_API_ERROR_MARKERS)


def tool_guidance(base_root: Path, pr_root: Path) -> str:
    """Append the two filesystem roots, which are runtime paths the prompt
    file cannot state. How to use them is the prompt's own business."""
    return (
        "\n\n## The two roots in this session\n\n"
        f"- BASE (trusted, pre-change): `{base_root}`\n"
        f"- PR HEAD (contributor-authored data under review): `{pr_root}`\n"
    )


def build_command(schema: dict, system_prompt: str, base_root: Path, pr_root: Path, resume: str | None) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system_prompt,
        "--allowedTools", ALLOWED_TOOLS,
        "--disallowedTools", DISALLOWED_TOOLS,
        "--max-turns", str(MAX_TURNS),
        "--add-dir", str(base_root),
        "--add-dir", str(pr_root),
        # No project CLAUDE.md, skills, hooks, plugins or MCP servers: the
        # review must depend on the prompt we audit, not on ambient config.
        "--safe-mode",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
    ]
    if model := os.environ.get("CC_MODEL"):
        cmd += ["--model", model]
    # Session persistence must stay ON: --resume is how a rejected attempt keeps
    # its investigation, and --no-session-persistence makes it unresumable, which
    # turns every rejection into a hard failure.
    if resume:
        cmd += ["--resume", resume]
    return cmd


def parse_stream(stdout: str) -> tuple[dict | None, list[dict]]:
    """Split the stream-json output into (result envelope, intermediate events).

    Never `str.splitlines()`: JSON does not treat U+2028/U+2029/U+0085 as line
    breaks but splitlines() does, and JavaScript's JSON.stringify emits them
    literally. A review whose text contains one would be cut mid-record, both
    fragments would fail to parse, and the run would end with no artifact.
    """
    result, events = None, []
    for line in split_ndjson_lines(stdout):
        if not (line := line.strip()):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "result":
            result = record
        else:
            events.append(record)
    return result, events


def log_tool_calls(transcript: Transcript, events: list[dict], attempt: int) -> int:
    """Copy the CLI's tool calls into the transcript as `tool_request` records.

    Names are recorded as the agent called them, so a record can be matched
    against the CLI's own stream. The eval harness reads these to assert the
    agent investigated.
    """
    count = 0
    for record in events:
        if record.get("type") != "assistant":
            continue
        for block in record.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            transcript.log(
                "tool_request",
                round=attempt,
                tool=block.get("name", "?"),
                input=block.get("input", {}),
            )
            count += 1
    return count


def fail(transcript: Transcript, reason: str, **fields) -> int:
    """Log a terminal failure, echo it to stderr, close the transcript, return 1.

    The echo is why this exists: the transcript lands in an uploaded artifact, so
    without it a failed run leaves an EMPTY job log and the reason is only
    visible to someone who thinks to download the artifact.
    """
    transcript.log("run_failed", reason=reason, **fields)
    print(f"::error::ai-review generator failed: {reason}", file=sys.stderr)
    transcript.close()
    return 1


def run(base_root: Path, pr_root: Path, context_dir: Path, output_dir: Path, verify_fn=verify) -> int:
    """Return 0 with a verified review.json written, or non-zero with none.

    verify_fn is the eval harness's fault-injection seam; production passes none.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    policy_text = POLICY_PATH.read_text()
    policy = json.loads(policy_text)
    transcript = Transcript(output_dir / "transcript.jsonl", policy)

    schema = build_artifact_schema(policy)
    system_prompt = (
        PROMPT_PATH.read_text()
        + render_constraints(policy)
        + tool_guidance(base_root.resolve(), pr_root.resolve())
    )

    transcript.log(
        "run_start",
        generator="claude-code",
        model_id=os.environ.get("CC_MODEL", "default"),
        prompt_sha256=sha256(system_prompt),
        policy_sha256=sha256(policy_text),
        max_rounds=MAX_ATTEMPTS,
    )

    user_message = build_user_message(context_dir)
    transcript.log("context", sha256=sha256(user_message), bytes=len(user_message.encode()))

    diff_text = (context_dir / "diff.patch").read_text()
    changed_files = json.loads((context_dir / "changed_files.json").read_text())
    guidance = render_rejection_guidance(policy)

    message, session_id = user_message, None
    repeated, last_fingerprint = 0, None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        cmd = build_command(schema, system_prompt, base_root.resolve(), pr_root.resolve(), session_id)
        try:
            proc = subprocess.run(
                cmd,
                input=message,
                capture_output=True,
                text=True,
                cwd=str(base_root),
                timeout=WALL_CLOCK_SECONDS,
                check=False,  # a non-zero exit is a logged run_failed, not an exception
            )
        except subprocess.TimeoutExpired as timeout:
            # TimeoutExpired carries whatever was read before the kill; a hang is
            # precisely when that partial output is the only evidence there is.
            partial = timeout.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            (output_dir / f"cc_stream_{attempt}_timeout.jsonl").write_text(redact_text(partial, policy))
            return fail(
                transcript,
                f"wall-clock timeout after {WALL_CLOCK_SECONDS}s on attempt {attempt}",
                partial_bytes=len(partial),
            )

        # Redacted before being written, not after: the whole output_dir is
        # uploaded as a 90-day CI artifact, so writing the raw stream would route
        # a credential the agent surfaced around the same secret scan the
        # transcript applies. Redacting the TEXT rather than parsed records keeps
        # malformed lines, which are the ones worth having when diagnosing.
        (output_dir / f"cc_stream_{attempt}.jsonl").write_text(redact_text(proc.stdout, policy))
        result, events = parse_stream(proc.stdout)

        # A turn-limit exit is non-zero but DOES carry a result envelope, so
        # name it: "exited 1" alone would send a reader hunting for a CLI fault
        # when the real cause is a bound we set.
        if result is not None and result.get("subtype") == "error_max_turns":
            return fail(
                transcript,
                f"agent hit the {MAX_TURNS}-turn limit without submitting a review",
                num_turns=result.get("num_turns"),
                tool_calls=log_tool_calls(transcript, events, attempt),
            )

        # The CLI retries schema-invalid structured output internally (5 attempts)
        # and then exits with no artifact. Name it: "exited 1" sent a reader
        # hunting for a CLI fault when the real cause was the agent repeatedly
        # omitting a required field. `errors` carries the CLI's own message.
        if result is not None and result.get("subtype") == "error_max_structured_output_retries":
            return fail(
                transcript,
                "agent could not produce schema-valid output within the CLI's retry budget",
                errors=result.get("errors"),
                num_turns=result.get("num_turns"),
                tool_calls=log_tool_calls(transcript, events, attempt),
            )

        # Reports `subtype: success` with no structured_output, so it must be
        # matched on terminal_reason or a dead run counts as a successful one.
        # No --resume on retry, since the session may have died mid-turn.
        if result is not None and result.get("terminal_reason") == "api_error":
            detail = str(result.get("result") or "")
            permanent = is_permanent_api_error(detail)
            retrying = not permanent and attempt < MAX_ATTEMPTS
            backoff = API_ERROR_BACKOFF_SECONDS * 2 ** (attempt - 1) if retrying else 0
            # One record per occurrence, so counting `event == "api_error"`
            # across transcripts gives the error rate and summing
            # `backoff_seconds` gives what the waiting cost.
            transcript.log(
                "api_error",
                round=attempt,
                reason=str(result.get("result"))[:500],
                num_turns=result.get("num_turns"),
                api_ms=result.get("duration_api_ms"),
                wall_ms=result.get("duration_ms"),
                retrying=retrying,
                backoff_seconds=backoff,
                tool_calls=log_tool_calls(transcript, events, attempt),
            )
            if retrying:
                time.sleep(backoff)
                message, session_id = user_message, None
                continue
            if permanent:
                return fail(transcript, f"unretryable API error: {detail[:300]}")
            return fail(transcript, f"upstream API error on all {MAX_ATTEMPTS} attempts")

        if proc.returncode != 0 or result is None:
            return fail(
                transcript,
                f"claude CLI exited {proc.returncode} without a result envelope",
                subtype=result.get("subtype") if result else None,
                terminal_reason=result.get("terminal_reason") if result else None,
                stderr=proc.stderr[-2000:],
            )

        tool_calls = log_tool_calls(transcript, events, attempt)
        session_id = result.get("session_id")
        transcript.log(
            "model_response",
            round=attempt,
            stop_reason=result.get("stop_reason"),
            usage=result.get("usage"),
            num_turns=result.get("num_turns"),
            cost_usd=result.get("total_cost_usd"),
            duration_ms=result.get("duration_ms"),
            # With num_turns this makes ms-per-turn derivable, which is how a
            # throttled-but-successful run is distinguishable from a healthy one.
            api_ms=result.get("duration_api_ms"),
            tool_calls=tool_calls,
            permission_denials=result.get("permission_denials"),
        )

        artifact = result.get("structured_output")
        if artifact is None:
            return fail(
                transcript,
                f"no structured_output in result envelope (subtype={result.get('subtype')})",
            )

        try:
            verify_fn(artifact, diff_text, changed_files, policy)
        except Rejection as exc:
            transcript.log("submit_rejected", round=attempt, reason=str(exc), artifact=artifact)
            fingerprint = rejection_fingerprint(str(exc))
            repeated = repeated + 1 if fingerprint == last_fingerprint else 1
            last_fingerprint = fingerprint
            if repeated >= MAX_REPEATED_REJECTIONS or attempt == MAX_ATTEMPTS:
                return fail(transcript, f"final submission rejected: {exc}")
            # Resume the session so the retry keeps the investigation it already
            # did, and tell it WHY — the property PRs #496/#503 exist for.
            message = f"Your review was rejected by the verifier: {exc}\n\n{guidance}"
            continue

        (output_dir / "review.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False))
        transcript.log("artifact", sha256=sha256(json.dumps(artifact, sort_keys=True)))
        transcript.log("run_complete", rounds=attempt)
        transcript.close()
        return 0

    return fail(transcript, "attempt budget exhausted without a verified artifact")


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
