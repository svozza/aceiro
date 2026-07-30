"""Tests for the generator's session handling and artifact hygiene.

Running the CLI needs a real model; the submission tool's verify/reject/accept
logic, the failure classification, and redacting what gets uploaded do not —
and each is a place a bug is invisible until it matters. query() is faked with
scripted message streams; submit_review's handler is exercised directly, since
in production it runs in this process either way.
"""

import json
from pathlib import Path

import anyio
import cc_loop
import pytest
from artifact import build_artifact_schema, redact_text
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from conftest import POLICY

HARNESS_DIR = Path(__file__).parent.parent / "src" / "smtithy"
# base_root: the trusted pre-change tree the model may read. Upstream this was
# the staging repo's root, three levels above .github/scripts/ai_review. Here the
# harness IS the repo, so the repo root is base_root -- and since cc_loop no
# longer reads its policy or prompt from base_root (see artifact.PROMPT_PATH),
# nothing requires it to have any particular layout.
REPO_ROOT = Path(__file__).parent.parent

SCENARIO = HARNESS_DIR / "evals/scenarios/lru_eviction_bug"

FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def result_message(**overrides):
    fields = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=2,
        session_id="s",
        total_cost_usd=0.0,
        usage={},
        terminal_reason="completed",
    )
    fields.update(overrides)
    return ResultMessage(**fields)


def fake_query(messages):
    """A query() stand-in yielding a scripted message stream.

    Accepts a list of lists: one message stream per invocation, so retry
    behaviour (how many sessions run() starts) is scripted and observable.
    """
    streams = list(messages)
    calls = []

    async def _query(prompt, options):
        calls.append(options)
        for message in streams.pop(0):
            yield message

    _query.calls = calls
    return _query


def run_loop(tmp_path, monkeypatch, streams, verify_fn=None):
    monkeypatch.setattr(cc_loop, "query", fake_query(streams))
    kwargs = {"verify_fn": verify_fn} if verify_fn else {}
    code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path, **kwargs)
    return code


def transcript_events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]


class TestSubmitTool:
    """The submission channel: verify in-process, reject with the real reason,
    accept exactly once. Exercised directly — this is the code that replaced
    the CLI's opaque five-retry structured-output loop."""

    def make(self, verify_fn=lambda *a: None, diff="", files=()):
        state = {
            "round": 0, "repeated": 0, "last_fingerprint": None,
            "accepted": None, "abort_reason": None, "tool_calls": 0,
        }
        transcript_lines = []

        class FakeTranscript:
            def log(self, event, **data):
                transcript_lines.append({"event": event, **data})

        submit = cc_loop.make_submit_tool(
            build_artifact_schema(POLICY), state, FakeTranscript(), verify_fn,
            diff, list(files), POLICY, "guidance text",
        )
        return submit, state, transcript_lines

    def call(self, submit, args):
        return anyio.run(submit.handler, args)

    def test_a_valid_submission_is_accepted_and_recorded(self):
        submit, state, _ = self.make()
        artifact = {"summary": "ok", "findings": [], "residual_risk": ""}
        response = self.call(submit, artifact)
        assert not response.get("is_error")
        assert state["accepted"] == artifact

    def test_a_rejection_returns_the_verifier_reason_as_tool_feedback(self):
        from verify import Rejection

        def reject(*a):
            raise Rejection("summary: too long")

        submit, state, lines = self.make(verify_fn=reject)
        response = self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert response["is_error"]
        assert "summary: too long" in response["content"][0]["text"]
        assert "guidance text" in response["content"][0]["text"]
        assert state["accepted"] is None
        assert lines[0]["event"] == "submit_rejected"

    def test_repeating_one_failure_trips_the_breaker(self):
        from verify import Rejection

        def reject(*a):
            raise Rejection("summary: contains a link to 'evil.example'")

        submit, state, _ = self.make(verify_fn=reject)
        for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
            response = self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert state["abort_reason"], "same-class rejections must trip the breaker"
        assert "aborted" in response["content"][0]["text"]

    def test_varied_failures_do_not_trip_the_breaker_early(self):
        from verify import Rejection

        reasons = iter(["summary: too long", "findings[0]: unexpected keys ['x']", "residual_risk: bad link"])

        def reject(*a):
            raise Rejection(next(reasons))

        submit, state, _ = self.make(verify_fn=reject)
        for _ in range(3):
            self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        # Three DIFFERENT rejections is a converging run; only the submission
        # budget may end it, one short of MAX_SUBMISSIONS here.
        assert state["abort_reason"] is None

    def test_the_submission_budget_is_finite(self):
        from verify import Rejection

        counter = {"n": 0}

        def reject(*a):
            counter["n"] += 1
            raise Rejection(f"reason {'x' * counter['n']} varies")

        submit, state, _ = self.make(verify_fn=reject)
        for _ in range(cc_loop.MAX_SUBMISSIONS):
            self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert state["abort_reason"], "varied rejections must still exhaust the budget"

    def test_a_nested_submission_is_told_about_the_layer_not_just_the_keys(self):
        # The observed spiral: findings serialized as text inside summary,
        # resubmitted identically against the generic missing-keys reason.
        from verify import verify

        submit, _, _ = self.make(verify_fn=verify)
        nested = {"summary": 'prose... "findings": [{"path": "x.py"}] more', "residual_risk": ""}
        response = self.call(submit, nested)
        assert response["is_error"]
        assert "serialized as text" in response["content"][0]["text"]

    def test_a_merely_incomplete_submission_gets_no_nesting_accusation(self):
        # Falsely telling a model its artifact is nested induces degradation
        # (the run_evals INJECTED_REJECTION_REASON lesson) — so the note needs
        # evidence, not just a missing key.
        from verify import verify

        submit, _, _ = self.make(verify_fn=verify)
        response = self.call(submit, {"summary": "plain prose, nothing nested", "residual_risk": ""})
        assert response["is_error"]
        assert "serialized as text" not in response["content"][0]["text"]

    def test_a_second_submission_after_acceptance_is_refused(self):
        submit, state, _ = self.make()
        first = {"summary": "one", "findings": [], "residual_risk": ""}
        self.call(submit, first)
        response = self.call(submit, {"summary": "two", "findings": [], "residual_risk": ""})
        assert response["is_error"]
        assert state["accepted"] == first, "acceptance is first-wins"


