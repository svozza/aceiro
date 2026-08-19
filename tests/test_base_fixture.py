"""Tests for the BASE fixture fetcher.

base_fixture takes a declaration from a scenario directory and writes files from
remote content into a cache. Both halves need pinning: the parser is what stops a
declaration from naming a moving ref or escaping its cache directory, and the
fetch is what a scenario's whole premise rests on.

No network here. The fetch tests inject a fake opener, so the deterministic suite
keeps making no external calls; the live fetch is exercised by
TestCallerImpactScenarioPremise under ACEIRO_FETCH_FIXTURES=1 and by any real
eval run.
"""

import base64
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "aceiro" / "evals"))

import base_fixture  # noqa: E402
from base_fixture import FixtureError, fetch, load_declaration, materialise  # noqa: E402

SHA = "a" * 40
VALID = {"repo": "owner/name", "sha": SHA, "paths": ["pkg/mod.py"]}


def write(tmp_path: Path, declaration) -> Path:
    (tmp_path / "base.json").write_text(json.dumps(declaration))
    return tmp_path


def fake_opener(contents: dict, calls: list | None = None):
    """An opener returning the contents API's shape for known paths."""

    def opener(url):
        if calls is not None:
            calls.append(url)
        path = url.split("/contents/")[1].split("?")[0]
        if path not in contents:
            raise OSError(f"404 for {path}")
        payload = {"encoding": "base64", "content": base64.b64encode(contents[path].encode()).decode()}
        return io.BytesIO(json.dumps(payload).encode())

    return opener


class TestLoadDeclaration:
    def test_absent_base_json_is_not_an_error(self, tmp_path):
        """Ten of the eleven scenarios declare no BASE; that is the norm."""
        assert load_declaration(tmp_path) is None

    def test_valid_declaration_round_trips(self, tmp_path):
        assert load_declaration(write(tmp_path, VALID)) == VALID

    def test_a_why_field_is_allowed(self, tmp_path):
        """Every fixture should be able to say why it needs a BASE at all."""
        declaration = {**VALID, "why": "grades investigation outside the diff"}
        assert load_declaration(write(tmp_path, declaration))["why"]

    @pytest.mark.parametrize("missing", ["repo", "sha", "paths"])
    def test_missing_required_key_rejects(self, tmp_path, missing):
        declaration = {k: v for k, v in VALID.items() if k != missing}
        with pytest.raises(FixtureError, match=missing):
            load_declaration(write(tmp_path, declaration))

    def test_unknown_key_rejects(self, tmp_path):
        """Fail closed on a key nobody reads: a misspelled 'paths' must not
        silently fetch nothing."""
        with pytest.raises(FixtureError, match="unexpected"):
            load_declaration(write(tmp_path, {**VALID, "pathz": ["x.py"]}))

    @pytest.mark.parametrize("ref", ["develop", "main", "v1.2.3", "a" * 39, "A" * 40, "z" * 40, ""])
    def test_a_moving_or_malformed_ref_rejects(self, tmp_path, ref):
        """The reason pinning exists: expect.json grades an exact line number, so
        a branch or tag would let the premise drift out from under it."""
        with pytest.raises(FixtureError, match=r"\['sha'\].*invalid"):
            load_declaration(write(tmp_path, {**VALID, "sha": ref}))

    @pytest.mark.parametrize("repo", ["name", "owner/", "/name", "owner/name/extra", "-bad/name", ""])
    def test_malformed_repo_rejects(self, tmp_path, repo):
        with pytest.raises(FixtureError, match=r"\['repo'\].*invalid"):
            load_declaration(write(tmp_path, {**VALID, "repo": repo}))

    def test_empty_paths_rejects(self, tmp_path):
        """An empty list would fetch nothing and produce a BASE indistinguishable
        from no declaration at all -- a scenario that looks configured but grades
        against emptiness."""
        with pytest.raises(FixtureError, match="empty"):
            load_declaration(write(tmp_path, {**VALID, "paths": []}))

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../../../etc/passwd",
            "pkg/../../escape.py",
            "pkg/../ok.py",
            "~/secrets",
            "pkg/mod.py\nextra",
        ],
    )
    def test_unsafe_path_rejects(self, tmp_path, path):
        """A declaration is data. It must not be able to write outside its cache."""
        with pytest.raises(FixtureError, match=r"\['paths'\]\[0\].*invalid"):
            load_declaration(write(tmp_path, {**VALID, "paths": [path]}))


