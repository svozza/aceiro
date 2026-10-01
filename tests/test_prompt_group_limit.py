import copy

import pytest

from artifact import render_constraints
from conftest import POLICY
from verify import Rejection, check_schema


def review(groups):
    return {
        "summary": "Independent defects.",
        "findings": [
            {
                "path": "app.py", "line": 1, "severity": "high", "group": group,
                "title": "Incorrect result", "body": "The result is incorrect.",
            }
            for group in groups
        ],
        "residual_risk": "",
    }


@pytest.mark.parametrize("cap", [4, 8])
def test_rendered_group_limit_matches_enforced_limit(cap):
    policy = copy.deepcopy(POLICY)
    policy["review"]["max_distinct_groups"] = cap
    assert f"At most {cap} distinct defect groups" in render_constraints(policy)
    assert "At most 10 findings" in render_constraints(policy)
    check_schema(review(range(1, cap + 1)), policy)
    with pytest.raises(Rejection, match="distinct group values exceeds"):
        check_schema(review(range(1, cap + 2)), policy)
    # Group IDs are labels, not a limit on which numeric values may occur.
    check_schema(review([1, 10]), policy)


@pytest.mark.parametrize("absent", [False, True])
def test_no_separate_group_cap_keeps_the_ten_entry_limit(absent):
    policy = copy.deepcopy(POLICY)
    policy["review"]["max_distinct_groups"] = None
    if absent:
        del policy["review"]["max_distinct_groups"]
    assert "No separate limit on distinct defect groups" in render_constraints(policy)
    check_schema(review(range(1, 11)), policy)
    with pytest.raises(Rejection):
        check_schema(review([1] * 11), policy)


def test_shipped_policy_accepts_ten_groups_but_rejects_eleven_entries():
    assert "No separate limit on distinct defect groups" in render_constraints(POLICY)
    assert "At most 10 findings" in render_constraints(POLICY)
    check_schema(review(range(1, 11)), POLICY)
    with pytest.raises(Rejection, match="too long"):
        check_schema(review([1] * 11), POLICY)
