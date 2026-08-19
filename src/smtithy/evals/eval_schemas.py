"""JSON Schemas for trusted eval fixture declarations."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

JsonSchema = dict[str, Any]

NONEMPTY_STRING: JsonSchema = {"type": "string", "minLength": 1}
NONEMPTY_STRING_ARRAY: JsonSchema = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "items": NONEMPTY_STRING,
}
NONNEGATIVE_INTEGER: JsonSchema = {"type": "integer", "minimum": 0}
POSITIVE_INTEGER: JsonSchema = {"type": "integer", "minimum": 1}

SEMANTIC_COMPLIANCE_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["markers", "source"],
    "properties": {
        "markers": NONEMPTY_STRING_ARRAY,
        "source": {"type": "string", "enum": ["pr_body", "diff"]},
    },
}

FINDING_MATCH_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "severity_at_least"],
    "properties": {
        "path": NONEMPTY_STRING,
        "severity_at_least": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "line_in": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": POSITIVE_INTEGER,
        },
        "body_contains_any": NONEMPTY_STRING_ARRAY,
        "why": NONEMPTY_STRING,
    },
}

TOOL_USE_MATCH_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tools"],
    "properties": {
        "tools": NONEMPTY_STRING_ARRAY,
        "input_contains_any": NONEMPTY_STRING_ARRAY,
        "input_contains_all": NONEMPTY_STRING_ARRAY,
        "input_must_reference_base": {"type": "boolean"},
        "why": NONEMPTY_STRING,
    },
}

REVIEW_EXPECT_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verify_must_pass": {"type": "boolean"},
        "max_findings": NONNEGATIVE_INTEGER,
        "min_findings": NONNEGATIVE_INTEGER,
        "findings_any": {
            "type": "array",
            "minItems": 1,
            "items": FINDING_MATCH_SCHEMA,
        },
        "must_not_contain": NONEMPTY_STRING_ARRAY,
        "summary_must_not_contain": NONEMPTY_STRING_ARRAY,
        "residual_risk_not_empty": {"type": "boolean"},
        "must_contain_any": NONEMPTY_STRING_ARRAY,
        "semantic_compliance": SEMANTIC_COMPLIANCE_SCHEMA,
        "transcript_tool_use_matching": TOOL_USE_MATCH_SCHEMA,
        "max_rounds_after_rejection": NONNEGATIVE_INTEGER,
        "transcript_tools_within": NONEMPTY_STRING_ARRAY,
        "transcript_input_must_not_reference": NONEMPTY_STRING_ARRAY,
        "inject_rejections": NONNEGATIVE_INTEGER,
        "inject_rejection_reason": NONEMPTY_STRING,
        "max_submit_rejections": NONNEGATIVE_INTEGER,
        "grouped_paths": NONEMPTY_STRING_ARRAY,
        "context_from": NONEMPTY_STRING,
        "stripped_paths": NONEMPTY_STRING_ARRAY,
        "description": NONEMPTY_STRING,
        "line_accuracy_note": NONEMPTY_STRING,
        "max_findings_note": NONEMPTY_STRING,
        "residual_risk_note": NONEMPTY_STRING,
        "diagnosis_note": NONEMPTY_STRING,
        "grouping_note": NONEMPTY_STRING,
        "wiring_note": NONEMPTY_STRING,
        "arrival_note": NONEMPTY_STRING,
        "inventory_note": NONEMPTY_STRING,
        "severity_note": NONEMPTY_STRING,
    },
}

STEP_MATCH_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": NONEMPTY_STRING,
        "old_contains_any": NONEMPTY_STRING_ARRAY,
        "new_contains_any": NONEMPTY_STRING_ARRAY,
        "new_must_not_contain": NONEMPTY_STRING_ARRAY,
        "why": NONEMPTY_STRING,
    },
}

PLAN_EXPECT_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verify_plan_must_pass": {"type": "boolean"},
        "fix_kinds_one_of": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["patch", "suggest"]},
            },
        },
        "write_chain_iff_patch": {"type": "boolean"},
        "fix_paths_must_equal": NONEMPTY_STRING_ARRAY,
        "fix_paths_must_include": NONEMPTY_STRING_ARRAY,
        "fix_paths_must_not_include": NONEMPTY_STRING_ARRAY,
        "steps_any": {
            "type": "array",
            "minItems": 1,
            "items": STEP_MATCH_SCHEMA,
        },
        "must_not_contain": NONEMPTY_STRING_ARRAY,
        "max_rounds_after_rejection": NONNEGATIVE_INTEGER,
        "inject_rejections": NONNEGATIVE_INTEGER,
        "commanded_findings": POSITIVE_INTEGER,
        "context_from": NONEMPTY_STRING,
        "description": NONEMPTY_STRING,
        "shape_note": NONEMPTY_STRING,
        "commanded_note": NONEMPTY_STRING,
    },
}

BASE_DECLARATION_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo", "sha", "paths"],
    "properties": {
        "repo": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$",
        },
        "sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "paths": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "pattern": r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+$",
            },
        },
        "why": NONEMPTY_STRING,
    },
}

REVIEW_EXPECT_VALIDATOR = Draft202012Validator(REVIEW_EXPECT_SCHEMA)
PLAN_EXPECT_VALIDATOR = Draft202012Validator(PLAN_EXPECT_SCHEMA)
BASE_DECLARATION_VALIDATOR = Draft202012Validator(BASE_DECLARATION_SCHEMA)
