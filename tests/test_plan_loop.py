"""Tests for the plan generator's session handling and artifact hygiene.

The mirror of test_cc_loop.py for the second session: submit_plan's
verify/reject/accept logic through the REAL verify_plan, the schema and
prompt derivations from policy.json, and the run() wiring — query() faked
with scripted streams, the handler exercised directly (in production it runs
in this process either way). Whether the prompt STEERS the model is the eval
suite's question, not this file's.
"""

import json
from pathlib import Path

import anyio
import cc_loop
import plan_loop
from conftest import POLICY
from plan_verify import verify_plan

REPO_ROOT = Path(__file__).parent.parent

from test_cc_loop import fake_query, result_message  # noqa: E402
from test_plan_verify import (  # noqa: E402
    PLAN_CHANGED_FILES,
    PLAN_DIFF,
    anchored_suggest,
    push_step,
    tree_source,
)

PLAN_PROMPT = plan_loop.PLAN_PROMPT_PATH.read_text()


def make_submit(verify_fn=None, tree=None):
    """A submit_plan tool wired exactly as plan_loop.run wires it, with a
    dict-backed content source instead of pr_root."""
    state = {
        "round": 0, "repeated": 0, "last_fingerprint": None,
        "accepted": None, "abort_reason": None, "tool_calls": 0,
    }
    lines = []

    class FakeTranscript:
        def log(self, event, **data):
            lines.append({"event": event, **data})

    source = tree_source(tree)
    inner = verify_fn or verify_plan

    def checked(artifact, diff, files, policy):
        inner(artifact, diff, files, policy, source)

    submit = cc_loop.make_submit_tool(
        plan_loop.build_plan_schema(POLICY), state, FakeTranscript(), checked,
        PLAN_DIFF, PLAN_CHANGED_FILES, POLICY, "plan guidance text",
        tool_name="submit_plan", noun="plan", note_fn=None,
    )
    return submit, state, lines


def call(submit, args):
    return anyio.run(submit.handler, args)


def valid_plan():
    return {"steps": [anchored_suggest()]}


class TestSubmitPlanTool:
    """The submission channel, against the REAL verifier: acceptance runs the
    whole phase chain (schema through secrets), so a plan accepted here is a
    plan verify_plan accepts, content source included."""

    def test_a_valid_plan_is_accepted_and_recorded(self):
        submit, state, _ = make_submit()
        plan = valid_plan()
        response = call(submit, plan)
        assert not response.get("is_error"), response["content"][0]["text"]
        assert state["accepted"] == plan

    def test_the_feedback_names_the_tool_and_noun(self):
        submit, state, _ = make_submit()
        response = call(submit, valid_plan())
        assert "Plan accepted" in response["content"][0]["text"]

    def test_a_rejection_returns_the_real_verifier_reason(self):
        submit, state, lines = make_submit()
        bad = {"steps": [anchored_suggest(path="src/evil.py")]}
        response = call(submit, bad)
        assert response["is_error"]
        assert "not a file this PR touched" in response["content"][0]["text"]
        assert "plan guidance text" in response["content"][0]["text"]
        assert state["accepted"] is None
        assert lines[0]["event"] == "submit_rejected"

    def test_an_unanchored_old_is_rejected_with_the_anchoring_reason(self):
        # The rejection the model can act on: re-read the file, copy verbatim.
        submit, _, _ = make_submit()
        response = call(submit, {"steps": [anchored_suggest(old="def load():\n")]})
        assert response["is_error"]
        assert "byte-match" in response["content"][0]["text"]

    def test_repeating_one_failure_trips_the_breaker(self):
        submit, state, _ = make_submit()
        bad = {"steps": [anchored_suggest(path="src/evil.py")]}
        for _ in range(cc_loop.MAX_REPEATED_REJECTIONS):
            response = call(submit, bad)
        assert state["abort_reason"], "same-class rejections must trip the breaker"
        assert "aborted" in response["content"][0]["text"]
        assert "no plan will be posted" in response["content"][0]["text"]

    def test_a_second_submission_after_acceptance_is_refused(self):
        submit, state, _ = make_submit()
        first = valid_plan()
        call(submit, first)
        response = call(submit, {"steps": [push_step("s9")]})
        assert response["is_error"]
        assert state["accepted"] == first, "acceptance is first-wins"

    def test_no_nesting_note_fires_on_plan_rejections(self):
        # note_fn=None: the nested-artifact note is a review-channel diagnosis
        # (evidence lives in `summary`, which plans do not have); firing its
        # text on a plan rejection would be the false-accusation degradation
        # run_evals measured, with no evidence behind it.
        submit, _, _ = make_submit()
        response = call(submit, {"steps": [anchored_suggest(path="src/evil.py")]})
        assert "serialized as text" not in response["content"][0]["text"]


