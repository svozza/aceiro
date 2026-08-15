"""The hash-pinned lockfiles agree with the requirements they were compiled from.

requirements.txt and requirements-dev.txt are what CI installs; requirements.in
and requirements-dev.in are what a reader edits. Nothing made the two agree, so
bumping `claude-agent-sdk==0.2.129` in the .in file and forgetting to recompile
left CI installing, testing and merging 0.2.128 — a declaration that was never
exercised. The version in the .in file is the version a reader believes shipped.

Deliberately NOT a regenerate-and-diff. That is the report's fix direction and it
is worse here: it needs a compiler outside the hash-pinned set (and the two
lockfiles were compiled by DIFFERENT ones — requirements.txt by `uv pip compile`,
requirements-dev.txt by `pip-compile`), so the check would fail on tool-version
drift rather than on the declaration drift it exists to catch. Instead this
asserts the property that actually matters and that a reader can verify by eye:
every version a .in file pins is the version the lockfile pins, and every name it
requires is in there at all. The transitive closure stays the compiler's business.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# `name==version` or a bare `name`, ignoring comments, blank lines, and pip's own
# directives (`-c requirements.txt`). Extras and markers are stripped: the name
# and the pin are what this compares.
REQUIREMENT_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?(?:==(?P<version>[^\s;]+))?")

PAIRS = [
    ("requirements.in", "requirements.txt"),
    ("requirements-dev.in", "requirements-dev.txt"),
    ("requirements-typecheck.in", "requirements-typecheck.txt"),
]


def declared(path: Path) -> dict[str, str | None]:
    """`name -> pinned version or None` for one requirements input."""
    out: dict[str, str | None] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if match := REQUIREMENT_RE.match(line):
            out[match.group("name").lower().replace("_", "-")] = match.group("version")
    return out


def locked(path: Path) -> dict[str, str]:
    """`name -> version` for one compiled lockfile. A pinned line opens with
    `name==version` and continues into `--hash=` lines, which are skipped by the
    same comment/continuation filter."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip().removesuffix("\\").strip()
        if not line or line.startswith("-"):
            continue
        if match := re.fullmatch(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s]+)", line):
            out[match.group("name").lower().replace("_", "-")] = match.group("version")
    return out


class TestParsers:
    """Both readers are test infrastructure: one that silently found nothing
    would make every assertion below vacuous."""

    def test_the_input_reader_finds_the_pinned_declarations(self):
        assert declared(ROOT / "requirements.in") == {"markdown-it-py": "4.2.0", "claude-agent-sdk": "0.2.128"}

    def test_the_input_reader_skips_directives_and_keeps_unpinned_names(self):
        assert declared(ROOT / "requirements-dev.in") == {"pytest": None, "hypothesis": None}

    def test_the_typecheck_input_declares_only_the_checker(self):
        # ADR-0017: the checker is a dev tool for its own CI job, so anything
        # else appearing here is a dependency creeping into that job unreviewed.
        assert declared(ROOT / "requirements-typecheck.in") == {"ty": None}

    def test_the_lockfile_reader_finds_pins_and_not_hashes(self):
        pins = locked(ROOT / "requirements.txt")
        assert pins["markdown-it-py"] == "4.2.0"
        assert pins["claude-agent-sdk"] == "0.2.128"
        assert len(pins) > 5, "the lockfile reader found almost nothing; it has stopped reading the format"
        assert not any(name.startswith("sha256") for name in pins)


@pytest.mark.parametrize(("source", "lockfile"), PAIRS)
class TestTheLockfileMatchesItsInput:
    def test_every_declared_name_is_locked(self, source, lockfile):
        missing = sorted(set(declared(ROOT / source)) - set(locked(ROOT / lockfile)))
        assert not missing, (
            f"{source} requires {missing}, which {lockfile} does not pin; the file CI installs "
            "does not carry the dependency the file a reader edits declares"
        )

    def test_every_pinned_version_is_the_locked_version(self, source, lockfile):
        pins = locked(ROOT / lockfile)
        drifted = {
            name: (version, pins[name])
            for name, version in declared(ROOT / source).items()
            if version is not None and name in pins and pins[name] != version
        }
        assert not drifted, (
            f"{source} pins versions {lockfile} disagrees with (declared, locked): {drifted}. "
            "CI installs the locked version, so the declaration was never exercised — recompile "
            "the lockfile in the same commit as the bump"
        )
