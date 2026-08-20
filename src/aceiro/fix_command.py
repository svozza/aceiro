"""Parse the `/fix N[,M...]` remediation command out of an issue-comment body.

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

The command names a SET of findings (ADR-0013), and naming several asserts they
take one remediation. That assertion is the commander's, and the only source of a
fix's scope that is neither the model's nor the harness's — so this module bounds
its SHAPE and nothing more. Whether the named findings are one defect is ADR-0005's
unverifiable content question and is never asked here.

The ordinals are 1-BASED, because they are the positions a human read in the posted
comment. commanded_index.json is 0-based, because it indexes a list. This module
is where the two meet, and it is the only place that conversion happens.
"""

from __future__ import annotations

import json
import re

from artifact import POLICY_PATH, finding_limit
from canonicalize import read_harness_text

# No review has more findings than the policy allows, so no ordinal above that cap
# can name one. Read from the policy rather than restated, because a restated
# number would keep bounding at 10 after an operator raised the cap and silently
# refuse commands for the findings they just enabled.
#
# Bounded HERE rather than left to the gate's range check: this refuses to carry an
# unbounded integer from an untrusted body into an int() at all.
MAX_ORDINAL = finding_limit(json.loads(read_harness_text(POLICY_PATH)))

# The count is bounded by the same cap, and by the same reasoning: a review has at
# most max_items findings, so a command listing more ordinals than that cannot name
# a distinct finding with each one. The ceiling already exists; this is it applied
# to the command's own length, so an untrusted body cannot make the parse walk an
# arbitrarily long list.
MAX_ORDINALS = MAX_ORDINAL

# ONE ASCII comma, no space around it. The separator is decided as narrowly as the
# verb's own separator and for the same reason: every spelling this admits is a
# spelling an untrusted body may use, and `/fix 1, 3` costs a commander one
# keystroke to correct while `[,\s]+` would be a new parse surface on input anyone
# can write. A comma rather than a space because a space-separated list cannot be
# distinguished from the trailing prose the whole-comment rule exists to refuse.
_ORDINAL = rf"[0-9]{{1,{len(str(MAX_ORDINAL))}}}"

# ONE ASCII space after the verb, and `\d` deliberately not used: Python's `\s`
# matches U+00A0 and U+2007, and `\d` matches every Unicode decimal digit, so a
# permissive pattern would accept bodies that do not read as this command in any
# renderer. `fullmatch` against the stripped body is what makes the command the
# whole comment.
#
# The digit bound is derived from the cap for the same reason the cap is derived: a
# pattern narrower than MAX_ORDINAL cannot express the highest legal ordinal, and
# the failure would be a command that reads correctly and does nothing. It is also
# what stops a million-digit body reaching int().
FIX_COMMAND_RE = re.compile(
    rf"/fix ({_ORDINAL}(?:,{_ORDINAL}){{0,{MAX_ORDINALS - 1}}})"
)


def parse_fix_command(body: str) -> frozenset[int] | None:
    """The 0-based indices the command names, or None if `body` is not a command.

    A SET, because `/fix 3,1` and `/fix 1,3` name the same findings and therefore
    the same command (ADR-0013); ordering is the commander's typing, not part of
    what was asserted. Duplicates collapse for the same reason — `/fix 1,1` names
    one finding, and reading it as two would let one ordinal be remediated twice
    within one plan's caps.

    None means "no command", never "a malformed command": there is nothing to
    report to a commander who did not issue one, and every non-command body on a
    busy pull request would otherwise produce noise. A body that IS shaped like a
    command and names an out-of-range ordinal is also None — `/fix 0` has no honest
    reading as "the first", since a comment a human read has no zeroth finding, and
    ONE bad ordinal refuses the WHOLE command rather than the set it could resolve:
    a commander who typed `/fix 1,0` asked for two findings, and delivering one of
    them is a scope the harness chose.
    """
    match = FIX_COMMAND_RE.fullmatch((body or "").strip())
    if match is None:
        return None
    ordinals = [int(part) for part in match.group(1).split(",")]
    if not all(1 <= ordinal <= MAX_ORDINAL for ordinal in ordinals):
        return None
    return frozenset(ordinal - 1 for ordinal in ordinals)
