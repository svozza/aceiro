"""Runs Claude Code via the Agent SDK to produce a review artifact.

Writes review.json and transcript.jsonl; exits non-zero without an artifact on
any failure. Everything derived from policy.json lives in artifact.py.

The artifact arrives through an in-process `submit_review` tool rather than the
CLI's --json-schema structured output. Structured output routed the whole
review through one long text channel, and the model leaked function-calling
XML into it often enough to burn the CLI's five internal retries on ~20% of
runs (docs/findings/0001). A named tool gives the model separate native
arguments per field, and its handler runs verify() in this process — so a
rejected submission gets the verifier's actual reason as tool feedback and the
retry keeps its session, instead of five blind identical attempts.

Tool calls are not mediated: the CLI's Read/Grep/Glob run in its own sandbox
scoped by add_dirs, and the transcript records them after the fact for audit
only. submit_review is the one exception — its handler IS this process.

Usage:
    python cc_loop.py --context-dir "$CONTEXT_DIR" --pr-root "$PR_QUARANTINE" \
        --base-root "$GITHUB_WORKSPACE" --output-dir "$OUTPUT_DIR"
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from math import ceil
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ProcessError,
    ResultMessage,
    ToolUseBlock,
    query,
    tool,
)
from mcp.server import Server
from mcp.types import CallToolResult, TextContent, Tool

from artifact import (
    MAX_REPEATED_REJECTIONS,
    POLICY_PATH,
    PROMPT_PATH,
    Transcript,
    apply_project_description,
    build_artifact_schema,
    build_user_message,
    redact_text,
    rejection_fingerprint,
    render_constraints,
    render_rejection_guidance,
    sha256,
)
from canonicalize import read_contributor_text, read_harness_text
from verify import Rejection, verify

SUBMIT_TOOL = "mcp__review__submit_review"

# Read-only investigation plus the one submission channel. The plan generator
# shares the read-only set and swaps in its own submit tool via build_options.
READONLY_TOOLS = ["Read", "Grep", "Glob"]
ALLOWED_TOOLS = [*READONLY_TOOLS, SUBMIT_TOOL]

# allowed_tools does NOT restrict the surface to the tools it names: probing the
# agent showed Workflow, Skill, ToolSearch, ReportFindings and the deferred
# Task*/Cron*/Worktree set all still reachable. Workflow spawns subagents with
# their own Bash/Write; ReportFindings looks like the reviewer's own reporting
# tool but writes to the CLI's UI, not to the artifact, and the agent that used
# it then omitted `findings` entirely. Both must be denied by name.
DISALLOWED_TOOLS = [
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Task", "Agent", "TodoWrite", "ReportFindings",
    "Workflow", "Skill", "ToolSearch", "SendMessage",
    "TaskCreate", "TaskUpdate", "TaskStop", "TaskGet", "TaskList", "TaskOutput",
    "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    "EnterWorktree", "ExitWorktree",
]

# Both bounds fail closed: exceeding either yields no accepted submission, so
# run() returns non-zero with no artifact.
#
# The FALLBACK ceiling, for a run with no deadline; otherwise turn_ceiling() derives
# it from the attempt's clock. The two are co-limits on how far the reviewer may
# investigate and only the clock has a measured rationale, so the clock must bind
# first -- exceeding this reports a constraint nobody chose. ADR-0015 has the
# argument; test_workflow_shape asserts the ordering over the constant and the
# derivation both.
MAX_TURNS = int(os.environ.get("CC_MAX_TURNS", "45"))

# PER ATTEMPT, and likewise the FALLBACK: with a deadline set, attempt_budget()
# sizes each attempt from the time left instead.
#
# What it bounds is REASONING VOLUME, not latency. 150, 420 and 600 were each set or
# raised against a misread measurement -- a per-event `usage.output_tokens`, which is
# a streaming snapshot (see serialize_message): a run reading as "3 to 9 output
# tokens per turn" emitted 29,533, with duration_api_ms 397,726 of a 396,325ms run,
# so every millisecond was the provider generating at a normal 74 tok/s. 900 covers
# the ~45k tokens a review that explores harder spends; 600 sat on the boundary and
# lost twice in production.
#
# `effort` is the cheaper lever and is deliberately not taken: it is unset, so the
# CLI resolves it per model, and lowering it trades review depth for wall clock.
#
# Reachable as an environment variable, which serves direct invocation only -- a
# caller of a reusable workflow cannot inject env into the callee's jobs. What a
# consumer sets is TIMEOUT_INPUT.
WALL_CLOCK_SECONDS = int(os.environ.get("CC_WALL_CLOCK_SECONDS", "900"))

# THREE, not four. Attempts defend against API errors, where a retry is cheap and
# independent; a wall-clock timeout is not that kind of failure, so depth per
# attempt is worth more than another shallow one. Three 15-minute attempts fit the
# 50-minute job with headroom to spare, where four would cap each at 675s.
MAX_ATTEMPTS = 3

# Submissions are bounded separately from API-error attempts now that a
# rejected submission retries in-session rather than consuming a whole CLI
# invocation. Four keeps the old budget: at most three rejections, then the
# final one either lands or the run fails. Counted over the RUN, so the
# ceiling is four submissions however many sessions they are spread across.
MAX_SUBMISSIONS = 4

# Doubled per attempt, so the budget spans 1s + 2s + 4s of waiting.
API_ERROR_BACKOFF_SECONDS = float(os.environ.get("CC_API_ERROR_BACKOFF_SECONDS", "1"))

# The consumer's budget (ADR-0015). TIMEOUT_INPUT is a `workflow_call` input --
# the only channel a caller of a reusable workflow has -- and it governs the agent
# job's `timeout-minutes` and the deadline below from ONE number.
#
# The deadline crosses a job boundary, so it is joined by YAML and by nothing else:
# this pairing gets decline.OUTPUT_ENV's treatment, asserted against both workflows
# in both directions. That pin is what makes the tolerated-absent fallback safe, a
# workflow that forgot the variable being indistinguishable at runtime from direct
# invocation.
TIMEOUT_INPUT = "agent-timeout-minutes"
DEADLINE_ENV = "SMTITHY_AGENT_DEADLINE"

# Held back for the steps after the generator, which assemble and upload the
# bundle: a generator spending the whole job leaves the failure path nowhere to
# write the transcript. Setup is NOT held back -- the deadline is stamped by the
# job's first step, so setup is spent inside the consumer's number.
POST_GENERATOR_RESERVE_SECONDS = 120

# Twice the ~40s a whole pinned eval session costs, the smallest complete session
# measured. Below it an attempt cannot reach a submission, so starting one reports
# a timeout where the honest reason is a spent budget. Applied to a DERIVED budget
# only: a remainder can be arbitrarily small, where a configured
# CC_WALL_CLOCK_SECONDS is what an operator said the budget is.
MIN_ATTEMPT_SECONDS = 90

# One production run, kept as the measurement rather than as a rate so that
# re-measuring moves everything derived from it.
MEASURED_TOOL_CALLS = 21
MEASURED_TOOL_CALL_SECONDS = 618

# How far the turn ceiling stays above the calls the clock permits: the ratio the
# hand-set pair already had (45 turns against the ~31 that 900s buys).
TURN_HEADROOM = 1.5

# GitHub kills any hosted-runner job at 6 hours whatever `timeout-minutes` says, and
# both agent jobs are `runs-on: ubuntu-latest`, so a deadline past this is one the
# platform will not honour -- the generator would be killed mid-attempt with the
# transcript unwritten. Clamped rather than refused: a consumer over the platform's
# limit still wants their reviews to run. The clamp measures from the generator's
# start rather than the job's, so it is approximate by the setup duration; the
# alternative is a second value crossing the job boundary.
GITHUB_JOB_LIMIT_SECONDS = 6 * 60 * 60


def session_deadline() -> float | None:
    """When the generator must be finished, on the MONOTONIC clock, or None.

    None means no deadline was set, which is direct invocation. A deadline that is
    present and unreadable RAISES instead: absence is a caller that never had a
    budget, garbage is broken wiring, and falling back there would run a consumer's
    review on a budget they did not set and say nothing.

    Converted to monotonic time once, here, because anyio.fail_after measures
    against a monotonic clock and this arrives as a wall-clock epoch second.
    """
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    if not raw:
        return None
    try:
        epoch = int(raw)
    except ValueError:
        raise Rejection(
            f"{DEADLINE_ENV} is {raw!r}, which is not an epoch second. The agent job's "
            f"first step computes it from the {TIMEOUT_INPUT} input, so a value that "
            f"cannot be read means that wiring is broken; falling back to "
            f"{WALL_CLOCK_SECONDS}s would hide it behind a budget nobody chose."
        ) from None
    remaining = min(epoch - time.time(), GITHUB_JOB_LIMIT_SECONDS)
    return time.monotonic() + remaining


def attempt_budget(deadline: float | None) -> float:
    """The wall clock one attempt may spend, in seconds.

    From time REMAINING rather than `timeout-minutes` divided by MAX_ATTEMPTS
    (ADR-0015): a wall-clock timeout does not retry, so dividing reserves two thirds
    of every job for a worst case never observed. Call it per attempt — an attempt
    that ended early returns its unused time to the next.

    May return less than MIN_ATTEMPT_SECONDS, or a negative value; what to do about
    a budget too small to use is the caller's decision.
    """
    if deadline is None:
        return float(WALL_CLOCK_SECONDS)
    return deadline - time.monotonic() - POST_GENERATOR_RESERVE_SECONDS


def turn_ceiling(budget_seconds: float) -> int:
    """The turn limit for an attempt holding `budget_seconds` of wall clock.

    Kept above what the clock permits, so the clock binds first and a review that
    runs out of room reports the budget the consumer set. Proportional to a
    consumer-supplied number, which ADR-0015 accepts because the turn count bounds
    cost and runaway rather than what a review can reach.
    """
    calls_the_clock_permits = budget_seconds * MEASURED_TOOL_CALLS / MEASURED_TOOL_CALL_SECONDS
    return max(1, ceil(calls_the_clock_permits * TURN_HEADROOM))


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


def configured_model() -> str | None:
    """The model this run was CONFIGURED to use, or None if nothing named one.

    CC_MODEL is build_options' own override and is set by nothing in either
    workflow, so reading it alone recorded model_id="default" on every production
    run — a placeholder in the audit trail that exists specifically to make runs
    comparable, hiding a model swap between two runs. ANTHROPIC_MODEL is what the
    workflows actually set (from the cli-model input), so it is the fallback.

    None rather than "default" when neither is set: "default" is a claim about a
    value nobody supplied, and the honest record is that there was none. What
    ACTUALLY answered is separate and only knowable mid-session — the
    AssistantMessage's model, logged at run_complete and written to
    run_metadata.json for the executor's attribution stamp.
    """
    return os.environ.get("CC_MODEL") or os.environ.get("ANTHROPIC_MODEL") or None


def tool_guidance(base_root: Path, pr_root: Path) -> str:
    """Append the two filesystem roots, which are runtime paths the prompt
    file cannot state. How to use them is the prompt's own business."""
    return (
        "\n\n## The two roots in this session\n\n"
        f"- BASE (trusted, pre-change): `{base_root}`\n"
        f"- PR HEAD (contributor-authored data under review): `{pr_root}`\n"
    )


