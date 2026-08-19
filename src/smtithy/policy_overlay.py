"""Derive the effective policy from narrow, caller-owned configuration.

The reusable workflow caller is trusted configuration from the consumer's
default branch. It may replace the shipped link-host allowlist, but it may not
supply an arbitrary policy object or policy file.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path

from canonicalize import read_harness_text
from verify import normalize_host

MAX_LINK_HOSTS = 50
MAX_LINK_HOST_LENGTH = 500
LINK_HOSTS_ENV = "SMTITHY_LINK_HOST_ALLOWLIST"


class ConfigurationError(ValueError):
    """Caller configuration cannot be represented safely as policy."""


def canonical_link_host(entry: str) -> str:
    """Validate and canonicalize one host or host/path allowlist entry."""
    if len(entry) > MAX_LINK_HOST_LENGTH:
        raise ConfigurationError(
            f"link host entry exceeds {MAX_LINK_HOST_LENGTH} characters"
        )
    if any(character.isspace() for character in entry):
        raise ConfigurationError(f"link host entry contains whitespace: {entry!r}")
    if "://" in entry or any(character in entry for character in "?#"):
        raise ConfigurationError(
            f"link host entry must be host or host/path without scheme, query, or fragment: {entry!r}"
        )

    normalized = normalize_host(f"https://{entry}")
    if normalized is None:
        raise ConfigurationError(f"link host entry is not a clean ASCII host/path: {entry!r}")

    # normalize_host represents a path-less URL as "host/"; policy uses "host"
    # for the whole-host form and a trailing slash only for path-prefix matching.
    return normalized.removesuffix("/") if "/" not in entry else normalized


def parse_link_hosts(raw: str) -> list[str]:
    """Canonical entries from a newline-delimited workflow input."""
    entries = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(entries) > MAX_LINK_HOSTS:
        raise ConfigurationError(
            f"link host allowlist has {len(entries)} entries; maximum is {MAX_LINK_HOSTS}"
        )

    canonical = [canonical_link_host(entry) for entry in entries]
    if len(set(canonical)) != len(canonical):
        raise ConfigurationError("link host allowlist contains duplicate entries")
    return canonical


def effective_policy(policy: dict, raw_link_hosts: str) -> dict:
    """Return a copy with the caller-configurable allowlist replaced."""
    derived = copy.deepcopy(policy)
    derived["markdown"]["link_host_allowlist"] = parse_link_hosts(raw_link_hosts)
    return derived


def write_policy(path: Path, policy: dict) -> None:
    """Atomically replace the job-private policy checkout."""
    rendered = json.dumps(policy, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args()

    policy = json.loads(read_harness_text(args.policy))
    derived = effective_policy(policy, os.environ.get(LINK_HOSTS_ENV, ""))
    write_policy(args.policy, derived)
    print(
        f"effective policy allows {len(derived['markdown']['link_host_allowlist'])} link host(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
