"""Tests for the generator's session handling and artifact hygiene.

Running the CLI needs a real model; the submission tool's verify/reject/accept
logic, the failure classification, and redacting what gets uploaded do not —
and each is a place a bug is invisible until it matters. query() is faked with
scripted message streams; submit_review's handler is exercised directly, since
in production it runs in this process either way.
"""

import json
import shutil
import time
from pathlib import Path

import anyio
import cc_loop
import pytest
from artifact import build_artifact_schema, redact_secrets, redact_text
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
    scenario = tmp_path / "scenario"
    shutil.copytree(SCENARIO, scenario)
    kwargs = {"verify_fn": verify_fn} if verify_fn else {}
    code = cc_loop.run(REPO_ROOT, scenario / "pr_root", scenario / "context", tmp_path, **kwargs)
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

    def test_a_failing_transcript_write_still_counts_the_submission(self):
        # 0f12f81's invariant is "every failed submission is counted", and both
        # log() calls run BEFORE their spend() with nothing guarding them. The
        # comment on the bare except names a transcript write failing on a full
        # disk as exactly the case it exists to count, and it could not: a raising
        # log left the handler before the breaker advanced, so the model resubmits
        # until the turn limit and the run blames the turn budget for a disk fault.
        from verify import Rejection

        class BoomTranscript:
            def log(self, event, **data):
                raise OSError(28, "No space left on device")

        state = {
            "round": 0, "repeated": 0, "last_fingerprint": None,
            "accepted": None, "abort_reason": None, "tool_calls": 0,
        }
        submit = cc_loop.make_submit_tool(
            build_artifact_schema(POLICY), state, BoomTranscript(),
            lambda *a: (_ for _ in ()).throw(Rejection("summary: nope")),
            "", [], POLICY, "guidance",
        )
        for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
            self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert state["abort_reason"], "an unloggable rejection must still spend the budget"

    def test_a_recursion_error_while_logging_still_counts_the_submission(self):
        # The variant reachable from a submission alone, with the REAL Transcript
        # and the REAL verifier: verify() rejects, then the rejected artifact is
        # echoed into the record and redact_secrets recurses over it. No
        # filesystem manipulation needed, which is what makes this the live case.
        import tempfile

        from artifact import Transcript
        from verify import verify as real_verify

        deep = {"a": 1}
        for _ in range(3000):
            deep = {"a": deep}

        transcript = Transcript(Path(tempfile.mkdtemp()) / "t.jsonl", POLICY)
        state = {
            "round": 0, "repeated": 0, "last_fingerprint": None,
            "accepted": None, "abort_reason": None, "tool_calls": 0,
        }
        submit = cc_loop.make_submit_tool(
            build_artifact_schema(POLICY), state, transcript, real_verify, "", [], POLICY, "guidance",
        )
        for _ in range(cc_loop.MAX_SUBMISSIONS):
            self.call(submit, {"summary": "s" * 50, "findings": deep, "residual_risk": ""})
        transcript.close()
        assert state["abort_reason"], "a submission that breaks its own logging must still be counted"

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

    def test_a_handler_fault_is_counted_against_the_same_budget(self):
        # Only Rejection advanced the breaker, so any OTHER failure inside the
        # handler — a TypeError on a malformed submission, ENOSPC on the
        # transcript write — went back to the model as an opaque tool error with
        # nothing counting it. Measured: twelve identical faults, round 12, no
        # abort. The model resubmits the same shape until the turn limit, and the
        # run then blames the turn budget for a fault that is not the model's.
        def explode(*a):
            raise TypeError("'NoneType' object is not subscriptable")

        submit, state, lines = self.make(verify_fn=explode)
        for _ in range(cc_loop.MAX_SUBMISSIONS):
            response = self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert state["abort_reason"], "a non-Rejection fault must exhaust the same budget"
        assert state["accepted"] is None
        faults = [line for line in lines if line["event"] == "submit_failed"]
        assert faults and "TypeError" in faults[0]["error"], "the real fault must reach the audit trail"

    def test_a_handler_fault_does_not_read_as_a_verifier_rejection(self):
        # The model must not be told its artifact violated policy when the
        # harness broke: that is the false-specifics failure mode
        # run_evals.INJECTED_REJECTION_REASON exists to avoid, and it induces the
        # degradation spiral rather than a retry.
        def explode(*a):
            raise TypeError("boom")

        submit, _, _ = self.make(verify_fn=explode)
        response = self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert response["is_error"]
        text = response["content"][0]["text"]
        assert "rejected by the verifier" not in text
        assert "could not be processed" in text

    def test_repeated_handler_faults_trip_the_repeat_breaker(self):
        # Same fault every time is the same spiral a repeated rejection is, so it
        # ends on MAX_REPEATED_REJECTIONS rather than only on the budget.
        def explode(*a):
            raise TypeError("identical fault")

        submit, state, _ = self.make(verify_fn=explode)
        assert cc_loop.MAX_REPEATED_REJECTIONS < cc_loop.MAX_SUBMISSIONS
        for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
            self.call(submit, {"summary": "x", "findings": [], "residual_risk": ""})
        assert state["abort_reason"]

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


class TestIsSdkStreamError:
    """ADR-0019's predicate: only the SDK's own bare-Exception wrapper for a
    CLI that reported an error result may be treated as teardown noise. Every
    exception of ours is a subclass and must keep propagating — a crashed
    harness must never read as anything else."""

    def test_the_sdk_wrapper_shape_matches(self):
        exc = Exception(cc_loop.SDK_STREAM_ERROR_PREFIX + "success")
        assert cc_loop.is_sdk_stream_error(exc)

    def test_a_subclass_does_not_match_even_with_the_prefix(self):
        exc = TypeError(cc_loop.SDK_STREAM_ERROR_PREFIX + "success")
        assert not cc_loop.is_sdk_stream_error(exc)

    def test_an_unprefixed_bare_exception_does_not_match(self):
        # The reader wraps ALL its fatal errors (stream corruption, decode
        # failures) as bare Exception too, without the prefix. Those have no
        # envelope backing them and stay crashes.
        assert not cc_loop.is_sdk_stream_error(Exception("Fatal error in message reader: boom"))