def nested_artifact_note(args) -> str:
    """Name the leak shape when a review submission carries it, or ''.

    The generic missing-keys reason measurably fails to dislodge this mode:
    two live runs resubmitted the identical shape three times against it
    (docs/findings/0001 has the XML-dialect ancestor). The model composes
    the review correctly and fumbles the serialization layer, so the fix
    is feedback that names the layer. Only fires on evidence — a missing
    field plus serialization markup in the summary — because falsely
    telling a model its complete artifact is nested induces the very
    degradation it warns about (see run_evals.INJECTED_REJECTION_REASON).
    """
    summary = args.get("summary") if isinstance(args, dict) else None
    if not isinstance(summary, str) or (isinstance(args, dict) and "findings" in args):
        return ""
    markers = ('"findings"', "'findings'", "<parameter", "</summary>")
    if not any(marker in summary for marker in markers):
        return ""
    return (
        "\n\nYour summary contains the rest of the review serialized as text. "
        "Do not serialize fields inside other fields: pass `findings` as its "
        "own array argument and `residual_risk` as its own string argument, "
        "with `summary` holding only your prose summary."
    )


def make_submit_tool(schema: dict, state: dict, transcript: Transcript, verify_fn, diff_text: str,
                     changed_files: list[str], policy: dict, guidance: str,
                     tool_name: str = "submit_review", noun: str = "review",
                     note_fn=nested_artifact_note):
    """The submission channel: verify in-process, answer with the real reason.

    Returning `is_error` sends the text back as tool feedback, so the model
    retries in the SAME session with its investigation intact — the property
    the old flow needed --resume plumbing for. Fail-closed throughout: nothing
    is recorded as accepted except through verify_fn.

    One breaker for every channel: the plan generator passes its own
    tool_name/noun and a verify_fn that closes over its content source, so the
    spiral-bounding logic cannot drift between the two generators. note_fn
    inspects a rejected submission for a channel-specific leak shape (see
    nested_artifact_note); pass None for channels without a known one.
    """

    def error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def log_safely(event: str, **fields) -> None:
        """Record a failed submission without letting the record become the
        failure.

        Both failure paths log before they spend, and a raising log would leave
        the handler with the breaker unadvanced -- so the model resubmits, the
        budget never runs out, and the run dies at the turn limit naming the turn
        budget as the fault. Counting the submission matters more than describing
        it: a lost record costs an audit line, an uncounted failure costs the run.
        """
        try:
            transcript.log(event, **fields)
        except Exception as exc:  # noqa: BLE001 - the record is best-effort, the count is not
            print(f"::warning::could not record {event}: {type(exc).__name__}", file=sys.stderr)

    def spend(reason: str) -> bool:
        """Count one failed submission against the breaker. True if it aborts.

        One budget for every way a submission can fail, because the spiral the
        breaker exists to stop is the same either way: the model resubmits, and
        an uncounted failure loops until the turn limit — which then reports the
        turn budget as the fault.
        """
        fingerprint = rejection_fingerprint(reason)
        state["repeated"] = state["repeated"] + 1 if fingerprint == state["last_fingerprint"] else 1
        state["last_fingerprint"] = fingerprint
        if state["repeated"] >= MAX_REPEATED_REJECTIONS or state["round"] >= MAX_SUBMISSIONS:
            state["abort_reason"] = reason
            return True
        return False

    @tool(
        name=tool_name,
        description=f"Submit the completed {noun} artifact. Call exactly once, when your {noun} is final.",
        input_schema=schema,
    )
    async def submit(args):
        state["round"] += 1
        if state["accepted"] is not None:
            return error(f"A {noun} has already been accepted; this submission was not saved.")
        if state["abort_reason"]:
            return error("The run is aborted; this submission was not saved.")
        try:
            verify_fn(args, diff_text, changed_files, policy)
        except Rejection as exc:
            log_safely("submit_rejected", round=state["round"], reason=str(exc), artifact=args)
            # Same breaker as before the port: a run repeating one failure
            # degrades into a placeholder that passes; fail loud instead.
            if spend(f"final submission rejected: {exc}"):
                return error(
                    f"Your {noun} was rejected by the verifier: {exc}\n\n"
                    f"The submission budget is exhausted; the run is aborted and no {noun} will be posted."
                )
            note = note_fn(args) if note_fn is not None else ""
            return error(f"Your {noun} was rejected by the verifier: {exc}{note}\n\n{guidance}")
        except Exception as exc:
            # Anything the handler itself broke on: a malformed submission
            # reaching an unguarded index, a transcript write failing on a full
            # disk. The model cannot act on it, so the only thing that must
            # happen is that it counts — an uncounted failure returns an opaque
            # tool error and the model resubmits the same shape until the turn
            # limit, which then names the turn budget as the fault.
            #
            # Never phrased as a verifier rejection: telling a model its valid
            # artifact violated policy is the false-specifics failure mode
            # run_evals.INJECTED_REJECTION_REASON records, which induces the
            # degradation spiral rather than a retry.
            detail = f"{type(exc).__name__}: {exc}"
            log_safely("submit_failed", round=state["round"], error=detail, artifact=args)
            aborted = spend(f"submission handler failed: {detail}")
            tail = (
                f"The submission budget is exhausted; the run is aborted and no {noun} will be posted."
                if aborted
                else "Submit the same complete artifact again."
            )
            return error(f"Your {noun} could not be processed: an error inside the harness. {tail}")
        state["accepted"] = args
        return {"content": [{"type": "text", "text": f"{noun.capitalize()} accepted and recorded. Do not submit again."}]}

    return submit


