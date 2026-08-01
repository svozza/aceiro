"""Deterministic, fail-closed verifier for AI review artifacts.

Interprets policy.json (the reviewable security object) against a review
artifact plus the SHA-anchored diff it claims to describe. Every check
allowlists a safe grammar; anything outside it rejects the whole artifact.

Checks, in order:
1. Strict structural schema (types, lengths, enums, no extra keys).
2. Provenance: each finding's path is a changed file and its line falls
   inside a diff hunk for that file.
3. Markdown AST allowlist on all text fields: node-type allowlist, link-host
   allowlist, no images, no raw HTML, no @-mentions in rendered text.
4. Secret scan (defense-in-depth, last layer).

Head-SHA recheck lives in post.py where the write token is.

Exit codes: 0 verified, 1 rejected (reasons on stderr), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt

from canonicalize import is_invisible, read_contributor_text, read_harness_text, strip_invisible
from diff_map import walk_diff

# Over-approximates GitHub's mention grammar (no trailing boundary check:
# rejecting a near-mention is safe, missing a real one is not).
MENTION_RE = re.compile(r"(?<![\w/])@[a-zA-Z0-9][a-zA-Z0-9-]{0,38}")

# GitHub (GFM) auto-links bare URLs in plain text even though commonmark does
# not, so URL-shaped text outside code is a link on GitHub and must pass the
# same allowlist as explicit links.
URL_CANDIDATE_RE = re.compile(r"(?:https?://|\bwww\.)[^\s<>]+", re.IGNORECASE)

# Inline links and reference definitions with a non-https scheme degrade to
# plain text under markdown-it (so the AST walk never sees them) but may be
# parsed as links by other renderers; reject them at the source level.
BAD_LINK_SCHEME_RE = re.compile(r"(?:\]\(|\]:)\s*(?:javascript|vbscript|data|file)\s*:", re.IGNORECASE)

# GitHub (GFM) also renders issue references (owner/repo#123), commit
# references (owner/repo@sha), and bare email addresses in comment prose as
# links, even though CommonMark surfaces none of them. Each reference form is
# mapped to the URL GitHub links it to and passed through the same host
# allowlist as explicit links; emails become mailto: (never https) and are
# rejected outright. Same lookbehind philosophy as MENTION_RE: no trailing
# boundary check, over-matching rejects safely.
ISSUE_REF_RE = re.compile(r"(?<![\w/])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)")
COMMIT_REF_RE = re.compile(r"(?<![\w/])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)@([0-9a-fA-F]{7,40})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class Rejection(Exception):
    """Artifact violates policy. Message states which check and why."""


# ---------------------------------------------------------------- schema ---


def check_scalar(value, spec: dict, where: str) -> None:
    match spec["type"]:
        case "string":
            if not isinstance(value, str):
                raise Rejection(f"{where}: expected string, got {type(value).__name__}")
            # Length measured on NFC so decomposed forms can't smuggle extra budget.
            length = len(unicodedata.normalize("NFC", value))
            if length < spec.get("min_length", 0):
                raise Rejection(f"{where}: shorter than min_length {spec['min_length']}")
            if length > spec["max_length"]:
                raise Rejection(f"{where}: exceeds max_length {spec['max_length']}")
            if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                raise Rejection(f"{where}: does not match required pattern {spec['pattern']!r}")
        case "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise Rejection(f"{where}: expected integer, got {type(value).__name__}")
            if value < spec.get("minimum", float("-inf")):
                raise Rejection(f"{where}: below minimum {spec['minimum']}")
        case "enum":
            if value not in spec["values"]:
                raise Rejection(f"{where}: {value!r} not in {spec['values']}")
        case kind:
            raise Rejection(f"policy error: unknown scalar type {kind!r} at {where}")


def top_level_scalars(schema: dict) -> dict[str, dict]:
    """The artifact's top-level scalar specs: everything that is not the
    findings array.

    One definition, used by the schema gate, the markdown walk and the secret
    scan, so a field the policy adds cannot be enforced by some of them and not
    others. Selected by NOT being the array rather than by naming the scalar
    types, so an unrecognised `type` reaches check_scalar's policy-error path
    instead of being silently skipped.
    """
    return {name: spec for name, spec in schema.items() if spec.get("type") != "array"}


def check_schema(artifact: dict, policy: dict) -> None:
    schema = policy["artifact_schema"]
    if not isinstance(artifact, dict):
        raise Rejection("artifact: expected a JSON object")
    extra = set(artifact) - set(schema)
    if extra:
        raise Rejection(f"artifact: unexpected keys {sorted(extra)}")
    missing = set(schema) - set(artifact)
    if missing:
        raise Rejection(f"artifact: missing keys {sorted(missing)}")

    for field, spec in top_level_scalars(schema).items():
        check_scalar(artifact[field], spec, field)

    findings_spec = schema["findings"]
    findings = artifact["findings"]
    if not isinstance(findings, list):
        raise Rejection("findings: expected an array")
    if len(findings) > findings_spec["max_items"]:
        raise Rejection(f"findings: {len(findings)} items exceeds max {findings_spec['max_items']}")

    item_fields = findings_spec["item_fields"]
    for index, finding in enumerate(findings):
        where = f"findings[{index}]"
        if not isinstance(finding, dict):
            raise Rejection(f"{where}: expected an object")
        extra = set(finding) - set(item_fields)
        if extra:
            raise Rejection(f"{where}: unexpected keys {sorted(extra)}")
        missing = set(item_fields) - set(finding)
        if missing:
            raise Rejection(f"{where}: missing keys {sorted(missing)}")
        for field, spec in item_fields.items():
            check_scalar(finding[field], spec, f"{where}.{field}")


# ------------------------------------------------------------ provenance ---


def parse_diff_hunks(diff_text: str) -> dict[str, set[int]]:
    """Map new-file path -> set of new-side line numbers present in hunks.

    Counts context and added lines (both exist at the head SHA); deleted lines
    take no new-side number. A projection of `diff_map.walk_diff`, which is
    shared with the diff annotation the model reads — one walk, so the lines
    offered to the model cannot drift from the lines provenance accepts.
    """
    hunk_lines: dict[str, set[int]] = {}
    for position in walk_diff(diff_text):
        if position.is_hunk_header and position.path is not None:
            hunk_lines.setdefault(position.path, set())
        elif position.new_line is not None and position.path is not None:
            hunk_lines[position.path].add(position.new_line)
    return hunk_lines


def check_provenance(artifact: dict, diff_text: str, changed_files: list[str], policy: dict) -> None:
    rules = policy["provenance"]
    changed = set(changed_files)
    hunks = parse_diff_hunks(diff_text)

    for index, finding in enumerate(artifact["findings"]):
        where = f"findings[{index}]"
        path, line = finding["path"], finding["line"]
        if rules["path_must_be_changed_file"] and path not in changed:
            raise Rejection(f"{where}: path {path!r} is not a changed file in this PR")
        if rules["line_must_be_in_diff_hunk"] and line not in hunks.get(path, set()):
            raise Rejection(f"{where}: line {line} of {path!r} is not inside any diff hunk")


# -------------------------------------------------------------- markdown ---


CODE_TOKEN_TYPES = {"code_inline", "code_block", "fence"}


def extract_prose(tokens) -> str:
    """Collect the rendered *text* content of a parsed AST, excluding code
    spans and code blocks (GitHub renders neither mentions nor auto-links
    inside code). Working from the AST rather than the source means escapes
    (``\\`@x\\```) and entity references (``&#64;x``) are seen exactly as they
    render. Chunks are newline-joined so no pattern can span a boundary that
    a rendered element (e.g. a code span) would interrupt."""
    parts: list[str] = []

    def walk(token_list) -> None:
        for token in token_list:
            if token.type in CODE_TOKEN_TYPES:
                continue
            if token.type == "text":
                parts.append(token.content)
            elif token.children:
                walk(token.children)

    walk(tokens)
    return "\n".join(parts)


def rendered_text(tokens) -> str:
    """The visible text a rendered document displays, for the secret scan.

    The threat is a human READING a complete credential, so unlike extract_prose:
    code spans and blocks are included (a key in backticks is fully visible), and
    adjacent inline chunks are joined with NO separator, since `AKIA**XX**YY`
    renders as one contiguous run. Only real rendered breaks become newlines, and
    invisible code points are dropped as a renderer drops them.
    """
    parts: list[str] = []

    def walk(token_list) -> None:
        for token in token_list:
            if token.type in ("code_block", "fence"):
                parts.append("\n" + token.content)
            elif token.type in ("text", "code_inline"):
                parts.append(token.content)
            elif token.type in ("softbreak", "hardbreak"):
                parts.append("\n")
            elif token.children:
                walk(token.children)
            elif token.block and token.type.endswith("_close"):
                parts.append("\n")

    walk(tokens)
    # canonicalize.strip_invisible, not a category test: much of the
    # Default_Ignorable table is Mn/Lo/Cn. It retains \n\t\r, which render as
    # separation — dropping them would fuse two innocent runs into a false secret.
    return strip_invisible("".join(parts))


_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def _has_dot_segment(path: str) -> bool:
    """A path segment is `.` or `..`, in literal or percent-encoded form.

    A browser applies RFC 3986 remove_dot_segments before issuing the request, so
    such a path resolves somewhere the allowlist prefix never covered.

    Percent-escapes are decoded first (``%2e%2e`` and ``..%2f`` reach the same
    resolved path) and backslash counts as a separator. Decoding is for detection
    only; nothing decoded is returned for comparison.
    """
    decoded = _PERCENT_RE.sub(lambda m: chr(int(m.group(1), 16)), path)
    return any(segment in (".", "..") for segment in re.split(r"[/\\]", decoded))


def normalize_host(url: str) -> str | None:
    """Extract a normalized host+path prefix for allowlist comparison.

    Returns None for anything that is not clean https to an ASCII host —
    unicode/homoglyph hosts, userinfo tricks, dot-segment traversal out of an
    allowlisted path prefix, and other schemes all fail closed rather than being
    'normalized' into acceptance.
    """
    url = url.strip()
    match = re.match(r"^https://([^/?#\s]+)(/[^\s?#]*)?", url)
    if not match:
        return None
    authority, path = match.group(1).lower(), match.group(2) or "/"
    if "@" in authority or ":" in authority:
        return None  # userinfo/port tricks: reject rather than normalize
    if not re.fullmatch(r"[a-z0-9.-]+", authority):
        return None  # non-ASCII / punycode-ambiguous host: reject
    if _has_dot_segment(path):
        return None  # traversal out of a path prefix: reject rather than resolve
    return authority + path  # path keeps its case; only the authority folds


def check_link(url: str, allowlist: list[str], where: str) -> None:
    normalized = normalize_host(url)
    if normalized is None:
        raise Rejection(f"{where}: link {url!r} is not clean https to an ASCII host")
    for prefix in allowlist:
        if prefix.endswith("/"):
            if normalized.startswith(prefix):
                return
        elif normalized == prefix or normalized.startswith(prefix + "/"):
            return
    raise Rejection(f"{where}: link host/path {normalized!r} not on the allowlist")


def walk_tokens(tokens, allowed: set[str], link_allowlist: list[str], where: str) -> None:
    for token in tokens:
        match token.type.removesuffix("_open").removesuffix("_close"):
            case "html_inline" | "html_block":
                raise Rejection(f"{where}: raw HTML is not allowed")
            case "image":
                raise Rejection(f"{where}: images are not allowed")
            case "link":
                if token.type == "link_open":
                    check_link(token.attrGet("href") or "", link_allowlist, where)
            case base_type if base_type not in allowed:
                raise Rejection(f"{where}: markdown node {token.type!r} is not allowed")

        if token.children:
            walk_tokens(token.children, allowed, link_allowlist, where)


# Stateless across parse() calls; construction is the expensive part.
_PARSER = MarkdownIt("commonmark").enable(["strikethrough"])


# A line the executor appends is probed against the real parser rather than
# against a hand-rolled fence scanner (see `unterminated_fence`). Unique enough
# that model text cannot collide with it, and it is never emitted anywhere.
_APPEND_PROBE = "\x00aipr-probe\x00"

# markdown-it normalises CR and CRLF to LF before parsing, so the probe must be
# appended to text normalised the same way or a lone \r shifts every token map
# and the probe lands on the wrong line (found by a property test).
_NEWLINES_RE = re.compile(r"\r\n?")


def _source_lines(text: str) -> list[str]:
    """The lines markdown-it will see, so token maps index into this list."""
    return _NEWLINES_RE.sub("\n", text).split("\n")


def code_lines(text: str) -> list[tuple[str, bool]]:
    """Every line of *text*, paired with whether markdown-it renders it as CODE.

    Code means fenced blocks (delimiters and all, since `~~` inside a fence
    renders literally) and indented code blocks. `token.map` is the token's
    ``[start, end)`` range over source lines, so this is the parser's own answer
    to "is this line code" rather than a re-derivation of it — which is the point:
    every hand-rolled version of the fence rules in this harness was wrong.

    Lines come back from here rather than being re-split by the caller because
    markdown-it normalises CR/CRLF to LF before parsing, so its line numbering
    matches `text.split("\\n")` only when the text has no lone CR. It returns the
    pairs so a consumer cannot index the parser's answer into the wrong list
    (found by a property test: a lone \\r shifted every index).

    Consumers that must not modify code — the reconciler's strike_through — use
    this.
    """
    lines = _source_lines(text)
    inside: set[int] = set()
    for token in _PARSER.parse("\n".join(lines)):
        if token.type in ("fence", "code_block") and token.map:
            inside.update(range(token.map[0], token.map[1]))
    return [(line, index in inside) for index, line in enumerate(lines)]


def unterminated_fence(text: str) -> str | None:
    """The opening marker of a code fence *text* never closes, or None.

    CommonMark allows a field to end inside a fence, but post.py appends its own
    content (the attribution footer) after the model's text, which an unclosed
    fence would swallow and render as literal code.

    Answered by asking markdown-it rather than scanning for fence syntax: append
    a probe line and see whether the parser puts it inside a fence. Hand-rolled
    versions of the rules were twice wrong here — a fence-line count read
    "```text ... ~~~" as balanced, and a marker-tracking walk accepted an
    over-indented closer.

    Returns the opener's marker, since a different one does not close the block.
    """
    document = _NEWLINES_RE.sub("\n", text) + f"\n{_APPEND_PROBE}"
    probe_line = len(document.split("\n")) - 1
    for token in _PARSER.parse(document):
        if token.type == "fence" and token.map and token.map[0] <= probe_line < token.map[1]:
            return token.markup
    return None


def check_markdown_field(text: str, policy_markdown: dict, where: str) -> None:
    # post.py renders the artifact's own strings, so the checked text has to BE
    # the posted text: canonicality is rejected-if-absent, never normalized here.
    # ADR-0011 for why rejection rather than stripping.
    if invisible := next((ch for ch in text if is_invisible(ch)), None):
        raise Rejection(
            f"{where}: contains invisible or bidirectional control U+{ord(invisible):04X}, "
            "which renders as nothing or reorders the text around it"
        )
    if text != unicodedata.normalize("NFC", text):
        raise Rejection(
            f"{where}: is not in Unicode NFC form; the posted text must be the checked text, "
            "so submit the composed form"
        )
    env: dict = {}
    tokens = _PARSER.parse(text, env)
    walk_tokens(tokens, set(policy_markdown["allowed_nodes"]), policy_markdown["link_host_allowlist"], where)

    # A link reference DEFINITION (``[label]: https://host``) emits no AST
    # token — the walk above never sees it — and renders as nothing on its
    # own. But post.py composes every field into one document, and reference
    # definitions are document-global: a definition planted in one field
    # resolves a ``[x][label]`` use (or ``![x][label]`` image) in another,
    # producing a link/image to an unchecked host that neither field's
    # in-isolation check catches. A use whose definition is in the SAME field
    # resolves to a real link token and is caught by the allowlist walk; the
    # only way to smuggle one past composition is to split def and use across
    # fields, so reject any field that carries a reference definition at all.
    if env.get("references"):
        raise Rejection(f"{where}: link reference definitions are not allowed")

    if BAD_LINK_SCHEME_RE.search(text):
        raise Rejection(f"{where}: link with a non-https scheme is not allowed")

    if marker := unterminated_fence(text):
        raise Rejection(
            f"{where}: code fence opened with {marker!r} is never closed; "
            f"close it with a line of {marker!r}",
        )

    # Rendered-text checks on the AST's non-code text nodes: GitHub renders
    # mentions and auto-links bare URLs there even where commonmark does not.
    prose = extract_prose(tokens)

    # Mentions, raw HTML, and images are unconditional verifier invariants
    # (not policy knobs): nothing configurable may re-enable them.
    match = MENTION_RE.search(prose)
    if match:
        raise Rejection(f"{where}: @-mention {match.group(0)!r} is not allowed")

    for candidate in URL_CANDIDATE_RE.findall(prose):
        check_link(candidate.rstrip(".,;:!?)"), policy_markdown["link_host_allowlist"], where)

    # GitHub renders cross-repo references in prose as links; validate the
    # URL each form resolves to against the same allowlist. Same-repo forms
    # (#123, bare SHAs) resolve inside this repo and need no check. URL
    # candidates were validated above, so blank them first — otherwise an
    # allowlisted URL's own path/fragment (…/repo#1) false-positives here.
    ref_prose = URL_CANDIDATE_RE.sub(" ", prose)
    for owner, repo, number in ISSUE_REF_RE.findall(ref_prose):
        check_link(f"https://github.com/{owner}/{repo}/issues/{number}", policy_markdown["link_host_allowlist"], where)
    for owner, repo, sha in COMMIT_REF_RE.findall(ref_prose):
        check_link(f"https://github.com/{owner}/{repo}/commit/{sha}", policy_markdown["link_host_allowlist"], where)

    # Bare emails autolink to mailto: — never https, so never allowlistable.
    match = EMAIL_RE.search(ref_prose)
    if match:
        raise Rejection(f"{where}: email address {match.group(0)!r} renders as a mailto link; put it in backticks")


def markdown_fields(specs: dict) -> list[str]:
    """String fields the policy marks as markdown-bearing. Fail closed: a
    string field that is neither markdown-checked nor pattern-constrained
    would flow into the posted comment unchecked, so it is a policy error."""
    fields = []
    for name, spec in specs.items():
        if spec.get("markdown"):
            fields.append(name)
        elif spec["type"] == "string" and "pattern" not in spec:
            raise Rejection(f"policy error: string field {name!r} is neither markdown-checked nor pattern-constrained")
    return fields


def check_all_markdown(artifact: dict, policy: dict) -> None:
    markdown_policy = policy["markdown"]
    schema = policy["artifact_schema"]

    top_level = top_level_scalars(schema)
    for field in markdown_fields(top_level):
        check_markdown_field(artifact[field], markdown_policy, field)

    finding_fields = markdown_fields(schema["findings"]["item_fields"])
    for index, finding in enumerate(artifact["findings"]):
        for field in finding_fields:
            check_markdown_field(finding[field], markdown_policy, f"findings[{index}].{field}")


# ------------------------------------------------------------ secret scan --


def _iter_markdown_values(artifact: dict, policy: dict):
    schema = policy["artifact_schema"]
    top_level = top_level_scalars(schema)
    for field in markdown_fields(top_level):
        yield artifact[field]
    finding_fields = markdown_fields(schema["findings"]["item_fields"])
    for finding in artifact["findings"]:
        for field in finding_fields:
            yield finding[field]


def link_destinations(tokens) -> list[str]:
    """Every href the document renders, for the secret scan.

    rendered_text collects text nodes, never attributes, so destinations need
    their own collector. Only link_open survives to be rendered: images and
    reference definitions are rejected before this runs.

    Each href is returned twice, raw and percent-decoded — markdown-it
    percent-encodes an href, so an entity for U+200B arrives as ``%E2%80%8B``
    where nothing can see it, while a browser decodes before sending. For
    scanning only; the allowlist compares the undecoded form (normalize_host).
    """
    found: list[str] = []

    def walk(token_list) -> None:
        for token in token_list:
            if token.type == "link_open":
                href = token.attrGet("href") or ""
                found.append(href)
                decoded = unquote(href)
                if decoded != href:
                    found.append(decoded)
            if token.children:
                walk(token.children)

    walk(tokens)
    return found


def rendered_markdown(value: str) -> str:
    """A markdown field's visible rendered text, NFC-normalized first — the
    one spelling of "what the reader sees" that every secret scan uses."""
    return rendered_text(_PARSER.parse(unicodedata.normalize("NFC", value)))


def check_secrets(artifact: dict, policy: dict) -> None:
    # Markdown can make these differ, so all three are scanned: the raw JSON
    # source (non-markdown fields, and markdown syntax itself), each field's
    # rendered text (formatting and entities that split a run in the source but
    # render as one credential), and each rendered link destination. Every one is
    # scanned both verbatim and invisible-stripped; keeping the verbatim copy
    # means stripping can only add matches, never fuse two runs into a false
    # negative.
    source = json.dumps(artifact, ensure_ascii=False)
    texts = [source, strip_invisible(source)]
    for value in _iter_markdown_values(artifact, policy):
        tokens = _PARSER.parse(unicodedata.normalize("NFC", value))
        texts.append(rendered_text(tokens))
        for href in link_destinations(tokens):
            texts.append(href)
            texts.append(strip_invisible(href))
    for pattern in policy["secret_scan_patterns"]:
        for text in texts:
            if re.search(pattern, text):
                raise Rejection(f"secret scan: content matches pattern {pattern!r}")


# ----------------------------------------------------------------- driver --


def verify(artifact: dict, diff_text: str, changed_files: list[str], policy: dict) -> None:
    """Raise Rejection on the first policy violation; return None if verified."""
    check_schema(artifact, policy)
    check_provenance(artifact, diff_text, changed_files, policy)
    check_all_markdown(artifact, policy)
    check_secrets(artifact, policy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--diff", required=True, type=Path)
    parser.add_argument("--changed-files", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args()

    try:
        artifact = json.loads(read_harness_text(args.artifact))
        diff_text = read_contributor_text(args.diff)
        changed_files = json.loads(read_harness_text(args.changed_files))
        policy = json.loads(read_harness_text(args.policy))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"verifier: cannot load inputs: {exc}", file=sys.stderr)
        return 2

    try:
        verify(artifact, diff_text, changed_files, policy)
    except Rejection as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1

    print("verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