class TestRunFailureModes:
    def test_secret_taint_runs_before_the_model_reads_context(self, tmp_path, monkeypatch):
        context = tmp_path / "context"
        pr_root = tmp_path / "pr_root"
        shutil.copytree(SCENARIO / "context", context)
        shutil.copytree(SCENARIO / "pr_root", pr_root)
        secret = "vby4471-qmt83e2-prod"
        with (context / "diff.patch").open("a", encoding="utf-8") as handle:
            handle.write(f'\n+password = "{secret}"\n')
        source = pr_root / "credential.py"
        source.write_text(f'password = "{secret}"\n', encoding="utf-8")
        prompts = []

        async def _query(prompt, options):
            prompts.append(prompt)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        out = tmp_path / "out"

        assert cc_loop.run(REPO_ROOT, pr_root, context, out) == 1
        assert secret not in prompts[0]
        assert "<SECRET_1:type=secret_keyword,length=20>" in prompts[0]
        assert secret not in source.read_text(encoding="utf-8")
        events = transcript_events(out)
        assert {"event": "secret_taint", "candidates": 1}.items() <= events[1].items()

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

    def test_the_run_records_the_model_that_actually_answered(self, tmp_path, monkeypatch):
        # The comment footer's attribution is this value. Taken from the
        # AssistantMessage rather than from configuration, because the two arms
        # are configured by different inputs and only one of them ran.
        created = []
        original = cc_loop.make_submit_tool
        monkeypatch.setattr(
            cc_loop, "make_submit_tool",
            lambda *a, **k: created.append(original(*a, **k)) or created[-1],
        )
        artifact = {"summary": "ok", "findings": [], "residual_risk": ""}

        async def _query(prompt, options):
            yield AssistantMessage(content=[TextBlock(text="working")], model="claude-sonnet-4-5")
            await created[-1].handler(artifact)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        assert cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path) == 0

        assert json.loads((tmp_path / "run_metadata.json").read_text())["model"] == "claude-sonnet-4-5"

    def test_a_tripped_breaker_survives_an_api_error_retry(self, tmp_path, monkeypatch):
        # The breaker's whole point is that a run repeating one failure fails
        # loud. A fresh per-attempt state would forgive the abort, and the next
        # attempt's placeholder would be written to review.json.
        from verify import Rejection

        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: None)
        placeholder = {"summary": "content-free placeholder", "findings": [], "residual_risk": ""}

        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)

        submissions = {"n": 0}

        def verify_fn(*_a):
            submissions["n"] += 1
            if submissions["n"] <= cc_loop.MAX_REPEATED_REJECTIONS:
                raise Rejection("findings[0]: line 5 is not inside a diff hunk")

        sessions = []

        async def _query(prompt, options):
            sessions.append(options)
            if len(sessions) == 1:
                for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
                    await created[-1].handler(placeholder)
                yield result_message(
                    terminal_reason="api_error", result="API Error: 503 ServiceUnavailable"
                )
            else:
                await created[-1].handler(placeholder)
                yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path,
                           verify_fn=verify_fn)
        assert code == 1
        assert not (tmp_path / "review.json").exists()
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert reasons and "final submission rejected" in reasons[0]["reason"]

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

    def test_an_accepted_artifact_survives_a_turn_limit_exit(self, tmp_path, monkeypatch):
        # A verified artifact was being discarded: the model submits on turn 27,
        # verify() accepts, it then spends its last turns double-checking and hits
        # the limit. The turn-limit branch was consulted before state["accepted"],
        # so the run failed with "hit the 30-turn limit without calling
        # submit_review" — a reason that is false, about a run that succeeded —
        # and wrote no review.json.
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)
        artifact = {"summary": "a complete review", "findings": [], "residual_risk": ""}

        async def _query(prompt, options):
            await created[-1].handler(artifact)
            yield result_message(subtype="error_max_turns", is_error=True)

        monkeypatch.setattr(cc_loop, "query", _query)
        assert cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path) == 0
        assert json.loads((tmp_path / "review.json").read_text()) == artifact

    def test_a_turn_limit_after_acceptance_is_still_recorded(self, tmp_path, monkeypatch):
        # Accepting the artifact must not hide that the session ran out of turns:
        # a run consistently ending this way is a budget worth revisiting, and the
        # transcript is where that is visible.
        created = []
        original = cc_loop.make_submit_tool
        monkeypatch.setattr(
            cc_loop, "make_submit_tool",
            lambda *a, **k: created.append(original(*a, **k)) or created[-1],
        )

        async def _query(prompt, options):
            await created[-1].handler({"summary": "ok", "findings": [], "residual_risk": ""})
            yield result_message(subtype="error_max_turns", is_error=True)

        monkeypatch.setattr(cc_loop, "query", _query)
        cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        events = transcript_events(tmp_path)
        assert [e for e in events if e["event"] == "run_complete"]
        responses = [e for e in events if e["event"] == "model_response"]
        assert responses and responses[0]["subtype"] == "error_max_turns"

    def test_a_turn_limit_with_no_artifact_still_fails(self, tmp_path, monkeypatch):
        # The complement: consulting state["accepted"] must not turn an empty
        # turn-limit exit into a success.
        stream = [result_message(subtype="error_max_turns", is_error=True)]
        assert run_loop(tmp_path, monkeypatch, [stream]) == 1
        assert not (tmp_path / "review.json").exists()

    def test_an_aborted_run_is_not_rescued_by_an_acceptance(self, tmp_path, monkeypatch):
        # Ordering: the breaker's verdict outranks an acceptance exactly as it
        # outranks the session's ending. A run told it was aborted must not be
        # resurrected because a later attempt got something through.
        from verify import Rejection

        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: None)
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)
        placeholder = {"summary": "placeholder", "findings": [], "residual_risk": ""}

        def verify_fn(*_a):
            raise Rejection("findings[0]: line 5 is not inside a diff hunk")

        async def _query(prompt, options):
            for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
                await created[-1].handler(placeholder)
            yield result_message(subtype="error_max_turns", is_error=True)

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path,
                           verify_fn=verify_fn)
        assert code == 1
        assert not (tmp_path / "review.json").exists()

    def test_an_accepted_artifact_survives_an_api_error_retry(self, tmp_path, monkeypatch):
        # 0f12f81 guarded the turn-limit exit only. Session 1 gets an artifact
        # through the verifier, then the stream reports api_error (which carries
        # subtype=success, so it is matched on terminal_reason); the loop retries
        # and the per-attempt reset erased the artifact the verifier had already
        # accepted. A review that passed every gate was lost.
        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: None)
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)
        artifact = {"summary": "a complete verified review", "findings": [], "residual_risk": ""}
        sessions = []

        async def _query(prompt, options):
            sessions.append(len(sessions) + 1)
            if len(sessions) == 1:
                await created[-1].handler(artifact)
                yield result_message(terminal_reason="api_error", result="API Error: 503")
            else:
                # Session 2 submits nothing at all: the artifact from session 1
                # is the only one this run ever produced. Reaching here at all
                # means the acceptance was thrown away.
                yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert json.loads((tmp_path / "review.json").read_text()) == artifact
        # No second session: a retry can only produce a second artifact for a run
        # that already has one, so the verified one ends the run.
        assert sessions == [1]

    def test_a_wall_clock_timeout_keeps_an_already_accepted_artifact(self, tmp_path, monkeypatch):
        # The likelier of the two in production: the model submits, is verified,
        # and burns its remaining seconds re-reading the diff to double-check
        # itself. WALL_CLOCK_SECONDS defaults to 150 and neither workflow sets it.
        monkeypatch.setattr(cc_loop, "WALL_CLOCK_SECONDS", 1)
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)
        artifact = {"summary": "verified before the clock ran out", "findings": [], "residual_risk": ""}

        async def _query(prompt, options):
            await created[-1].handler(artifact)
            await anyio.sleep(5)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert json.loads((tmp_path / "review.json").read_text()) == artifact
        # The timeout is still recorded: keeping the artifact must not hide that
        # the session was cut off.
        assert any(e["event"] == "wall_clock_timeout" for e in transcript_events(tmp_path))

    def test_a_wall_clock_timeout_with_no_artifact_still_fails(self, tmp_path, monkeypatch):
        # The complement, and the half that had no test at all before: consulting
        # the acceptance must not turn an empty timeout into a success.
        monkeypatch.setattr(cc_loop, "WALL_CLOCK_SECONDS", 1)

        async def _query(prompt, options):
            await anyio.sleep(5)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 1
        assert not (tmp_path / "review.json").exists()
        reasons = [e["reason"] for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert any("wall-clock timeout" in r for r in reasons)

    def test_an_aborted_run_is_not_rescued_by_a_timeout_acceptance(self, tmp_path, monkeypatch):
        # adcd943's ordering, applied to the new path: the breaker's verdict
        # outranks an acceptance, so a run that exhausted its budget must not
        # succeed because a timeout arrived after something got through.
        from verify import Rejection

        monkeypatch.setattr(cc_loop, "WALL_CLOCK_SECONDS", 1)
        created = []
        original = cc_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(cc_loop, "make_submit_tool", spying_make)
        rejections = []

        def verify_fn(args, *_a):
            if len(rejections) < cc_loop.MAX_REPEATED_REJECTIONS:
                rejections.append(1)
                raise Rejection("findings[0]: line 5 is not inside a diff hunk")

        async def _query(prompt, options):
            for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
                await created[-1].handler({"summary": "p", "findings": [], "residual_risk": ""})
            await anyio.sleep(5)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path,
                           verify_fn=verify_fn)
        assert code == 1
        assert not (tmp_path / "review.json").exists()

    def test_the_transcript_records_the_model_that_answered_not_a_placeholder(self, tmp_path, monkeypatch):
        # run_start logged CC_MODEL or the literal "default", and CC_MODEL is set
        # by nothing in either workflow — so every production run recorded
        # model_id="default" in the audit trail that exists to make runs
        # comparable. A model swap between two runs was invisible in it.
        monkeypatch.delenv("CC_MODEL", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "global.anthropic.claude-opus-4-8[1m]")
        created = []
        original = cc_loop.make_submit_tool
        monkeypatch.setattr(
            cc_loop, "make_submit_tool",
            lambda *a, **k: created.append(original(*a, **k)) or created[-1],
        )

        async def _query(prompt, options):
            yield AssistantMessage(content=[TextBlock(text="working")], model="claude-opus-4-8-v1")
            await created[-1].handler({"summary": "ok", "findings": [], "residual_risk": ""})
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        assert cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path) == 0

        events = transcript_events(tmp_path)
        start = next(e for e in events if e["event"] == "run_start")
        assert start["model_id"] == "global.anthropic.claude-opus-4-8[1m]", "the configured model must be logged"
        # And the model that actually answered, once known, is recorded too: the
        # configured value is a request, the reported one is what happened.
        complete = next(e for e in events if e["event"] == "run_complete")
        assert complete["model"] == "claude-opus-4-8-v1"

    def test_an_unconfigured_run_says_so_rather_than_claiming_a_default(self, tmp_path, monkeypatch):
        # With neither variable set there IS no configured model, and "default" is
        # a claim about a value nobody supplied. None is the honest record.
        monkeypatch.delenv("CC_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        run_loop(tmp_path, monkeypatch, [[result_message()]])
        start = next(e for e in transcript_events(tmp_path) if e["event"] == "run_start")
        assert start["model_id"] is None

    def test_a_stream_without_a_result_envelope_fails_closed(self, tmp_path, monkeypatch):
        assert run_loop(tmp_path, monkeypatch, [[]]) == 1
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "without a result envelope" in reasons[0]["reason"]


class TestTheEnvelopeOutranksTheTeardownException:
    """ADR-0019. The CLI exits non-zero on purpose after an error result and
    the SDK re-raises that exit as a bare Exception AFTER delivering the
    envelope — an exception that escaped every arm of the retry ladder and
    once killed a run over a process-teardown anomaly (notes §11). Once the
    envelope arrived, it classifies the session; without one, the exception
    stays a crash."""

    API_ERROR_ENVELOPE = dict(
        subtype="success", is_error=True, terminal_reason="api_error",
        result="API Error: 503 ServiceUnavailable", api_error_status=503,
    )
    TEARDOWN_EXC_TEXT = cc_loop.SDK_STREAM_ERROR_PREFIX + "success"

    def spy_submit(self, monkeypatch):
        created = []
        original = cc_loop.make_submit_tool
        monkeypatch.setattr(
            cc_loop, "make_submit_tool",
            lambda *a, **k: created.append(original(*a, **k)) or created[-1],
        )
        return created

    def test_a_teardown_exception_after_an_api_error_envelope_is_retried(self, tmp_path, monkeypatch):
        # The §11 reproduction: envelope delivered, then the bare Exception.
        # Before the fix this escaped run() entirely; now the envelope reaches
        # the api_error arm and the run retries into a successful session.
        waits = []
        monkeypatch.setattr(cc_loop.time, "sleep", waits.append)
        created = self.spy_submit(monkeypatch)
        artifact = {"summary": "ok", "findings": [], "residual_risk": ""}
        sessions = []

        async def _query(prompt, options):
            sessions.append(len(sessions) + 1)
            if len(sessions) == 1:
                yield result_message(**self.API_ERROR_ENVELOPE)
                raise Exception(self.TEARDOWN_EXC_TEXT)
            await created[-1].handler(artifact)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert waits == [cc_loop.API_ERROR_BACKOFF_SECONDS]
        assert json.loads((tmp_path / "review.json").read_text()) == artifact
        errors = [e for e in transcript_events(tmp_path) if e["event"] == "api_error"]
        assert errors and errors[0]["api_error_status"] == 503

    def test_a_verified_artifact_survives_the_teardown_exception(self, tmp_path, monkeypatch):
        # The production stake: the session got a review through the verifier,
        # then died on teardown. Delivering beats retrying, exactly as on the
        # api_error arm this envelope now reaches.
        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: pytest.fail("must deliver, not retry"))
        created = self.spy_submit(monkeypatch)
        artifact = {"summary": "a complete verified review", "findings": [], "residual_risk": ""}

        async def _query(prompt, options):
            await created[-1].handler(artifact)
            yield result_message(**self.API_ERROR_ENVELOPE)
            raise Exception(self.TEARDOWN_EXC_TEXT)

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert json.loads((tmp_path / "review.json").read_text()) == artifact

    def test_the_exception_without_an_envelope_stays_a_crash(self, tmp_path, monkeypatch):
        # In-order delivery means the prefixed text without an envelope can
        # only be an SDK fault; swallowing it would read a crashed harness as
        # a session ending.
        async def _query(prompt, options):
            raise Exception(self.TEARDOWN_EXC_TEXT)
            yield  # pragma: no cover — makes this an async generator

        monkeypatch.setattr(cc_loop, "query", _query)
        with pytest.raises(Exception, match="returned an error result"):
            cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

    def test_an_unprefixed_exception_after_an_envelope_stays_a_crash(self, tmp_path, monkeypatch):
        # The reader's OTHER fatal errors carry no prefix and no claim that an
        # error result preceded them; the envelope does not launder those.
        async def _query(prompt, options):
            yield result_message()
            raise Exception("Fatal error in message reader: boom")

        monkeypatch.setattr(cc_loop, "query", _query)
        with pytest.raises(Exception, match="message reader"):
            cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

    def test_the_documented_error_shape_is_matched_without_terminal_reason(self, tmp_path, monkeypatch):
        # is_error with subtype "success" is the CLI's documented shape for a
        # failing API call (the one combination carrying api_error_status), and
        # terminal_reason is nullable — the shape alone must reach the arm.
        waits = []
        monkeypatch.setattr(cc_loop.time, "sleep", waits.append)
        failing = [result_message(
            subtype="success", is_error=True, terminal_reason=None,
            result="API Error: 529 Overloaded", api_error_status=529,
        )]
        assert run_loop(tmp_path, monkeypatch, [failing, [result_message()]]) == 1
        assert waits == [cc_loop.API_ERROR_BACKOFF_SECONDS]
        errors = [e for e in transcript_events(tmp_path) if e["event"] == "api_error"]
        assert errors and errors[0]["api_error_status"] == 529 and errors[0]["retrying"]

    def test_an_unclaimed_error_envelope_fails_with_its_own_name(self, tmp_path, monkeypatch):
        # An error envelope no arm recognizes must not fall through to
        # "completed without calling submit_review" — a false reason for a
        # session that died. It fails clean, named by its own subtype, and is
        # not retried: an unrecognized error class gets the ClaudeSDKError
        # treatment.
        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: pytest.fail("must not retry"))
        stream = [result_message(
            subtype="error_during_execution", is_error=True, terminal_reason=None,
            errors=["engine exploded"],
        )]
        assert run_loop(tmp_path, monkeypatch, [stream]) == 1
        reasons = [e for e in transcript_events(tmp_path) if e["event"] == "run_failed"]
        assert "error_during_execution" in reasons[0]["reason"]
        assert "engine exploded" in reasons[0]["reason"]
        assert "without calling" not in reasons[0]["reason"]

    def test_a_verified_artifact_survives_an_unclaimed_error_envelope(self, tmp_path, monkeypatch):
        # An error at the end of a session is not a verdict on what the
        # verifier already accepted, on this arm as on every other.
        created = self.spy_submit(monkeypatch)
        artifact = {"summary": "ok", "findings": [], "residual_risk": ""}

        async def _query(prompt, options):
            await created[-1].handler(artifact)
            yield result_message(subtype="error_during_execution", is_error=True, terminal_reason=None)

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 0
        assert json.loads((tmp_path / "review.json").read_text()) == artifact


