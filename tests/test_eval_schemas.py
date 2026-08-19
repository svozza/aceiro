"""The eval declaration schemas are valid and accept every shipped fixture."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

import eval_schemas

EVALS = Path(__file__).parent.parent / "src" / "smtithy" / "evals"


def test_all_eval_schemas_are_valid_draft_2020_12():
    for schema in (
        eval_schemas.REVIEW_EXPECT_SCHEMA,
        eval_schemas.PLAN_EXPECT_SCHEMA,
        eval_schemas.BASE_DECLARATION_SCHEMA,
    ):
        Draft202012Validator.check_schema(schema)


def test_all_review_expectations_match_the_schema():
    for path in sorted((EVALS / "scenarios").glob("*/expect.json")):
        eval_schemas.REVIEW_EXPECT_VALIDATOR.validate(json.loads(path.read_text()))


def test_all_plan_expectations_match_the_schema():
    for path in sorted((EVALS / "plan_scenarios").glob("*/expect.json")):
        eval_schemas.PLAN_EXPECT_VALIDATOR.validate(json.loads(path.read_text()))


def test_all_base_declarations_match_the_schema():
    for path in sorted(EVALS.glob("scenarios/*/base.json")):
        eval_schemas.BASE_DECLARATION_VALIDATOR.validate(json.loads(path.read_text()))
