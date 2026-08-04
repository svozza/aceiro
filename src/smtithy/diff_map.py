"""One walk over a unified diff, mapping each line to its new-file position.

Two consumers must never disagree: `verify.parse_diff_hunks` decides which lines
a finding may anchor to, and `artifact.annotate_diff` shows those numbers to the
generator. Were they to diverge, generators would be handed anchors the verifier
rejects. Sharing this walk makes that divergence inexpressible.

The rules, encoded once:
- `+` (added) and context lines exist at the head SHA and take the next number.
- `-` (removed) and `\\ No newline` lines take no number — they are not in the
  new file, so nothing can be anchored to them.
- Inside a hunk the `@@` header's declared new-side count is authoritative: a
  line that merely LOOKS like a `+++ ` header is hunk content there (an added
  line whose text starts with `++ `).
- A hunk whose `+++ ` target is /dev/null (a deletion) contributes no positions.
- A `+++ ` target git C-quoted (any path that is not plain ASCII-printable) is
  decoded back to the real filename before the `b/` prefix is stripped, so the
  hunk map's key is the same string the files API reports.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

from canonicalize import decode_contributor_bytes

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# git's C-quoting escapes. Octal (\NNN) is handled separately: it encodes raw
# BYTES that must be reassembled before UTF-8 decoding, or a two-escape accented
# character decodes to mojibake.
_C_ESCAPES = {
    "a": b"\a", "b": b"\b", "f": b"\f", "n": b"\n",
    "r": b"\r", "t": b"\t", "v": b"\v", "\\": b"\\", '"': b'"',
}
_OCTAL_RE = re.compile(r"[0-7]{1,3}")


def unquote_path(target: str) -> str:
    """Decode a diff header's path the way git wrote it.

    Git C-quotes any path that is not plain ASCII-printable: the target is wrapped
    in double quotes and the offending bytes octal-escaped, so ``café.py`` arrives
    as ``"b/caf\\303\\251.py"``. Callers must unquote BEFORE stripping the `b/`
    prefix, which otherwise sits inside the quotes.

    An unquoted target is returned verbatim, so a literal backslash stays literal;
    /dev/null is never quoted and passes through.
    """
    if not (target.startswith('"') and target.endswith('"') and len(target) >= 2):
        return target
    body = target[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\" or i + 1 >= len(body):
            out.extend(char.encode("utf-8"))
            i += 1
            continue
        following = body[i + 1]
        if octal := _OCTAL_RE.match(body, i + 1):
            # Accumulated as a byte; the decode happens once, at the end.
            out.append(int(octal.group(0), 8) & 0xFF)
            i = octal.end()
        elif following in _C_ESCAPES:
            out.extend(_C_ESCAPES[following])
            i += 2
        else:
            # Not an escape git emits: keep it verbatim rather than guess.
            out.extend(char.encode("utf-8"))
            i += 1
    # surrogateescape: a contributor-controlled name that is not valid UTF-8 must
    # still yield a usable key rather than raise inside the shared walk.
    return out.decode("utf-8", errors="surrogateescape")


def split_diff_lines(diff_text: str) -> list[str]:
    """Split on REAL line terminators only — never `str.splitlines()`.

    `splitlines()` also breaks on U+2028/U+2029, U+000B/U+000C and U+0085, none
    of which end a line in a unified diff. The diff is contributor-controlled, so
    a single U+2028 inside one added line would make it count as two: every later
    line's number shifts, the annotation column labels a fragment, and the line
    the model is told about is not the line it sees. Git itself only ever splits
    on \\n, so that is what we do (tolerating a \\r\\n tail).
    """
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


class DiffPosition(NamedTuple):
    """Where one physical line of a diff sits in the new version of the file.

    Carries the line's own `text`, so a consumer needing both gets them already
    paired rather than re-splitting the diff and risking a misalignment.
    """

    path: str | None  # new-side path, or None outside any file / in a deletion
    new_line: int | None  # new-file line number, or None if it has none
    is_hunk_header: bool
    text: str  # the physical diff line, verbatim (no trailing newline)


# Indentation the signature deliberately ignores, and nothing else. `str.split()`
# and `str.strip()` are both wrong here: each folds every Unicode whitespace code
# point, including U+2028/U+2029/U+000B/U+000C/U+0085 — the very separators
# split_diff_lines refuses to treat as line ends — so two different lines could
# collapse to one signature and hand one comment's identity to another anchor.
# This class is the whole answer for the interior AND the ends; whitespace
# normalization exists to survive reindentation, not to erase content.
_INDENT_RE = re.compile(r"[ \t]+")


def normalize_signature_line(text: str) -> str:
    """One line's contribution to a signature: NFC, then indentation-folded.

    NFC first, because a canonically equivalent re-encoding (an editor rewriting
    `café` from NFC to NFD on save) is no semantic change and GitHub renders both
    identically — an anchor that moves for it would make the executor delete a
    live comment thread and repost the same comment, which is the churn the
    anchor design exists to prevent. NFC also composes before the fold, so a
    decomposed sequence cannot hide a separator from it.

    Stripped of SPACES ONLY, never with a bare `str.strip()`: that folds every
    Unicode whitespace code point, so _INDENT_RE's deliberate narrowness ended at
    the first and last character and a line differing from its neighbour only by a
    trailing U+2028 collapsed onto it. A collision hands one comment's identity —
    and so the gate on every DELETE and PATCH — to a different anchor.

    A space is the whole class needed because the fold has already run: every
    `[ \\t]` run is one space by the time this strips, so there is no tab left to
    name.
    """
    return _INDENT_RE.sub(" ", unicodedata.normalize("NFC", text)).strip(" ")


def head_content_lines(content_source, path: str) -> dict[int, str] | None:
    """`path`'s lines at the head SHA, numbered from 1, or None if unreadable.

    Split by split_diff_lines' rule rather than str.splitlines(), so a U+2028 in
    contributor content does not end a line here while ending none in the diff —
    the two numberings must agree or a window is taken around the wrong line.

    Decoded as contributor bytes: this is head content, so an invalid byte is a
    reviewable fact about the file rather than something to raise over.
    """
    try:
        raw = content_source(path)
    except OSError:
        return None
    return dict(enumerate(split_diff_lines(decode_contributor_bytes(raw)), start=1))


def anchor_signatures(diff_text: str, window: int = 1, content_source=None) -> dict[tuple[str, int], str]:
    """Map (path, new-side line) -> a signature of the CODE at that anchor.

    The signature is the anchored line's text plus `window` lines either side of
    it, each canonicalized by normalize_signature_line. It exists to give the
    executor an identity key for a finding that does not depend on the model's
    prose:

    - stable when the model rewords the same finding (observed: it rewords on
      essentially every run, so a prose-derived key almost never matches);
    - stable when the code moves, since the window moves with it;
    - stable when a line is re-encoded without changing (ADR-0009 addendum
      specifies the fingerprint as NFC'd);
    - changes when the anchored code changes — at which point the finding really
      is about something new.

    The window matters: a bare line is often not unique (a file can hold two
    identical `return True` lines, one correct and one the defect), so the
    neighbours are what keep two findings on similar lines distinct.

    `content_source` is a path -> bytes reader for file content at the head SHA,
    and where the window comes from when one is given (ADR-0009 addendum: the
    window's source is part of the identity contract). Without it the window is
    over lines the DIFF makes visible, so a neighbour outside every hunk reads as
    `absent` rather than as its real text — and an unrelated push that grows a
    hunk around an unchanged line moves that line's signature, which costs a live
    comment thread. No function of the diff alone can close that: in the
    narrow-hunk run the neighbour's text is not in the input.

    The diff decides WHICH lines are anchorable either way. That is provenance's
    answer, and the head content is a superset of it — keying comments on a line
    no hunk makes visible would anchor them where a finding may not point.

    A path the source cannot read falls back to the diff-derived window for that
    path alone. Anchoring read the file moments earlier, so an unreadable one
    means the tree moved underneath the executor; identity is not a containment
    property, and failing the whole delivery over it would trade a churn cost for
    a total one.
    """
    # new-side content per path, in order, so a window can be taken around a line
    by_path: dict[str, dict[int, str]] = {}
    for position in walk_diff(diff_text):
        if position.new_line is not None and position.path is not None:
            raw = position.text
            text = raw[1:] if raw[:1] in ("+", " ") else raw
            by_path.setdefault(position.path, {})[position.new_line] = text

    signatures: dict[tuple[str, int], str] = {}
    for path, numbered in by_path.items():
        window_lines = numbered
        if content_source is not None:
            if (from_head := head_content_lines(content_source, path)) is not None:
                window_lines = from_head
        for number in numbered:
            parts = [
                normalize_signature_line(window_lines[key]) if (key := number + offset) in window_lines
                else "\x00absent"
                for offset in range(-window, window + 1)
            ]
            signatures[(path, number)] = "\x00".join(parts)
    return signatures


def walk_diff(diff_text: str) -> list[DiffPosition]:
    """One DiffPosition per line of `diff_text`, in order."""
    positions: list[DiffPosition] = []
    current_path: str | None = None
    new_line = 0
    remaining = 0  # new-side lines left in the current hunk, per the @@ header

    for line in split_diff_lines(diff_text):
        if remaining > 0 and current_path is not None:
            if line[:1] in ("-", "\\"):
                positions.append(DiffPosition(current_path, None, False, line))
            else:
                positions.append(DiffPosition(current_path, new_line, False, line))
                new_line += 1
                remaining -= 1
            continue

        is_header = False
        if line.startswith("+++ "):
            target = unquote_path(line[4:].split("\t")[0])
            current_path = None if target == "/dev/null" else target.removeprefix("b/")
        elif header := HUNK_HEADER_RE.match(line):
            if current_path is not None:
                new_line = int(header.group(1))
                remaining = int(header.group(2) or "1")
                is_header = True
        positions.append(DiffPosition(current_path, None, is_header, line))
    return positions