class TestIsPermanentApiError:
    """The CLI reports every upstream failure as terminal_reason: api_error, so
    the message is the only signal. Retrying a misconfiguration cannot succeed
    and buries the reason under identical failures."""

    PERMANENT = [
        # The real 403 from PR #517: the IAM policy lacked the streaming action.
        (
            'API Error: 403 {"Message":"User: arn:aws:sts::1:assumed-role/r is not authorized to '
            'perform: bedrock:InvokeModelWithResponseStream"}'
        ),
        "AccessDeniedException: no identity-based policy allows this",
        "UnrecognizedClientException: security token is invalid",
        "ExpiredTokenException: the security token has expired",
        "ValidationException: the model could not be found",
    ]
    TRANSIENT = [
        "API Error: The operation timed out.",
        "API Error: 429 ThrottlingException: Too many requests",
        "API Error: 503 ServiceUnavailable",
        "API Error: 500 InternalServerException",
    ]

    def test_misconfiguration_is_permanent(self):
        for detail in self.PERMANENT:
            assert cc_loop.is_permanent_api_error(detail), detail

    def test_capacity_and_transport_errors_are_retryable(self):
        for detail in self.TRANSIENT:
            assert not cc_loop.is_permanent_api_error(detail), detail

    def test_empty_detail_is_treated_as_retryable(self):
        # No message is not evidence of misconfiguration; prefer the retry.
        assert not cc_loop.is_permanent_api_error("")


class TestRunFailureModes:
    def test_a_403_is_not_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: pytest.fail("must not back off"))
        stream = [result_message(
            terminal_reason="api_error",
            result="API Error: 403 not authorized to perform: bedrock:InvokeModel",
        )]
        # ONE scripted stream: a second session would pop an empty list and fail.
        assert run_loop(tmp_path, monkeypatch, [stream]) == 1
        assert not (tmp_path / "review.json").exists()
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "unretryable" in reasons[0]["reason"]

    def test_a_transient_error_is_retried_with_backoff(self, tmp_path, monkeypatch):
        waits = []
        monkeypatch.setattr(cc_loop.time, "sleep", waits.append)
        failing = [result_message(terminal_reason="api_error", result="API Error: 503 ServiceUnavailable")]
        artifact = {"summary": "ok", "findings": [], "residual_risk": ""}

        # The accepted artifact is recorded by the submit tool during the
        # session, so the fake second session must call the REAL handler the
        # way the SDK would. Spy on make_submit_tool to hold a reference.
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)

        streams = [failing, [result_message()]]
        calls = []
        real_fake = fake_query(streams)

        async def _query(prompt, options):
            calls.append(options)
            if len(calls) == 2:
                await created[-1].handler(artifact)
            async for message in real_fake(prompt, options):
                yield message

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert waits == [cc_loop.API_ERROR_BACKOFF_SECONDS]
        assert json.loads((tmp_path / "review.json").read_text()) == artifact

    def test_completing_without_a_submission_fails_closed(self, tmp_path, monkeypatch):
        assert run_loop(tmp_path, monkeypatch, [[result_message()]]) == 1
        assert not (tmp_path / "review.json").exists()
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "without calling submit_review" in reasons[0]["reason"]

    def test_a_turn_limit_exit_is_named(self, tmp_path, monkeypatch):
        stream = [result_message(subtype="error_max_turns", is_error=True)]
        assert run_loop(tmp_path, monkeypatch, [stream]) == 1
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "turn limit" in reasons[0]["reason"]

    def test_a_stream_without_a_result_envelope_fails_closed(self, tmp_path, monkeypatch):
        assert run_loop(tmp_path, monkeypatch, [[]]) == 1
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "without a result envelope" in reasons[0]["reason"]


