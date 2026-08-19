"""Where text becomes text: one spelling of "invisible", and one of "decode".

CONTEXT.md's canonicalization: establishing what a target environment will
actually render a piece of text as, before deciding whether it is safe.

The invisible table is read by the input fence (artifact.escape_fence), the
artifact secret scan (verify.rendered_text), the plan secret scan, and verify's
canonical-text gate (ADR-0011), so a code point added here closes every one of
them at once.

The readers exist so the codec is never the platform's choice, and so
contributor-controlled bytes and harness-written bytes are decoded by different
rules.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

# Unicode Default_Ignorable_Code_Point ranges (DerivedCoreProperties.txt,
# Unicode 16.0), inclusive. Tabulated explicitly because unicodedata exposes no
# API for this property, and general category is not a usable proxy: several of
# these are Mn/Lo/Cn rather than Cf/Cc.
DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x115F, 0x1160),  # HANGUL CHOSEONG/JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ/AA
    (0x180B, 0x180F),  # MONGOLIAN FREE VARIATION SELECTORS, VOWEL SEPARATOR
    (0x200B, 0x200F),  # ZERO WIDTH SPACE..RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),  # bidi embeddings/overrides
    (0x2060, 0x206F),  # WORD JOINER..NOMINAL DIGIT SHAPES (incl. reserved)
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE (BOM)
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),  # reserved, default-ignorable
    (0x1BCA0, 0x1BCA3),  # SHORTHAND FORMAT CONTROLS
    (0x1D173, 0x1D17A),  # MUSICAL SYMBOL BEGIN/END BEAM..END PHRASE
    (0xE0000, 0xE0FFF),  # TAGS + VARIATION SELECTORS SUPPLEMENT (incl. reserved)
)


def is_default_ignorable(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in DEFAULT_IGNORABLE_RANGES)


def is_invisible(ch: str) -> bool:
    """`ch` renders as nothing, and is not a whitespace control.

    BOTH classes are needed — either alone is a bypass: general category Cf/Cc,
    and Default_Ignorable, which is Mn/Lo/Cn and so invisible to a category test.

    \\n, \\r and \\t are visible separation and never invisible here; dropping a
    tab would fuse two innocent runs into a false secret.
    """
    if ch in "\n\r\t":
        return False
    return unicodedata.category(ch) in ("Cf", "Cc") or is_default_ignorable(ch)


def strip_invisible(text: str) -> str:
    """Drop the code points that render as nothing but break exact matching."""
    return "".join(ch for ch in text if not is_invisible(ch))


def mark_invisible(text: str) -> str:
    """Replace each invisible code point with a visible ``<U+XXXX>`` marker.

    For text a HUMAN will read through a model — the fenced diff, above all.
    strip_invisible's deletion is right where the text is being matched or
    scanned, and wrong where it is being shown: a reviewer sent sanitised text
    cannot report a Trojan-Source construct it was never shown, and the whole
    class (a bidi override commenting out a guard, a ZWSP splitting one
    identifier into two) is exactly what the deletion makes read as ordinary
    code.

    Equally strong as a splice guard: the code point is gone as a code point
    either way, so text that only spelled a fence tag through an invisible no
    longer spells it. The marker is ASCII, so it cannot fuse with anything into
    a tag, and a payload writing the marker out literally gains nothing.
    """
    return "".join(f"<U+{ord(ch):04X}>" if is_invisible(ch) else ch for ch in text)


def decode_contributor_bytes(data: bytes) -> str:
    """Decode bytes a contributor controls — the diff, above all.

    Explicit UTF-8 with errors="replace", never the platform default: the diff is
    raw bytes in whatever encoding the changed files use, so a latin-1 byte would
    otherwise raise UnicodeDecodeError out of context assembly, and under a
    POSIX/C locale the effective codec is ASCII. U+FFFD in a diff line is a
    reviewable defect; a crashed job with no logged reason is not.

    Replacement is safe for the checks downstream because provenance compares
    LINE NUMBERS, and anchoring compares raw bytes read from the tree rather than
    anything decoded here.
    """
    return data.decode("utf-8", errors="replace")


def read_contributor_text(path: Path) -> str:
    """Read a file whose bytes a contributor controls, under
    decode_contributor_bytes' discipline. Same bytes from an API response go
    through that function directly."""
    return decode_contributor_bytes(path.read_bytes())


def read_harness_text(path: Path) -> str:
    """Decode a file the harness itself wrote (policy, prompt, JSON artifacts).

    Strict UTF-8: these are ours, so a decode error is a broken deployment to fail
    on rather than paper over.
    """
    return path.read_text(encoding="utf-8")
