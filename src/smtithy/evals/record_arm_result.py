#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def convert(args: argparse.Namespace, files: list[Path]) -> dict:
    cells = []
    artifacts = []
    scored_count = 0
    review_matches = 0
    review_misses = 0
    security_events = 0
    for iteration, path in enumerate(files, start=1):
        payload = json.loads(path.read_text())
        if len(payload) != 1 or payload[0]["name"] != args.fixture:
            raise ValueError(f"{path}: expected one {args.fixture!r} result")
        result = payload[0]
        status = "scored" if result["valid"] else "excluded"
        if status == "scored":
            scored_count += 1
            if result["passed"]:
                review_matches += 1
            else:
                review_misses += 1
                security_events += 1
        cells.append({
            "cell_id": f"{args.fixture}:{iteration}",
            "fixture": args.fixture,
            "iteration": iteration,
            "status": status,
            "exclusion": result.get("invalid_reason") if status == "excluded" else None,
            "dimensions": {
                "security": {
                    "shared_expectations_passed": result["passed"]
                    if status == "scored" else None,
                },
                "review": {
                    "shared_expectations_passed": result["passed"]
                    if status == "scored" else None,
                },
                "capability": {
                    "api_errors": result.get("api_errors", 0),
                    "backoff_seconds": result.get("backoff_seconds", 0),
                },
            },
            "native": result,
        })
        artifacts.append({
            "kind": "redacted-native-result",
            "name": path.parent.name,
            "sha256": sha256(path),
            "github_run_id": args.run_id,
        })

    digest = hashlib.sha256()
    for path in files:
        digest.update(path.parent.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return {
        "schema_version": 1,
        "experiment_id": args.experiment_id,
        "arm_id": "smtithy",
        "variant": None,
        "cohort_id": args.cohort_id,
        "provenance": {
            "result_created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "harness_repository": "svozza/smtithy",
            "harness_sha": args.harness_sha,
            "fixture_repository": "svozza/smtithy",
            "fixture_sha": args.fixture_sha,
            "github_run_id": args.run_id,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "region": args.region,
            "source_result_sha256": digest.hexdigest(),
        },
        "summary": {
            "requested": len(cells),
            "scored": scored_count,
            "excluded": len(cells) - scored_count,
            "structural_na": 0,
            "security_events": security_events,
            "review_matches": review_matches,
            "review_misses": review_misses,
            "false_findings": 0,
            "capability_attempts": 0,
            "side_effects": 0,
        },
        "cells": cells,
        "artifacts": artifacts,
        "supersedes": args.supersedes,
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
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--harness-sha", required=True)
    parser.add_argument("--fixture-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--supersedes", action="append", default=[])
    args = parser.parse_args()
    files = sorted(
        args.input_root.glob("run*/results.json"),
        key=lambda path: int(path.parent.name.removeprefix("run")),
    )
    if not files:
        raise SystemExit(f"no retained results under {args.input_root}")
    record = convert(args, files)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    update_index(args.index, args.output, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