class TestPlanSchema:
    """build_plan_schema is documentation for the model (the MCP layer never
    validates against it — build_review_server's whole point), so what these
    pin is agreement with policy.json, not enforcement."""

    def test_one_branch_per_step_kind(self):
        schema = plan_loop.build_plan_schema(POLICY)
        branches = schema["properties"]["steps"]["items"]["oneOf"]
        kinds = {b["properties"]["kind"]["const"] for b in branches}
        assert kinds == set(POLICY["plan"]["step_kinds"])

    def test_each_branch_requires_exactly_the_declared_args(self):
        schema = plan_loop.build_plan_schema(POLICY)
        for branch in schema["properties"]["steps"]["items"]["oneOf"]:
            kind = branch["properties"]["kind"]["const"]
            declared = set(POLICY["plan"]["step_kinds"][kind]["args"])
            assert set(branch["properties"]["args"]["required"]) == declared
            assert set(branch["properties"]["args"]["properties"]) == declared
            assert branch["properties"]["args"]["additionalProperties"] is False

    def test_steps_are_bounded_by_the_policy_cap(self):
        schema = plan_loop.build_plan_schema(POLICY)
        assert schema["properties"]["steps"]["maxItems"] == POLICY["plan"]["max_steps"]
        assert schema["properties"]["steps"]["minItems"] == 1


class TestPlanPromptMechanics:
    """test_prompt.py's discipline for the plan prompt: steering is the eval
    suite's question; drift against policy.json is this file's."""

    def test_the_prompt_resolves_and_is_not_empty(self):
        assert plan_loop.PLAN_PROMPT_PATH.exists()
        assert len(PLAN_PROMPT) > 500

    def test_the_prompt_is_marked_as_an_unmeasured_draft(self):
        # The prompt is a measured change (docs/findings/0001); until evals
        # run, the file itself must say it is not to be trusted. Delete this
        # test in the commit that records the first eval results.
        assert "DRAFT" in PLAN_PROMPT.splitlines()[0]

    def test_the_assembled_prompt_names_every_step_kind(self):
        # The prompt the model receives is file + constraints; the file
        # discusses the fix-expressing kinds and the constraints enumerate
        # the full universe, so the ASSEMBLY is what must be complete.
        assembled = PLAN_PROMPT + plan_loop.render_plan_constraints(POLICY)
        for kind in POLICY["plan"]["step_kinds"]:
            assert f"`{kind}`" in assembled, f"assembled prompt never mentions {kind!r}"

    def test_it_names_the_submit_tool(self):
        assert "`submit_plan`" in PLAN_PROMPT

    def test_the_embedded_example_verifies_against_a_real_tree(self):
        # The example is graded by the same code that grades the model: a
        # tree is constructed in which its anchor holds, and verify_plan runs
        # the full phase chain over it. An example the verifier rejects
        # teaches the model a shape that burns its submission budget.
        import re

        examples = re.findall(r"```json\n(.*?)```", PLAN_PROMPT, re.S)
        assert examples, "no ```json example in the plan prompt"
        plan = json.loads(examples[0])
        step = plan["steps"][0]
        path, line, old = step["args"]["path"], step["args"]["line"], step["args"]["old"]
        diff = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{line},1 +{line},1 @@\n"
            f"+{old.rstrip(chr(10))}\n"
        )
        tree = {path: ("x\n" * (line - 1)).encode() + old.encode()}
        verify_plan(plan, diff, [path], POLICY, tree_source(tree))

    def test_constraints_render_the_shipped_numbers(self):
        rendered = plan_loop.render_plan_constraints(POLICY)
        plan = POLICY["plan"]
        for value in (plan["max_steps"], plan["max_patched_files"], plan["max_changed_lines"]):
            assert str(value) in rendered
        for pattern in plan["path_denylist"]:
            assert pattern in rendered

    def test_constraints_render_the_ordering_rules(self):
        # An enforced rule the prompt omits is a rule the model can only
        # discover by burning a submission on a rejection. Rendered from the
        # policy rather than restated, so an edited `ordering` moves the prose.
        rendered = plan_loop.render_plan_constraints(POLICY)
        for rule in POLICY["plan"]["ordering"]:
            assert f"`{rule['before']}`" in rendered
            assert f"`{rule['after']}`" in rendered
        assert "before" in rendered.lower()

    def test_constraints_have_no_empty_interpolation(self):
        assembled = PLAN_PROMPT + plan_loop.render_plan_constraints(POLICY)
        for line in assembled.splitlines():
            stripped = line.rstrip()
            assert not stripped.endswith(": ."), f"empty interpolation: {line!r}"
            assert not stripped.endswith(": ,"), f"empty list element: {line!r}"

    def test_rejection_guidance_names_the_kinds(self):
        guidance = plan_loop.render_plan_rejection_guidance(POLICY)
        for kind in POLICY["plan"]["step_kinds"]:
            assert kind in guidance


