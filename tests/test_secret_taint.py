import json

from secret_taint import (
    RUNTIME_SECRET_VALUES,
    candidates_from_diff,
    detect_candidates,
    redact_review_inputs,
)


PROPRIETARY_SECRET = "vby4471-qmt83e2-prod"


def test_keyword_detector_catches_proprietary_password():
    assert detect_candidates(f'password = "{PROPRIETARY_SECRET}"') == [
        (PROPRIETARY_SECRET, "Secret Keyword")
    ]


def test_common_hash_and_uuid_are_allowlisted():
    text = "\n".join([
        'sha = "0123456789abcdef0123456789abcdef01234567"',
        'request_id = "123e4567-e89b-12d3-a456-426614174000"',
    ])
    assert detect_candidates(text) == []


def test_lockfile_is_allowlisted(tmp_path):
    lockfile = tmp_path / "package-lock.json"
    assert detect_candidates(f'password = "{PROPRIETARY_SECRET}"', lockfile) == []


def test_redacts_context_and_quarantine_with_one_stable_placeholder(tmp_path):
    context = tmp_path / "context"
    quarantine = tmp_path / "pr"
    context.mkdir()
    quarantine.mkdir()
    (context / "pr.json").write_text(json.dumps({
        "number": 1,
        "title": "credential cleanup",
        "body": f"remove {PROPRIETARY_SECRET}",
    }))
    (context / "diff.patch").write_text(
        f'+PASSWORD = "{PROPRIETARY_SECRET}"\n', encoding="utf-8"
    )
    source = quarantine / "settings.py"
    source.write_text(f'PASSWORD = "{PROPRIETARY_SECRET}"\n', encoding="utf-8")
    policy = {}

    candidates = redact_review_inputs(context, quarantine, policy)

    assert len(candidates) == 1
    placeholder = candidates[0].placeholder
    assert placeholder == "<SECRET_1:type=secret_keyword,length=20>"
    assert policy[RUNTIME_SECRET_VALUES] == (PROPRIETARY_SECRET,)
    for path in (context / "pr.json", context / "diff.patch", source):
        text = path.read_text(encoding="utf-8")
        assert PROPRIETARY_SECRET not in text
        assert placeholder in text


def test_binary_file_is_untouched(tmp_path):
    context = tmp_path / "context"
    quarantine = tmp_path / "pr"
    context.mkdir()
    quarantine.mkdir()
    (context / "pr.json").write_text("{}")
    (context / "diff.patch").write_text("")
    binary = quarantine / "image.bin"
    payload = b"\xff\x00password=vby4471-qmt83e2-prod"
    binary.write_bytes(payload)

    redact_review_inputs(context, quarantine, {})

    assert binary.read_bytes() == payload


def test_posting_gate_rederives_plaintext_without_persisting_it():
    policy = {}
    candidates = candidates_from_diff(
        f'+password = "{PROPRIETARY_SECRET}"\n', policy
    )
    assert [candidate.value for candidate in candidates] == [PROPRIETARY_SECRET]
    assert policy[RUNTIME_SECRET_VALUES] == (PROPRIETARY_SECRET,)
