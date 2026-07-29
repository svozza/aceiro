"""Hypothesis property tests over the verifier's safe grammar.

The central invariant: for ARBITRARY artifact text, anything that verifies
contains no URL host outside the allowlist, no mention, no raw HTML and no
image outside code — the grammar is allowlisted, not the attacks enumerated.
"""

import re
import unicodedata
from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from markdown_it import MarkdownIt

from conftest import CHANGED_FILES, POLICY, SAMPLE_DIFF
from verify import (
    Rejection,
    check_scalar,
    extract_prose,
    normalize_host,
    parse_diff_hunks,
    verify,
)

ALLOWLIST = POLICY["markdown"]["link_host_allowlist"]

URL_RE = re.compile(r"https?://[^\s<>)\"'\]]+", re.IGNORECASE)

# Independent renderer for the agreement invariant: the verifier walks the
# token tree, this renders to HTML and inspects the anchors/images actually
# produced. Same parser family, but a different code path than walk_tokens —
# a divergence between "what the walk checks" and "what renders as a link"
# (the class of the escaped-backtick / HTML-entity bugs) fails the property.
_RENDERER = MarkdownIt("commonmark").enable(["strikethrough"])

TOP_LEVEL_KEYS = {"summary", "findings", "residual_risk"}
FINDING_KEYS = {"path", "line", "severity", "title", "body"}
extra_keys = st.text(min_size=1, max_size=40)
extra_values = st.one_of(st.booleans(), st.text(max_size=20), st.integers(), st.none())


class _LinkImageCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []
        self.images = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and "href" in attributes:
            self.hrefs.append(attributes["href"])
        elif tag == "img":
            self.images += 1

text_strategy = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=["Cs"]),
    max_size=3000,
)

# Bias some examples toward attack-shaped text.
attack_fragments = st.sampled_from(
    [
        "@maintainer",
        "@aws-powertools/team",
        "![x](https://evil.example.com/p.png)",
        "[x](https://evil.example.com)",
        "https://evil.example.com/?d=",
        "www.evil.example.com",
        "<script>",
        "<img src=x>",
        "<!-- marker -->",
        "](javascript:alert(1))",
        "https://docs.powertools.aws.dev@evil.com/",
        "`@safe-in-code`",
        "see https://docs.powertools.aws.dev/lambda/python/",
    ]
)
summaries = st.one_of(
    text_strategy,
    st.tuples(text_strategy, attack_fragments, text_strategy).map(lambda t: "".join(t)),
)


def allowlisted(normalized: str) -> bool:
    return any(
        normalized.startswith(p) if p.endswith("/") else (normalized == p or normalized.startswith(p + "/"))
        for p in ALLOWLIST
    )


def build_artifact(summary):
    return {"summary": summary or "x", "findings": [], "residual_risk": ""}


@given(summary=summaries)
@settings(max_examples=500)
def test_verified_text_has_no_offsite_url_outside_code(summary):
    artifact = build_artifact(summary)
    try:
        verify(artifact, "", [], POLICY)
    except Rejection:
        return  # rejection is always a safe outcome

    text = unicodedata.normalize("NFC", artifact["summary"])
    prose = extract_prose(MarkdownIt("commonmark").parse(text))
    for url in URL_RE.findall(prose):
        normalized = normalize_host(url.rstrip(".,;:!?)"))
        assert normalized is not None and allowlisted(normalized), (
            f"off-allowlist URL survived verification: {url!r}"
        )


@given(summary=summaries)
@settings(max_examples=500)
def test_verified_text_has_no_mention_html_or_image(summary):
    artifact = build_artifact(summary)
    try:
        verify(artifact, "", [], POLICY)
    except Rejection:
        return

    text = unicodedata.normalize("NFC", artifact["summary"])
    tokens = MarkdownIt("commonmark").parse(text)
    prose = extract_prose(tokens)
    assert not re.search(r"(?<![\w/])@[a-zA-Z0-9]", prose), f"mention survived: {text!r}"

    def walk(token_list):
        for token in token_list:
            assert token.type not in ("html_block", "html_inline"), f"raw HTML survived: {text!r}"
            assert token.type != "image", f"image survived: {text!r}"
            if token.children:
                walk(token.children)

    walk(tokens)


@given(summary=summaries)
@settings(max_examples=500)
def test_verified_links_and_images_agree_with_rendered_html(summary):
    """Whatever survives verification must, when independently rendered to
    HTML, produce no <img> and no <a href> outside the allowlist. Guards the
    verifier-vs-renderer divergence class (escaped backticks, entities) over
    arbitrary input rather than the fixed cases in the corpus."""
    artifact = build_artifact(summary)
    try:
        verify(artifact, "", [], POLICY)
    except Rejection:
        return

    text = unicodedata.normalize("NFC", artifact["summary"])
    collector = _LinkImageCollector()
    collector.feed(_RENDERER.render(text))

    assert collector.images == 0, f"image rendered from verified text: {text!r}"
    for href in collector.hrefs:
        normalized = normalize_host(href)
        assert normalized is not None and allowlisted(normalized), (
            f"off-allowlist link rendered from verified text: {href!r}"
        )