class TestPlanUserMessage:
    def test_the_commanded_finding_is_fenced_and_first(self, tmp_path):
        context = self.write_context(tmp_path)
        message = plan_loop.build_plan_user_message(context)
        assert "<commanded_finding>" in message
        assert message.index("commanded_finding") < message.index("untrusted_diff")
        assert "no other" in message

    def test_the_review_context_is_the_reviewers_with_only_the_closing_swapped(self, tmp_path):
        from artifact import build_user_message

        context = self.write_context(tmp_path)
        message = plan_loop.build_plan_user_message(context)
        # Byte-identical up to the one swapped sentence: the plan is anchored
        # against the same fenced, SHA-anchored context the review was.
        reviewer = build_user_message(context)
        assert "then return your review." not in message
        swapped = reviewer.replace("then return your review.", "then return your plan.")
        assert swapped in message

    def write_context(self, tmp_path):
        context = tmp_path / "context"
        context.mkdir()
        (context / "pr.json").write_text(json.dumps(
            {"number": 7, "base_sha": "b" * 40, "head_sha": "h" * 40, "title": "t", "body": ""}
        ))
        (context / "diff.patch").write_text(PLAN_DIFF)
        (context / "changed_files.json").write_text(json.dumps(PLAN_CHANGED_FILES))
        (context / "finding.json").write_text(json.dumps({
            "path": "src/app.py", "line": 2, "severity": "high",
            "title": "load() breaks callers", "body": "the body",
        }))
        return context


class TestRunWiring:
    """run() end-to-end with a faked query(): the fail-closed exits, the
    artifact filename, and the tool name in failure reasons — the places a
    copy-paste from cc_loop.run would silently keep saying 'review'."""

    def scenario(self, tmp_path):
        context = TestPlanUserMessage().write_context(tmp_path)
        pr_root = tmp_path / "pr_root"
        (pr_root / "src").mkdir(parents=True)
        (pr_root / "src" / "app.py").write_bytes(
            b"import os\ndef load(path):\n    check(path)\n    return os.environ\n"
        )
        (pr_root / "src" / "util.py").write_bytes(b"def check(path):\n    pass\n")
        return context, pr_root

    def test_completing_without_a_submission_fails_closed_naming_submit_plan(self, tmp_path, monkeypatch):
        context, pr_root = self.scenario(tmp_path)
        monkeypatch.setattr(cc_loop, "query", fake_query([[result_message()]]))
        out = tmp_path / "out"
        assert plan_loop.run(REPO_ROOT, pr_root, context, out) == 1
        assert not (out / "plan.json").exists()
        events = [json.loads(line) for line in (out / "transcript.jsonl").read_text().splitlines()]
        reasons = [e for e in events if e["event"] == "run_failed"]
        assert "without calling submit_plan" in reasons[0]["reason"]

    def test_an_accepted_plan_is_written_as_plan_json(self, tmp_path, monkeypatch):
        context, pr_root = self.scenario(tmp_path)
        plan = valid_plan()

        created = []
        original = plan_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        # plan_loop imports the tool factory by name, so the patch must land
        # on ITS reference, not cc_loop's.
        monkeypatch.setattr(plan_loop, "make_submit_tool", spying_make)

        async def _query(prompt, options):
            await created[-1].handler(plan)
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        out = tmp_path / "out"
        assert plan_loop.run(REPO_ROOT, pr_root, context, out) == 0
        assert json.loads((out / "plan.json").read_text()) == plan
        events = [json.loads(line) for line in (out / "transcript.jsonl").read_text().splitlines()]
        assert any(e["event"] == "run_start" and e.get("artifact_kind") == "plan" for e in events)

    def test_the_content_source_is_the_real_pr_root(self, tmp_path, monkeypatch):
        # An `old` that matches the test fixture dict but not the tree on
        # disk must reject: run() anchors against pr_root, nothing else.
        context, pr_root = self.scenario(tmp_path)
        (pr_root / "src" / "app.py").write_bytes(b"entirely different content\n")

        created = []
        original = plan_loop.make_submit_tool

        def spying_make(*args, **kwargs):
            created.append(original(*args, **kwargs))
            return created[-1]

        # plan_loop imports the tool factory by name, so the patch must land
        # on ITS reference, not cc_loop's.
        monkeypatch.setattr(plan_loop, "make_submit_tool", spying_make)
        responses = []

        async def _query(prompt, options):
            responses.append(await created[-1].handler(valid_plan()))
            yield result_message()

        monkeypatch.setattr(cc_loop, "query", _query)
        out = tmp_path / "out"
        assert plan_loop.run(REPO_ROOT, pr_root, context, out) == 1
        assert responses[0]["is_error"]
        assert "byte-match" in responses[0]["content"][0]["text"]
        assert not (out / "plan.json").exists()

    def test_the_session_options_use_the_plan_server_and_tool(self, tmp_path, monkeypatch):
        context, pr_root = self.scenario(tmp_path)
        query = fake_query([[result_message()]])
        monkeypatch.setattr(cc_loop, "query", query)
        plan_loop.run(REPO_ROOT, pr_root, context, tmp_path / "out")
        options = query.calls[0]
        assert plan_loop.SUBMIT_TOOL in options.allowed_tools
        assert cc_loop.SUBMIT_TOOL not in options.allowed_tools
        assert "plan" in options.mcp_servers
        # The security posture is cc_loop's, unchanged.
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        for name in ("Bash", "Write", "Workflow", "ToolSearch", "Task"):
            assert name in options.disallowed_tools
