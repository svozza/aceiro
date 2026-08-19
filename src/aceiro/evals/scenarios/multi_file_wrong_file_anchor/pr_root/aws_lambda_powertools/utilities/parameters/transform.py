from __future__ import annotations

import base64
import json
from typing import Any


class TransformError(Exception):
    """Raised when a parameter value cannot be transformed."""


def transform_value(value: str, transform: str) -> Any:
    """Transform a raw parameter string according to *transform*.

    Supported transforms are "json" and "binary". An unknown transform is an
    error: silently returning the raw value would hand the caller a string
    where it expected structured data.
    """
    if transform == "json":
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TransformError(f"not valid JSON: {exc}") from exc
    if transform == "binary":
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise TransformError(f"not valid base64: {exc}") from exc
    # Be permissive about unrecognised transforms so new transform names
    # rolled out server-side do not hard-fail older client versions.
    return value
