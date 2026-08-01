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


def diff_for(*paths):
    """A minimal diff whose hunks make line 1 of each path anchorable."""
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,1 @@\n+one\n" for p in paths
    ).encode()


@pytest.fixture
def stubs(monkeypatch):
    """Install stubs; return a config dict the test can mutate.

    `compare_pages` is the files list from the SHA-anchored compare endpoint —
    the same call the diff comes from, which is the point of finding 6.
    """
    cfg = {
        "pr_sequence": [pr_payload(), pr_payload()],  # before, after collection
        "diff": diff_for("x.py"),
        "compare_pages": [{"files": [{"filename": "x.py"}]}],
        "requested": [],
    }

    def fake_api_json(path, method="GET", payload=None):
        cfg["requested"].append(path)
        if "/compare/" in path:
            return cfg["compare_pages"].pop(0) if cfg["compare_pages"] else {"files": []}
        return cfg["pr_sequence"].pop(0)

    def fake_api_request(path, method="GET", payload=None, accept="application/vnd.github+json"):
        cfg["requested"].append(path)
        return cfg["diff"]

    monkeypatch.setattr(prepare_context, "api_json", fake_api_json)
    monkeypatch.setattr(prepare_context, "api_request", fake_api_request)
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
        stubs["diff"] = diff_for("a.py", "b.py")
        stubs["compare_pages"] = [{"files": [{"filename": "a.py"}, {"filename": "b.py"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["a.py", "b.py"]


class TestChangedFilesAreAnchoredToTheEventBase:
    """The file list must come from the SAME anchored comparison as the diff.

    diff.patch is fetched from /compare/{base_sha}...{head}, but the list came
    from /pulls/N/files, which GitHub recomputes against the PR's CURRENT base
    branch tip. The module docstring claims everything is anchored to the
    recorded SHAs, and the comment at the BASE_SHA read explicitly recognises
    that the base may advance while the run waits at the approval gate — the
    file list was the one artifact derived from the live base.
    """

    def test_the_list_comes_from_the_compare_endpoint(self, env, stubs):
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["x.py"]
        assert any("/compare/event-base-sha...head-sha" in p for p in stubs["requested"])

    def test_the_live_files_endpoint_is_never_consulted(self, env, stubs):
        # /pulls/{n}/files is recomputed against the CURRENT base tip, so
        # reaching it at all reintroduces the drift — asserted on the request
        # log rather than by stubbing, since the module no longer imports
        # paginate and a reintroduction might not use it.
        prepare_context.main()
        assert not any("/files" in path for path in stubs["requested"]), stubs["requested"]

    def test_a_path_in_the_diff_but_not_the_list_aborts(self, env, stubs):
        # The inverse of the base-advance scenario: a file the anchored diff
        # shows hunks for, absent from the list, makes every finding on it
        # rejected by check_provenance while the model was shown the hunks.
        stubs["diff"] = diff_for("x.py", "drifted.py")
        stubs["compare_pages"] = [{"files": [{"filename": "x.py"}]}]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "changed_files.json").exists()

    def test_a_path_in_the_list_but_not_the_diff_aborts(self, env, stubs):
        # Advertises to the model a file whose contents were never part of the
        # reviewed comparison.
        stubs["compare_pages"] = [{"files": [{"filename": "x.py"}, {"filename": "ghost.py"}]}]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "changed_files.json").exists()

    def test_a_deleted_file_does_not_trip_the_assertion(self, env, stubs):
        # A deletion is in the files list but contributes NO walk_diff path
        # (its `+++ ` target is /dev/null), so a naive set equality would fail
        # every PR that deletes a file. The assertion is one-directional per
        # side: no path may appear in only one place for a reason other than
        # this one.
        stubs["diff"] = (
            diff_for("x.py")
            + b"diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-z\n"
        )
        stubs["compare_pages"] = [{"files": [{"filename": "x.py"}, {"filename": "gone.py"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["x.py", "gone.py"]

    def test_a_c_quoted_path_does_not_trip_the_assertion(self, env, stubs):
        # The C-quoting fix is load-bearing here: undecoded, the diff's path key
        # is `"b/caf\303\251.py"` and the API's is `café.py`, so the assertion
        # this finding adds would abort every PR touching such a file.
        stubs["diff"] = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,1 +1,1 @@\n+one\n"
        ).encode()
        stubs["compare_pages"] = [{"files": [{"filename": "café.py"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["café.py"]

    def test_a_binary_file_does_not_trip_the_assertion(self, env, stubs):
        # GitHub emits no hunks for a binary file, so it too is in the list and
        # absent from walk_diff's numbered positions.
        stubs["diff"] = (
            diff_for("x.py")
            + b"diff --git a/img.png b/img.png\n"
            + b"Binary files a/img.png and b/img.png differ\n"
        )
        stubs["compare_pages"] = [{"files": [{"filename": "x.py"}, {"filename": "img.png"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["x.py", "img.png"]
