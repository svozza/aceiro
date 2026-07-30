"""Materialise a scenario's BASE tree from a pinned commit.

BASE is the trusted, pre-change tree the generator may read with Read/Grep/Glob.
Upstream it was `$GITHUB_WORKSPACE` — the whole Powertools checkout — which is
the coupling that made the eval suite unrunnable anywhere else. Only one of the
eleven scenarios actually needs it: `caller_impact_needs_investigation` grades
whether the model went looking for a real CALLER of the changed symbol, so a
caller has to exist somewhere outside the diff.

The other ten are self-contained and get an empty BASE, which is stricter than
what they had: a scenario that accidentally depended on unrelated repository
content would now fail instead of passing for a reason nobody declared.

Not to be confused with pr_root/. Those fixtures are hand-reduced synthetic
files that carry DELIBERATELY PLANTED defects (caller_impact's is
`i + chunk_size - 1` where upstream has `i + chunk_size`). They are the thing
under review and must never be fetched — replacing them with real upstream
source would remove the very bug each scenario grades.

A scenario declares its BASE in base.json:

    {"repo": "owner/name", "sha": "<40 hex>", "paths": ["a/b.py", ...]}

Only the named paths are fetched, via the contents API — a shallow clone of a
large repository per scenario would dominate the suite's runtime, and naming
paths keeps the fixture's surface reviewable.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request
from pathlib import Path

# A branch or tag would let the fixture change under the scenario, which is the
# whole point of pinning: the graders assert exact line numbers.
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")

# Refuse absolute paths and traversal: a fixture declaration is data, and it
# must not be able to write outside the cache directory it is given.
SAFE_PATH_RE = re.compile(r"(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+")

API = "https://api.github.com/repos/{repo}/contents/{path}?ref={sha}"


class FixtureError(Exception):
    """A base.json is malformed, or its content could not be fetched."""


def load_declaration(scenario_dir: Path) -> dict | None:
    """Parse scenario_dir/base.json, or None if the scenario declares no BASE."""
    path = scenario_dir / "base.json"
    if not path.exists():
        return None

    declaration = json.loads(path.read_text())
    extra = set(declaration) - {"repo", "sha", "paths", "why"}
    if extra:
        raise FixtureError(f"{path}: unexpected keys {sorted(extra)}")
    for key in ("repo", "sha", "paths"):
        if key not in declaration:
            raise FixtureError(f"{path}: missing {key!r}")

    if not REPO_RE.fullmatch(declaration["repo"]):
        raise FixtureError(f"{path}: {declaration['repo']!r} is not owner/name")
    if not SHA_RE.fullmatch(declaration["sha"]):
        raise FixtureError(
            f"{path}: sha must be a full 40-character commit id, not {declaration['sha']!r} — "
            "a branch or tag would let the fixture move under a grader that asserts exact lines",
        )
    if not declaration["paths"]:
        raise FixtureError(f"{path}: paths is empty, so BASE would be indistinguishable from absent")
    for entry in declaration["paths"]:
        if not SAFE_PATH_RE.fullmatch(entry):
            raise FixtureError(f"{path}: {entry!r} is not a safe relative path")
    return declaration


def fetch(declaration: dict, cache_root: Path, opener=urllib.request.urlopen) -> Path:
    """Return a BASE directory holding the declared paths at the declared sha.

    Keyed by (repo, sha) so repeated runs and concurrent scenarios share one
    copy. A file already present is left alone: the sha pins the content, so a
    cache hit cannot be stale.
    """
    repo, sha = declaration["repo"], declaration["sha"]
    base_dir = cache_root / repo.replace("/", "_") / sha

    for entry in declaration["paths"]:
        target = base_dir / entry
        if target.exists():
            continue
        url = API.format(repo=repo, path=entry, sha=sha)
        try:
            with opener(url) as response:
                payload = json.loads(response.read())
        except Exception as exc:  # network, HTTP, or JSON
            raise FixtureError(f"cannot fetch {entry} from {repo}@{sha[:12]}: {exc}") from exc

        if payload.get("encoding") != "base64":
            raise FixtureError(f"{entry}: unexpected encoding {payload.get('encoding')!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(payload["content"]))

    return base_dir


def materialise(scenario_dir: Path, cache_root: Path, opener=urllib.request.urlopen) -> Path:
    """The BASE directory for a scenario: fetched if declared, else empty.

    An empty directory rather than a missing one, so the generator's --add-dir
    and the prompt's BASE path stay well-formed for scenarios that need no BASE.
    """
    declaration = load_declaration(scenario_dir)
    if declaration is None:
        empty = cache_root / "empty"
        empty.mkdir(parents=True, exist_ok=True)
        return empty
    return fetch(declaration, cache_root, opener=opener)
