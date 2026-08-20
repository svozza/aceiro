"""The fixtures are test infrastructure, and one of them was shared mutable state.

`policy` was session-scoped and returned the module-level POLICY dict itself, so
any test doing `policy['markdown']['link_host_allowlist'] = []` — a natural
fail-closed test to write — emptied the allowlist for every test that ran after it
in the session. The goldens asserting a legitimate link is ACCEPTED would then
fail, or, worse, a test asserting only a rejection path would keep passing while
testing nothing.

Ordering-dependent, so it would have arrived as a test that passes alone and fails
in the suite. These two tests are a matched pair by design: the first mutates, the
second asserts the mutation did not travel. `test_a` sorts before `test_b`, and
pytest runs a file top to bottom, so the pair holds either way.
"""

import pytest

from conftest import POLICY

SHIPPED_HOSTS = ["github.com/aws-powertools/", "docs.powertools.aws.dev"]


def test_a_mutating_test_empties_the_allowlist(policy):
    # Exactly the shape the finding names.
    policy["markdown"]["link_host_allowlist"] = []
    policy["artifact_schema"]["properties"]["summary"]["maxLength"] = 1
    assert policy["markdown"]["link_host_allowlist"] == []


def test_b_the_next_test_gets_an_unmutated_policy(policy):
    assert policy["markdown"]["link_host_allowlist"] == SHIPPED_HOSTS, (
        "a previous test's mutation reached this one; the fixture is shared mutable state"
    )
    assert policy["artifact_schema"]["properties"]["summary"]["maxLength"] > 1


def test_the_module_level_policy_is_not_handed_out(policy):
    # A copy, not the object every module imports: `from conftest import POLICY` is
    # used directly in a dozen files, and handing the same dict to a fixture makes
    # a fixture mutation visible to all of them.
    assert policy is not POLICY
    assert policy == POLICY


def test_the_copy_is_deep(policy):
    # A shallow copy would still share every nested dict, which is where every
    # interesting mutation lands.
    policy["markdown"]["allowed_nodes"].append("table_open")
    assert "table_open" not in POLICY["markdown"]["allowed_nodes"]


@pytest.mark.parametrize("run", [1, 2])
def test_each_parametrized_case_gets_its_own_copy(policy, run):
    # Parametrized cases share a fixture instance under any scope wider than
    # function, which is the same hazard one call site down.
    assert policy["markdown"]["link_host_allowlist"] == SHIPPED_HOSTS
    policy["markdown"]["link_host_allowlist"] = []


def test_the_test_hosts_are_the_calibrated_ones(policy):
    # conftest injects these because the shipped allowlist is empty and an empty
    # one can only reject. test_verify_adversarial's near-misses are near-misses OF
    # these exact strings, so a changed value silently weakens those cases into
    # ordinary rejections.
    assert policy["markdown"]["link_host_allowlist"] == SHIPPED_HOSTS