def build_review_server(submit, name: str = "review") -> dict:
    """Wrap the submit tool in an in-process MCP server, WITHOUT the MCP
    layer's own input validation. `name` is the MCP server name — the plan
    generator reuses this wrapper verbatim under "plan" (the no-validation
    property is the point, and it is channel-agnostic).

    The SDK's create_sdk_mcp_server validates arguments against the input
    schema before the handler runs, and a leak-shaped submission (everything
    serialized into `summary`, `findings` absent) then bounces off it with the
    same generic "'findings' is a required property" the CLI's structured
    output gave — observed live: 16 identical bounces until the wall clock
    killed the run, because the handler's breaker never saw a single one.
    verify() rejects the same shapes with a reason the model can act on, so
    validation is delegated to it: every submission must reach the handler.
    """
    server = Server(name, version="1.0.0")
    tool_def = Tool(name=submit.name, description=submit.description, inputSchema=submit.input_schema)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [tool_def]

    @server.call_tool(validate_input=False)
    async def call_tool(tool_name: str, arguments: dict) -> CallToolResult:
        result = await submit.handler(arguments)
        content = [TextContent(type="text", text=item["text"]) for item in result["content"]]
        return CallToolResult(content=content, isError=result.get("is_error", False))

    return {"type": "sdk", "name": name, "instance": server}


