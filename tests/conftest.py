import json
import sys
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).parent.parent / "src" / "smtithy"
sys.path.insert(0, str(HARNESS_DIR))


SAMPLE_DIFF = """\
diff --git a/aws_lambda_powertools/logging/logger.py b/aws_lambda_powertools/logging/logger.py
index 1111111..2222222 100644
--- a/aws_lambda_powertools/logging/logger.py
+++ b/aws_lambda_powertools/logging/logger.py
@@ -10,7 +10,9 @@ import os
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


@pytest.fixture(scope="session")
def policy():
    return POLICY


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
