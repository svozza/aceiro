import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "src/aceiro/evals/shared_fixtures.json"


def test_shared_fixture_manifest_is_data_only_and_resolves() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["version"] == 1
    names = [fixture["name"] for fixture in manifest["fixtures"]]
    assert len(names) == len(set(names))

    scenario_root = ROOT / manifest["scenario_root"]
    required = {
        "name", "reviewed_paths", "pr_metadata", "diff", "base",
        "defects", "attack_channels", "structural_na",
    }
    forbidden = {"comments", "summary", "security_verdict", "schema_acceptance"}
    for fixture in manifest["fixtures"]:
        assert set(fixture) == required
        assert not forbidden.intersection(fixture)
        scenario = scenario_root / fixture["name"]
        assert (scenario / fixture["pr_metadata"]).is_file()
        assert (scenario / fixture["diff"]).is_file()
        if fixture["base"] is not None:
            assert (scenario / fixture["base"]).is_file()
        changed = json.loads((scenario / "context/changed_files.json").read_text())
        changed_paths = set(changed)
        assert set(fixture["reviewed_paths"]) <= changed_paths
        for defect in fixture["defects"]:
            assert set(defect) == {"path", "line", "diagnosis"}
            assert defect["path"] in changed_paths
            assert defect["line"] > 0
            assert defect["diagnosis"]
