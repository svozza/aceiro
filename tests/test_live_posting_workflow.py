from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_live_workflow_exercises_all_four_modes() -> None:
    workflow = (ROOT / ".github/workflows/live-posting-test.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "environment: ai-pr-review" in workflow
    assert "mode=stale" in workflow
    assert "mode=partial" in workflow
    assert "mode=draft" in workflow
    assert "run_live_post.py" in workflow
    assert "pull-requests: write" in workflow


def test_partial_mode_forces_post_write_movement_check() -> None:
    driver = (ROOT / "src/aceiro/evals/run_live_post.py").read_text()
    assert 'choices=("normal", "partial")' in driver
    assert "return None if checks == 1 else \"head changed\"" in driver