def build_options(system_prompt: str, base_root: Path, pr_root: Path, server,
                  server_name: str = "review", submit_tool: str = SUBMIT_TOOL,
                  max_turns: int | None = None) -> ClaudeAgentOptions:
    """`max_turns` None means the module constant; drive_session passes the ceiling
    derived from the attempt's own clock. Resolved in the body rather than as a
    default argument so the constant stays live."""
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={server_name: server},
        allowed_tools=[*READONLY_TOOLS, submit_tool],
        disallowed_tools=DISALLOWED_TOOLS,
        max_turns=MAX_TURNS if max_turns is None else max_turns,
        permission_mode="dontAsk",
        cwd=str(base_root),
        add_dirs=[str(base_root), str(pr_root)],
        model=os.environ.get("CC_MODEL"),
        # No project CLAUDE.md, skills, hooks, plugins or ambient MCP servers:
        # the review must depend on the prompt we audit, not on ambient config.
        setting_sources=[],
        strict_mcp_config=True,
        extra_args={"safe-mode": None},
    )


def _note_api_turn(message, state: dict) -> None:
    """Record one AssistantMessage against the session's API-call accounting.

    Deduplicated by `message_id` because the SDK yields one event per content
    block, so counting events over-counts API calls by two to four times.
    `cache_read_input_tokens` is taken as a PEAK rather than a sum: it is
    cumulative over the conversation already, so summing the snapshots
    multiple-counts the whole cached prefix on every turn.
    """
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        state["api_message_ids"].add(message_id)
    usage = getattr(message, "usage", None) or {}
    cache_read = usage.get("cache_read_input_tokens") or 0
    if cache_read > state["cache_read_peak"]:
        state["cache_read_peak"] = cache_read


