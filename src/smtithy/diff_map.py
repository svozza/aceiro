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
from typing import NamedTuple

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# git's C-quoting escapes, as they appear in a diff header. Octal (\NNN) is
# handled separately because it encodes raw BYTES that must be reassembled before
# being decoded as UTF-8 — an accented name arrives as two octal escapes, and
# decoding them one at a time would produce mojibake rather than the character.
_C_ESCAPES = {
    "a": b"\a", "b": b"\b", "f": b"\f", "n": b"\n",
    "r": b"\r", "t": b"\t", "v": b"\v", "\\": b"\\", '"': b'"',
}
_OCTAL_RE = re.compile(r"[0-7]{1,3}")


def unquote_path(target: str) -> str:
    """Decode a diff header's path the way git wrote it.

    Git C-quotes any path that is not plain ASCII-printable: the whole target is
    wrapped in double quotes and the offending bytes are octal-escaped, so
    ``café.py`` arrives as ``"b/caf\\303\\251.py"`` and ``q"uote.py`` as
    ``"b/q\\"uote.py"``. Left undecoded, the `b/` prefix strip is a no-op (the
    string starts with a quote), and the hunk map ends up keyed on a value the
    files API can never produce — so every finding on such a file is rejected and
    one accented filename makes a whole review unusable.

    A target that does not start with a quote is returned verbatim: only quoted
    targets are escaped, so a literal backslash in an unquoted path is a literal
    backslash. /dev/null is never quoted, and passes through untouched.
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
            # Raw byte, accumulated into `out` so a multi-byte sequence is
            # decoded as one character at the end rather than per escape.
            out.append(int(octal.group(0), 8) & 0xFF)
            i = octal.end()
        elif following in _C_ESCAPES:
            out.extend(_C_ESCAPES[following])
            i += 2
        else:
            # Not an escape git emits. Keep it verbatim rather than guessing.
            out.extend(char.encode("utf-8"))
            i += 1
    # surrogateescape, not strict: the path is contributor-controlled, and a
    # filename that is not valid UTF-8 must still produce a usable key rather
    # than raising inside the shared walk.
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


def anchor_signatures(diff_text: str, window: int = 1) -> dict[tuple[str, int], str]:
    """Map (path, new-side line) -> a signature of the CODE at that anchor.

    The signature is the anchored line's text plus `window` new-side lines either
    side of it, whitespace-normalized. It exists to give the executor an identity
    key for a finding that does not depend on the model's prose:

    - stable when the model rewords the same finding (observed: it rewords on
      essentially every run, so a prose-derived key almost never matches);
    - stable when the code moves, since the window moves with it;
    - changes when the anchored code changes — at which point the finding really
      is about something new.

    The window matters: a bare line is often not unique (a file can hold two
    identical `return True` lines, one correct and one the defect), so the
    neighbours are what keep two findings on similar lines distinct.
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
        for number in numbered:
            parts = [
                " ".join(numbered.get(number + offset, "\x00absent").split())
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
            # Unquote BEFORE stripping `b/`: on a quoted target the prefix sits
            # inside the quotes, so removeprefix would otherwise be a no-op.
            target = unquote_path(line[4:].split("\t")[0])
            current_path = None if target == "/dev/null" else target.removeprefix("b/")
        elif header := HUNK_HEADER_RE.match(line):
            if current_path is not None:
                new_line = int(header.group(1))
                remaining = int(header.group(2) or "1")
                is_header = True
        positions.append(DiffPosition(current_path, None, is_header, line))
    return positions
