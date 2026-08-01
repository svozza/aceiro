"""Collect SHA-anchored PR context via the GitHub API.

Runs in the review job before the agent. Anchors everything to the head SHA
recorded in the triggering event: verifies the PR's current head equals that
SHA before and after collection, so a mid-run push cannot swap the content
under review. Applies sanity caps (files/bytes) and fails loud on breach.

"Everything" includes the changed-file list, which comes from the same anchored
compare call as the diff rather than from /pulls/{n}/files — that endpoint is
recomputed against the base branch's CURRENT tip, which may have advanced while
the run sat at the approval gate. The two artifacts are then asserted to describe
the same comparison, because sharing an endpoint is an argument while an
assertion is a check.

Writes to --output-dir: pr.json, diff.patch, changed_files.json

Environment: GITHUB_TOKEN (read-only), GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA,
BASE_SHA (the event's base SHA — the commit the review job checked out).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from diff_map import split_diff_lines, unquote_path, walk_diff
from github_api import api_json, api_request, fail

MAX_CHANGED_FILES = 300
MAX_DIFF_BYTES = 1_500_000


def fetch_pr(repo: str, pr_number: int) -> dict:
    return api_json(f"/repos/{repo}/pulls/{pr_number}")


DIFF_GIT_RE = re.compile(r'^diff --git (?:"a/.*"|a/.*) (?:"(b/.*)"|(b/.*))$')


def diff_mentioned_paths(text: str) -> set[str]:
    """Every path the diff names at all, whether or not it has hunks.

    The `diff --git` header is the only place a deletion (`+++ /dev/null`) or a
    binary file ("Binary files … differ", which emits no ---/+++ pair at all) is
    named on the new side. Deliberately over-approximating: this feeds an
    allow-set, so a path counted loosely produces a missed check at worst, while
    the exact direction is asserted separately below.
    """
    mentioned: set[str] = set()
    for line in split_diff_lines(text):
        if match := DIFF_GIT_RE.match(line):
            target = unquote_path(f'"{match.group(1)}"' if match.group(1) else match.group(2) or "")
            mentioned.add(target.removeprefix("b/"))
        elif line.startswith("+++ ") or line.startswith("--- "):
            target = unquote_path(line[4:].split("\t")[0])
            if target != "/dev/null":
                mentioned.add(target.removeprefix("b/").removeprefix("a/"))
    return mentioned


def assert_diff_and_list_agree(diff: bytes, changed_files: list[str]) -> None:
    """Fail loud if the anchored diff and the file list describe different sets.

    The two directions are checked at different strengths, because the diff and
    the list are not symmetric by construction.

    EXACT, and the direction that breaks a review: every path the diff attributes
    hunks to must be in the list. Otherwise check_provenance rejects every finding
    anchored there — while the prompt showed the model those very hunks — and one
    such file discards the whole artifact. This is the direction an advancing base
    branch produces.

    LOOSE, and the direction that would silently mislead: every path in the list
    must at least be NAMED in the diff. A deletion and a binary file legitimately
    have no hunks, so requiring hunks here would abort every PR that deletes a
    file; requiring only a mention still catches a list entry the anchored
    comparison never covered.
    """
    # utf-8 with replacement: a diff carrying binary content is not decodable, and
    # only the path headers are read here. The decode discipline for the reviewed
    # bytes themselves belongs to the consumers of diff.patch.
    text = diff.decode("utf-8", errors="replace")
    listed = set(changed_files)

    anchored = {
        position.path
        for position in walk_diff(text)
        if position.path is not None and (position.new_line is not None or position.is_hunk_header)
    }
    if missing := sorted(anchored - listed):
        fail(
            f"anchored diff carries hunks for {missing} which the changed-file list omits; "
            "the two are not describing the same comparison"
        )

    if unmentioned := sorted(listed - diff_mentioned_paths(text)):
        fail(
            f"changed-file list names {unmentioned} which the anchored diff never mentions; "
            "the two are not describing the same comparison"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    expected_head = os.environ["HEAD_SHA"]
    # Anchor to the EVENT's base SHA, not the PR's current one: the review job
    # checked out the event base, and the base branch may have advanced while
    # the run sat at the human-approval gate. Diff and checkout must agree.
    base_sha = os.environ["BASE_SHA"]

    pr = fetch_pr(repo, pr_number)
    if pr["head"]["sha"] != expected_head:
        fail(f"head moved before collection: {pr['head']['sha']} != {expected_head}")

    if pr["changed_files"] > MAX_CHANGED_FILES:
        fail(f"PR changes {pr['changed_files']} files (cap {MAX_CHANGED_FILES}); no review")

    # SHA-anchored diff: compare base.sha...head SHA, immune to branch moves.
    compare_path = f"/repos/{repo}/compare/{base_sha}...{expected_head}"
    diff = api_request(compare_path, accept="application/vnd.github.diff")
    if len(diff) > MAX_DIFF_BYTES:
        fail(f"diff is {len(diff)} bytes (cap {MAX_DIFF_BYTES}); no review")

    # The changed-file list comes from the SAME anchored comparison as the diff,
    # NOT from /pulls/{n}/files: that endpoint recomputes against the PR's CURRENT
    # base branch tip, and the base may have advanced while the run sat at the
    # approval gate (the reason BASE_SHA is read above). When it does, the two
    # artifacts disagree — the diff carries hunks for a file the list omits, so
    # every finding the model anchors there is rejected by check_provenance for a
    # file the prompt showed it, and the whole review fails closed on a genuine
    # finding. Everything downstream (check_provenance, plan_verify's frame,
    # proveFrame) reads this list, so it has to describe the same comparison.
    compare = api_json(compare_path)
    changed_files = [item["filename"] for item in compare.get("files", [])]
    # The compare endpoint returns at most 300 files per page. MAX_CHANGED_FILES
    # is 300 and was already enforced above, so a PR that would truncate here has
    # been refused; the assertion below is what would catch it regardless, since a
    # truncated list drops paths the diff still carries hunks for.
    if len(changed_files) > MAX_CHANGED_FILES:
        fail(f"compare lists {len(changed_files)} files (cap {MAX_CHANGED_FILES}); no review")

    # And the two are asserted to agree, because "same endpoint" is an argument
    # about provenance while this is a check on the bytes in hand.
    assert_diff_and_list_agree(diff, changed_files)

    # TOCTOU recheck: the head must not have moved during collection.
    if fetch_pr(repo, pr_number)["head"]["sha"] != expected_head:
        fail("head moved during collection; aborting")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "pr.json").write_text(
        json.dumps(
            {
                "number": pr_number,
                "title": pr["title"],
                "body": pr["body"],
                "base_sha": base_sha,
                "head_sha": expected_head,
            },
            ensure_ascii=False,
        )
    )
    (args.output_dir / "diff.patch").write_bytes(diff)
    (args.output_dir / "changed_files.json").write_text(json.dumps(changed_files))
    print(f"context ready: {len(changed_files)} files, {len(diff)} diff bytes, head {expected_head}")


if __name__ == "__main__":
    main()
