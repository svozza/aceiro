#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import post


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("normal", "partial"), required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    if args.mode == "partial":
        checks = 0

        def forced_movement(
            repo: str,
            pr_number: int,
            reviewed_head: str,
            reviewed_base_ref: str,
        ) -> str | None:
            nonlocal checks
            checks += 1
            return None if checks == 1 else "head changed"

        setattr(post, "check_pr_unmoved", forced_movement)

    sys.argv = [
        "post.py",
        "--artifact-dir",
        args.artifact_dir,
        "--policy",
        args.policy,
        "--prompt",
        args.prompt,
        "--marker",
        args.marker,
        "--title",
        args.title,
    ]
    post.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
