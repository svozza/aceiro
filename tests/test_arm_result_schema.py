import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "src/smtithy/evals/arm_result.schema.json"


def test_arm_result_schema_has_closed_top_level_and_version() -> None:
    schema = json.loads(SCHEMA.read_text())
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert set(schema["required"]) == {
        "schema_version", "experiment_id", "arm_id", "cohort_id",
        "provenance", "summary", "cells", "artifacts", "supersedes",
    }


def test_result_cells_keep_dimensions_separate() -> None:
    schema = json.loads(SCHEMA.read_text())
    cell = schema["properties"]["cells"]["items"]
    dimensions = cell["properties"]["dimensions"]
    assert dimensions["additionalProperties"] is False
    assert set(dimensions["required"]) == {"security", "review", "capability"}
    assert set(cell["properties"]["status"]["enum"]) == {
        "scored", "excluded", "structural_na",
    }
