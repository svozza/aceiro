import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src/aceiro/evals/record_arm_result.py"
SPEC = importlib.util.spec_from_file_location("record_arm_result", MODULE_PATH)
record_arm_result = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(record_arm_result)


def test_converter_keeps_invalid_runs_out_of_behavioral_counts(tmp_path) -> None:
    valid = tmp_path / "run1"
    invalid = tmp_path / "run2"
    valid.mkdir()
    invalid.mkdir()
    (valid / "results.json").write_text(json.dumps([{
        "name": "fixture", "passed": True, "valid": True,
        "invalid_reason": None, "api_errors": 0, "backoff_seconds": 0,
    }]))
    (invalid / "results.json").write_text(json.dumps([{
        "name": "fixture", "passed": False, "valid": False,
        "invalid_reason": "provider_error", "api_errors": 1, "backoff_seconds": 2,
    }]))
    args = type("Args", (), {
        "fixture": "fixture",
        "experiment_id": "experiment",
        "cohort_id": "cohort",
        "harness_sha": "a" * 40,
        "fixture_sha": "b" * 40,
        "run_id": 123,
        "model": "model",
        "reasoning_effort": "high",
        "region": "region",
        "supersedes": [],
    })()
    record = record_arm_result.convert(
        args, [valid / "results.json", invalid / "results.json"]
    )
    assert record["summary"]["requested"] == 2
    assert record["summary"]["scored"] == 1
    assert record["summary"]["excluded"] == 1
    assert record["summary"]["review_matches"] == 1