def serialize_message(message) -> str:
    """One JSONL line per SDK message, for the captured-stream artifact.

    The SDK consumes the CLI's raw stdout itself, so what we capture is its
    typed messages re-serialized. `default=str` because a message may carry
    non-JSON values; a lossy field beats a lost line when diagnosing.

    `ts` is when the line was CAPTURED, not a field of the message, and it is
    what makes the stream answer "where did the wall clock go": without it the
    only timestamps are the transcript's tool_request records, so any turn that
    ends without a tool call is invisible and the gap before it gets attributed
    to whichever call happens to follow.

    An AssistantMessage's `usage` is a STREAMING SNAPSHOT and MUST NOT be
    summed. The SDK yields one event per content block, so a single API message
    appears two to four times carrying the usage it had mid-generation, and
    thinking text is stripped by the redaction pass — so the events of a turn
    that emitted thousands of thinking tokens all read as two to nine
    `output_tokens` with empty `thinking`. Summing them under-reported one real
    run by 200x (148 against the ResultMessage's 29,533) and that arithmetic is
    what produced a diagnosis of "provider latency" for what was ordinary
    generation at 74 tok/s. Group by `message_id` to count real API calls, and
    take totals from `session_usage` (below) or the ResultMessage. Never from
    these.
    """
    record = {"type": type(message).__name__}
    if dataclasses.is_dataclass(message):
        record.update(dataclasses.asdict(message))
    record["ts"] = time.time()
    return json.dumps(record, ensure_ascii=False, default=str)


async def _run_session(user_message: str, options: ClaudeAgentOptions, transcript: Transcript,
                       state: dict, output_dir: Path, attempt: int, policy: dict,
                       budget_seconds: float) -> ResultMessage | None:
    """One CLI session. Returns its ResultMessage, or None if the stream ended
    without one. The captured stream is written even when the session dies —
    a partial stream is precisely the evidence a hang or crash leaves.

    `budget_seconds` is this attempt's clock, passed rather than read from the
    module so a session cannot run on a budget the loop did not grant it."""
    lines: list[str] = []
    result = None
    try:
        with anyio.fail_after(budget_seconds):
            async for message in query(prompt=user_message, options=options):
                lines.append(serialize_message(message))
                if isinstance(message, AssistantMessage):
                    # The model that ACTUALLY answered, which is the only
                    # honest source for the posted attribution: the two arms
                    # are configured by different inputs, and configuration
                    # cannot say which one ran.
                    state["model"] = message.model
                    _note_api_turn(message, state)
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            transcript.log("tool_request", round=attempt, tool=block.name, input=block.input)
                            state["tool_calls"] += 1
                if isinstance(message, ResultMessage):
                    result = message
                    # The aggregate, in the SMALL file. It is already in the
                    # captured stream, but that stream is a few hundred KB of
                    # redacted messages whose per-event usage reads as a
                    # fraction of the truth, so the numbers that answer "was
                    # this slow, and why" were effectively unfindable.
                    # duration_api_ms against duration_ms is the one that
                    # settles it: they were 397726 against 396325 on a real
                    # run, so the wall clock was the provider generating, not
                    # the harness waiting on anything local.
                    usage = getattr(message, "usage", None) or {}
                    transcript.log(
                        "session_usage",
                        round=attempt,
                        duration_ms=getattr(message, "duration_ms", None),
                        duration_api_ms=getattr(message, "duration_api_ms", None),
                        num_turns=getattr(message, "num_turns", None),
                        api_messages=len(state["api_message_ids"]),
                        tool_calls=state["tool_calls"],
                        output_tokens=usage.get("output_tokens"),
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                        total_cost_usd=getattr(message, "total_cost_usd", None),
                        stop_reason=getattr(message, "stop_reason", None),
                    )
    finally:
        # Redacted before being written, not after: the whole output_dir is
        # uploaded as a CI artifact, so writing the raw capture would route a
        # credential the agent surfaced around the transcript's secret scan.
        #
        # Guarded, because this block's purpose is that the capture survives a
        # session that died: an exception raised HERE would replace the one
        # propagating, so a full disk would be reported instead of the wall clock
        # that fired. The capture is evidence about the failure, never a reason to
        # lose it. encoding is explicit for the reason read_harness_text's is —
        # the locale's would decide how the artifact is encoded.
        try:
            text = "\n".join(lines) + ("\n" if lines else "")
            (output_dir / f"cc_stream_{attempt}.jsonl").write_text(
                redact_text(text, policy), encoding="utf-8"
            )
        except OSError as exc:
            print(f"could not write the captured stream: {exc}", file=sys.stderr)
    return result


def fail(transcript: Transcript, reason: str, **fields) -> int:
    """Log a terminal failure, echo it to stderr, close the transcript, return 1.

    The echo is why this exists: the transcript lands in an uploaded artifact, so
    without it a failed run leaves an EMPTY job log and the reason is only
    visible to someone who thinks to download the artifact.

    The reason is a Rejection message on several paths, and a Rejection
    interpolates the value it refused, so the echo goes through the transcript's
    own policy — a job log has its own retention and audience.
    """
    transcript.log("run_failed", reason=reason, **fields)
    print(f"::error::ai-review generator failed: {transcript.redact(reason)}", file=sys.stderr)
    transcript.close()
    return 1