class TestTheConsumersBudgetGovernsTheSession:
    """ADR-0015: both ceilings come from the deadline the agent job stamped.

    The consumer sets ONE number; each attempt's clock is the time left less the
    reserve, and its turn ceiling is derived from that clock so the clock binds
    first. Absence and garbage are deliberately different — no deadline is direct
    invocation, an unreadable one is broken wiring.
    """

    def deadline_in(self, seconds: int) -> str:
        return str(int(time.time()) + seconds)

    def test_no_deadline_falls_back_to_the_module_constants(self, monkeypatch):
        # The eval runners and every local invocation reach this branch.
        monkeypatch.delenv(cc_loop.DEADLINE_ENV, raising=False)
        assert cc_loop.session_deadline() is None
        assert cc_loop.attempt_budget(None) == cc_loop.WALL_CLOCK_SECONDS
        assert cc_loop.build_options("p", REPO_ROOT, SCENARIO / "pr_root", None).max_turns == (
            cc_loop.MAX_TURNS
        )

    def test_a_deadlineless_session_is_given_the_constant_and_not_a_derivation(
        self, tmp_path, monkeypatch
    ):
        # Asserted on the SESSION, because build_options' own default is not what the
        # loop passes: deriving the ceiling unconditionally left CC_MAX_TURNS with no
        # effect at all -- turn_ceiling(900) is 46 against the constant's 45, close
        # enough that nothing else would have noticed the knob was dead.
        monkeypatch.delenv(cc_loop.DEADLINE_ENV, raising=False)
        monkeypatch.setattr(cc_loop, "MAX_TURNS", 7)
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

        assert query.calls[0].max_turns == 7
        granted = next(e for e in transcript_events(tmp_path) if e["event"] == "attempt_budget")
        assert granted["max_turns"] == 7 and granted["consumer_set"] is False

    @pytest.mark.parametrize("raw", ["", "  "])
    def test_an_empty_deadline_reads_as_absence(self, monkeypatch, raw):
        # Indistinguishable from unset at this layer; what catches a WORKFLOW that
        # emptied it is test_workflow_shape's pairing.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, raw)
        assert cc_loop.session_deadline() is None

    @pytest.mark.parametrize("raw", ["50 minutes", "50.0", "later", "0x400"])
    def test_an_unreadable_deadline_refuses_rather_than_falling_back(self, monkeypatch, raw):
        # Falling back would run the review on the harness's budget while the
        # consumer's input said otherwise, and report nothing.
        from verify import Rejection

        monkeypatch.setenv(cc_loop.DEADLINE_ENV, raw)
        with pytest.raises(Rejection, match=cc_loop.DEADLINE_ENV):
            cc_loop.session_deadline()

    def test_the_deadline_is_converted_to_the_clock_the_timeout_measures(self, monkeypatch):
        # Handing the epoch on unconverted is a budget of ~1.8 billion seconds, a
        # timeout that can never fire, and the other timeout tests set the constant
        # instead so none of them would notice.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(1000))
        assert cc_loop.session_deadline() == pytest.approx(time.monotonic() + 1000, abs=5)

    def test_an_attempt_gets_the_time_remaining_less_the_reserve(self, monkeypatch):
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(1000))
        budget = cc_loop.attempt_budget(cc_loop.session_deadline())
        assert budget == pytest.approx(1000 - cc_loop.POST_GENERATOR_RESERVE_SECONDS, abs=5)

    def test_a_deadline_past_githubs_own_job_limit_is_clamped(self, monkeypatch):
        # `timeout-minutes` is the consumer's; GitHub's six-hour job limit is not, so
        # past it the platform kills the job with the transcript unwritten.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(20 * 60 * 60))
        remaining = cc_loop.session_deadline() - time.monotonic()
        assert remaining == pytest.approx(cc_loop.GITHUB_JOB_LIMIT_SECONDS, abs=5)

    def test_the_session_is_given_the_derived_clock_and_ceiling(self, tmp_path, monkeypatch):
        # End to end: what the loop computed is what the session runs under.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(2000))
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

        expected = 2000 - cc_loop.POST_GENERATOR_RESERVE_SECONDS
        granted = next(e for e in transcript_events(tmp_path) if e["event"] == "attempt_budget")
        assert granted["seconds"] == pytest.approx(expected, abs=5)
        assert granted["consumer_set"] is True
        # Proportional to the clock, not the constant beside it.
        assert granted["max_turns"] == cc_loop.turn_ceiling(expected)
        assert query.calls[0].max_turns == granted["max_turns"]
        assert granted["max_turns"] > cc_loop.MAX_TURNS

    def test_one_attempt_may_use_nearly_the_whole_job(self, tmp_path, monkeypatch):
        # A wall-clock timeout does not retry, so reserving two thirds of the job for
        # api_errors near the full clock -- never observed -- constrained the failure
        # that does occur.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(2000))
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

        granted = next(e for e in transcript_events(tmp_path) if e["event"] == "attempt_budget")
        assert granted["seconds"] > 2000 / cc_loop.MAX_ATTEMPTS, (
            "the attempt was rationed against the retry count rather than given the "
            "time actually remaining"
        )

    def test_an_attempt_that_died_early_returns_its_time_to_the_next(self, tmp_path, monkeypatch):
        # The other half: each attempt is sized when it STARTS, which is what makes
        # three fast api_error attempts still fit.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(2000))
        streams = [
            [result_message(terminal_reason="api_error", result="API Error: 503")],
            [result_message()],
        ]
        run_loop(tmp_path, monkeypatch, streams)
        budgets = [e["seconds"] for e in transcript_events(tmp_path)
                   if e["event"] == "attempt_budget"]
        assert len(budgets) == 2
        assert budgets[1] < budgets[0], (
            "the second attempt was given the same budget as the first, so the clock "
            "is being read once rather than per attempt"
        )

    def test_a_budget_too_small_to_use_starts_no_session(self, tmp_path, monkeypatch):
        # Starting it anyway spends the reserve and then reports a timeout, where the
        # honest reason is a spent budget and the remedy is the consumer's.
        monkeypatch.setenv(
            cc_loop.DEADLINE_ENV, self.deadline_in(cc_loop.POST_GENERATOR_RESERVE_SECONDS)
        )
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)

        assert code == 1
        assert not (tmp_path / "review.json").exists()
        assert query.calls == [], "a session was started into a budget it cannot submit in"
        reason = next(e["reason"] for e in transcript_events(tmp_path)
                      if e["event"] == "run_failed")
        assert cc_loop.TIMEOUT_INPUT in reason, (
            f"the failure does not name the input a consumer would raise: {reason!r}"
        )

    def test_a_configured_constant_is_not_second_guessed(self, tmp_path, monkeypatch):
        # The floor applies to a DERIVED remainder only: a configured
        # CC_WALL_CLOCK_SECONDS is what an operator said the budget is. The eval
        # harness's short-budget runs depend on this, as do this file's timeout cases.
        monkeypatch.delenv(cc_loop.DEADLINE_ENV, raising=False)
        monkeypatch.setattr(cc_loop, "WALL_CLOCK_SECONDS", 1)
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert len(query.calls) == 1, "a directly-configured budget was refused as too small"

    def test_a_timeout_on_a_consumer_budget_names_the_input(self, tmp_path, monkeypatch):
        # The one failure that reports itself, so it must report the remedy too.
        monkeypatch.setattr(cc_loop, "POST_GENERATOR_RESERVE_SECONDS", 0)
        monkeypatch.setattr(cc_loop, "MIN_ATTEMPT_SECONDS", 1)
        # Three rather than one: `int(time.time())` drops a fraction of a second, so
        # a one-second deadline can land under the floor and refuse to start instead.
        monkeypatch.setenv(cc_loop.DEADLINE_ENV, self.deadline_in(3))

        async def _query(prompt, options):
            await anyio.sleep(5)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 1
        reason = next(e["reason"] for e in transcript_events(tmp_path)
                      if e["event"] == "run_failed")
        assert "wall-clock timeout" in reason and cc_loop.TIMEOUT_INPUT in reason
        # The seconds recorded are the attempt's own, not the module constant.
        timeout = next(e for e in transcript_events(tmp_path)
                       if e["event"] == "wall_clock_timeout")
        assert timeout["seconds"] < cc_loop.WALL_CLOCK_SECONDS


