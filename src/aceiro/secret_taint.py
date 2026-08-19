"""Detect and redact contributor-supplied secret candidates before review.

Detection is intentionally separate from enforcement. ``detect-secrets``
supplies candidate detectors; aceiro owns the allowlists, stable placeholders,
and the in-memory plaintext set used by the verifier.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from detect_secrets.core.scan import scan_line
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
_ENTROPY_KINDS = frozenset({"Base64 High Entropy String", "Hex High Entropy String"})

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
    {"name": "Base64HighEntropyString", "limit": 4.5},
    {"name": "HexHighEntropyString", "limit": 3.0},
]


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


def detect_candidates(text: str, path: Path | None = None) -> list[tuple[str, str]]:
    """Return unique plaintext candidates and detector kinds in source order."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    settings = {"plugins_used": _PLUGINS, "filters_used": []}
    with transient_settings(settings):
        for line in text.splitlines():
            for secret in scan_line(line):
                value = secret.secret_value
                if value is None:
                    continue
                if secret.type in _ENTROPY_KINDS and not any(
                    f"{quote}{value}{quote}" in line for quote in ("'", '"', "`")
                ):
                    continue
                allowlisted_containers = [
                    *(match.group() for match in _UUID_IN_TEXT_RE.finditer(line)),
                    *(match.group() for match in _COMMON_HASH_IN_TEXT_RE.finditer(line)),
                ]
                if any(value in container for container in allowlisted_containers):
                    continue
                if value in seen or _is_allowlisted(value, secret.type, path):
                    continue
                seen.add(value)
                found.append((value, secret.type))
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