def start_session_on(verify_fn) -> None:
    """Tell a verify_fn a new session is starting, if it cares.

    The eval harness's fault injector has a per-session budget and needs to know
    where a session begins; production's verify() has no state and no hook. The
    notification lives here rather than in the harness because only this loop
    knows when it restarts the CLI.
    """
    if new_session := getattr(verify_fn, "new_session", None):
        new_session()


def drive_session(*, transcript: Transcript, policy: dict, system_prompt: str, user_message: str,
                  base_root: Path, pr_root: Path, output_dir: Path, make_tool, verify_fn,
                  server_name: str = "review", submit_tool_name: str = SUBMIT_TOOL,
                  artifact_filename: str = "review.json", tool_display_name: str = "submit_review") -> int:
    """The generator-agnostic session loop: attempts, backoff, failure naming,
    stream capture, and the fail-closed artifact write. Everything specific to
    a channel — what the tool verifies, what the artifact is called — arrives
    through the parameters, so the plan generator runs THIS loop rather than a
    diverging copy of it. `make_tool` is called once per attempt with the run's
    state dict and returns the submit tool.

    `verify_fn` is passed in addition to being closed over by `make_tool`
    because this loop is what decides where a session begins, and a stateful
    verifier (the eval harness's fault injector) has to be told.

    The quarantine assertion lives here, not in the callers: this is the function
    that grants `pr_root` to a session, so every channel inherits the refusal
    rather than each caller remembering to make it."""
    try:
        assert_no_symlinks(pr_root, transcript)
    except Rejection as exc:
        return fail(transcript, str(exc))

    # Once, before the loop: the conversion to monotonic time is a startup fact.
    try:
        deadline = session_deadline()
    except Rejection as exc:
        return fail(transcript, str(exc))

    # The submission breaker is scoped to the RUN, not to a session: its budget
    # and its abort verdict are properties of the one artifact being produced,
    # so an api_error retry inherits them rather than being forgiven them.
    state = {
        "round": 0, "repeated": 0, "last_fingerprint": None,
        "accepted": None, "abort_reason": None, "tool_calls": 0, "model": None,
        # Counted by message_id rather than by event, for serialize_message's
        # reason: one API message arrives as several events. This is the only
        # turn count a session that DIES still has — a ResultMessage carries
        # num_turns and a timed-out session has no ResultMessage at all, which
        # is precisely the run whose cost needs accounting for.
        "api_message_ids": set(),
        "cache_read_peak": 0,
    }
    # An acceptance is a verdict the verifier already reached, so it outlives the
    # session that produced it: a later attempt cannot unmake it, and no ending
    # short of the breaker's may discard it. Held here rather than in `state`
    # because the per-attempt reset below is deliberate — leaving `state`
    # populated would make session 2's handler refuse every submission with "a
    # review has already been accepted" and burn its turns.
    verified: dict | None = None

    def deliver(artifact: dict) -> int:
        """Write the artifact and close the run successfully.

        One spelling for every ending that reaches an accepted artifact, so a new
        ending inherits the metadata write and the completion records instead of
        reproducing them.
        """
        (output_dir / artifact_filename).write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Written by the arm that ran, for the executor to stamp into what it
        # posts. Configuration cannot supply this: both arms are configured on
        # every run and only one of them invoked a model.
        (output_dir / "run_metadata.json").write_text(
            json.dumps({"model": state["model"]}), encoding="utf-8"
        )
        transcript.log("artifact", sha256=sha256(json.dumps(artifact, sort_keys=True)))
        # The model that answered joins the completion record, so the transcript
        # carries both halves of the attribution: what was configured (run_start)
        # and what actually ran.
        transcript.log("run_complete", rounds=state["round"], model=state["model"])
        transcript.close()
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        budget = attempt_budget(deadline)
        if deadline is not None and budget < MIN_ATTEMPT_SECONDS:
            # An artifact already verified is still the run's answer, as at every
            # other ending here; otherwise the failure names the budget rather than
            # letting a doomed attempt report a timeout it was started into.
            if verified is not None:
                return deliver(verified)
            return fail(
                transcript,
                f"{budget:.0f}s of the review budget remain, which cannot reach a "
                f"submission, so attempt {attempt} was not started. Raise the "
                f"`{TIMEOUT_INPUT}` input on the calling workflow if reviews of this "
                "repository need longer.",
                attempt=attempt,
                tool_calls=state["tool_calls"],
            )
        turns = turn_ceiling(budget)
        # What room the attempt HAD. session_usage records what it spent, and only a
        # timeout records what it was allowed, so a run that submitted under budget
        # pressure was indistinguishable from one with room to spare.
        transcript.log(
            "attempt_budget",
            round=attempt,
            seconds=round(budget),
            max_turns=turns,
            consumer_set=deadline is not None,
        )

        # What a restarted session does start over on: it may submit again, and
        # its tool calls are counted against it alone.
        state["accepted"] = None
        state["tool_calls"] = 0
        state["api_message_ids"] = set()
        state["cache_read_peak"] = 0
        start_session_on(verify_fn)
        submit = make_tool(state)
        server = build_review_server(submit, server_name)
        options = build_options(system_prompt, base_root.resolve(), pr_root.resolve(), server,
                                server_name, submit_tool_name, turns)

        timed_out = False
        try:
            result = anyio.run(_run_session, user_message, options, transcript, state, output_dir,
                               attempt, policy, budget)
        except TimeoutError:
            # Recorded here and decided below: the session was cut off, but it may
            # already have got an artifact through the verifier, and this branch
            # runs before the breaker gate that outranks an acceptance.
            timed_out, result = True, None
            transcript.log(
                "wall_clock_timeout",
                round=attempt,
                seconds=round(budget),
                tool_calls=state["tool_calls"],
                # What the killed session spent, as far as it can be known
                # WITHOUT a ResultMessage — which is the whole problem with
                # this branch: the aggregate lives on a message that a session
                # cut off by the wall clock never emits, so the run most in
                # need of accounting was the one that recorded none. Turns and
                # tool calls put a floor under "how much work was in flight";
                # output tokens are deliberately absent rather than guessed,
                # because the only per-event figures available are streaming
                # snapshots and summing them is the error this instrumentation
                # exists to stop.
                api_messages=len(state["api_message_ids"]),
                cache_read_peak=state["cache_read_peak"],
            )
        except ProcessError as exc:
            return fail(
                transcript,
                f"claude CLI exited {exc.exit_code} without a result envelope",
                stderr=(exc.stderr or "")[-2000:],
            )
        except ClaudeSDKError as exc:
            return fail(transcript, f"claude-agent-sdk error: {exc}")

        # Promoted the moment the session ends, so every branch below decides
        # against the run's acceptance rather than this session's.
        if state["accepted"] is not None:
            verified = state["accepted"]

        # Checked before the session is classified, because the breaker's
        # verdict outranks how the session happened to end: a run told it was
        # aborted must not be resurrected by a retry the classification allows.
        # The session's own ending is recorded alongside it, since that is the
        # telemetry the branches below would otherwise have logged.
        if state["abort_reason"]:
            return fail(
                transcript,
                state["abort_reason"],
                attempt=attempt,
                subtype=result.subtype if result is not None else None,
                terminal_reason=result.terminal_reason if result is not None else None,
                tool_calls=state["tool_calls"],
            )

        # A turn-limit exit is an error subtype but DOES carry a result
        # envelope, so name it: a generic failure would send a reader hunting
        # for a CLI fault when the real cause is a bound we set.
        #
        # Only when nothing was accepted. A session may submit, be verified, and
        # then spend its remaining turns re-reading the diff — the artifact is
        # already recorded, and "hit the turn limit WITHOUT calling
        # {tool_display_name}" would be a false reason for a run that succeeded.
        # The acceptance is checked here and not before the breaker above,
        # because an aborted run must not be resurrected by one.
        if result is not None and result.subtype == "error_max_turns" and verified is None:
            return fail(
                transcript,
                f"agent hit the {turns}-turn limit without calling {tool_display_name}",
                num_turns=result.num_turns,
                tool_calls=state["tool_calls"],
            )

        # A cut-off session, decided on the same rule as the turn limit: the
        # artifact if one is verified, the failure if none is. Placed after the
        # breaker gate so an exhausted budget still outranks it.
        if timed_out:
            if verified is None:
                # Names the input when the budget was the consumer's, because this
                # is the one failure that reports itself and the remedy is theirs.
                remedy = f"; raise the `{TIMEOUT_INPUT}` input" if deadline is not None else ""
                return fail(
                    transcript,
                    f"wall-clock timeout after {budget:.0f}s on attempt {attempt}{remedy}",
                    tool_calls=state["tool_calls"],
                )
            return deliver(verified)

        # Reports `subtype: success` with no submission, so it must be matched
        # on terminal_reason or a dead run counts as a successful one. No
        # session resume on retry, since the session may have died mid-turn.
        if result is not None and result.terminal_reason == "api_error":
            detail = str(result.result or "")
            permanent = is_permanent_api_error(detail)
            retrying = not permanent and attempt < MAX_ATTEMPTS
            backoff = API_ERROR_BACKOFF_SECONDS * 2 ** (attempt - 1) if retrying else 0
            # One record per occurrence, so counting `event == "api_error"`
            # across transcripts gives the error rate and summing
            # `backoff_seconds` gives what the waiting cost.
            transcript.log(
                "api_error",
                round=attempt,
                reason=detail[:500],
                num_turns=result.num_turns,
                api_ms=result.duration_api_ms,
                wall_ms=result.duration_ms,
                retrying=retrying,
                backoff_seconds=backoff,
                tool_calls=state["tool_calls"],
            )
            # An error in the stream is not a verdict on an artifact the verifier
            # already accepted: this session may have submitted and then died on
            # the NEXT turn. Delivering beats retrying, since a retry can only
            # produce a second artifact for a run that already has one.
            if verified is not None:
                return deliver(verified)
            if retrying:
                time.sleep(backoff)
                continue
            if permanent:
                return fail(transcript, f"unretryable API error: {detail[:300]}")
            return fail(transcript, f"upstream API error on all {MAX_ATTEMPTS} attempts")

        if result is None:
            return fail(transcript, "stream ended without a result envelope")

        transcript.log(
            "model_response",
            round=attempt,
            # How the session ENDED, recorded even when an artifact was accepted:
            # a run that consistently reaches the turn limit is a budget worth
            # revisiting, and this is where that is visible.
            subtype=result.subtype,
            stop_reason=result.stop_reason,
            usage=result.usage,
            num_turns=result.num_turns,
            cost_usd=result.total_cost_usd,
            duration_ms=result.duration_ms,
            # With num_turns this makes ms-per-turn derivable, which is how a
            # throttled-but-successful run is distinguishable from a healthy one.
            api_ms=result.duration_api_ms,
            tool_calls=state["tool_calls"],
            model=state["model"],
            permission_denials=result.permission_denials,
        )

        if verified is None:
            return fail(
                transcript,
                f"agent completed without calling {tool_display_name} (subtype={result.subtype})",
                num_turns=result.num_turns,
            )

        return deliver(verified)

    # Reached only when every attempt was consumed by a retry. An artifact
    # verified along the way is still the run's answer.
    if verified is not None:
        return deliver(verified)
    return fail(transcript, "attempt budget exhausted without a verified artifact")


