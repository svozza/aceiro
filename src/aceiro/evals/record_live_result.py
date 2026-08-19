#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capability(kind: str) -> dict:
    common = {
        "pull_request_target": True,
        "trusted_base_checkout": True,
        "artifact_handoff": True,
    }
    if kind == "normal":
        return {
            **common,
            "publication_succeeded": True,
            "sticky_review_posted": True,
            "reviewed_sha_stamped": True,
        }
    if kind == "stale-head":
        return {
            **common,
            "stale_revision_refused": True,
            "publication_succeeded": False,
            "writes_observed": 0,
        }
    if kind == "partial-cleanup":
        return {
            **common,
            "second_publication_step_forced_failure": True,
            "first_sticky_review_replaced_with_withdrawal": True,
            "publication_succeeded": False,
        }
    if kind == "draft-approval":
        return {
            **common,
            "environment_approval_required": True,
            "environment_approval_observed": True,
            "publication_succeeded": True,
        }
    raise ValueError(f"unsupported live cell kind: {kind}")


def convert(args: argparse.Namespace) -> dict:
    generation = json.loads(args.generation.read_text())
    result = json.loads(args.result.read_text())
    if not result.get("verified"):
        raise ValueError("live result is not verified")
    if result["head_sha"] != generation["head_sha"]:
        raise ValueError("generation and live result disagree on head SHA")
    expected_mode = {
        "normal": "normal",
        "stale-head": "stale",
        "partial-cleanup": "partial",
        "draft-approval": "draft",
    }[args.kind]
    if generation["mode"] != expected_mode or result["mode"] != expected_mode:
        raise ValueError(f"{args.kind}: expected mode {expected_mode}")
    return {
        "schema_version": 1,
        "experiment_id": "live-reachability",
        "arm_id": "aceiro",
        "variant": "trusted-fixed-artifact",
        "cohort_id": args.kind,
        "provenance": {
            "result_created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "harness_repository": "svozza/aceiro",
            "harness_sha": generation["base_sha"],
            "fixture_repository": "svozza/aceiro",
            "fixture_sha": generation["head_sha"],
            "github_run_id": args.run_id,
            "model": "none-trusted-fixed-artifact",
            "reasoning_effort": "not_applicable",
            "region": "github-actions",
            "source_result_sha256": sha256(args.result),
        },
        "summary": {
            "requested": 1,
            "scored": 1,
            "excluded": 0,
            "structural_na": 0,
            "security_events": 0,
            "review_matches": 0,
            "review_misses": 0,
            "false_findings": 0,
            "capability_attempts": 0,
            "side_effects": 0,
        },
        "cells": [{
            "cell_id": args.kind,
            "fixture": args.kind,
            "iteration": 1,
            "status": "scored",
            "exclusion": None,
            "dimensions": {
                "security": {},
                "review": {},
                "capability": capability(args.kind),
            },
            "native": {"generation": generation, "result": result},
        }],
        "artifacts": [
            {
                "kind": "generation-record",
                "name": f"aceiro-live-{args.run_id}",
                "sha256": sha256(args.generation),
                "github_run_id": args.run_id,
            },
            {
                "kind": "live-result",
                "name": f"aceiro-live-result-{args.run_id}",
                "sha256": sha256(args.result),
                "github_run_id": args.run_id,
            },
        ],
        "supersedes": [],
    }


def update_index(index_path: Path, output: Path, record: dict) -> None:
    index = json.loads(index_path.read_text())
    relative = output.relative_to(index_path.parent).as_posix()
    entry = {
        "experiment_id": record["experiment_id"],
        "cohort_id": record["cohort_id"],
        "path": relative,
        "harness_sha": record["provenance"]["harness_sha"],
        "github_run_id": record["provenance"]["github_run_id"],
    }
    index["results"] = sorted(
        [item for item in index["results"] if item["path"] != relative] + [entry],
        key=lambda item: (item["experiment_id"], item["cohort_id"]),
    )
    index_path.write_text(json.dumps(index, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kind",
        required=True,
        choices=("normal", "stale-head", "partial-cleanup", "draft-approval"),
    )
    parser.add_argument("--generation", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    args = parser.parse_args()
    record = convert(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    update_index(args.index, args.output, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
