"""Tests for canonicalize.py — the one spelling of "invisible" and of "decode".

The invisible table's behaviour is exercised through its consumers (the fence in
test_artifact.py, the secret scans in the two adversarial corpora). What is
pinned here is the module's own contract: which code points count, and that no
read ever depends on the platform's preferred encoding.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "smtithy"))

from canonicalize import (  # noqa: E402
    is_default_ignorable,
    is_invisible,
    read_contributor_text,
    read_harness_text,
    strip_invisible,
)

LATIN1_DIFF = b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n+caf\xe9\n"


class TestIsInvisible:
    @pytest.mark.parametrize(
        "ch",
        ["​", "‍", "‮", "⁦", "﻿", "­",  # Cf
         "͏", "️", "᠋",  # Mn, default-ignorable
         "ㅤ", "ﾠ",  # Lo, default-ignorable
         "⁥",  # Cn, reserved default-ignorable
         "\x00", "\x1b"],  # Cc
    )
    def test_invisible_code_points(self, ch):
        assert is_invisible(ch)

    @pytest.mark.parametrize("ch", ["\n", "\r", "\t"])
    def test_whitespace_controls_are_visible_separation(self, ch):
        # Dropping a tab would fuse two innocent runs into a false secret.
        assert not is_invisible(ch)

    @pytest.mark.parametrize("ch", ["a", " ", "é", "́", "中", "🎉"])
    def test_ordinary_characters_are_visible(self, ch):
        # U+0301 is Mn but NOT default-ignorable: an accent renders.
        assert not is_invisible(ch)

    def test_default_ignorable_is_not_the_same_test_as_the_category(self):
        # The reason the table exists: these are Mn/Lo/Cn, so a category-only
        # test lets them through.
        for ch in ("͏", "️", "ㅤ"):
            assert is_default_ignorable(ch)

    def test_strip_keeps_whitespace_and_drops_the_rest(self):
        assert strip_invisible("a​b\tc\nd͏e") == "ab\tc\nde"


class TestContributorReads:
    """The diff is written as raw bytes in whatever encoding the changed files
    use, so its decode must not raise and must not consult the locale."""

    def test_undecodable_bytes_become_replacement_characters(self, tmp_path):
        path = tmp_path / "diff.patch"
        path.write_bytes(LATIN1_DIFF)
        text = read_contributor_text(path)
        assert "�" in text
        assert "diff --git" in text  # the rest of the diff survives

    def test_valid_utf8_round_trips(self, tmp_path):
        path = tmp_path / "diff.patch"
        path.write_bytes("+café\n".encode())
        assert read_contributor_text(path) == "+café\n"

    def test_harness_reads_are_strict(self, tmp_path):
        # Our own files: a decode error is a broken deployment, not something to
        # paper over.
        path = tmp_path / "policy.json"
        path.write_bytes(b"{\"k\": \"\xe9\"}")
        with pytest.raises(UnicodeDecodeError):
            read_harness_text(path)


class TestNoReadDependsOnTheLocale:
    """Under LC_ALL=C the platform codec can be ASCII, so an ordinary accented
    byte in a diff was enough to kill the job with no logged reason."""

    def context_dir(self, tmp_path):
        (tmp_path / "pr.json").write_text(
            json.dumps({"number": 1, "title": "t", "body": "b", "base_sha": "a", "head_sha": "h"}),
            encoding="utf-8",
        )
        (tmp_path / "diff.patch").write_bytes(LATIN1_DIFF)
        (tmp_path / "changed_files.json").write_text(json.dumps(["x.py"]), encoding="utf-8")
        return tmp_path

    def run_under_c_locale(self, code: str, tmp_path) -> subprocess.CompletedProcess:
        harness = Path(__file__).parent.parent / "src" / "smtithy"
        return subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            capture_output=True, text=True,
            env={"LC_ALL": "C", "LANG": "C", "PYTHONPATH": str(harness),
                 "PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
        )

    def test_build_user_message_survives_a_latin1_diff(self, tmp_path):
        context = self.context_dir(tmp_path)
        result = self.run_under_c_locale(
            "import sys; from pathlib import Path;"
            "from artifact import build_user_message;"
            "m = build_user_message(Path(sys.argv[1]));"
            "print('OK', len(m))",
            context,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_a_broken_context_is_logged_not_raised(self, tmp_path):
        # The other half of the finding: the crash was OPAQUE. A context that
        # cannot be assembled must leave a run_failed reason in the transcript
        # rather than a bare traceback and an empty job log.
        context = self.context_dir(tmp_path)
        (context / "changed_files.json").write_text("{not json", encoding="utf-8")
        out = tmp_path / "out"
        result = self.run_under_c_locale(
            "import sys; from pathlib import Path;"
            "import cc_loop;"
            "code = cc_loop.run(Path(sys.argv[1]), Path(sys.argv[1]), Path(sys.argv[1]),"
            "                   Path(sys.argv[1]) / 'out');"
            "print('exit', code)",
            context,
        )
        assert "exit 1" in result.stdout, result.stderr
        events = [
            __import__("json").loads(line)
            for line in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failures = [e for e in events if e["event"] == "run_failed"]
        assert failures and "cannot assemble" in failures[0]["reason"]

    def test_the_undecodable_byte_does_not_shift_line_numbers(self, tmp_path):
        # Provenance compares LINE NUMBERS, so replacement must not change how
        # many lines the walk sees.
        context = self.context_dir(tmp_path)
        result = self.run_under_c_locale(
            "import sys, json; from pathlib import Path;"
            "from verify import parse_diff_hunks;"
            "from canonicalize import read_contributor_text;"
            "h = parse_diff_hunks(read_contributor_text(Path(sys.argv[1]) / 'diff.patch'));"
            "print(json.dumps({k: sorted(v) for k, v in h.items()}))",
            context,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"x.py": [1]}
