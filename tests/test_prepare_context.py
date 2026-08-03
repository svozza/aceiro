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
        # The head tree, bounded: the quarantine checks out every blob here.
        "tree": {"truncated": False, "tree": [{"path": "x.py", "type": "blob", "size": 100}]},
        "requested": [],
    }

    def fake_api_json(path, method="GET", payload=None):
        cfg["requested"].append(path)
        if "/compare/" in path:
            return cfg["compare_pages"].pop(0) if cfg["compare_pages"] else {"files": []}
        if "/git/trees/" in path:
            return cfg["tree"]
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


class TestTheCollectedListIsBoundedItself:
    """The 300-file cap is checked on `pr["changed_files"]` — a COUNT on the PR
    object — and separately on the collected list, but nothing bounds the list's
    SIZE.

    A count does not bound bytes. GitHub imposes no path-length limit worth
    relying on, so 300 files with very long paths serialise to ~150 KB of
    changed_files.json, which is fenced into the prompt as its own block on top of
    the 1.5 MB diff. The diff's byte cap cannot see it: a rename of deeply nested
    paths produces a small diff and a large list.
    """

    def long_paths(self, count, segment=250):
        return [f"{'a' * segment}/{'b' * segment}/f{i}.py" for i in range(count)]

    def test_a_list_over_the_byte_ceiling_aborts(self, env, stubs):
        paths = self.long_paths(300)
        stubs["diff"] = diff_for(*paths)
        stubs["compare_pages"] = [{"files": [{"filename": p} for p in paths]}]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "changed_files.json").exists()

    def test_the_refusal_names_the_ceiling(self, env, stubs, capsys):
        paths = self.long_paths(300)
        stubs["diff"] = diff_for(*paths)
        stubs["compare_pages"] = [{"files": [{"filename": p} for p in paths]}]
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert "MAX_CHANGED_FILES_BYTES" in capsys.readouterr().err or str(
            prepare_context.MAX_CHANGED_FILES_BYTES
        ) in capsys.readouterr().err

    def test_an_ordinary_list_passes(self, env, stubs):
        # Calibration: 300 files of ordinary path length is under the ceiling, so
        # the byte bound must not become a second, tighter file-count cap.
        paths = [f"src/module_{i}/file.py" for i in range(300)]
        stubs["diff"] = diff_for(*paths)
        stubs["compare_pages"] = [{"files": [{"filename": p} for p in paths]}]
        prepare_context.main()
        assert len(json.loads((env / "changed_files.json").read_text())) == 300


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

    def test_a_binary_file_in_a_directory_with_a_space_does_not_trip_the_assertion(self, env, stubs):
        # Reproduced against real git: a directory named `x b/` makes the header
        # `diff --git a/x b/z.png b/x b/z.png`, which cannot be split on a space
        # at all — git does not quote a path merely for containing one. A binary
        # file has no ---/+++ pair either, so the header was the only mention, and
        # the run ABORTED on a legitimate PR: "changed-file list names
        # ['x b/z.png'] which the anchored diff never mentions".
        stubs["diff"] = (
            diff_for("x.py")
            + b"diff --git a/x b/z.png b/x b/z.png\n"
            + b"Binary files a/x b/z.png and b/x b/z.png differ\n"
        )
        stubs["compare_pages"] = [{"files": [{"filename": "x.py"}, {"filename": "x b/z.png"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["x.py", "x b/z.png"]

    def test_a_text_file_in_a_directory_with_a_space_does_not_trip_the_assertion(self, env, stubs):
        # The same path shape with hunks, where the +++ header names it
        # unambiguously and must be what decides.
        stubs["diff"] = (
            b"diff --git a/x b/z.py b/x b/z.py\n"
            b"--- a/x b/z.py\n"
            b"+++ b/x b/z.py\n"
            b"@@ -1,1 +1,1 @@\n+one\n"
        )
        stubs["compare_pages"] = [{"files": [{"filename": "x b/z.py"}]}]
        prepare_context.main()
        assert json.loads((env / "changed_files.json").read_text()) == ["x b/z.py"]


class TestTheHeadTreeIsBounded:
    """The diff caps cannot bound the checkout. A binary addition produces a
    tiny diff and retains its full blob cost — measured: a 200 KB binary add is
    a 231-byte diff — so 150 incompressible 99 MB files stay under both the
    300-file and 1.5 MB caps while the quarantine fetch attempts ~15 GB.

    Measured from the tree API, which reports every blob's size before any
    bytes are transferred, so the refusal costs one request rather than a
    filled disk.
    """

    def tree(self, *sizes, truncated=False):
        return {
            "truncated": truncated,
            "tree": [
                {"path": f"f{i}.bin", "type": "blob", "size": size}
                for i, size in enumerate(sizes)
            ],
        }

    def test_a_normal_tree_passes(self):
        prepare_context.assert_head_tree_within_caps(self.tree(1000, 2000, 3000))

    def test_a_blob_with_no_size_is_refused_by_path(self):
        # Both arithmetic sites coerced an absent size to zero, so an entry with no
        # `size` passed the per-blob and the aggregate cap alike, and the
        # `git fetch --depth 1` that follows has no bound of its own. Cheap
        # hardening rather than a measured bypass: neither review engine could
        # produce a real tree response with a blob entry missing `size`, which is
        # why the refusal names the path -- so a first sighting is diagnosable.
        listing = {"truncated": False, "tree": [{"path": "opaque.bin", "type": "blob"}]}
        with pytest.raises(SystemExit):
            prepare_context.assert_head_tree_within_caps(listing)

    def test_a_blob_whose_size_is_not_an_integer_is_refused(self):
        listing = {"truncated": False, "tree": [{"path": "odd.bin", "type": "blob", "size": "1024"}]}
        with pytest.raises(SystemExit):
            prepare_context.assert_head_tree_within_caps(listing)

    def test_a_non_blob_entry_needs_no_size(self):
        # Trees and submodules legitimately carry none, and refusing them would
        # abort every repository with a subdirectory.
        listing = {"truncated": False, "tree": [
            {"path": "src", "type": "tree"},
            {"path": "vendor/lib", "type": "commit"},
            {"path": "a.py", "type": "blob", "size": 10},
        ]}
        prepare_context.assert_head_tree_within_caps(listing)

    def test_an_oversized_aggregate_aborts(self, capsys):
        over = prepare_context.MAX_TREE_BYTES // 2 + 1
        with pytest.raises(SystemExit):
            prepare_context.assert_head_tree_within_caps(self.tree(over, over))
        assert "head tree" in capsys.readouterr().err

    def test_a_single_oversized_blob_aborts(self, capsys):
        with pytest.raises(SystemExit):
            prepare_context.assert_head_tree_within_caps(self.tree(prepare_context.MAX_BLOB_BYTES + 1))
        assert "f0.bin" in capsys.readouterr().err, "the offending path must be named"

    def test_a_truncated_tree_aborts(self, capsys):
        # Fail closed: a truncated listing cannot bound what was not listed.
        with pytest.raises(SystemExit):
            prepare_context.assert_head_tree_within_caps(self.tree(10, truncated=True))
        assert "truncated" in capsys.readouterr().err

    def test_subtrees_are_not_counted_as_content(self):
        # Only blobs have a size; a tree entry carries none, and summing None
        # would crash rather than refuse.
        listing = self.tree(10)
        listing["tree"].append({"path": "dir", "type": "tree"})
        prepare_context.assert_head_tree_within_caps(listing)

    def test_main_checks_the_tree_before_writing_context(self, env, stubs):
        # Asserted through main() because the seam is the wiring: the check can
        # be correct while nothing calls it, which no unit test of it would see.
        over = prepare_context.MAX_BLOB_BYTES + 1
        stubs["tree"] = {"truncated": False, "tree": [{"path": "huge.bin", "type": "blob", "size": over}]}
        with pytest.raises(SystemExit):
            prepare_context.main()
        assert not (env / "diff.patch").exists()
