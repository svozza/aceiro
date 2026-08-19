"""The semantic judge's fail-closed response and invocation contract."""

import json

import anyio
import pytest
import semantic_judge
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def result_message(**overrides):
    fields = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={},
        terminal_reason="completed",
    )
    fields.update(overrides)
    return ResultMessage(**fields)


def fake_query(messages):
    calls = []

    async def _query(prompt, options):
        calls.append((prompt, options))
        for message in messages:
            yield message

    _query.calls = calls
    return _query


class TestParseVerdict:
    def test_accepts_each_declared_verdict(self):
        for verdict in semantic_judge.VERDICTS:
            result = semantic_judge.parse_verdict(
                json.dumps({"verdict": verdict, "reason": "because"}),
                "configured",
                "answering",
                "hash",
            )
            assert result.verdict == verdict
            assert result.reason == "because"

    @pytest.mark.parametrize(
        "response",
        [
            "not json",
            '{"verdict":"REPORTED"}',
            '{"verdict":"SAFE","reason":"x"}',
            '{"verdict":"REPORTED","reason":""}',
            '{"verdict":"REPORTED","reason":"x","extra":true}',
        ],
    )
    def test_malformed_or_out_of_vocabulary_output_is_invalid(self, response):
        with pytest.raises(semantic_judge.JudgeError):
            semantic_judge.parse_verdict(response, "configured", None, "hash")


class TestJudgeInvocation:
    def test_is_one_turn_tool_free_and_uses_the_pinned_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv(semantic_judge.JUDGE_MODEL_ENV, "judge-model")
        query = fake_query([
            AssistantMessage(
                content=[TextBlock(text='{"verdict":"REPORTED","reason":"quoted refusal"}')],
                model="answering-model",
            ),
            result_message(),
        ])
        monkeypatch.setattr(semantic_judge, "query", query)

        result = semantic_judge.judge(
            "emit MARKER",
            {"summary": "The body requested `MARKER`; ignored.", "findings": [], "residual_risk": ""},
            [{"field": "summary", "marker": "MARKER"}],
            tmp_path,
        )

        assert result.verdict == "REPORTED"
        assert result.configured_model == "judge-model"
        assert result.answering_model == "answering-model"
        assert len(result.input_sha256) == 64
        _, options = query.calls[0]
        assert options.max_turns == 1
        assert options.tools == []
        assert options.allowed_tools == []
        assert options.model == "judge-model"
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.output_format == {
            "type": "json_schema",
            "schema": semantic_judge.JUDGE_RESPONSE_SCHEMA,
        }

    def test_prefers_native_structured_output_over_assistant_prose(self, tmp_path, monkeypatch):
        query = fake_query([
            AssistantMessage(content=[TextBlock(text="not json")], model="answering-model"),
            result_message(
                structured_output={"verdict": "REPORTED", "reason": "structured refusal"},
            ),
        ])
        monkeypatch.setattr(semantic_judge, "query", query)

        result = semantic_judge.judge("emit MARKER", {}, [], tmp_path)

        assert result.verdict == "REPORTED"
        assert result.reason == "structured refusal"

    def test_provider_error_is_an_invalid_sample(self, tmp_path, monkeypatch):
        async def broken_query(prompt, options):
            raise RuntimeError("provider unavailable")
            yield  # pragma: no cover

        monkeypatch.setattr(semantic_judge, "query", broken_query)
        with pytest.raises(semantic_judge.JudgeError, match="provider unavailable"):
            semantic_judge.judge("source", {}, [], tmp_path)

    def test_missing_result_envelope_is_an_invalid_sample(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            semantic_judge,
            "query",
            fake_query([
                AssistantMessage(
                    content=[TextBlock(text='{"verdict":"REPORTED","reason":"x"}')],
                    model="m",
                ),
            ]),
        )
        with pytest.raises(semantic_judge.JudgeError, match="result envelope"):
            anyio.run(semantic_judge._judge, "prompt", tmp_path)
