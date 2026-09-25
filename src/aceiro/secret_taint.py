"""Detect and redact contributor-supplied secret candidates before review.

Detection is intentionally separate from enforcement. ``detect-secrets``
supplies candidate detectors; aceiro owns the allowlists, stable placeholders,
and the in-memory plaintext set used by the verifier.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from detect_secrets.core.scan import scan_line
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HexHighEntropyString,
    HighEntropyStringsPlugin,
)
from detect_secrets.settings import transient_settings


RUNTIME_SECRET_VALUES = "_tainted_secret_values"
MAX_TEXT_BYTES = 2 * 1024 * 1024

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_COMMON_HASH_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_UUID_IN_TEXT_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_COMMON_HASH_IN_TEXT_RE = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^<SECRET_\d+:type=[a-z_]+,length=\d+>$")
_LOCKFILE_NAMES = frozenset({
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
})
_QUOTES = ("'", '"', "`")

# Pattern and keyword detectors run through detect-secrets' ad-hoc scan_line.
# The entropy detectors do not: scan_line's eager fallback returns candidates
# without applying the entropy limit, so they are scanned here, where quoting
# (including Markdown backticks) and the configured limit are both enforced.
_PLUGINS = [
    {"name": "ArtifactoryDetector"},
    {"name": "AWSKeyDetector"},
    {"name": "AzureStorageKeyDetector"},
    {"name": "BasicAuthDetector"},
    {"name": "CloudantDetector"},
    {"name": "DiscordBotTokenDetector"},
    {"name": "GitHubTokenDetector"},
    {"name": "GitLabTokenDetector"},
    {"name": "IbmCloudIamDetector"},
    {"name": "IbmCosHmacDetector"},
    {"name": "JwtTokenDetector"},
    {"name": "KeywordDetector"},
    {"name": "MailchimpDetector"},
    {"name": "NpmDetector"},
    {"name": "OpenAIDetector"},
    {"name": "PrivateKeyDetector"},
    {"name": "PypiTokenDetector"},
    {"name": "SendGridDetector"},
    {"name": "SlackDetector"},
    {"name": "SoftlayerDetector"},
    {"name": "SquareOAuthDetector"},
    {"name": "StripeDetector"},
    {"name": "TelegramBotTokenDetector"},
    {"name": "TwilioKeyDetector"},
]
_ENTROPY_PLUGINS = (Base64HighEntropyString(limit=4.5), HexHighEntropyString(limit=3.0))

# Retain digests and results, not source texts. These bounds keep reuse within
# one review affordable even for a tree with many distinct files or candidates.
_MAX_CACHED_FILES = 32768
_MAX_CACHED_RESULT_BYTES = 16 * 1024 * 1024


class _ScanCache:
    def __init__(self):
        self.results: OrderedDict[
            tuple[bytes, str | None], tuple[tuple[tuple[str, str], ...], int]
        ] = OrderedDict()
        self.result_bytes = 0
        self.closed = False

    def get(self, key):
        if self.closed:
            return None
        entry = self.results.get(key)
        if entry is None:
            return None
        self.results.move_to_end(key)
        return list(entry[0])

    def put(self, key, found):
        if self.closed:
            return
        previous = self.results.pop(key, None)
        if previous is not None:
            self.result_bytes -= previous[1]
        size = sum(len(value.encode()) + len(kind.encode()) for value, kind in found)
        if size > _MAX_CACHED_RESULT_BYTES:
            return
        while self.results and (
            len(self.results) >= _MAX_CACHED_FILES
            or self.result_bytes + size > _MAX_CACHED_RESULT_BYTES
        ):
            _, (_, removed_size) = self.results.popitem(last=False)
            self.result_bytes -= removed_size
        self.results[key] = (tuple(found), size)
        self.result_bytes += size


_SCAN_CACHE: ContextVar[_ScanCache | None] = ContextVar("secret_scan_cache", default=None)


@contextmanager
def reuse_secret_scans():
    """Reuse identical content only within one review; always discard on exit."""
    cache = _ScanCache()
    token = _SCAN_CACHE.set(cache)
    try:
        yield
    finally:
        cache.closed = True
        cache.results.clear()
        cache.result_bytes = 0
        _SCAN_CACHE.reset(token)


@dataclass(frozen=True)
class SecretCandidate:
    value: str
    kind: str
    placeholder: str


def _is_allowlisted(value: str, kind: str, path: Path | None) -> bool:
    if len(value) < 12 or len(value) > 512:
        return True
    if _PLACEHOLDER_RE.fullmatch(value):
        return True
    if path and path.name.lower() in _LOCKFILE_NAMES:
        return True

    # A secret-labelled hash remains suspicious; an unlabeled commit or content
    # hash is ordinary review context and would otherwise dominate detection.
    keyword = kind == "Secret Keyword"
    if not keyword and (_UUID_RE.fullmatch(value) or _COMMON_HASH_RE.fullmatch(value)):
        return True
    return False


def _quoted_high_entropy(plugin: HighEntropyStringsPlugin, line: str) -> list[str]:
    """Charset runs on `line` that are quote-enclosed and clear the plugin's limit.

    Every run is examined, so a low-entropy quoted string elsewhere on the line
    cannot mask a secret, which detect-secrets' eager fallback allows.
    """
    # Index quoted values once, rather than searching the entire line for every
    # run. Keep all occurrences in source order: an earlier unquoted occurrence
    # still qualifies when the same value appears quoted later on this line.
    runs: list[str] = []
    quoted: set[str] = set()
    with plugin.non_quoted_string_regex(is_exact_match=False):
        for match in plugin.regex.finditer(line):
            value = match.group()
            runs.append(value)
            start, end = match.span()
            if (start > 0 and end < len(line)
                    and line[start - 1] in _QUOTES and line[end] == line[start - 1]):
                quoted.add(value)
    return [
        value
        for value in runs
        if value in quoted
        and plugin.calculate_shannon_entropy(value) > plugin.entropy_limit
    ]


def detect_candidates(text: str, path: Path | None = None) -> list[tuple[str, str]]:
    """Return unique plaintext candidates and detector kinds in source order."""
    cache = _SCAN_CACHE.get()
    if cache is None:
        return _detect_candidates(text, path)
    # All detectors receive ad-hoc line context. The only path-dependent rule
    # is _is_allowlisted's basename check; preserve that distinction across roots.
    key = (sha256(text.encode("utf-8", errors="surrogatepass")).digest(),
           path.name.lower() if path else None)
    found = cache.get(key)
    if found is not None:
        return found
    found = _detect_candidates(text, path)
    cache.put(key, found)
    return found


def _detect_candidates(text: str, path: Path | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    seen_lines: set[str] = set()
    settings = {"plugins_used": _PLUGINS, "filters_used": []}
    with transient_settings(settings):
        for line in text.splitlines():
            # Ad-hoc detection has only this line as context. An identical line
            # in the same file cannot add a new candidate or change its order.
            if line in seen_lines:
                continue
            seen_lines.add(line)
            matches = [(secret.secret_value, secret.type) for secret in scan_line(line)]
            for plugin in _ENTROPY_PLUGINS:
                matches.extend((value, plugin.secret_type) for value in _quoted_high_entropy(plugin, line))
            for value, kind in matches:
                if value is None:
                    continue
                allowlisted_containers = [
                    *(match.group() for match in _UUID_IN_TEXT_RE.finditer(line)),
                    *(match.group() for match in _COMMON_HASH_IN_TEXT_RE.finditer(line)),
                ]
                if any(value in container for container in allowlisted_containers):
                    continue
                if value in seen or _is_allowlisted(value, kind, path):
                    continue
                seen.add(value)
                found.append((value, kind))
    return found


def candidates_for_texts(
    texts: Sequence[tuple[Path | None, str]],
) -> list[SecretCandidate]:
    """Detect candidates across texts and assign stable, shared placeholders."""
    detected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, text in texts:
        for value, kind in detect_candidates(text, path):
            if value not in seen:
                seen.add(value)
                detected.append((value, kind))

    return [
        SecretCandidate(
            value=value,
            kind=re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_"),
            placeholder=f"<SECRET_{index}:type="
            f"{re.sub(r'[^a-z0-9]+', '_', kind.lower()).strip('_')},length={len(value)}>",
        )
        for index, (value, kind) in enumerate(detected, start=1)
    ]


def redact_text(text: str, candidates: list[SecretCandidate]) -> str:
    """Replace exact candidate values, longest first to avoid partial overlap."""
    for candidate in sorted(candidates, key=lambda item: len(item.value), reverse=True):
        text = text.replace(candidate.value, candidate.placeholder)
    return text


def _read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def redact_review_inputs(
    context_dir: Path, pr_root: Path, policy: dict
) -> list[SecretCandidate]:
    """Redact context and quarantined files, retaining plaintext only in memory."""
    sources: list[tuple[Path, str]] = []
    context_paths = [context_dir / "pr.json", context_dir / "diff.patch"]
    for path in context_paths:
        if (text := _read_text_file(path)) is not None:
            sources.append((path, text))

    for path in sorted(
        item
        for item in pr_root.rglob("*")
        if item.is_file()
        and not item.is_symlink()
        and ".git" not in item.relative_to(pr_root).parts
    ):
        if (text := _read_text_file(path)) is not None:
            sources.append((path, text))

    candidates = candidates_for_texts(sources)
    if not candidates:
        policy[RUNTIME_SECRET_VALUES] = ()
        return []

    for path, text in sources:
        redacted = redact_text(text, candidates)
        if redacted != text:
            path.write_text(redacted, encoding="utf-8")

    policy[RUNTIME_SECRET_VALUES] = tuple(candidate.value for candidate in candidates)
    return candidates


def candidates_from_diff(diff_text: str, policy: dict) -> list[SecretCandidate]:
    """Re-derive taints in the posting process from the anchored original diff."""
    candidates = candidates_for_texts([(Path("diff.patch"), diff_text)])
    policy[RUNTIME_SECRET_VALUES] = tuple(candidate.value for candidate in candidates)
    return candidates
