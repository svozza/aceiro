"""One spelling of "invisible", shared by every gate that needs it.

CONTEXT.md's canonicalization: establishing what a target environment will
actually render a piece of text as, before deciding whether it is safe. Read by
the input fence (artifact.escape_fence), the artifact secret scan
(verify.rendered_text), the plan secret scan, and verify's canonical-text gate
(ADR-0011), so a code point added here closes every one of them at once.

If the artifact verifier is ported to TypeScript (ADR-0003 phases this table
first), it must stay one source of truth across both languages.
"""

from __future__ import annotations

import unicodedata

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
