"""Golden artifacts that MUST verify. If one of these fails, the verifier is
rejecting legitimate reviews (fail-closed is right, but useless if nothing
passes)."""

import pytest

from conftest import DELETED_FILE_DIFF
from verify import parse_diff_hunks, verify


def test_valid_artifact_passes(valid_artifact, sample_diff, changed_files, policy):
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_empty_findings_pass(valid_artifact, sample_diff, changed_files, policy):
    valid_artifact["findings"] = []
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_all_severities_pass(valid_artifact, sample_diff, changed_files, policy):
    finding = valid_artifact["findings"][0]
    for severity in ("critical", "high", "medium", "low"):
        finding["severity"] = severity
        verify(valid_artifact, sample_diff, changed_files, policy)


def test_allowed_markdown_passes(valid_artifact, sample_diff, changed_files, policy):
    valid_artifact["summary"] = (
        "Some *emphasis*, **strong**, `code`, ~~strikethrough~~ and:\n\n"
        "```python\nraise ValueError\n```\n\n"
        "- a list item\n"
        "- another\n\n"
        "1. ordered"
    )
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_allowlisted_links_pass(valid_artifact, sample_diff, changed_files, policy):
    valid_artifact["summary"] = (
        "See [the docs](https://docs.powertools.aws.dev/lambda/python/latest/) and "
        "[the repo](https://github.com/aws-powertools/powertools-lambda-python/issues/1)."
    )
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_deep_path_under_prefix_with_dots_passes(valid_artifact, sample_diff, changed_files, policy):
    # Rejecting dot SEGMENTS must not reject dots in ordinary filenames: a
    # legitimate deep path under the allowlisted prefix stays legitimate. The
    # traversal cases these bracket are in
    # test_verify_adversarial.py TestExfiltration.
    valid_artifact["summary"] = (
        "See [the file]"
        "(https://github.com/aws-powertools/powertools-lambda-python/blob/v2.1.0/"
        "aws_lambda_powertools/shared/functions.py#L10) and "
        "[a dotfile](https://github.com/aws-powertools/powertools-lambda-python/"
        "blob/main/.github/workflows/ci.yml)."
    )
    verify(valid_artifact, sample_diff, changed_files, policy)


DOTFILE_DIFF = (
    "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
    "--- a/.github/workflows/ci.yml\n"
    "+++ b/.github/workflows/ci.yml\n"
    "@@ -1,2 +1,3 @@\n"
    " name: ci\n"
    "+  persist-credentials: true\n"
    " on: push\n"
)


def test_finding_on_dotfile_path_passes(valid_artifact, policy):
    # Harness/CI PRs change files under .github/ (or .gitignore etc.); a
    # leading dot must not make such findings unanchorable.
    valid_artifact["findings"] = [
        {
            "path": ".github/workflows/ci.yml",
            "line": 2,
            "severity": "high",
            "title": "checkout persists credentials",
            "body": "The new step leaves the token on disk for later steps.",
        }
    ]
    verify(valid_artifact, DOTFILE_DIFF, [".github/workflows/ci.yml"], policy)


def test_finding_on_new_file_passes(valid_artifact, sample_diff, changed_files, policy):
    valid_artifact["findings"] = [
        {
            "path": "tests/unit/test_logger.py",
            "line": 4,
            "severity": "medium",
            "title": "test asserts the buggy behaviour",
            "body": "The new test pins `get_level()` to `None`, codifying the bug.",
        }
    ]
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_residual_risk_can_be_populated(valid_artifact, sample_diff, changed_files, policy):
    valid_artifact["residual_risk"] = "Could not confirm thread-safety of the new cache."
    verify(valid_artifact, sample_diff, changed_files, policy)


def test_max_findings_pass(valid_artifact, sample_diff, changed_files, policy):
    finding = valid_artifact["findings"][0]
    valid_artifact["findings"] = [dict(finding) for _ in range(10)]
    verify(valid_artifact, sample_diff, changed_files, policy)


class TestHunkParsing:
    def test_modified_file_lines(self, sample_diff):
        hunks = parse_diff_hunks(sample_diff)
        lines = hunks["aws_lambda_powertools/logging/logger.py"]
        # Hunk covers new lines 10-18: context + added lines.
        assert 13 in lines  # added line
        assert 10 in lines  # context line
        assert 100 not in lines

    def test_new_file_lines(self, sample_diff):
        hunks = parse_diff_hunks(sample_diff)
        assert hunks["tests/unit/test_logger.py"] == {1, 2, 3, 4}

    def test_deleted_lines_not_counted(self, sample_diff):
        # The removed "def get_level():" line has no new-side number; ensure
        # counting stayed aligned (line 13 is "if default is None:").
        hunks = parse_diff_hunks(sample_diff)
        assert max(hunks["aws_lambda_powertools/logging/logger.py"]) <= 20

    def test_deleted_file_ignored(self):
        assert parse_diff_hunks(DELETED_FILE_DIFF) == {}
