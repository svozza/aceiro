import json

import pytest

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


# 24 distinct base64 characters: entropy 4.585, above the 4.5 limit.
HIGH_ENTROPY = "kQ9zX2vB7nM4pL8wR3tY6uH1"
# Hex entropy 3.918: above the hex limit of 3.0, below the base64 limit.
HEX_SECRET = "3f9a1c7e2b8d4f60a5e1c9b7"
ENTROPY_KIND = "Base64 High Entropy String"


class TestEntropyDetectors:
    """The entropy limits hold for every quoting style, backticks included.

    detect-secrets' ad-hoc scan_line enables an eager fallback that returns
    unquoted charset runs WITHOUT applying the entropy limit. Aceiro accepted
    those runs whenever they sat in quotes or Markdown backticks, so in run
    35323035751 the public paths `packages/rboto-sns/` (entropy 3.68) and
    `crates/rboto-core/` (3.20) became secrets: redaction renamed the files in
    the diff and every later mention of them was rejected as a leak.
    """

    def test_backticked_ordinary_paths_are_not_secrets(self):
        text = "\n".join([
            "The shared code is in `crates/rboto-core/src/lib.rs`.",
            "Moved `packages/rboto-sns/` next to `packages/rboto-dynamodb/`.",
            "See `crates/rboto-core/`, \"packages/rboto-sqs/\" and 'codegen/tests/'.",
        ])
        assert detect_candidates(text) == []

    @pytest.mark.parametrize("quote", ["`", '"', "'"])
    def test_quoted_high_entropy_secret_is_detected(self, quote):
        assert detect_candidates(f"token = {quote}{HIGH_ENTROPY}{quote}") == [(HIGH_ENTROPY, ENTROPY_KIND)]

    def test_unquoted_high_entropy_string_is_not_a_candidate(self):
        assert detect_candidates(f"token = {HIGH_ENTROPY}") == []

    def test_a_low_entropy_quoted_string_cannot_mask_a_backticked_secret(self):
        # The eager fallback only runs when the quoted pass finds nothing, so a
        # quoted low-entropy string on the same line used to hide this secret.
        line = f'name = "hello-world-value"; token `{HIGH_ENTROPY}`'
        assert detect_candidates(line) == [(HIGH_ENTROPY, ENTROPY_KIND)]

    def test_hex_detector_keeps_its_own_limit_and_digit_heuristic(self):
        assert detect_candidates(f'digest = "{HEX_SECRET}"') == [(HEX_SECRET, "Hex High Entropy String")]
        assert detect_candidates(f"digest = `{HEX_SECRET}`") == [(HEX_SECRET, "Hex High Entropy String")]
        # All digits: 2.915 under the hex detector's digit penalty, below 3.0.
        assert detect_candidates('phone = "0123456789012"') == []

    def test_pattern_and_keyword_detectors_are_unaffected_by_quoting(self):
        aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        text = "\n".join([
            f"key in `{aws_key}` and again {aws_key} unquoted",
            f'password = "{PROPRIETARY_SECRET}"',
        ])
        assert detect_candidates(text) == [
            (aws_key, "AWS Access Key"),
            (PROPRIETARY_SECRET, "Secret Keyword"),
        ]

    def test_a_mixed_line_keeps_every_detector(self):
        line = f'password = "{PROPRIETARY_SECRET}"  # rotated from `{HIGH_ENTROPY}` in `crates/rboto-core/`'
        assert detect_candidates(line) == [
            (PROPRIETARY_SECRET, "Secret Keyword"),
            (HIGH_ENTROPY, ENTROPY_KIND),
        ]

    def test_redaction_leaves_ordinary_paths_and_the_diff_header_alone(self, tmp_path):
        context = tmp_path / "context"
        quarantine = tmp_path / "pr"
        context.mkdir()
        quarantine.mkdir()
        (context / "pr.json").write_text(json.dumps({"body": "Fixes `packages/rboto-sns/`."}))
        diff = (
            "+++ b/packages/rboto-sns/python/rboto_sns/exceptions.py\n"
            "@@ -1,2 +1,2 @@\n"
            f"+TOKEN = `{HIGH_ENTROPY}`\n"
            " import `packages/rboto-sns/`\n"
        )
        (context / "diff.patch").write_text(diff, encoding="utf-8")
        policy = {}

        candidates = redact_review_inputs(context, quarantine, policy)

        assert policy[RUNTIME_SECRET_VALUES] == (HIGH_ENTROPY,)
        redacted = (context / "diff.patch").read_text(encoding="utf-8")
        assert redacted.startswith("+++ b/packages/rboto-sns/python/rboto_sns/exceptions.py\n")
        assert HIGH_ENTROPY not in redacted
        assert candidates[0].placeholder in redacted
