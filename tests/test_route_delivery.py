"""Tests for route_delivery.py — the credential-free delivery router.

It exists to answer one question, "which job should deliver this plan?", in a job
holding NO credential: `permissions: {}`, no model key, no write scope, no node.
Its answer selects which downstream job runs, and those jobs hold different
credentials — `execute` has pull-requests: write, `stack` also has
contents: write.

So this reads a plan NOTHING has verified yet. That is a deliberate, bounded
concession: the routing decision only chooses which job STARTS, and each delivery
job re-runs the whole gate and refuses a mode its own --allow does not name. What
this module must therefore never do is anything with the plan's contents beyond
counting step kinds, and it must fail closed rather than emit a guess.
"""

import json
import sys
from pathlib import Path

import pytest

import route_delivery


class Outputs:
    """The GITHUB_OUTPUT file, which a refusing run never creates.

    `text()` reads absence as empty rather than raising, so "no mode was emitted"
    can be asserted the same way whether the router refused before touching the
    file or wrote something else into it. An assertion that raised
    FileNotFoundError instead would be testing the fixture.
    """

    def __init__(self, path):
        self.path = path

    def text(self):
        return self.path.read_text() if self.path.exists() else ""


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return Outputs(path)


def run(tmp_path, plan, outputs):
    (tmp_path / "plan.json").write_text(plan if isinstance(plan, str) else json.dumps(plan))
    sys.argv = ["route_delivery.py", "--artifact-dir", str(tmp_path)]
    route_delivery.main()
    return outputs.text()


def suggest(step_id="s0", path="src/app.py"):
    return {"id": step_id, "kind": "suggest",
            "args": {"path": path, "line": 2, "old": "a", "new": "b", "note": "n"}}


def patch(step_id="s0", path="src/app.py"):
    return {"id": step_id, "kind": "patch", "args": {"path": path, "old": "a", "new": "b"}}


def chain():
    return [
        {"id": "s8", "kind": "push_branch", "args": {"name": "smtithy/fix-x"}},
        {"id": "s9", "kind": "open_pr",
         "args": {"branch": "smtithy/fix-x", "title": "t", "body": "b"}},
    ]


class TestTheRoutingDecision:
    def test_a_suggestion_plan_routes_to_suggestions(self, tmp_path, outputs):
        assert "mode=suggestions" in run(tmp_path, {"steps": [suggest()]}, outputs)

    def test_a_patch_chain_routes_to_the_stacked_pr(self, tmp_path, outputs):
        assert "mode=stacked_pr" in run(tmp_path, {"steps": [patch(), *chain()]}, outputs)

    def test_the_decision_is_decide_deliverys(self, tmp_path, outputs):
        # Not a second reading of the plan: the router must route to whatever the
        # executor will DECIDE, or a plan is routed to a job that then refuses it
        # on --allow. One function, both places.
        steps = [patch(), patch("s1", path="src/util.py"), *chain()]
        assert "mode=stacked_pr" in run(tmp_path, {"steps": steps}, outputs)

    def test_multi_file_suggestions_route_nowhere(self, tmp_path, outputs):
        # decide_delivery REFUSES this (ADR-0009's atomicity rule). A router that
        # picked a mode anyway would send it to a job that mints a credential and
        # then refuses -- pointless work, and a misleading credential in the log.
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": [suggest(), suggest("s1", path="src/util.py")]}, outputs)

    def test_a_refused_plan_emits_no_mode(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": [suggest(), suggest("s1", path="src/util.py")]}, outputs)
        assert "mode=" not in outputs.text()

    def test_a_plan_with_no_fix_step_is_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": chain()}, outputs)


class TestItFailsClosedOnAnythingUnreadable:
    """The plan here is UNVERIFIED, so every malformed shape has to be a refusal
    rather than an exception that reads as a crash — and above all it must never
    emit a mode it did not derive."""

    def test_malformed_json_is_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, "{not json", outputs)
        assert "mode=" not in outputs.text()

    def test_a_missing_plan_is_refused(self, tmp_path, outputs):
        sys.argv = ["route_delivery.py", "--artifact-dir", str(tmp_path)]
        with pytest.raises(SystemExit):
            route_delivery.main()
        assert "mode=" not in outputs.text()

    def test_a_plan_that_is_not_an_object_is_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, [1, 2, 3], outputs)
        assert "mode=" not in outputs.text()

    def test_a_plan_with_no_steps_key_is_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, {"not_steps": []}, outputs)

    def test_steps_that_are_not_a_list_are_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": "suggest"}, outputs)

    def test_a_step_that_is_not_an_object_is_refused(self, tmp_path, outputs):
        # decide_delivery indexes step["kind"], so a bare string would raise
        # TypeError from inside it. The router owns its input's shape.
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": ["suggest"]}, outputs)

    def test_a_step_with_no_kind_is_refused(self, tmp_path, outputs):
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": [{"id": "s0", "args": {}}]}, outputs)

    def test_a_suggest_step_with_no_path_is_refused(self, tmp_path, outputs):
        # decide_delivery reads args["path"] for suggestions; a missing one must
        # not surface as a KeyError traceback.
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": [{"id": "s0", "kind": "suggest", "args": {}}]}, outputs)

    def test_an_unknown_step_kind_is_refused(self, tmp_path, outputs):
        # The schema gate would reject it, but the schema gate has not run. An
        # unknown kind is not a fix step, so decide_delivery would refuse it as
        # "no fix step" -- correct, and asserted so the reason cannot drift into
        # a mode.
        with pytest.raises(SystemExit):
            run(tmp_path, {"steps": [{"id": "s0", "kind": "exfiltrate", "args": {}}]}, outputs)
        assert "mode=" not in outputs.text()


class TestItTouchesNothingElse:
    def test_it_needs_no_token(self, tmp_path, outputs, monkeypatch):
        # The job holds permissions: {} and no secrets. Reading a token here would
        # be a credential in the one job that exists to have none.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert "mode=suggestions" in run(tmp_path, {"steps": [suggest()]}, outputs)

    def test_it_reads_only_the_plan(self, tmp_path, outputs):
        # No review.json, no diff, no changed_files, no quarantine tree: the
        # routing decision is over step kinds alone, and anything else it read
        # would be an input nothing has verified being trusted for more than
        # choosing a job.
        assert "mode=suggestions" in run(tmp_path, {"steps": [suggest()]}, outputs)
        # plan.json is the only thing it opened. The output file is the fixture's,
        # written by the router on purpose; everything else the bundle carries --
        # review.json, diff.patch, changed_files.json -- is absent here and the
        # routing decision did not miss it.
        assert not (tmp_path / "review.json").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["github_output", "plan.json"]