def find_symlinks(root: Path) -> list[Path]:
    """Every symlink at or under `root`, however deep.

    followlinks=False, so a linked directory is reported rather than descended
    into.
    """
    found: list[Path] = []
    for parent, directories, files in os.walk(root, followlinks=False):
        base = Path(parent)
        for name in (*directories, *files):
            if (base / name).is_symlink():
                found.append(base / name)
    return found


def assert_no_symlinks(pr_root: Path, transcript: Transcript) -> None:
    """Refuse to run the generator over a quarantine containing symlinks.

    git materialises mode-120000 entries from the head tree verbatim, and the
    generator is granted Read/Grep/Glob here — a permission check on the requested
    path cannot see where a link points. The workflow strips links when it
    materialises the tree; this asserts that happened, in the process about to
    hand the directory to the model.
    """
    links = find_symlinks(pr_root)
    if not links:
        return
    relative = sorted(str(link.relative_to(pr_root)) for link in links)
    transcript.log("quarantine_rejected", reason="symlinks in the reviewed tree", paths=relative)
    raise Rejection(
        f"quarantine contains {len(relative)} symlink(s) ({relative[:5]}): the reviewed tree is "
        "contributor-authored and a link would serve bytes from outside it to the generator. "
        "The workflow strips these when it materialises the quarantine."
    )


