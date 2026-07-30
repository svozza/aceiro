"""Tests for environment_gate.py — the in-code assertion that the approval
environment actually gates (ADR-0006).

The threat is a SILENT fail-open: GitHub creates a referenced-but-missing
environment with no protection rules, the approve job passes instantly, and
the generator runs ungated with a green run. So the load-bearing cases are
the refusals: 404, empty rules, empty reviewer list, wrong rule type, HTTP
error. Per the ADR these are mutation-verified — each was run against a
variant of has_required_reviewers that returns True on an empty reviewer
list, and failed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "smtithy"))

import environment_gate  # noqa: E402


def reviewers_rule(reviewers):
    return {"type": "required_reviewers", "prevent_self_review": False, "reviewers": reviewers}


REVIEWER = {"type": "User", "reviewer": {"login": "svozza", "id": 8573472}}


def stub_environment(monkeypatch, response):
    monkeypatch.setattr(environment_gate, "api_json", lambda path, **kwargs: response)


def stub_environment_error(monkeypatch, exc):
    def raise_it(path, **kwargs):
        raise exc

    monkeypatch.setattr(environment_gate, "api_json", raise_it)


class TestHasRequiredReviewers:
    def test_environment_with_a_reviewer_gates(self, monkeypatch):
        stub_environment(monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is True

    def test_reviewer_rule_among_other_rules_is_found(self, monkeypatch):
        stub_environment(
            monkeypatch,
            {"protection_rules": [{"type": "wait_timer", "wait_timer": 30}, reviewers_rule([REVIEWER])]},
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is True

    def test_empty_reviewer_list_does_not_gate(self, monkeypatch):
        # The mutation target: a rule of the right TYPE with nobody on it
        # approves nothing. Accepting it re-opens the fail-open.
        stub_environment(monkeypatch, {"protection_rules": [reviewers_rule([])]})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_no_protection_rules_does_not_gate(self, monkeypatch):
        # The implicitly-created environment: exists, carries nothing.
        stub_environment(monkeypatch, {"protection_rules": []})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_missing_rules_key_does_not_gate(self, monkeypatch):
        stub_environment(monkeypatch, {"name": "ai-pr-review"})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_wrong_rule_type_does_not_gate(self, monkeypatch):
        # A wait timer or branch policy is not a human; only required_reviewers
        # is the gate ADR-0006 means.
        stub_environment(monkeypatch, {"protection_rules": [{"type": "wait_timer", "wait_timer": 30}]})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_http_error_does_not_gate(self, monkeypatch):
        # 404 is the common case: the environment does not exist yet (or was
        # just implicitly created). Any other error reads the same way --
        # "could not establish" collapses to "does not gate".
        stub_environment_error(monkeypatch, OSError("HTTP Error 404: Not Found"))
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_non_dict_response_does_not_gate(self, monkeypatch):
        stub_environment(monkeypatch, [])
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False


@pytest.fixture
def gate_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GATE_ENVIRONMENT", "ai-pr-review")
    monkeypatch.setenv("GITHUB_TOKEN", "t")


class TestMain:
    def test_untrusted_author_with_real_gate_proceeds(self, gate_env, monkeypatch):
        monkeypatch.setenv("AUTHOR_TRUSTED", "false")
        monkeypatch.setenv("PR_DRAFT", "false")
        stub_environment(monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]})
        assert environment_gate.main() == 0

    def test_untrusted_author_with_ungated_environment_refuses(self, gate_env, monkeypatch):
        monkeypatch.setenv("AUTHOR_TRUSTED", "false")
        monkeypatch.setenv("PR_DRAFT", "false")
        stub_environment(monkeypatch, {"protection_rules": []})
        assert environment_gate.main() == 1

    def test_draft_from_trusted_author_still_needs_the_gate(self, gate_env, monkeypatch):
        # Drafts wait at the gate regardless of author trust -- same rule the
        # workflow's approve job applies.
        monkeypatch.setenv("AUTHOR_TRUSTED", "true")
        monkeypatch.setenv("PR_DRAFT", "true")
        stub_environment(monkeypatch, {"protection_rules": []})
        assert environment_gate.main() == 1

    def test_trusted_nondraft_needs_no_gate(self, gate_env, monkeypatch):
        # The trusted path never waited at the environment, so an ungated
        # environment is not a lie about THIS run. No API call should even
        # be needed.
        monkeypatch.setenv("AUTHOR_TRUSTED", "true")
        monkeypatch.setenv("PR_DRAFT", "false")
        stub_environment_error(monkeypatch, AssertionError("should not be called"))
        assert environment_gate.main() == 0

    def test_missing_trust_reads_as_untrusted(self, gate_env, monkeypatch):
        # AUTHOR_TRUSTED absent (the output never made it across the job
        # boundary) must not read as trusted.
        monkeypatch.delenv("AUTHOR_TRUSTED", raising=False)
        monkeypatch.setenv("PR_DRAFT", "false")
        stub_environment(monkeypatch, {"protection_rules": []})
        assert environment_gate.main() == 1

    def test_missing_draft_flag_reads_as_draft(self, gate_env, monkeypatch):
        monkeypatch.setenv("AUTHOR_TRUSTED", "true")
        monkeypatch.delenv("PR_DRAFT", raising=False)
        stub_environment(monkeypatch, {"protection_rules": []})
        assert environment_gate.main() == 1