class TestCapturedStreamIsRedacted:
    """The whole output dir is uploaded as a CI artifact, so the captured
    stream must pass the same secret scan as the transcript. Asserted through
    run() rather than against redact_text alone: testing the helper in
    isolation leaves 'run() forgot to call it' passing."""

    def run_with_messages(self, tmp_path, monkeypatch, messages):
        run_loop(tmp_path, monkeypatch, [messages])
        return (tmp_path / "cc_stream_1.jsonl").read_text()

    def test_a_key_in_the_stream_never_reaches_the_artifact(self, tmp_path, monkeypatch):
        messages = [
            AssistantMessage(content=[TextBlock(text=f"found {FAKE_KEY}")], model="m"),
            result_message(),
        ]
        written = self.run_with_messages(tmp_path, monkeypatch, messages)
        assert FAKE_KEY not in written
        assert "[REDACTED]" in written

    def test_tool_calls_are_captured_for_audit(self, tmp_path, monkeypatch):
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="1", name="Grep", input={"pattern": "popitem"})],
                model="m",
            ),
            result_message(),
        ]
        self.run_with_messages(tmp_path, monkeypatch, messages)
        events = transcript_events(tmp_path)
        tool_requests = [e for e in events if e["event"] == "tool_request"]
        assert tool_requests and tool_requests[0]["tool"] == "Grep"
        assert tool_requests[0]["input"] == {"pattern": "popitem"}


class TestRedactText:
    def test_redacts_a_key(self):
        assert FAKE_KEY not in redact_text(f"key {FAKE_KEY} here", POLICY)

    def test_leaves_innocent_text_alone(self):
        assert redact_text("nothing to see", POLICY) == "nothing to see"


class TestReviewServer:
    """The MCP layer must not pre-validate submissions.

    Observed live: a leak-shaped submission (everything serialized into
    `summary`, `findings` absent) bounced off the SDK server's own jsonschema
    check 16 times with the generic "'findings' is a required property" — the
    handler, and therefore the breaker, never saw one. The whole point of the
    tool channel is that verify() answers with an actionable reason and the
    breaker bounds the spiral, so every submission must REACH the handler.
    """

    def call_via_server(self, submit, arguments):
        import mcp.types as types

        server = cc_loop.build_review_server(submit)["instance"]
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="submit_review", arguments=arguments),
        )
        return anyio.run(handler, request).root

    def test_a_leak_shaped_submission_reaches_the_verifier_not_the_schema_check(self):
        from verify import verify

        submit, state, lines = TestSubmitTool().make(
            verify_fn=verify, diff="", files=[],
        )
        result = self.call_via_server(submit, {"summary": "the whole review, nested"})
        assert result.isError
        # The verifier's reason, not the MCP layer's "required property".
        assert "missing keys" in result.content[0].text
        assert state["round"] == 1, "the handler must see the submission"
        assert lines and lines[0]["event"] == "submit_rejected"

    def test_repeated_leak_shaped_submissions_trip_the_breaker(self):
        from verify import verify

        submit, state, _ = TestSubmitTool().make(verify_fn=verify, diff="", files=[])
        for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
            result = self.call_via_server(submit, {"summary": "nested again"})
        assert state["abort_reason"], "identical rejections must abort, not spiral to the wall clock"
        assert "aborted" in result.content[0].text

    def test_a_valid_submission_still_verifies_and_accepts(self, sample_diff, changed_files, valid_artifact):
        from verify import verify

        submit, state, _ = TestSubmitTool().make(
            verify_fn=verify, diff=sample_diff, files=changed_files,
        )
        result = self.call_via_server(submit, valid_artifact)
        assert not result.isError
        assert state["accepted"] == valid_artifact


class TestBundledCli:
    def test_the_sdk_bundles_the_pinned_cli_version(self):
        # The SDK wheel ships the CLI binary; this is the ONE place the
        # generator's version is asserted now that the npm pin is gone. An SDK
        # bump that changes the bundled CLI is a generator behaviour change and
        # must arrive as a deliberate edit here, evals re-run.
        from claude_agent_sdk._cli_version import __cli_version__

        assert __cli_version__ == "2.1.220"


class TestOptions:
    """The security-relevant invariants of the session configuration."""

    def options(self):
        submit, *_ = TestSubmitTool().make()
        server = cc_loop.build_review_server(submit)
        return cc_loop.build_options("prompt", REPO_ROOT, SCENARIO / "pr_root", server)

    def test_the_deny_list_survives_the_port(self):
        options = self.options()
        for name in ("Bash", "Write", "ReportFindings", "Workflow", "ToolSearch", "Task"):
            assert name in options.disallowed_tools

    def test_no_ambient_configuration_is_loaded(self):
        options = self.options()
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert "safe-mode" in options.extra_args

    def test_submit_review_is_allowed_by_its_full_mcp_name(self):
        assert cc_loop.SUBMIT_TOOL in self.options().allowed_tools