class TestFetch:
    def test_writes_declared_paths_under_repo_and_sha(self, tmp_path):
        opener = fake_opener({"pkg/mod.py": "def f(): pass\n"})
        base = fetch(VALID, tmp_path, opener=opener)

        assert base == tmp_path / "owner_name" / SHA
        assert (base / "pkg/mod.py").read_text() == "def f(): pass\n"

    def test_the_sha_is_in_the_url(self, tmp_path):
        """Pinning is worthless if the request does not carry the pin."""
        calls = []
        fetch(VALID, tmp_path, opener=fake_opener({"pkg/mod.py": "x\n"}, calls))
        assert f"ref={SHA}" in calls[0]
        assert "owner/name" in calls[0]

    def test_nested_paths_get_their_directories(self, tmp_path):
        declaration = {**VALID, "paths": ["a/b/c/deep.py"]}
        base = fetch(declaration, tmp_path, opener=fake_opener({"a/b/c/deep.py": "deep\n"}))
        assert (base / "a/b/c/deep.py").read_text() == "deep\n"

    def test_a_cached_file_is_not_refetched(self, tmp_path):
        """The sha pins the content, so a hit cannot be stale. Concurrent
        scenarios share one copy (run_evals runs them in a thread pool)."""
        calls = []
        opener = fake_opener({"pkg/mod.py": "x\n"}, calls)
        fetch(VALID, tmp_path, opener=opener)
        fetch(VALID, tmp_path, opener=opener)
        assert len(calls) == 1

    def test_a_missing_path_fails_loudly(self, tmp_path):
        """Not silently: a BASE missing the file a scenario greps for would make
        the eval fail with an unrelated-looking reason."""
        with pytest.raises(FixtureError, match="cannot fetch"):
            fetch(VALID, tmp_path, opener=fake_opener({}))

    def test_an_unexpected_encoding_fails_loudly(self, tmp_path):
        """A large file comes back with encoding "none" and no content, which
        would otherwise decode to an empty file that looks fetched."""

        def opener(_url):
            return io.BytesIO(json.dumps({"encoding": "none", "content": ""}).encode())

        with pytest.raises(FixtureError, match="unexpected encoding"):
            fetch(VALID, tmp_path, opener=opener)


class TestMaterialise:
    def test_no_declaration_yields_an_existing_empty_directory(self, tmp_path):
        """Empty rather than missing: the generator passes BASE to --add-dir and
        names it in the prompt, so it has to be a real directory."""
        base = materialise(tmp_path, tmp_path / "cache")
        assert base.is_dir()
        assert not list(base.iterdir())

    def test_a_declaration_is_fetched(self, tmp_path):
        scenario = tmp_path / "scenario"
        scenario.mkdir()
        write(scenario, VALID)
        base = materialise(scenario, tmp_path / "cache", opener=fake_opener({"pkg/mod.py": "y\n"}))
        assert (base / "pkg/mod.py").read_text() == "y\n"


class TestTheRealScenarioDeclaration:
    """The shipped base.json, not a synthetic one."""

    def test_caller_impact_declaration_is_valid(self):
        scenarios = Path(__file__).parent.parent / "src" / "aceiro" / "evals" / "scenarios"
        declaration = load_declaration(scenarios / "caller_impact_needs_investigation")
        assert declaration is not None
        assert declaration["repo"] == "aws-powertools/powertools-lambda-python"
        base_fixture.BASE_DECLARATION_VALIDATOR.validate(declaration)
        assert declaration.get("why"), "a fixture that reaches the network should say why"

    def test_no_other_scenario_declares_a_base(self):
        """The coupling this replaced gave every scenario the same checkout. If a
        second scenario grows a BASE, that is a decision to make deliberately --
        not something to discover from a slow suite."""
        scenarios = Path(__file__).parent.parent / "src" / "aceiro" / "evals" / "scenarios"
        with_base = sorted(d.name for d in scenarios.iterdir() if d.is_dir() and (d / "base.json").exists())
        assert with_base == ["caller_impact_needs_investigation"]
