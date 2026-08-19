"""Tests for the caller-configurable link-host policy overlay."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "aceiro"))

from policy_overlay import (  # noqa: E402
    ConfigurationError,
    canonical_link_host,
    effective_policy,
    main,
    parse_link_hosts,
)
from verify import Rejection, check_link  # noqa: E402


@pytest.mark.parametrize(
    ("entry", "canonical"),
    [
        ("docs.example.com", "docs.example.com"),
        ("DOCS.EXAMPLE.COM", "docs.example.com"),
        ("github.com/your-org/", "github.com/your-org/"),
        ("github.com/your-org/project", "github.com/your-org/project"),
    ],
)
def test_canonical_link_host(entry, canonical):
    assert canonical_link_host(entry) == canonical


@pytest.mark.parametrize(
    "entry",
    [
        "https://docs.example.com",
        "docs.example.com?next=attacker.example",
        "docs.example.com#fragment",
        "docs.example.com:443",
        "user@docs.example.com",
        "docs.example.com/../attacker",
        "docs.example.com path",
        "döcs.example.com",
    ],
)
def test_invalid_link_host_is_refused(entry):
    with pytest.raises(ConfigurationError):
        canonical_link_host(entry)


def test_newline_input_ignores_blank_lines_and_preserves_order():
    assert parse_link_hosts(
        "\nDOCS.EXAMPLE.COM\n\ngithub.com/your-org/\n"
    ) == ["docs.example.com", "github.com/your-org/"]


def test_duplicate_canonical_entries_are_refused():
    with pytest.raises(ConfigurationError, match="duplicate"):
        parse_link_hosts("DOCS.EXAMPLE.COM\ndocs.example.com")


def test_overlay_replaces_the_default_without_mutating_it(policy):
    policy["markdown"]["link_host_allowlist"] = ["old.example"]
    derived = effective_policy(policy, "docs.example.com")

    assert derived["markdown"]["link_host_allowlist"] == ["docs.example.com"]
    assert policy["markdown"]["link_host_allowlist"] == ["old.example"]


def test_empty_input_restores_the_fail_closed_default(policy):
    policy["markdown"]["link_host_allowlist"] = ["old.example"]
    assert effective_policy(policy, "")["markdown"]["link_host_allowlist"] == []


def test_derived_path_prefix_is_enforced_by_the_existing_verifier(policy):
    derived = effective_policy(policy, "github.com/your-org/")
    allowlist = derived["markdown"]["link_host_allowlist"]

    check_link("https://github.com/your-org/project/docs", allowlist, "summary")
    with pytest.raises(Rejection, match="not on the allowlist"):
        check_link("https://github.com/another-org/project", allowlist, "summary")


def test_cli_rewrites_the_job_private_policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "markdown": {"link_host_allowlist": []},
        "other": {"unchanged": True},
    }))
    monkeypatch.setenv(
        "ACEIRO_LINK_HOST_ALLOWLIST",
        "docs.example.com\ngithub.com/your-org/",
    )
    monkeypatch.setattr(sys, "argv", ["policy_overlay.py", "--policy", str(path)])

    assert main() == 0
    assert json.loads(path.read_text()) == {
        "markdown": {
            "link_host_allowlist": [
                "docs.example.com",
                "github.com/your-org/",
            ]
        },
        "other": {"unchanged": True},
    }
