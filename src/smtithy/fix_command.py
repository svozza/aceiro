"""Parse the `/fix N` remediation command out of an issue-comment body.

The first thing the remediation lane runs, and the only thing standing between an
arbitrary comment and a credential-bearing job. On a public repository anyone may
comment, so this body is untrusted input in the ordinary sense; whether its author
may command anything is a separate question (author_trust, resolved on the COMMENT
author — ADR-0007).

Two decisions, both deliberately strict, because the effect is a real remediation:

**The command must be the comment's entire content.** A body that merely mentions
`/fix 2` — quoting someone else's command, or discussing one — is not a command.
The alternative makes every quotation fire an effect, and the cost of strictness is
that a commander writes a second, dedicated comment.

**One spelling, exactly.** No case folding, no whitespace normalisation, no
invisible-code-point stripping. `/fix​ 1` with a zero-width joiner renders as the
command and is not one: stripping would mean acting on a body different from the
one displayed, which is the equality ADR-0011 settles for posted text, applied to
input.

The ordinal is 1-BASED, because it is the position a human read in the posted
comment. commanded_index.json is 0-based, because it indexes a list. This module
is where the two meet, and it is the only place that conversion happens.
"""

from __future__ import annotations

import json
import re

from artifact import POLICY_PATH
from canonicalize import read_harness_text

# No review has more findings than the policy allows, so no ordinal above that cap
# can name one. Read from the policy rather than restated, because a restated
# number would keep bounding at 10 after an operator raised the cap and silently
# refuse commands for the findings they just enabled.
#
# Bounded HERE rather than left to the gate's range check: this refuses to carry an
# unbounded integer from an untrusted body into an int() at all.
MAX_ORDINAL = json.loads(read_harness_text(POLICY_PATH))["artifact_schema"]["findings"]["max_items"]

# ONE ASCII space, and `\d` deliberately not used: Python's `\s` matches U+00A0 and
# U+2007, and `\d` matches every Unicode decimal digit, so a permissive pattern
# would accept bodies that do not read as this command in any renderer. `fullmatch`
# against the stripped body is what makes the command the whole comment.
#
# The digit bound is derived from the cap for the same reason the cap is derived: a
# pattern narrower than MAX_ORDINAL cannot express the highest legal ordinal, and
# the failure would be a command that reads correctly and does nothing. It is also
# what stops a million-digit body reaching int().
FIX_COMMAND_RE = re.compile(rf"/fix ([0-9]{{1,{len(str(MAX_ORDINAL))}}})")


def parse_fix_command(body: str) -> int | None:
    """The 0-based index the command names, or None if `body` is not a command.

    None means "no command", never "a malformed command": there is nothing to
    report to a commander who did not issue one, and every non-command body on a
    busy pull request would otherwise produce noise. A body that IS a command and
    names an out-of-range ordinal is also None — `/fix 0` has no honest reading as
    "the first", since a comment a human read has no zeroth finding.
    """
    match = FIX_COMMAND_RE.fullmatch((body or "").strip())
    if match is None:
        return None
    ordinal = int(match.group(1))
    if not 1 <= ordinal <= MAX_ORDINAL:
        return None
    return ordinal - 1
