"""Tests for the deterministic half of the leak probe (leak_probe.py).

The probe's model runs need real Bedrock; the stream miner does not. It is
pinned here because a miner that misses a leak-shaped call turns the probe
into a instrument that always reads zero -- worse than no instrument, given
its job is to be believed over a green suite.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "aceiro" / "evals"))

import leak_probe  # noqa: E402
from cc_loop import SUBMIT_TOOL  # noqa: E402


def assistant_line(*blocks):
    return json.dumps({"type": "AssistantMessage", "content": list(blocks)})


def submit_call(tool_input):
    return {"type": "ToolUseBlock", "name": SUBMIT_TOOL, "input": tool_input}


def write_stream(tmp_path, *lines):
    stream = tmp_path / "cc_stream_1.jsonl"
    stream.write_text("\n".join(lines) + "\n")
    return str(stream)


class TestSubmissions:
    def test_complete_artifact_is_not_a_leak(self, tmp_path):
        stream = write_stream(
            tmp_path,
            assistant_line(submit_call({"findings": [], "residual_risk": "", "summary": "short"})),
        )
        [call] = leak_probe.submissions(stream)
        assert call["leaked"] is False
        assert call["keys"] == ["findings", "residual_risk", "summary"]
        assert call["summary_length"] == 5

    def test_missing_findings_is_a_leak_whatever_summary_holds(self, tmp_path):
        # The observed failure shape: the whole artifact serialized into
        # `summary` as function-calling XML, `findings` genuinely absent.
        leaked_summary = 'done</summary>\n<parameter name="findings">[]'
        stream = write_stream(tmp_path, assistant_line(submit_call({"summary": leaked_summary})))
        [call] = leak_probe.submissions(stream)
        assert call["leaked"] is True
        assert call["keys"] == ["summary"]
        assert call["summary_length"] == len(leaked_summary)

    def test_calls_come_back_in_stream_order(self, tmp_path):
        stream = write_stream(
            tmp_path,
            assistant_line(submit_call({"summary": "leaked first"})),
            assistant_line(submit_call({"findings": [], "summary": "recovered"})),
        )
        first, second = leak_probe.submissions(stream)
        assert first["leaked"] is True
        assert second["leaked"] is False

    def test_other_tools_and_message_types_are_ignored(self, tmp_path):
        stream = write_stream(
            tmp_path,
            assistant_line({"type": "ToolUseBlock", "name": "Read", "input": {"file_path": "x"}}),
            json.dumps({"type": "ResultMessage", "subtype": "success"}),
            json.dumps({"type": "UserMessage", "content": "tool result text"}),
        )
        assert leak_probe.submissions(stream) == []

    def test_unparseable_lines_are_skipped_not_fatal(self, tmp_path):
        # A crashed session leaves a truncated stream; the probe must still
        # mine the calls before the truncation point.
        stream = write_stream(
            tmp_path,
            assistant_line(submit_call({"findings": [], "summary": "ok"})),
            '{"type": "AssistantMessage", "content": [{"na',
        )
        [call] = leak_probe.submissions(stream)
        assert call["leaked"] is False

    def test_null_input_and_null_summary_do_not_crash(self, tmp_path):
        stream = write_stream(
            tmp_path,
            assistant_line({"type": "ToolUseBlock", "name": SUBMIT_TOOL, "input": None}),
            assistant_line(submit_call({"findings": [], "summary": None})),
        )
        first, second = leak_probe.submissions(stream)
        assert first["leaked"] is True  # no findings key in an empty input
        assert second["summary_length"] == 0


def probe_result(leaked=False, exit_code=0, retry_submissions=None, summary_length=100, keys=None):
    return {
        "scenario": "lru_eviction_bug",
        "index": 0,
        "exit_code": exit_code,
        "retry_submissions": retry_submissions or [],
        "leaked": leaked,
        "summary_length": summary_length,
        "keys": keys or ["summary", "findings"],
    }


class TestExitStatus:
    """The gate reads clean precisely when it measured nothing, unless "no data"
    is distinguishable from "no leaks". The docstring says the exit code exists
    to gate a loop, and a prompt-change loop gated on this would treat an
    unmeasured change as verified leak-free."""

    def test_clean_measured_runs_exit_zero(self):
        assert leak_probe.exit_status([probe_result(), probe_result()]) == 0

    def test_a_first_submission_leak_exits_non_zero(self):
        assert leak_probe.exit_status([probe_result(leaked=True)]) != 0

    def test_a_retry_leak_exits_non_zero(self):
        results = [probe_result(retry_submissions=[{"leaked": True, "summary_length": 9, "keys": []}])]
        assert leak_probe.exit_status(results) != 0

    def test_measuring_nothing_exits_non_zero(self):
        # Upstream throttling: every session died, no submit_review block was
        # captured, `valid` is empty. "0 leaks / 0 calls" is not a clean bill.
        results = [{"leaked": None, "retry_submissions": [], "exit_code": 1}] * 3
        assert leak_probe.exit_status(results) != 0

    def test_a_failed_run_exits_non_zero_even_with_a_clean_submission(self):
        # probe_once records exit_code and nothing read it. A run that captured
        # a first submission and then failed is not a measurement to trust.
        assert leak_probe.exit_status([probe_result(exit_code=1)]) != 0

    def test_no_results_at_all_exits_non_zero(self):
        assert leak_probe.exit_status([]) != 0

    def test_the_reason_is_stated_not_just_the_code(self, capsys):
        # The loop operator must be able to tell "measured nothing" from "clean".
        leak_probe.exit_status([{"leaked": None, "retry_submissions": [], "exit_code": 1}])
        printed = capsys.readouterr().out.lower()
        assert "measured nothing" in printed
        assert "not the same as 'no leaks'" in printed