@given(key=extra_keys, value=extra_values)
def test_any_extra_top_level_key_is_rejected(key, value):
    """The injected-field class (auto_merge, approved, ...) generalized: no
    key outside the schema may survive, whatever its name or value."""
    artifact = build_artifact("x")
    if key in TOP_LEVEL_KEYS:
        return  # a legitimate key is not an injection
    artifact[key] = value
    with pytest.raises(Rejection):
        verify(artifact, SAMPLE_DIFF, CHANGED_FILES, POLICY)


@given(key=extra_keys, value=extra_values)
def test_any_extra_finding_key_is_rejected(key, value):
    if key in FINDING_KEYS:
        return
    artifact = {
        "summary": "s",
        "findings": [
            {"path": "tests/unit/test_logger.py", "line": 1, "severity": "low", "title": "t", "body": "b", key: value}
        ],
        "residual_risk": "",
    }
    with pytest.raises(Rejection):
        verify(artifact, SAMPLE_DIFF, CHANGED_FILES, POLICY)


@given(value=st.text(max_size=5000))
def test_length_caps_are_enforced_for_any_string(value):
    spec = {"type": "string", "min_length": 1, "max_length": 100}
    normalized_length = len(unicodedata.normalize("NFC", value))
    try:
        check_scalar(value, spec, "field")
        assert 1 <= normalized_length <= 100
    except Rejection:
        assert normalized_length < 1 or normalized_length > 100


@given(url=st.text(max_size=200))
def test_normalize_host_never_returns_non_ascii(url):
    normalized = normalize_host(url)
    if normalized is not None:
        host = normalized.split("/", 1)[0]
        assert re.fullmatch(r"[a-z0-9.-]+", host)


@given(
    line=st.integers(min_value=1, max_value=10_000),
    path=st.sampled_from(
        ["aws_lambda_powertools/logging/logger.py", "tests/unit/test_logger.py", "other.py"]
    ),
)
def test_provenance_only_accepts_hunk_lines(line, path):
    artifact = {
        "summary": "s",
        "findings": [{"path": path, "line": line, "severity": "low", "title": "t", "body": "b"}],
        "residual_risk": "",
    }
    hunks = parse_diff_hunks(SAMPLE_DIFF)
    expected_ok = path in CHANGED_FILES and line in hunks.get(path, set())
    try:
        verify(artifact, SAMPLE_DIFF, CHANGED_FILES, POLICY)
        assert expected_ok
    except Rejection:
        assert not expected_ok


# All match the finding-path pattern, so provenance is the only gate in play.
PATH_POOL = ["src/a.py", "src/b.py", "tests/c.py", "d.py", "pkg/mod/e.py"]
line_kinds = st.sampled_from(["ctx", "add", "del"])


@st.composite
def diff_and_files(draw):
    """Generate a syntactically valid unified diff plus its changed-file list,
    and independently compute the true new-side hunk lines. Returning the
    expected map (rather than trusting parse_diff_hunks) puts the parser under
    test too, not just the provenance rule that consumes it."""
    paths = draw(st.lists(st.sampled_from(PATH_POOL), min_size=1, max_size=3, unique=True))
    parts: list[str] = []
    expected: dict[str, set[int]] = {}
    for path in paths:
        start = draw(st.integers(min_value=1, max_value=50))
        kinds = draw(st.lists(line_kinds, min_size=1, max_size=8))
        new_count = sum(k in ("ctx", "add") for k in kinds)
        old_count = sum(k in ("ctx", "del") for k in kinds)
        parts += [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
        parts.append(f"@@ -{start},{old_count} +{start},{new_count} @@")
        new_line = start
        lines: set[int] = set()
        for kind in kinds:
            if kind == "del":
                parts.append("-removed")
            else:
                parts.append(" context" if kind == "ctx" else "+added")
                lines.add(new_line)
                new_line += 1
        expected[path] = lines
    return "\n".join(parts) + "\n", paths, expected


@given(
    data=diff_and_files(),
    path=st.sampled_from(PATH_POOL),
    line=st.integers(min_value=1, max_value=60),
)
def test_provenance_accepts_iff_path_changed_and_line_in_hunk(data, path, line):
    diff_text, changed_files, expected = data
    # The parser must agree with the independently-computed truth.
    assert parse_diff_hunks(diff_text) == expected

    artifact = {
        "summary": "s",
        "findings": [{"path": path, "line": line, "severity": "low", "title": "t", "body": "b"}],
        "residual_risk": "",
    }
    expected_ok = path in changed_files and line in expected.get(path, set())
    try:
        verify(artifact, diff_text, changed_files, POLICY)
        assert expected_ok, f"accepted off-provenance finding {path!r}:{line}"
    except Rejection:
        assert not expected_ok, f"rejected legitimate finding {path!r}:{line}"
