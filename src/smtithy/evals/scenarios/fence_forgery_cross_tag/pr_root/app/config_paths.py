"""Configuration helpers."""
from __future__ import annotations

import os


def config_path(root: str) -> str:
    """Path to the config file under *root*."""
    return os.path.join(root, "config.toml")
