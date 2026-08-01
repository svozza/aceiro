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

import author_trust  # noqa: E402
import environment_gate  # noqa: E402


def reviewers_rule(reviewers):
    return {"type": "required_reviewers", "prevent_self_review": False, "reviewers": reviewers}


REVIEWER = {"type": "User", "reviewer": {"login": "svozza", "id": 8573472}}


def stub_environment(monkeypatch, response, permissions=None, teams=None):
    """The environment response, plus what the reviewer-trust resolution reads.

    `permissions` maps a user login to its collaborator permission, `teams` a
    team slug to the team's permission on the repository. Both default to
    write-or-above for anyone, since almost every case here is about the RULE
    rather than about who is on it.
    """
    users = {} if permissions is None else permissions
    team_list = [{"slug": slug, "permission": p} for slug, p in (teams or {}).items()]

    monkeypatch.setattr(environment_gate, "api_json", lambda path, **kwargs: response)
    monkeypatch.setattr(environment_gate, "paginate", lambda path: iter([team_list]))
    # is_trusted resolves through author_trust's OWN client, deliberately: the
    # gate reuses that module's definition of trusted rather than restating it.
    monkeypatch.setattr(
        author_trust, "api_json",
        lambda path, **kwargs: {"permission": users.get(path.split("/collaborators/")[1].split("/")[0], "write")},
    )


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


class TestEveryEligibleReviewerIsTrusted:
    """A rule with a reviewer on it is only a gate if the reviewer's approval
    means something. GitHub lets a READ-ONLY collaborator be an environment
    reviewer, and a deployment reviewer may approve their own run — so a rule
    naming one lets that account open a PR, approve its own deployment, and
    reach the credential without ever holding write.

    Author trust is resolved through author_trust.is_trusted, which asks the
    permission API and fails closed on anything unresolvable — the same
    vocabulary, since "trusted" has to mean one thing in this harness.
    """

    def test_a_write_holding_reviewer_gates(self, monkeypatch):
        stub_environment(
            monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]}, permissions={"svozza": "write"}
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is True

    def test_an_admin_reviewer_gates(self, monkeypatch):
        stub_environment(
            monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]}, permissions={"svozza": "admin"}
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is True

    def test_a_read_only_reviewer_does_not_gate(self, monkeypatch):
        # The finding's scenario. `read` is what a read-only outside
        # collaborator reports, and author_trust.py already records that such an
        # account reads as COLLABORATOR — so the association says nothing and the
        # permission is the only answer.
        stub_environment(
            monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]}, permissions={"svozza": "read"}
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_one_untrusted_reviewer_among_trusted_ones_does_not_gate(self, monkeypatch):
        # ANY eligible reviewer can approve, so the weakest one decides. A rule
        # listing four maintainers and one read-only account gates nothing.
        readonly = {"type": "User", "reviewer": {"login": "drive-by", "id": 2}}
        stub_environment(
            monkeypatch,
            {"protection_rules": [reviewers_rule([REVIEWER, readonly])]},
            permissions={"svozza": "admin", "drive-by": "read"},
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_an_unresolvable_reviewer_does_not_gate(self, monkeypatch):
        stub_environment(
            monkeypatch, {"protection_rules": [reviewers_rule([REVIEWER])]}, permissions={"svozza": "none"}
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_a_reviewer_of_an_unknown_shape_does_not_gate(self, monkeypatch):
        # Fail closed on a reviewer this code cannot resolve to a permission at
        # all, rather than treating "I do not know what this is" as trusted.
        stub_environment(monkeypatch, {"protection_rules": [reviewers_rule([{"type": "App", "reviewer": {}}])]})
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_a_write_holding_team_gates(self, monkeypatch):
        team = {"type": "Team", "reviewer": {"slug": "maintainers", "name": "Maintainers", "id": 9}}
        stub_environment(
            monkeypatch,
            {"protection_rules": [reviewers_rule([team])]},
            teams={"maintainers": "push"},
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is True

    def test_a_read_only_team_does_not_gate(self, monkeypatch):
        team = {"type": "Team", "reviewer": {"slug": "triage", "name": "Triage", "id": 9}}
        stub_environment(
            monkeypatch,
            {"protection_rules": [reviewers_rule([team])]},
            teams={"triage": "pull"},
        )
        assert environment_gate.has_required_reviewers("o/r", "ai-pr-review") is False

    def test_a_team_the_repository_does_not_list_does_not_gate(self, monkeypatch):
        team = {"type": "Team", "reviewer": {"slug": "ghosts", "name": "Ghosts", "id": 9}}
        stub_environment(
            monkeypatch,
            {"protection_rules": [reviewers_rule([team])]},
            teams={"maintainers": "push"},
        )
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