class TestSessionCostIsAccountedFor:
    """Where the wall clock went, recorded in the SMALL file.

    The aggregate has always been in the ResultMessage, but a per-event
    `usage.output_tokens` is a streaming snapshot: one API message arrives as
    several events, thinking text is stripped by redaction, and every event of a
    turn that emitted thousands of thinking tokens reads as single digits.
    Summing them under-reported a real run 200x and produced a diagnosis of
    "provider latency" for generation running at a normal 74 tok/s. These tests
    pin the figures that cannot be misread that way.
    """

    def test_the_aggregate_is_logged_from_the_result_message(self, tmp_path, monkeypatch):
        # duration_api_ms beside duration_ms is the pair that answers "was the
        # harness waiting on anything of its own?" -- so it must be recorded, not
        # merely available in a few hundred KB of captured stream.
        messages = [
            AssistantMessage(content=[TextBlock(text="thinking hard")], model="m",
                             message_id="msg_1", usage={"output_tokens": 4}),
            result_message(
                duration_ms=396325,
                duration_api_ms=397726,
                num_turns=12,
                total_cost_usd=0.966,
                usage={"output_tokens": 29533, "cache_creation_input_tokens": 22527,
                       "cache_read_input_tokens": 122386},
            ),
        ]
        run_loop(tmp_path, monkeypatch, [messages])
        usage = [e for e in transcript_events(tmp_path) if e["event"] == "session_usage"]
        assert len(usage) == 1
        record = usage[0]
        # The real total, NOT the 4 the single captured event advertised.
        assert record["output_tokens"] == 29533
        assert record["duration_ms"] == 396325
        assert record["duration_api_ms"] == 397726
        assert record["cache_read_input_tokens"] == 122386
        assert record["total_cost_usd"] == 0.966

    def test_api_calls_are_counted_by_message_id_not_by_event(self, tmp_path, monkeypatch):
        # The SDK yields one event per content block, so three events sharing a
        # message_id are ONE API call. Counting events is how a 12-call session
        # reads as 31 and its per-call latency looks four times better than it is.
        shared = [
            AssistantMessage(content=[TextBlock(text="a")], model="m", message_id="msg_1"),
            AssistantMessage(content=[TextBlock(text="b")], model="m", message_id="msg_1"),
            AssistantMessage(content=[TextBlock(text="c")], model="m", message_id="msg_1"),
            AssistantMessage(content=[TextBlock(text="d")], model="m", message_id="msg_2"),
            result_message(),
        ]
        run_loop(tmp_path, monkeypatch, [shared])
        record = [e for e in transcript_events(tmp_path) if e["event"] == "session_usage"][0]
        assert record["api_messages"] == 2

    def test_a_timed_out_session_still_accounts_for_what_it_spent(self, tmp_path, monkeypatch):
        # The run most in need of accounting recorded none: the aggregate lives on
        # a ResultMessage, and a session cut off by the wall clock never emits
        # one. Two real 600s timeouts produced transcripts with no cost figures
        # at all, which is why they were diagnosed by inference.
        monkeypatch.setattr(cc_loop, "WALL_CLOCK_SECONDS", 1)

        async def _query(prompt, options):
            yield AssistantMessage(content=[TextBlock(text="a")], model="m", message_id="m1",
                                   usage={"cache_read_input_tokens": 6882})
            yield AssistantMessage(content=[TextBlock(text="b")], model="m", message_id="m1",
                                   usage={"cache_read_input_tokens": 6882})
            yield AssistantMessage(content=[ToolUseBlock(id="1", name="Grep", input={"pattern": "x"})],
                                   model="m", message_id="m2",
                                   usage={"cache_read_input_tokens": 27131})
            await anyio.sleep(5)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        code = cc_loop.run(REPO_ROOT, SCENARIO / "pr_root", SCENARIO / "context", tmp_path)
        assert code == 1
        timeout = [e for e in transcript_events(tmp_path) if e["event"] == "wall_clock_timeout"][0]
        # Two distinct message_ids from three events, and the peak rather than
        # the 40894 their sum would give.
        assert timeout["api_messages"] == 2
        assert timeout["cache_read_peak"] == 27131
        assert timeout["tool_calls"] == 1
        # No token total is guessed for a session that never reported one.
        assert "output_tokens" not in timeout


