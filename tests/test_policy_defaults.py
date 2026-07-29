"""The SHIPPED policy.json, tested as the artifact a consumer actually gets.

Every other test file uses the `policy` fixture, which conftest.py populates
with a link_host_allowlist so that accept-path cases have something to accept.
That makes those files silent about the default: they would pass identically
whether the shipped allowlist were empty, populated, or still naming the hosts
of the repo this was extracted from.

So these tests read policy.json off disk and assert nothing has been injected.
The property that matters is that an unconfigured consumer fails CLOSED — a
default that quietly trusts a host is exactly the extraction bug ADR-0002 lists
(`policy.json`'s link_host_allowlist among the repo-specific coupling to
remove), and it would be invisible in a green suite.
"""

import json
from pathlib import Path

import pytest

from conftest import CHANGED_FILES, SAMPLE_DIFF
from verify import Rejection, verify

SHIPPED_POLICY = json.loads((Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text())


def test_shipped_link_allowlist_is_empty():
    assert SHIPPED_POLICY["markdown"]["link_host_allowlist"] == []


def test_shipped_policy_names_no_host_at_all():
    """No hostname anywhere in the shipped policy, under any key.

    Broader than the assertion above on purpose: it also fails if a host
    reappears somewhere new, rather than only in the key we remembered to check.
    """
    blob = json.dumps(SHIPPED_POLICY)
    for coupling in ("powertools", "aws.dev", "github.com", "amazonaws"):
        assert coupling not in blob, f"shipped policy names {coupling!r}"


def test_any_link_rejects_under_the_shipped_default(valid_artifact):
    """The fail-closed consequence, stated as behaviour rather than as config."""
    valid_artifact["summary"] = "See [the docs](https://docs.powertools.aws.dev/lambda/python/latest/)."
    with pytest.raises(Rejection, match="not on the allowlist"):
        verify(valid_artifact, SAMPLE_DIFF, CHANGED_FILES, SHIPPED_POLICY)


def test_a_consumer_allowlist_opens_exactly_what_it_names(valid_artifact):
    """And the default is a default, not a prohibition: naming a host works,
    and naming one host does not admit another."""
    policy = json.loads(json.dumps(SHIPPED_POLICY))
    policy["markdown"]["link_host_allowlist"] = ["docs.example.com"]

    valid_artifact["summary"] = "See [the docs](https://docs.example.com/guide/)."
    verify(valid_artifact, SAMPLE_DIFF, CHANGED_FILES, policy)

    valid_artifact["summary"] = "See [the docs](https://other.example.com/guide/)."
    with pytest.raises(Rejection, match="not on the allowlist"):
        verify(valid_artifact, SAMPLE_DIFF, CHANGED_FILES, policy)


def test_findings_still_verify_without_any_allowlist(valid_artifact):
    """An empty allowlist must not break ordinary reviews. Link-free prose is
    the common case, and it has to keep passing or fail-closed has cost the
    harness its purpose (the goldens' own warning: fail-closed is right, but
    useless if nothing passes)."""
    verify(valid_artifact, SAMPLE_DIFF, CHANGED_FILES, SHIPPED_POLICY)
