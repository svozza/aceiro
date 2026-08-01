"""Leak probe: N repeats of leak-prone scenarios, first-submission stats only.

Runs cc_loop.run against scenario fixtures, then mines each captured stream
for submit_review calls: did the FIRST submission of a session arrive with
`findings` present, or did the artifact get serialized into `summary` as
function-calling XML? How long was the summary, and which key came first?
No grading happens here -- the question is serialization behaviour, not
review quality, so this is the cheap instrument for prompt work: the suite
answers "is the review right", this answers "did the tool call survive" at a
fraction of the cost per data point.

This is the script that isolated the leak's two variables -- summary length
and argument order -- in docs/findings/0001-generator-leaks-tool-call-xml.md,
including the falsification arm. Kept for the next prompt change: probe a
same-day baseline, make ONE change, probe again, and try to *induce* the
failure before believing its absence.

Usage:
    python leak_probe.py --out "$OUT" [--n 7] [--scenarios a,b] [--cache-dir DIR]

Exits non-zero if any submission leaked, so it can gate a loop — and also when
it measured nothing or a probe run failed, since a gate that reads clean on no
data would let a prompt-change loop treat an unmeasured change as verified.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from base_fixture import materialise as materialise_base  # noqa: E402
from cc_loop import SUBMIT_TOOL  # noqa: E402
from cc_loop import run as run_loop  # noqa: E402
from run_evals import make_injected_verify  # noqa: E402

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# The first-submission leak boundary measured in finding 0001: 0/83 leaked
# below it, 5/10 at or above. Reported on every probe so a run that is clean
# but whose summaries have crept back over the line reads as the warning it is.
SUMMARY_LENGTH_BOUNDARY = 1200


def submissions(stream_path: str) -> list[dict]:
    """Every submit_review call in a captured stream, in order.

    A leak is a call whose input has no `findings` key: the artifact was
    serialized into some other parameter instead (always `summary`, in every
    observed case). `keys` preserves the model's argument order, because
    which key it wrote first is one of the two measured levers. Unparseable
    lines are skipped, not fatal -- a truncated stream is exactly what a
    crashed session leaves behind.
    """
    calls = []
    for line in open(stream_path, encoding="utf-8", errors="replace"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "AssistantMessage":
            continue
        for block in record.get("content") or []:
            if isinstance(block, dict) and block.get("name") == SUBMIT_TOOL:
                tool_input = block.get("input") or {}
                calls.append(
                    {
                        "leaked": "findings" not in tool_input,
                        "keys": list(tool_input),
                        "summary_length": len(tool_input.get("summary") or ""),
                    }
                )
    return calls


def probe_once(scenario: str, index: int, out_root: Path, cache_root: Path) -> dict:
    scenario_dir = SCENARIOS_DIR / scenario
    expect = json.loads((scenario_dir / "expect.json").read_text())
    # Variants borrow fixtures via context_from, same as run_evals.run_scenario.
    fixture_dir = SCENARIOS_DIR / expect["context_from"] if "context_from" in expect else scenario_dir
    base_root = materialise_base(fixture_dir, cache_root)
    output_dir = out_root / f"{scenario}_{index}"
    verify_fn = make_injected_verify(expect.get("inject_rejections", 0))
    exit_code = run_loop(base_root, fixture_dir / "pr_root", fixture_dir / "context", output_dir, verify_fn=verify_fn)

    calls = []
    for stream in sorted(glob.glob(str(output_dir / "cc_stream_*.jsonl"))):
        calls.extend(submissions(stream))
    first = calls[0] if calls else None
    return {
        "scenario": scenario,
        "index": index,
        "exit_code": exit_code,
        # Post-rejection submissions re-roll the same dice with the same long
        # summary, so they leak at a far higher rate (59% vs 5% in finding
        # 0001's forensics) and are reported as their own regime -- averaging
        # the two together buries the signal.
        "retry_submissions": calls[1:],
        **(first or {"leaked": None}),
    }


def exit_status(results: list[dict]) -> int:
    """0 only when the probe measured something and that something was clean.

    Three ways to be non-zero, because the exit code gates a prompt-change loop
    and "clean" has to mean clean. A leak in a first submission or a retry is the
    signal the probe exists for. A run that captured no submission at all
    measured nothing, and a run that failed measured something unreliable: both
    print "0 leaks / 0 calls", which reads as a pass. Every reason is printed,
    since the operator's next action differs — fix the prompt, or re-run when
    upstream recovers.
    """
    valid = [r for r in results if r["leaked"] is not None]
    leaks = [r for r in valid if r["leaked"]]
    retry_leaks = [s for r in results for s in r["retry_submissions"] if s["leaked"]]
    failed = [r for r in results if r.get("exit_code")]

    if not results:
        print("::error::no probe runs at all; nothing was measured")
        return 1
    if not valid:
        print(
            f"::error::{len(results)} runs, none with a captured submission: the probe measured "
            "nothing. This is not the same as 'no leaks' — re-run when upstream is healthy."
        )
        return 1
    if failed:
        print(
            f"::error::{len(failed)}/{len(results)} probe runs exited non-zero; their submissions "
            "are not a measurement to trust"
        )
        return 1
    return 1 if leaks or retry_leaks else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenarios",
        default="lru_eviction_bug,caller_impact_needs_investigation",
        help="Comma-separated scenario names (default: a leak-prone pair).",
    )
    parser.add_argument("--n", type=int, default=10, help="Repeats per scenario.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".eval-base-cache"),
        help="Where pinned BASE fixtures are cached (default: .eval-base-cache).",
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    jobs = [(scenario, i) for scenario in args.scenarios.split(",") for i in range(args.n)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda job: probe_once(job[0], job[1], args.out, args.cache_dir), jobs))

    (args.out / "probe_results.json").write_text(json.dumps(results, indent=2))

    valid = [r for r in results if r["leaked"] is not None]
    leaks = [r for r in valid if r["leaked"]]
    retries = [s for r in results for s in r["retry_submissions"]]
    retry_leaks = [s for s in retries if s["leaked"]]

    print(
        f"\nfirst-submission: {len(leaks)} leaks / {len(valid)} calls "
        f"({len(results) - len(valid)} runs with no submission)"
    )
    print(f"retry-submission: {len(retry_leaks)} leaks / {len(retries)} calls")
    if valid:
        lengths = sorted(r["summary_length"] for r in valid)
        over = sum(1 for length in lengths if length >= SUMMARY_LENGTH_BOUNDARY)
        print(f"first summary length: median={statistics.median(lengths):.0f} max={max(lengths)}")
        print(f"over-{SUMMARY_LENGTH_BOUNDARY}: {over}/{len(lengths)}")
    for r in leaks:
        print(f"  FIRST-LEAK {r['scenario']} #{r['index']} summary_length={r['summary_length']} keys={r['keys']}")
    for s in retry_leaks:
        print(f"  RETRY-LEAK summary_length={s['summary_length']} keys={s['keys']}")
    return exit_status(results)


if __name__ == "__main__":
    sys.exit(main())