class TestTheCapturedStreamIsTimestamped:
    """Without a per-line `ts` the only timestamps are tool_request records, so a
    turn that ends WITHOUT a tool call is invisible and the gap before it is
    attributed to whichever call follows it."""

    def test_every_captured_line_carries_a_capture_time(self, tmp_path, monkeypatch):
        messages = [
            AssistantMessage(content=[TextBlock(text="a")], model="m", message_id="m1"),
            result_message(),
        ]
        run_loop(tmp_path, monkeypatch, [messages])
        lines = (tmp_path / "cc_stream_1.jsonl").read_text().splitlines()
        assert lines
        stamps = [json.loads(line)["ts"] for line in lines]
        assert all(isinstance(t, float) for t in stamps)
        assert stamps == sorted(stamps)


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


class TestFailEchoIsRedacted:
    """fail() writes the same reason twice: a transcript record, which redaction
    covers, and a ::error:: line to the job log, which had none. Several of its
    reasons are Rejection messages, and a Rejection interpolates the value it
    refused."""

    def test_the_job_log_line_does_not_carry_the_value(self, tmp_path, capsys):
        transcript = cc_loop.Transcript(tmp_path / "t.jsonl", POLICY)
        cc_loop.fail(transcript, f"cannot assemble the review context: key {FAKE_KEY} leaked")

        err = capsys.readouterr().err
        assert "cannot assemble the review context" in err
        assert FAKE_KEY not in err
        assert "[REDACTED]" in err

    def test_the_transcript_record_is_redacted_too(self, tmp_path):
        transcript = cc_loop.Transcript(tmp_path / "t.jsonl", POLICY)
        cc_loop.fail(transcript, f"key {FAKE_KEY} leaked")
        assert FAKE_KEY not in (tmp_path / "t.jsonl").read_text()