def run(base_root: Path, pr_root: Path, context_dir: Path, output_dir: Path, verify_fn=verify) -> int:
    """Return 0 with a verified review.json written, or non-zero with none.

    verify_fn is the eval harness's fault-injection seam; production passes none.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    policy_text = read_harness_text(POLICY_PATH)
    policy = json.loads(policy_text)
    transcript = Transcript(output_dir / "transcript.jsonl", policy)

    schema = build_artifact_schema(policy)
    # SMTITHY_PROJECT_DESCRIPTION is the consumer's own account of their
    # repository; absent, the assembled prompt is byte-identical to before
    # this seam existed, so the shipped default carries its eval history.
    system_prompt = (
        apply_project_description(read_harness_text(PROMPT_PATH), os.environ.get("SMTITHY_PROJECT_DESCRIPTION"))
        + render_constraints(policy)
        + tool_guidance(base_root.resolve(), pr_root.resolve())
    )

    transcript.log(
        "run_start",
        generator="claude-agent-sdk",
        model_id=configured_model(),
        prompt_sha256=sha256(system_prompt),
        policy_sha256=sha256(policy_text),
        max_rounds=MAX_SUBMISSIONS,
    )

    # Context assembly reads four contributor-adjacent files. A failure here used
    # to raise past the open transcript, leaving an uploaded artifact with no
    # reason in it and an empty job log.
    try:
        user_message = build_user_message(context_dir)
    except (OSError, ValueError, UnicodeError) as exc:
        return fail(transcript, f"cannot assemble the review context: {exc}")
    transcript.log("context", sha256=sha256(user_message), bytes=len(user_message.encode()))

    diff_text = read_contributor_text(context_dir / "diff.patch")
    changed_files = json.loads(read_harness_text(context_dir / "changed_files.json"))
    guidance = render_rejection_guidance(policy)

    return drive_session(
        transcript=transcript,
        policy=policy,
        system_prompt=system_prompt,
        user_message=user_message,
        base_root=base_root,
        pr_root=pr_root,
        output_dir=output_dir,
        make_tool=lambda state: make_submit_tool(
            schema, state, transcript, verify_fn, diff_text, changed_files, policy, guidance
        ),
        verify_fn=verify_fn,
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
