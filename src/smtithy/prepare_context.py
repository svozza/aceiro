"""Collect SHA-anchored PR context via the GitHub API.

Runs in the review job before the agent. Anchors everything to the head SHA
recorded in the triggering event: verifies the PR's current head equals that
SHA before and after collection, so a mid-run push cannot swap the content
under review. Applies sanity caps (files/bytes) and fails loud on breach.

"Everything" includes the changed-file list: it comes from the same anchored
compare call as the diff, never from /pulls/{n}/files, and the two are asserted
to describe the same comparison.

The head tree's size is capped here too, though the tree is materialised by the
workflow's quarantine step rather than by this script: the tree API reports every
blob's size before any bytes move, so this is where the refusal is cheap.

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
from typing import cast

from diff_map import split_diff_lines, unquote_path, walk_diff
from github_api import api_json, api_request, fail

MAX_CHANGED_FILES = 300
MAX_DIFF_BYTES = 1_500_000

# The file list's own byte ceiling, because a COUNT does not bound bytes. The list
# is fenced into the prompt as its own block, and no path-length limit is worth
# relying on: 300 deeply nested paths serialise to ~150 KB on top of the diff,
# which MAX_DIFF_BYTES cannot see (a rename of nested paths is a small diff and a
# large list). Sized so an ordinary 300-file list passes with room to spare — this
# refuses a pathological list, not a large PR.
MAX_CHANGED_FILES_BYTES = 100_000

# The quarantine's bounds, which the diff caps cannot supply: a binary addition
# produces a tiny diff and retains its full blob cost (measured: a 200 KB binary
# add is a 231-byte diff), so a PR adding 150 incompressible 99 MB files stays
# under both caps while the head-tree fetch attempts ~15 GB. Sized for source
# trees the reviewer can plausibly review rather than for the runner's disk: the
# generator reads this tree with Read/Grep/Glob, and a repository above these
# bounds is not one a 30-turn review is going to cover.
MAX_BLOB_BYTES = 10_000_000
MAX_TREE_BYTES = 500_000_000


def fetch_pr(repo: str, pr_number: int) -> dict:
    return api_json(f"/repos/{repo}/pulls/{pr_number}")


# Only the QUOTED form is read from `diff --git`. Unquoted, the line is
# genuinely ambiguous: git does not quote a path merely for containing a space, so
# `diff --git a/x b/z.png b/x b/z.png` has no parse — every split point is a
# candidate, and a greedy `a/.*` picks the wrong one, yielding `z.png` for a file
# named `x b/z.png`.
DIFF_GIT_QUOTED_RE = re.compile(r'^diff --git "a/.*" "(b/.*)"$')

# A binary file has no ---/+++ pair, so this line is where its path is named.
# Anchored on ` and b/` rather than split on ` and `, which a filename containing
# that substring would defeat; the trailing ` differ` is stripped by the pattern.
BINARY_FILES_RE = re.compile(r'^Binary files (?:"?a/.*?"?) and (?:"(b/.*)"|(b/.*)) differ$')


def diff_mentioned_paths(text: str) -> set[str]:
    """Every path the diff names at all, whether or not it has hunks.

    Over-approximates on purpose: it feeds an allow-set, so counting loosely costs
    a missed check at worst.

    Read from the ---/+++ headers and the `Binary files` line, each of which names
    one path per line. The `diff --git` header names two on one line with a space
    between them and no quoting when the path itself contains a space, so it is
    read only in its quoted form — a path with a space in it is exactly the case
    where trusting it fails a legitimate pull request.
    """
    mentioned: set[str] = set()
    for line in split_diff_lines(text):
        if match := DIFF_GIT_QUOTED_RE.match(line):
            mentioned.add(unquote_path(f'"{match.group(1)}"').removeprefix("b/"))
        elif match := BINARY_FILES_RE.match(line):
            quoted, bare = match.group(1), match.group(2)
            target = unquote_path(f'"{quoted}"') if quoted else bare or ""
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


def assert_head_tree_within_caps(listing: dict) -> None:
    """Refuse a head tree too large to quarantine, per blob and in aggregate.

    Takes the recursive tree listing rather than fetching, so the decision is
    made from sizes the API reports before any bytes move — the quarantine fetch
    is the expensive step and this runs before it.

    A truncated listing fails closed: it cannot bound what it did not list.
    """
    if listing.get("truncated"):
        fail(
            "the head tree listing is truncated, so its size cannot be bounded; no review "
            "(a tree this large is not one a bounded review can cover)"
        )
    blobs = [entry for entry in listing.get("tree", []) if entry.get("type") == "blob"]
    for entry in blobs:
        # Refused rather than coerced to zero, the way `truncated` is refused: an
        # unsized blob cannot be bounded, and both arithmetic sites below would
        # have read it as free. Blobs only — a tree or submodule entry carries no
        # size and refusing those would abort every repository with a
        # subdirectory. Names the path, because no real listing is known to omit a
        # blob size and a first sighting should be diagnosable.
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            fail(
                f"head tree entry {entry.get('path')!r} is a blob whose size is {size!r}, "
                "so the tree cannot be bounded; no review"
            )
        if size > MAX_BLOB_BYTES:
            fail(
                f"head tree contains {entry['path']} at {size} bytes (per-file cap "
                f"{MAX_BLOB_BYTES}); no review"
            )
    total = sum(entry["size"] for entry in blobs)
    if total > MAX_TREE_BYTES:
        fail(
            f"head tree is {total} bytes across {len(blobs)} files (cap {MAX_TREE_BYTES}); "
            "no review"
        )


def fetch_head_tree(repo: str, head_sha: str) -> dict:
    """The recursive tree listing at the reviewed head SHA."""
    return cast("dict", api_json(f"/repos/{repo}/git/trees/{head_sha}?recursive=1"))


def fetch_anchored_pair(repo: str, base_sha: str, head_sha: str) -> tuple[bytes, list[str]]:
    """The diff and the changed-file list for base_sha...head_sha, capped and
    asserted to describe the same comparison.

    The provenance inputs verify() takes, produced from first-party API calls.
    Exported because the executor re-derives them rather than trusting the
    review job's copies: a re-verification whose diff comes from the job it
    distrusts can only re-check the phases that do not read the diff.
    """
    compare_path = f"/repos/{repo}/compare/{base_sha}...{head_sha}"
    diff = api_request(compare_path, accept="application/vnd.github.diff")
    if len(diff) > MAX_DIFF_BYTES:
        fail(f"diff is {len(diff)} bytes (cap {MAX_DIFF_BYTES}); no review")

    # From the SAME anchored comparison as the diff, never /pulls/{n}/files: that
    # endpoint recomputes against the base branch's CURRENT tip, which may have
    # advanced while the run sat at the approval gate.
    compare = cast("dict", api_json(compare_path))
    changed_files = [item["filename"] for item in compare.get("files", [])]
    # The endpoint returns at most 300 files per page, so a PR over the cap
    # yields a truncated list, which the assertion below also catches.
    #
    # Both dimensions, on the list ACTUALLY COLLECTED rather than on the PR
    # object's count: the two are computed against different bases, so the count
    # can read under the cap while this enumerates more. The byte ceiling is the
    # dimension the count cannot express at all.
    if len(changed_files) > MAX_CHANGED_FILES:
        fail(f"compare lists {len(changed_files)} files (cap {MAX_CHANGED_FILES}); no review")
    list_bytes = len(json.dumps(changed_files).encode())
    if list_bytes > MAX_CHANGED_FILES_BYTES:
        fail(
            f"the changed-file list is {list_bytes} bytes across {len(changed_files)} files "
            f"(cap MAX_CHANGED_FILES_BYTES {MAX_CHANGED_FILES_BYTES}); no review"
        )

    assert_diff_and_list_agree(diff, changed_files)
    return diff, changed_files


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

    # Before the diff, because this is the cheap refusal: the workflow's
    # quarantine step checks out this whole tree, and the diff caps below cannot
    # see a binary addition's real cost.
    assert_head_tree_within_caps(fetch_head_tree(repo, expected_head))

    diff, changed_files = fetch_anchored_pair(repo, base_sha, expected_head)

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
    (args.output_dir / "changed_files.json").write_text(json.dumps(changed_files), encoding="utf-8")
    print(f"context ready: {len(changed_files)} files, {len(diff)} diff bytes, head {expected_head}")


if __name__ == "__main__":
    main()