class TestRedactText:
    def test_redacts_a_key(self):
        assert FAKE_KEY not in redact_text(f"key {FAKE_KEY} here", POLICY)

    def test_leaves_innocent_text_alone(self):
        assert redact_text("nothing to see", POLICY) == "nothing to see"

    # The capture is JSONL of serialized SDK messages, so a tool input arrives
    # as {"aws_secret_access_key": "..."} — the label pattern needs the value to
    # follow `:` with only whitespace, and JSON puts a quote there.

    SECRET_VALUE = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY1"

    def test_redacts_a_labelled_secret_in_a_json_tool_input(self):
        line = json.dumps(
            {"type": "ToolUseBlock", "input": {"aws_secret_access_key": self.SECRET_VALUE}}
        )
        assert self.SECRET_VALUE not in redact_text(line, POLICY)

    def test_redacts_a_labelled_secret_nested_deeper(self):
        line = json.dumps(
            {"content": [{"input": {"env": {"AWS_SECRET_ACCESS_KEY": self.SECRET_VALUE}}}]}
        )
        assert self.SECRET_VALUE not in redact_text(line, POLICY)

    def test_redacts_a_labelled_secret_as_a_dict_key(self):
        line = json.dumps({"input": {FAKE_KEY: "value"}})
        assert FAKE_KEY not in redact_text(line, POLICY)

    def test_redacts_across_multiple_lines_independently(self):
        # The capture is JSONL: one message per line, and a line that fails to
        # parse must not stop the next line being scrubbed.
        clean = json.dumps({"type": "SystemMessage"})
        dirty = json.dumps({"input": {"aws_secret_access_key": self.SECRET_VALUE}})
        out = redact_text(f"{clean}\nnot json at all {FAKE_KEY}\n{dirty}\n", POLICY)
        assert self.SECRET_VALUE not in out
        assert FAKE_KEY not in out  # the unparseable line still gets pattern redaction
        assert "SystemMessage" in out  # and innocent content survives

    def test_preserves_the_line_structure(self):
        lines = [json.dumps({"i": i}) for i in range(3)]
        out = redact_text("\n".join(lines) + "\n", POLICY)
        assert out.count("\n") == 3
        assert [json.loads(line)["i"] for line in out.strip().split("\n")] == [0, 1, 2]

    def test_withholds_a_line_whose_secret_survives_redaction(self):
        # Fail-closed, matching redact_secrets: if a residual match remains, the
        # line is withheld rather than shipped.
        crafted = {"a": "AKIA", "b": "ABCDEFGHIJKLMNOP"}  # fuse only once serialized
        policy = {"secret_scan_patterns": ['"a": "AKIA", "b": "ABCDEFGHIJKLMNOP"']}
        out = redact_text(json.dumps(crafted), policy)
        assert "AKIAABCDEFGHIJKLMNOP" not in out.replace('", "b": "', "")


