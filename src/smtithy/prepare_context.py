"""Collect SHA-anchored PR context via the GitHub API.

Runs in the review job before the agent. Anchors everything to the head SHA
recorded in the triggering event: verifies the PR's current head equals that
SHA before and after collection, so a mid-run push cannot swap the content
under review. Applies sanity caps (files/bytes) and fails loud on breach.

"Everything" includes the changed-file list: it comes from the same anchored
compare call as the diff, never from /pulls/{n}/files, and the two are asserted
to describe the same comparison.

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
    binary file (no ---/+++ pair at all) is named. Over-approximates on purpose:
    it feeds an allow-set, so counting loosely costs a missed check at worst.
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

    Two directions at different strengths, because the diff and the list are not
    symmetric: EXACTLY every path with hunks must be listed (otherwise
    check_provenance rejects findings on hunks the prompt showed the model), while
    a listed path need only be NAMED in the diff — a deletion or a binary file
    legitimately has no hunks, so requiring them would abort every PR that
    deletes a file.
    """
    # errors="replace": a binary diff is not decodable and only headers are read
    # here. Decode discipline for the reviewed bytes belongs to diff.patch's
    # consumers.
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

    # From the SAME anchored comparison as the diff, never /pulls/{n}/files: that
    # endpoint recomputes against the base branch's CURRENT tip, which may have
    # advanced while the run sat at the approval gate.
    compare = api_json(compare_path)
    changed_files = [item["filename"] for item in compare.get("files", [])]
    # The endpoint returns at most 300 files per page, and MAX_CHANGED_FILES is
    # 300, enforced above; a truncated list would also trip the assertion below.
    if len(changed_files) > MAX_CHANGED_FILES:
        fail(f"compare lists {len(changed_files)} files (cap {MAX_CHANGED_FILES}); no review")

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
