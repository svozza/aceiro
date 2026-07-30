"""Tests for prepare_context.py — SHA-anchored context collection.

No network: api_json/api_request/paginate are stubbed. Covers the head-SHA
anchoring (before and after collection), the file/diff caps, and the happy
path that writes pr.json/diff.patch/changed_files.json anchored to the EVENT
base SHA (not the PR's live base).
"""

import json
import sys

import pytest

import prepare_context


def pr_payload(head="head-sha", changed_files=2, title="t", body="b"):
    return {"head": {"sha": head}, "changed_files": changed_files, "title": title, "body": body}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("HEAD_SHA", "head-sha")
    monkeypatch.setenv("BASE_SHA", "event-base-sha")
    output = tmp_path / "ctx"
    monkeypatch.setattr(sys, "argv", ["prepare_context.py", "--output-dir", str(output)])
    return output


@pytest.fixture
def stubs(monkeypatch):
    """Install stubs; return a config dict the test can mutate."""
    cfg = {
        "pr_sequence": [pr_payload(), pr_payload()],  # before, after collection
        "diff": b"diff --git a/x b/x\n",
        "files_pages": [[{"filename": "x.py"}]],
    }

    def fake_api_json(path, method="GET", payload=None):
        return cfg["pr_sequence"].pop(0)

    def fake_api_request(path, method="GET", payload=None, accept="application/vnd.github+json"):
        return cfg["diff"]

    def fake_paginate(path_with_query):
        yield from cfg["files_pages"]

    monkeypatch.setattr(prepare_context, "api_json", fake_api_json)
    monkeypatch.setattr(prepare_context, "api_request", fake_api_request)
    monkeypatch.setattr(prepare_context, "paginate", fake_paginate)
    return cfg


class TestMain:
    def test_happy_path_writes_all_files(self, env, stubs):
        prepare_context.main()
        pr = json.loads((env / "pr.json").read_text())
        assert pr == {
            "number": 1,
            "title": "t",
            "body": "b",
            "base_sha": "event-base-sha",  # event base, not PR live base
            "head_sha": "head-sha",
        }
        assert (env / "diff.patch").read_bytes() == stubs["diff"]
        assert json.loads((env / "changed_files.json").read_text()) == ["x.py"]

    def test_head_moved_before_collection_aborts(self, env, stubs):
        stubs["pr_sequence"] = [pr_payload(head="different-sha")]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "pr.json").exists()

    def test_too_many_files_aborts(self, env, stubs):
        stubs["pr_sequence"] = [pr_payload(changed_files=prepare_context.MAX_CHANGED_FILES + 1)]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "diff.patch").exists()

    def test_oversized_diff_aborts(self, env, stubs):
        stubs["diff"] = b"x" * (prepare_context.MAX_DIFF_BYTES + 1)
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "diff.patch").exists()

    def test_head_moved_during_collection_aborts(self, env, stubs):
        # First fetch matches; the post-collection recheck sees a moved head.
        stubs["pr_sequence"] = [pr_payload(), pr_payload(head="moved-sha")]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "pr.json").exists()

    def test_changed_files_span_multiple_pages(self, env, stubs):
        stubs["files_pages"] = [[{"filename": "a.py"}], [{"filename": "b.py"}]]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["a.py", "b.py"]