class TestRedactionFunctionsAgree:
    """redact_text and redact_secrets must not drift apart again.

    The gap this pins was created by redact_text's docstring claiming the
    key/value and dict-key cases "cannot arise" on a stream — while its only
    caller passed exactly a stream of JSON-serialized records.
    """

    LABELLED = {"aws_secret_access_key": "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY1"}

    def test_both_redact_a_flat_key(self):
        assert FAKE_KEY not in redact_text(f"key {FAKE_KEY}", POLICY)
        assert FAKE_KEY not in json.dumps(redact_secrets({"k": f"key {FAKE_KEY}"}, POLICY))

    def test_both_redact_the_key_value_bridge(self):
        value = self.LABELLED["aws_secret_access_key"]
        assert value not in redact_text(json.dumps(self.LABELLED), POLICY)
        assert value not in json.dumps(redact_secrets(self.LABELLED, POLICY))

    def test_both_redact_a_secret_used_as_a_dict_key(self):
        assert FAKE_KEY not in redact_text(json.dumps({FAKE_KEY: "v"}), POLICY)
        assert FAKE_KEY not in json.dumps(redact_secrets({FAKE_KEY: "v"}, POLICY))


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


class TestQuarantineContainment:
    """The quarantine handed to the generator carries no symlinks.

    A permission check on the path the generator requests cannot see where a link
    points, so a path textually inside the quarantine would serve outside bytes.
    """

    def test_no_symlinks_found_in_a_clean_tree(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        assert cc_loop.find_symlinks(tmp_path) == []

    def test_a_symlink_to_a_file_outside_is_found(self, tmp_path):
        outside = tmp_path / "secret"
        outside.write_text("credentials")
        root = tmp_path / "pr_root"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "NOTES.md").symlink_to(outside)
        assert [p.name for p in cc_loop.find_symlinks(root)] == ["NOTES.md"]

    def test_a_symlinked_directory_is_found_and_not_descended(self, tmp_path):
        outside = tmp_path / "elsewhere"
        (outside / "nested").mkdir(parents=True)
        (outside / "nested" / "deep.txt").write_text("x")
        root = tmp_path / "pr_root"
        root.mkdir()
        (root / "alias").symlink_to(outside, target_is_directory=True)
        found = cc_loop.find_symlinks(root)
        assert [p.name for p in found] == ["alias"]

    def test_a_deeply_nested_symlink_is_found(self, tmp_path):
        root = tmp_path / "pr_root"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "a" / "b" / "c" / "link").symlink_to(tmp_path / "target")
        assert [p.name for p in cc_loop.find_symlinks(root)] == ["link"]

    def test_assert_no_symlinks_passes_a_clean_tree(self, tmp_path):
        root = tmp_path / "pr_root"
        root.mkdir()
        (root / "a.py").write_text("x")
        transcript = cc_loop.Transcript(tmp_path / "t.jsonl", POLICY)
        cc_loop.assert_no_symlinks(root, transcript)  # must not raise
        transcript.close()

    def test_assert_no_symlinks_refuses_and_logs(self, tmp_path):
        root = tmp_path / "pr_root"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "NOTES.md").symlink_to(tmp_path / "outside")
        transcript = cc_loop.Transcript(tmp_path / "t.jsonl", POLICY)
        with pytest.raises(cc_loop.Rejection, match="symlink"):
            cc_loop.assert_no_symlinks(root, transcript)
        transcript.close()
        events = [
            json.loads(line)
            for line in (tmp_path / "t.jsonl").read_text().splitlines()
            if line.strip()
        ]
        rejected = [e for e in events if e["event"] == "quarantine_rejected"]
        assert rejected and rejected[0]["paths"] == ["docs/NOTES.md"]

    # The helper tests above pass with the CALL removed, so they pin the check and
    # not the containment. These drive the real loops: the assertion has to run
    # BEFORE the model is granted the directory, so the load-bearing observation
    # is that query() was never invoked -- a guard that refused afterwards would
    # satisfy a return-code-only test while the bytes had already been read.

    def plant_link(self, pr_root, tmp_path):
        outside = tmp_path / "credentials"
        outside.write_text("[default]\naws_secret_access_key=REALSECRET\n")
        (pr_root / "docs").mkdir(parents=True, exist_ok=True)
        (pr_root / "docs" / "NOTES.md").symlink_to(outside)

    def assert_refused_before_the_model_ran(self, run_result, query, out):
        assert run_result == 1
        assert query.calls == [], "the generator was handed a quarantine containing a symlink"
        events = [
            json.loads(line)
            for line in (out / "transcript.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert any(e["event"] == "quarantine_rejected" for e in events)

    def test_the_review_lane_refuses_before_the_session_starts(self, tmp_path, monkeypatch):
        pr_root = tmp_path / "pr_root"
        shutil.copytree(SCENARIO / "pr_root", pr_root)
        self.plant_link(pr_root, tmp_path)
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        out = tmp_path / "out"
        self.assert_refused_before_the_model_ran(
            cc_loop.run(REPO_ROOT, pr_root, SCENARIO / "context", out), query, out
        )

    def test_the_plan_lane_refuses_before_the_session_starts(self, tmp_path, monkeypatch):
        # 551b9bb calls the containment two independent layers. The in-process
        # layer was wired into one lane, so the plan generator had one.
        import plan_loop
        from test_plan_loop import TestRunWiring

        context, pr_root = TestRunWiring().scenario(tmp_path)
        self.plant_link(pr_root, tmp_path)
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        out = tmp_path / "out"
        self.assert_refused_before_the_model_ran(
            plan_loop.run(REPO_ROOT, pr_root, context, out), query, out
        )


class TestOptions:
    """The security-relevant invariants of the session configuration."""

    def options(self):
        submit, *_ = TestSubmitTool().make()
        server = cc_loop.build_review_server(submit)
        return cc_loop.build_options("prompt", REPO_ROOT, SCENARIO / "pr_root", server)

    # The denylist is load-bearing containment, not hygiene: allowed_tools does
    # NOT bound the surface (cc_loop's comment records the probing that
    # established this), so a name leaving DISALLOWED_TOOLS is a tool the model
    # regains. It was pinned by a six-name SENTINEL over 24 entries — so `Skill`
    # dropped in a refactor left the suite green while a managed skill stayed
    # reachable under safe mode, able to provide shell or write behaviour to a
    # model following contributor-authored instructions.

    EFFECTFUL = frozenset({
        # shell and filesystem
        "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
        # network
        "WebFetch", "WebSearch",
        # subagents, which arrive with their own Bash/Write
        "Task", "Agent", "Workflow", "SendMessage",
        # looks like the reviewer's own reporting tool, writes to the CLI's UI
        "ReportFindings",
        # loaders: reach code and config this session is meant not to have
        "Skill", "ToolSearch",
        # state outside the run
        "TodoWrite",
        "TaskCreate", "TaskUpdate", "TaskStop", "TaskGet", "TaskList", "TaskOutput",
        "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
        "EnterWorktree", "ExitWorktree",
    })

    def test_the_whole_effectful_deny_list_is_pinned(self):
        # Every name, not a sentinel: the set here is the claim, so removing one
        # from cc_loop fails rather than passing on the survivors.
        denied = set(self.options().disallowed_tools)
        assert self.EFFECTFUL <= denied, f"no longer denied: {sorted(self.EFFECTFUL - denied)}"

    def test_the_deny_list_carries_nothing_this_test_has_not_read(self):
        # The other direction, so the two cannot drift: a name added to cc_loop
        # without being classified here fails, which is where a reader decides
        # whether it is effectful or a readonly tool that should be permitted.
        unclassified = set(cc_loop.DISALLOWED_TOOLS) - self.EFFECTFUL
        assert not unclassified, f"denied but unclassified here: {sorted(unclassified)}"

    def test_the_permitted_surface_is_exactly_read_grep_glob_and_submit(self):
        # The positive half. allowed_tools does not bound the surface, but it does
        # say what the harness INTENDS to permit, and a fourth name appearing here
        # is a decision someone made.
        assert set(self.options().allowed_tools) == {"Read", "Grep", "Glob", cc_loop.SUBMIT_TOOL}

    def test_no_readonly_tool_is_also_denied(self):
        # A name on both lists is a contradiction the SDK resolves silently, and
        # the resolution is not this harness's to guess.
        assert not set(cc_loop.READONLY_TOOLS) & set(cc_loop.DISALLOWED_TOOLS)

    def test_the_permission_mode_does_not_prompt(self):
        # There is no human at this session; a mode that asks would hang the run
        # to its wall clock, and one that auto-accepts writes would grant exactly
        # what the denylist refuses.
        assert self.options().permission_mode == "dontAsk"

    def test_no_ambient_configuration_is_loaded(self):
        options = self.options()
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert "safe-mode" in options.extra_args

    def test_submit_review_is_allowed_by_its_full_mcp_name(self):
        assert cc_loop.SUBMIT_TOOL in self.options().allowed_tools


class TestTheStreamCaptureCannotMaskTheSessionsFailure:
    """The `finally` exists so the capture is written even when the session dies,
    and a partial stream IS the evidence a hang or crash leaves. An unguarded
    write there can raise on its own and replace the exception that was
    propagating -- losing the reason the session died in order to report that the
    evidence about it could not be stored."""

    def test_a_failing_capture_write_does_not_replace_the_session_error(
        self, tmp_path, monkeypatch
    ):
        import cc_loop

        def exploding_write_text(self, *args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "write_text", exploding_write_text)

        async def dying_query(**kwargs):
            raise TimeoutError("the wall clock fired")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(cc_loop, "query", dying_query)
        transcript = cc_loop.Transcript(tmp_path / "t.jsonl", POLICY)
        try:
            with pytest.raises(TimeoutError, match="wall clock"):
                anyio.run(
                    cc_loop._run_session, "prompt", object(), transcript,
                    {"model": None, "tool_calls": 0}, tmp_path, 1, POLICY, 60,
                )
        finally:
            monkeypatch.undo()
            transcript.close()
