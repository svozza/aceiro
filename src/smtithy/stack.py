"""Stacked follow-up pull request delivery: the fallback suggestions cannot carry.

ADR-0009 makes suggestions the default and this the fallback for what they
structurally cannot express — above all the coordinated multi-file fix. The
reason is atomicity, not size: a suggestion is independently applicable, so a
multi-file fix delivered as per-file suggestions can be HALF-applied, leaving the
branch broken in a way neither the reviewer nor the contributor intended. A pull
request's merge is atomic, which is the whole reason this mode exists — and why a
plan deciding it fails closed rather than being delivered as the other mode.

Same-repo only. A stacked pull request needs its base branch to exist in the base
repository and a fork PR's head branch does not, so the fork case is refused
above this point (ADR-0009 addendum's fork asymmetry: suggestions are the only
delivery that works across both topologies, and a multi-file fix on a fork PR has
no automated delivery at all).

THE BASE IS THE REVIEWED PR'S OWN HEAD BRANCH. The fix merges INTO the open pull
request, so broken code never lands on the default branch: the author or any
maintainer merges the fix, the original PR updates, review continues, and one
complete pull request merges. `open_pr` deliberately has no `base` argument and
both gates pin its argument set exactly — a model-chosen base is a model-chosen
merge target, the same banned move as a model-selected policy version.

ADR-0007's DEDUPLICATION KEY lands here, and this is its first consumer:
`suggest.py` deliberately does not carry it, because the head churns exactly when
a suggestion does not, while a stacked PR's whole premise dies with the head. A
command is not idempotent the way a push is — two maintainers typing `/fix 3`, or
one typing it twice, must not produce two branches and two pull requests.

The key rides a marker on LINE 1 of the follow-up PR's body, which is the one
position model text cannot reach (`open_pr.body` is model-authored), and matching
it takes the marker AND the authenticated author, as every other ownership
decision in this harness does. Identity within the key is the anchored CODE, not
the finding's prose: the reference implementation measured the model rewording
every finding on essentially every run over a byte-identical diff, so a
prose-derived key never matches twice and every repeat command would open another
pull request.
"""

from __future__ import annotations

import hashlib
import re

from diff_map import normalize_signature_line
from github_api import pull_requests_for_base

# Identity marker for a follow-up pull request of ours, carrying ADR-0007's
# deduplication key. Read from the first line only — see owned_fix_key.
FIX_MARKER_RE = re.compile(r"<!-- smtithy:fix:([0-9a-f]{16}) -->")


def fix_marker(key: str) -> str:
    return f"<!-- smtithy:fix:{key} -->"


def fix_key(pr_number: int, head_sha: str, finding: dict,
            signatures: dict[tuple[str, int], str]) -> str:
    """ADR-0007's (pr, head_sha, finding) deduplication key.

    Each component earns its place:

    - `pr_number`, because two pull requests can carry byte-identical findings on
      the same path and a fix for one must not dedup against the other's.
    - `head_sha`, because this delivery's premise dies with the head (ADR-0009
      addendum). A new head means the anchors were re-verified against different
      bytes, so an earlier fix PR does not speak for it and a fresh command must be
      honoured. This is exactly where the key differs from a suggestion's, which
      deliberately excludes the SHA because the head churns when the suggestion
      does not.
    - the finding, as its PATH plus the anchor signature of its line — the
      anchored CODE, never the model's prose. Measured on the extraction source:
      the model reworded every finding on every run over a byte-identical diff, so
      a title-or-body-derived key never matched twice. Severity is out for the same
      reason it is out of a suggestion's fingerprint: a re-graded finding is the
      same defect.

    A signature the map does not carry falls back to the path and line alone.
    Provenance makes that unreachable for a verified plan — the finding's line must
    be inside a diff hunk — but identity must degrade rather than crash.

    BOTH branches are tagged, not just the fallback. A signature is contributor
    code, so its text is not ours to choose: tagging only the fallback leaves a
    line that happens to read `unanchored:2` keying identically to a finding with
    no signature at all, and one would then silently dedup against the other.
    Distinct tags make the two cases unaliasable whatever the file contains.
    """
    path, line = finding["path"], finding["line"]
    signature = signatures.get((path, line))
    anchored = (
        f"unanchored\0{line}" if signature is None
        else f"anchored\0{normalize_signature_line(signature)}"
    )
    parts = [str(pr_number), head_sha, path, anchored]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def marker_line(pull_request: dict) -> str:
    """The pull request body's first line, the only part this module authored.

    The key is read from here rather than from the whole body, for the same reason
    suggest.marker_line exists: `open_pr.body` is model-authored text that may
    legally contain the marker's literal characters inside a code fence (raw HTML
    is rejected only OUTSIDE one). A body-wide scan would let crafted content
    present itself as a fix pull request of ours on any key — and since a match
    REFUSES the command, that is a denial of service on every future `/fix` for
    that finding, not merely a mis-read.
    """
    return (pull_request.get("body") or "").split("\n", 1)[0]


