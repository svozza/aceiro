import copy
import json
import sys
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).parent.parent / "src" / "smtithy"
sys.path.insert(0, str(HARNESS_DIR))

# test_run_evals.py imports run_evals directly, and upstream reached it as
# tests/../evals because tests/ sat beside evals/ inside the harness directory.
# Here the harness is under src/smtithy/, so that relative path resolves to a
# directory that does not exist. Added here rather than in the test file, which
# is kept byte-identical to upstream.
sys.path.insert(0, str(HARNESS_DIR / "evals"))


SAMPLE_DIFF = """\
diff --git a/aws_lambda_powertools/logging/logger.py b/aws_lambda_powertools/logging/logger.py
index 1111111..2222222 100644
--- a/aws_lambda_powertools/logging/logger.py
+++ b/aws_lambda_powertools/logging/logger.py
@@ -10,5 +10,7 @@ import os
 import logging


-def get_level():
+def get_level(default="INFO"):
+    if default is None:
+        raise ValueError("default required")
     return os.environ.get("LOG_LEVEL")
diff --git a/tests/unit/test_logger.py b/tests/unit/test_logger.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/unit/test_logger.py
@@ -0,0 +1,4 @@
+def test_get_level():
+    from aws_lambda_powertools.logging.logger import get_level
+
+    assert get_level() is None
"""

CHANGED_FILES = [
    "aws_lambda_powertools/logging/logger.py",
    "tests/unit/test_logger.py",
]

DELETED_FILE_DIFF = (
    "diff --git a/gone.py b/gone.py\n"
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-print(1)\n"
    "-print(2)\n"
)

POLICY = json.loads((HARNESS_DIR / "policy.json").read_text())

# The shipped policy's link_host_allowlist is empty (fail-closed: a consumer
# names the hosts it trusts). The tests need a POPULATED one, because an empty
# allowlist can only ever reject — every link case would pass for the same
# uninteresting reason, and the goldens asserting that a legitimate link is
# ACCEPTED would have nothing left to assert.
#
# These two hosts are the ones the corpus was calibrated against, so they stay
# exactly as they were: test_verify_adversarial.py's near-misses are near-misses
# OF these strings (docs.powertools.aws.dev.evil.com, aws-powertools-evil/repo,
# userinfo tricks ending @evil.com), and they only probe the prefix-matching
# boundary while the thing they bracket is still on the list. Changing these
# values silently weakens those cases into ordinary rejections.
POLICY["markdown"]["link_host_allowlist"] = [
    "github.com/aws-powertools/",
    "docs.powertools.aws.dev",
]


@pytest.fixture
def policy():
    """A fresh deep copy per test.

    Function-scoped and copied, because session-scoped handed every test the same
    mutable dict — and this dict, the one a dozen modules also import as POLICY
    directly. A test doing `policy['markdown']['link_host_allowlist'] = []` (the
    natural way to write a fail-closed case) emptied the allowlist for every test
    that ran after it, so the goldens asserting a legitimate link is ACCEPTED
    would fail, or a rejection-only test would pass while asserting nothing. The
    symptom is a test that passes alone and fails in the suite, which is the
    hardest kind to place.

    Deep, not shallow: every interesting mutation is on a nested dict.
    """
    return copy.deepcopy(POLICY)


@pytest.fixture
def sample_diff():
    return SAMPLE_DIFF


@pytest.fixture
def changed_files():
    return list(CHANGED_FILES)


@pytest.fixture
def valid_artifact():
    """A golden artifact: anchored to lines that exist in SAMPLE_DIFF hunks."""
    return {
        "summary": "The new `default` parameter of `get_level` is ignored; the "
        "function still returns only the environment value.",
        "findings": [
            {
                "path": "aws_lambda_powertools/logging/logger.py",
                "line": 13,
                "severity": "high",
                "title": "default parameter is accepted but never used",
                "body": "`get_level` gained a `default` argument but the return "
                "statement ignores it. Callers passing a default still get `None` "
                "when `LOG_LEVEL` is unset. Fix: `return os.environ.get(\"LOG_LEVEL\", default)`.",
            },
        ],
        "residual_risk": "",
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Every test in this suite is deterministic and offline. Enforced, not stated.

    Several modules' docstrings promise the network is never touched, and nothing
    held them to it: a stub that misses one call site produced a real HTTP request
    from the test suite (observed as a live 401 while wiring the plan executor's
    delivery), which reads as an assertion about a stub that was never installed.

    A test that genuinely exercises the transport (test_github_api's retry and
    backoff cases) installs its own urlopen fake, which happens after this
    fixture and so replaces the refusal — no opt-out is needed, and none exists:
    every escape is a visible monkeypatch in the test that needs it.
    """
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError(
            "the test suite attempted a real network call; stub the API helper the "
            "code under test actually uses"
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
