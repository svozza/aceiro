"""Collect SHA-anchored PR context via the GitHub API.

Runs in the review job before the agent. Anchors everything to the head SHA
recorded in the triggering event: verifies the PR's current head equals that
SHA before and after collection, so a mid-run push cannot swap the content
under review. Applies sanity caps (files/bytes) and fails loud on breach.

Writes to --output-dir: pr.json, diff.patch, changed_files.json

Environment: GITHUB_TOKEN (read-only), GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA,
BASE_SHA (the event's base SHA — the commit the review job checked out).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from github_api import api_json, api_request, fail, paginate

MAX_CHANGED_FILES = 300
MAX_DIFF_BYTES = 1_500_000


def fetch_pr(repo: str, pr_number: int) -> dict:
    return api_json(f"/repos/{repo}/pulls/{pr_number}")


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
    diff = api_request(
        f"/repos/{repo}/compare/{base_sha}...{expected_head}",
        accept="application/vnd.github.diff",
    )
    if len(diff) > MAX_DIFF_BYTES:
        fail(f"diff is {len(diff)} bytes (cap {MAX_DIFF_BYTES}); no review")

    changed_files: list[str] = []
    for batch in paginate(f"/repos/{repo}/pulls/{pr_number}/files?"):
        changed_files.extend(item["filename"] for item in batch)

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