def owned_fix_key(pull_request: dict, bot_login: str) -> str | None:
    """The dedup key of a follow-up pull request this harness opened, else None.

    Marker AND authenticated author, both load-bearing exactly as in
    suggest.owned_fingerprint: anyone can paste the marker into their own pull
    request body, and reading that as ours would refuse every later command for
    that finding. The author comes from the write token itself
    (post.resolve_bot_login) rather than from configuration.

    An empty `bot_login` matches nothing, including a pull request whose author
    GitHub reported as null: resolution fails closed upstream, and this must not
    turn an unresolved identity into ownership.
    """
    if not bot_login or (pull_request.get("user") or {}).get("login") != bot_login:
        return None
    match = FIX_MARKER_RE.match(marker_line(pull_request).strip())
    return match.group(1) if match else None


def find_existing_fix(repo: str, base: str, key: str, *, bot_login: str) -> dict | None:
    """The follow-up pull request already delivering this key, or None.

    ADR-0007's refusal, made checkable. Scoped to pull requests opened against the
    reviewed head branch, which is the only base this delivery ever uses, and
    spanning every state: a maintainer who CLOSED a fix has made a decision, and a
    repeat command must not overrule it by opening a second one. Reopening is
    theirs.

    The listing is not a security boundary — it decides whether to refuse, and the
    refusal is the safe direction — but ownership still takes both halves, because
    a false match refuses a command that should have been honoured.
    """
    for pull_request in pull_requests_for_base(repo, base):
        if owned_fix_key(pull_request, bot_login) == key:
            return pull_request
    return None


NOT_A_HUMAN_REVIEW = (
    "**AI-suggested fix.** Generated by an AI model, not a human review, and it "
    "counts toward **no approval**. The patch content is not verified — only "
    "anchored to the reviewed head and bounded — so read the diff before merging."
)

STACKED_BASE_NOTE = (
    "This pull request is **stacked onto the pull request it fixes**, so merging "
    "it updates that pull request rather than the default branch. Broken code "
    "never lands on the default branch through this path."
)


def render_pr_body(model_body: str, key: str, metadata: dict) -> str:
    """The follow-up pull request's body.

    Structure is ours; the model's `open_pr.body` is inserted verbatim only after
    check_plan_markdown proved it inside the safe grammar.

    The marker is the FIRST line, by position, so owned_fix_key never has to
    recognise a pattern model text could imitate.

    The notice and the policy hash are required by ADR-0005 ("the rendered pull
    request body must carry the same 'generated by an AI model, counts toward no
    approval' notice the review comment does, plus the policy hash"): patch content
    being unverified by construction has to be visible to whoever merges, not just
    recorded in an ADR.
    """
    return "\n".join([
        fix_marker(key),
        NOT_A_HUMAN_REVIEW,
        "",
        STACKED_BASE_NOTE,
        "",
        model_body,
        "",
        "<sub>🤖 model: `{model}` · policy: `{policy}` · reviewed SHA: `{sha}` · "
        "[run]({run_url})</sub>".format(**metadata),
    ])
