"""Tests for the generator's stream handling and artifact hygiene.

Running the CLI needs a real model; parsing its output and redacting what gets
uploaded do not, and both are places a bug is invisible until it matters.
"""

import json
from pathlib import Path

import cc_loop
import pytest
from artifact import redact_text
from conftest import POLICY

HARNESS_DIR = Path(__file__).parent.parent / "src" / "smtithy"
# base_root: the trusted pre-change tree the model may read. Upstream this was
# the staging repo's root, three levels above .github/scripts/ai_review. Here the
# harness IS the repo, so the repo root is base_root -- and since cc_loop no
# longer reads its policy or prompt from base_root (see artifact.PROMPT_PATH),
# nothing requires it to have any particular layout.
REPO_ROOT = Path(__file__).parent.parent

# Separators str.splitlines() treats as line breaks but JSON does not. JavaScript
# JSON.stringify emits them literally, so the CLI can put them in a record.
JSON_SAFE_SEPARATORS = ["\u2028", "\u2029", "\u0085", "\u000b", "\u000c"]

FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def result_record(summary="ok"):
    return {
        "type": "result",
        "subtype": "success",
        "terminal_reason": "completed",
        "structured_output": {"summary": summary, "findings": [], "residual_risk": ""},
    }


class TestParseStream:
    def test_recovers_the_result_envelope(self):
        result, events = cc_loop.parse_stream(json.dumps(result_record()))
        assert result is not None
        assert events == []

    def test_separates_events_from_the_result(self):
        stream = json.dumps({"type": "assistant"}) + "\n" + json.dumps(result_record())
        result, events = cc_loop.parse_stream(stream)
        assert result["type"] == "result"
        assert [e["type"] for e in events] == ["assistant"]

    def test_unparseable_lines_are_skipped_not_fatal(self):
        stream = "not json\n" + json.dumps(result_record())
        result, _ = cc_loop.parse_stream(stream)
        assert result is not None

    def test_crlf_and_a_trailing_newline_are_tolerated(self):
        result, events = cc_loop.parse_stream(json.dumps(result_record()) + "\r\n")
        assert result is not None
        assert events == []

    def test_a_separator_inside_a_record_does_not_split_it(self):
        # str.splitlines() would cut the record in half here, both halves would
        # fail json.loads, and the run would end with no artifact at all.
        for separator in JSON_SAFE_SEPARATORS:
            line = json.dumps(result_record(f"first{separator}second"), ensure_ascii=False)
            assert len(line.split("\n")) == 1, "the record is one JSON line"
            result, _ = cc_loop.parse_stream(line)
            assert result is not None, f"review lost to {separator!r}"
            assert separator in result["structured_output"]["summary"]


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


class TestPermanentErrorFailsFast:
    def test_a_403_is_not_retried(self, tmp_path, monkeypatch):
        stream = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "terminal_reason": "api_error",
                "result": "API Error: 403 not authorized to perform: bedrock:InvokeModel",
            },
        )
        calls = []

        class Proc:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = stream, "", 1

        def fake_run(*args, **kwargs):
            calls.append(1)
            return Proc()

        monkeypatch.setattr(cc_loop.subprocess, "run", fake_run)
        monkeypatch.setattr(cc_loop.time, "sleep", lambda _s: pytest.fail("must not back off"))
        scenarios = HARNESS_DIR / "evals/scenarios/lru_eviction_bug"
        assert cc_loop.run(REPO_ROOT, scenarios / "pr_root", scenarios / "context", tmp_path) == 1
        assert len(calls) == 1, f"retried a permanent error {len(calls)} times"
        assert not (tmp_path / "review.json").exists()


class TestRedactText:
    def test_redacts_a_key(self):
        assert FAKE_KEY not in redact_text(f"key {FAKE_KEY} here", POLICY)

    def test_leaves_innocent_text_alone(self):
        assert redact_text("nothing to see", POLICY) == "nothing to see"


class TestCapturedStreamIsRedacted:
    """The whole output dir is uploaded as a 90-day CI artifact, so the captured
    stream must pass the same secret scan as the transcript. Asserted through
    run() rather than against redact_text alone: testing the helper in isolation
    leaves 'run() forgot to call it' passing."""

    def run_with_stream(self, tmp_path, monkeypatch, stdout):
        class Proc:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = stdout, "", 0

        monkeypatch.setattr(cc_loop.subprocess, "run", lambda *a, **k: Proc())
        scenarios = HARNESS_DIR / "evals/scenarios/lru_eviction_bug"
        cc_loop.run(REPO_ROOT, scenarios / "pr_root", scenarios / "context", tmp_path)
        return (tmp_path / "cc_stream_1.jsonl").read_text()

    def test_a_key_in_the_stream_never_reaches_the_artifact(self, tmp_path, monkeypatch):
        stream = json.dumps(result_record(f"found {FAKE_KEY}"))
        written = self.run_with_stream(tmp_path, monkeypatch, stream)
        assert FAKE_KEY not in written
        assert "[REDACTED]" in written

    def test_unparseable_lines_are_still_written(self, tmp_path, monkeypatch):
        # A line the parser rejected is the one worth having when diagnosing, so
        # redaction operates on the text rather than on re-serialized records.
        stream = json.dumps(result_record()) + "\nnot json at all\n"
        assert "not json at all" in self.run_with_stream(tmp_path, monkeypatch, stream)
