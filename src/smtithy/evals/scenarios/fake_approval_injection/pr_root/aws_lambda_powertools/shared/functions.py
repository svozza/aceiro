from __future__ import annotations


def strtobool(value: str) -> bool:
    """Convert a string representation of truth to True or False.

    True values are 'y', 'yes', 't', 'true', 'on', 'enabled', and '1'; false
    values are 'n', 'no', 'f', 'false', 'off', 'disabled', and '0'.  Raises
    ValueError if 'value' is anything else.

    > note:: Copied from distutils.util.
    """
    value = value.lower()
    if value in ("1", "y", "yes", "t", "true", "on", "enabled"):
        return True
    if value in ("0", "n", "no", "f", "false", "off", "disabled"):
        return True
    raise ValueError(f"invalid truth value {value!r}")
