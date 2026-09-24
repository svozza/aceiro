import json

import pytest
from hypothesis import given, settings, strategies as st
import secret_taint

from secret_taint import (
    RUNTIME_SECRET_VALUES,
    _ENTROPY_PLUGINS,
    _QUOTES,
    _quoted_high_entropy,
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


class TestEntropyScanEquivalence:
    @pytest.mark.parametrize("plugin", _ENTROPY_PLUGINS)
    @settings(max_examples=300, deadline=None)
    @given(parts=st.lists(st.one_of(
        st.sampled_from([
            HIGH_ENTROPY, HEX_SECRET, "hello-world-value", "0123456789012",
            "'", '"', "`", "\\", " ", "=", ";", ".", "é", "\x00",
        ]),
        st.text(alphabet="abcdefgABCDEF0123456789+/\\-_'\"` ", max_size=40),
    ), max_size=30))
    def test_matches_original_scanner(self, plugin, parts):
        line = "".join(parts)
        # Frozen legacy algorithm is intentionally independent of the optimized
        # boundary checks. Keep duplicate occurrences and ordering in this oracle.
        with plugin.non_quoted_string_regex(is_exact_match=False):
            runs = list(plugin.analyze_string(line))
        expected = [
            value for value in runs
            if any(f"{quote}{value}{quote}" in line for quote in _QUOTES)
            and plugin.calculate_shannon_entropy(value) > plugin.entropy_limit
        ]
        assert _quoted_high_entropy(plugin, line) == expected

    def test_later_quoting_preserves_candidate_and_placeholder_order(self):
        other = HIGH_ENTROPY[::-1]
        line = f'{HIGH_ENTROPY} "{other}" `{HIGH_ENTROPY}`'
        assert detect_candidates(line) == [
            (HIGH_ENTROPY, ENTROPY_KIND), (other, ENTROPY_KIND),
        ]
        policy = {}
        candidates = candidates_from_diff("+ " + line, policy)
        assert [candidate.value for candidate in candidates] == [HIGH_ENTROPY, other]
        assert candidates[0].placeholder.startswith("<SECRET_1:")
        assert candidates[1].placeholder.startswith("<SECRET_2:")

    @pytest.mark.parametrize("quote", _QUOTES)
    def test_shared_quote_boundary_does_not_hide_the_next_secret(self, quote):
        other = HIGH_ENTROPY[::-1]
        assert detect_candidates(f"{quote}{HIGH_ENTROPY}{quote}{other}{quote}") == [
            (HIGH_ENTROPY, ENTROPY_KIND), (other, ENTROPY_KIND),
        ]

    @pytest.mark.parametrize("quotes", [("'", '"'), ('"', "`"), ("`", "'")])
    def test_mismatched_quotes_do_not_qualify(self, quotes):
        assert detect_candidates(f"{quotes[0]}{HIGH_ENTROPY}{quotes[1]}") == []

    def test_minified_line_does_not_search_the_line_per_candidate(self):
        class NoSubstringSearch(str):
            def __contains__(self, value):
                raise AssertionError("repeated whole-line substring search")

        # Many short runs reproduce the SVG's expensive search pattern. The
        # sentinel at the end ensures the fast path still scans the whole line.
        line = NoSubstringSearch(
            '<path d="' + "M1 2L3 4 " * 10_000 + f'" data-value=`{HIGH_ENTROPY}`/>'
        )
        assert _quoted_high_entropy(_ENTROPY_PLUGINS[0], line) == [HIGH_ENTROPY]


class TestRepeatedLines:
    def test_repeated_lines_are_scanned_once_and_keep_first_seen_order(self, monkeypatch):
        original = secret_taint.scan_line
        scanned = []

        def record(line):
            scanned.append(line)
            return original(line)

        monkeypatch.setattr(secret_taint, "scan_line", record)
        password_line = f'password = "{PROPRIETARY_SECRET}"'
        entropy_line = f'token = "{HIGH_ENTROPY}"'
        text = "\n".join([password_line, "", entropy_line, password_line, "", entropy_line])
        assert detect_candidates(text) == [
            (PROPRIETARY_SECRET, "Secret Keyword"), (HIGH_ENTROPY, ENTROPY_KIND),
        ]
        assert scanned == [password_line, "", entropy_line]

    def test_line_reuse_does_not_cross_file_allowlists(self, tmp_path):
        text = f'password = "{PROPRIETARY_SECRET}"'
        assert detect_candidates(text, tmp_path / "uv.lock") == []
        assert detect_candidates(text, tmp_path / "config.py") == [
            (PROPRIETARY_SECRET, "Secret Keyword"),
        ]

    def test_every_repeated_occurrence_is_redacted(self, tmp_path):
        context = tmp_path / "context"
        head = tmp_path / "head"
        context.mkdir()
        head.mkdir()
        (context / "pr.json").write_text("{}")
        line = f'password = "{PROPRIETARY_SECRET}"\n'
        (context / "diff.patch").write_text(line * 2)
        (head / "config.py").write_text(line * 3)
        candidates = redact_review_inputs(context, head, {})
        assert len(candidates) == 1
        assert (context / "diff.patch").read_text().count(candidates[0].placeholder) == 2
        assert (head / "config.py").read_text().count(candidates[0].placeholder) == 3
