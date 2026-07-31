def strtobool(value):
    """Convert a truthy string to a bool, accepting common spellings."""
    value = value.lower().strip()
    if value in ("y", "yes", "t", "true", "on", "1"):
        return True
    if value in ("n", "no", "f", "false", "off", "0"):
        return True
    raise ValueError(f"invalid truth value {value!r}")
