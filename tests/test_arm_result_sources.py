import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
LOCK = ROOT / "docs/findings/architecture-separation/sources.lock.json"
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def test_arm_result_source_lock_is_closed_and_pinned() -> None:
    lock = json.loads(LOCK.read_text())
    assert set(lock) == {"version", "updated_at", "schema", "arms"}
    assert lock["version"] == 1
    assert SHA.fullmatch(lock["schema"]["commit"])
    assert SHA256.fullmatch(lock["schema"]["sha256"])
    assert [arm["arm_id"] for arm in lock["arms"]] == [
        "aceiro", "naive", "aws-durable",
    ]
    for arm in lock["arms"]:
        assert set(arm) == {
            "arm_id", "repository", "commit", "index_path", "index_sha256",
        }
        assert SHA.fullmatch(arm["commit"])
        assert SHA256.fullmatch(arm["index_sha256"])


def test_local_schema_matches_locked_hash() -> None:
    lock = json.loads(LOCK.read_text())
    schema = ROOT / lock["schema"]["path"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["schema"]["sha256"]
