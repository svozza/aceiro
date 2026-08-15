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
import urllib.error

from diff_map import normalize_signature_line
from github_api import (
    create_blob,
    create_commit,
    create_ref,
    create_tree,
    open_pull_request,
    pull_requests_for_base,
    read_commit,
)

# Identity marker for a follow-up pull request of ours, carrying ADR-0007's
# deduplication key. Read from the first line only — see owned_fix_key.
FIX_MARKER_RE = re.compile(r"<!-- smtithy:fix:([0-9a-f]{16}) -->")

BRANCH_ARGS = {"push_branch": "name", "open_pr": "branch"}


class Refusal(Exception):
    """The plan verified and decided this mode, but its shape carries no delivery.

    Same distinction execute_plan.Refusal draws: inside the safe grammar, outside
    what this delivery is willing to perform. Raised before any write, so a refused
    plan leaves nothing behind.
    """


class StrandedDelivery(Exception):
    """A verified plan's delivery stopped with a fix branch standing (ADR-0018).

    Not a Refusal: nothing is wrong with the plan, and Refusal promises to leave
    nothing behind. Both raises fire where a branch bearing this fix exists — the
    403 after this run pushed it, the 422 when a prior run's survives — so the
    commander must be told, and the message names the state to clean up.

    A sibling of AlreadyDelivered rather than a Refusal subclass, deliberately:
    if the dedicated except arm in execute_plan were ever lost or reordered, a
    subclass would be silently swallowed by `except Refusal` — regressing to the
    silent orphan finding 0002 measured, invisibly. A sibling propagates as a
    loud traceback instead. No structured branch/commit attributes either: the
    raise sites already build the commander-addressed message, and they differ in
    a way a shared renderer would flatten (the 403's commit is the branch tip;
    the 422's is this run's dangling object).
    """


class AlreadyDelivered(Exception):
    """ADR-0007's deduplication refusal: a follow-up PR for this
    (pr, head_sha, finding) already exists.

    Not a Refusal, because nothing is wrong with the plan — the effect it asks for
    has already happened, and re-delivering would be the second branch and second
    pull request ADR-0007 forbids. Carries the existing PR so a commander is told
    where the fix is rather than that their command failed.
    """


def fix_marker(key: str) -> str:
    return f"<!-- smtithy:fix:{key} -->"


def finding_component(finding: dict, signatures: dict[tuple[str, int], str]) -> str:
    """One commanded finding's contribution to a fix key.

    The finding as its PATH plus its LINE plus the anchor signature of that line —
    the anchored CODE, never the model's prose. Measured on the extraction source:
    the model reworded every finding on every run over a byte-identical diff, so a
    title-or-body-derived key never matched twice. Severity is out for the same
    reason it is out of a suggestion's fingerprint: a re-graded finding is the same
    defect.

    The line is IN the component, on both branches. A window=1 signature is not
    unique for periodic code — two copy-pasted blocks give two anchors the same
    window — so signature-alone made two distinct findings one key, and the
    follow-up pull request for the first then refused every later command for the
    second with AlreadyDelivered, pointing the commander at a fix for another
    defect. Costs no anchor stability to include: `head_sha` is a component of the
    key this feeds, so within one key's scope the file's bytes are fixed and the
    line cannot shift. This is the same collision
    suggest.suggestion_fingerprint answers by folding `old` in.

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
        else f"anchored\0{line}\0{normalize_signature_line(signature)}"
    )
    return f"{path}\0{anchored}"


def fix_key(pr_number: int, head_sha: str, findings: list[dict],
            signatures: dict[tuple[str, int], str]) -> str:
    """ADR-0007's (pr, head_sha, findings) deduplication key, over the SET.

    Each component earns its place:

    - `pr_number`, because two pull requests can carry byte-identical findings on
      the same path and a fix for one must not dedup against the other's.
    - `head_sha`, because this delivery's premise dies with the head (ADR-0009
      addendum). A new head means the anchors were re-verified against different
      bytes, so an earlier fix PR does not speak for it and a fresh command must be
      honoured. This is exactly where the key differs from a suggestion's, which
      deliberately excludes the SHA because the head churns when the suggestion
      does not.
    - the commanded findings, each through finding_component, folded in SORTED
      order.

    SORTED is what makes this a key over the SET rather than over the typed list
    (ADR-0013): `/fix 3,1` and `/fix 1,3` are one command, so they must not open
    two follow-up pull requests. The sort is over the per-finding components rather
    than over the ordinals, because the ordinals are not in the key at all — a
    re-graded artifact reorders them while the defects are unchanged, which is the
    silent wrong-finding failure ADR-0007's second addendum exists to prevent.

    `/fix 1` and `/fix 1,3` therefore compute DIFFERENT keys, and that is
    deliberate: a wider scope is a different fix and a different artefact, so
    refusing it would mean a commander who narrowed too far could never widen. The
    count is in the key by construction — a set of one folds one component — so no
    prefix relationship can collide.

    Each component is HASHED before the fold, so what is joined is fixed-length hex
    and no content can span a boundary. A separator would not do, however exotic:
    the last field of a component is the anchor signature, which is CONTRIBUTOR CODE
    and may contain any byte at all (normalize_signature_line folds indentation and
    composes NFC — it does not restrict the alphabet). A file crafted to carry the
    separator plus a well-formed second component would then make `/fix 1` fold to
    the same string as `/fix 1,3`, and since a key match REFUSES with
    AlreadyDelivered that is a denial of service on every later command for those
    findings, pointing the commander at a fix for a different scope. Verified
    reachable before this was written, which is why the property is hex rather than
    a cleverer delimiter.

    A SET of components, so equal components fold once. The parse already collapses
    a repeated ordinal, and this agrees with it rather than relying on it — and it
    is the consistent reading of a key that deliberately ignores prose and severity:
    two findings sharing a path, a line and an anchor signature are one defect to
    every other identity in this harness, so they must not make one command look
    like two.
    """
    digests = sorted({
        hashlib.sha256(finding_component(finding, signatures).encode()).hexdigest()
        for finding in findings
    })
    parts = [str(pr_number), head_sha, *digests]
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


def commit_message(title: str, metadata: dict) -> str:
    """The fix commit's message: the plan's title, then the same disclosure.

    The title is model-authored and passed check_plan_markdown's single-line
    pattern, so it is a legal subject. The body repeats what the pull request body
    says because a commit outlives the pull request that carried it: someone reading
    `git log` a year later must still see that these bytes were model-authored and
    approved by nobody.
    """
    return (
        f"{title}\n\n"
        "Generated by an AI model, not a human review; it counts toward no "
        "approval, and its content is anchored and bounded rather than verified.\n"
        f"\nmodel: {metadata['model']}\npolicy: {metadata['policy']}\n"
        f"reviewed SHA: {metadata['sha']}\nrun: {metadata['run_url']}\n"
    )


def one_step(steps: list[dict], kind: str) -> dict:
    """The plan's single step of `kind`, or a Refusal.

    Both gates' cardinality already allow at most one of each write-class kind, and
    decide_delivery already required exactly one of these two. This re-derives
    rather than assuming a gate ran — the posture the whole executor takes, and the
    one decide_delivery's own docstring states: "should be unreachable" is not a
    delivery mechanism.
    """
    found = [step for step in steps if step["kind"] == kind]
    if len(found) != 1:
        raise Refusal(f"the plan carries {len(found)} {kind} steps; this delivery needs exactly one")
    return found[0]


def deliver_stacked_pr(repo: str, steps: list[dict], applied: dict[str, bytes], *,
                       base: str, reviewed_sha: str, key: str, metadata: dict,
                       bot_login: str) -> dict:
    """Create the branch, commit the applied bytes, open the follow-up PR.

    `applied` comes from plan_verify.apply_patch_steps — the SAME function the
    verifier's anchoring phase ran, not a re-derivation. That is what makes the
    committed bytes the bytes that were bounded, denylisted and secret-scanned; a
    second replace() model here is the divergence eight of the chunk B review's
    fourteen findings came from.

    `base` is the reviewed pull request's own head BRANCH, supplied by the caller
    from the live PR context. It is a parameter with no default precisely because
    ADR-0009's addendum makes it un-model-suppliable: `open_pr` has no base
    argument, so there is nothing in the plan for this to read even by mistake.

    ORDER IS THE PARTIAL-FAILURE STRATEGY, since no POST here is retried (a failed
    POST has an uncertain outcome, so github_api never repeats one) and this
    function therefore cannot be idempotent. It is arranged so that every failure
    before the last two calls leaves NOTHING a human has to clean up:

    1. the dedup check, so a duplicate command is refused before it creates
       anything;
    2. blobs, a tree and a commit — all UNREFERENCED objects, invisible to
       everyone and eventually garbage-collected;
    3. create_ref, the first visible mutation. It 422s if the branch exists, which
       is atomic on the BRANCH NAME — not on the deduplication key, which nothing
       binds to the branch the plan happens to name. What makes step 1 safe despite
       being a read-then-write is the fix lane's per-pull-request concurrency group
       (`cancel-in-progress: false`), which serialises commands rather than running
       them together;
    4. open_pull_request.

    The one window that cannot be closed is between 3 and 4: a failure there leaves
    a real branch with no pull request. It is left deliberately rather than
    swallowed — the branch is named by the plan and confined to the harness
    namespace, the commit on it carries the full disclosure in its message, and a
    re-run refuses at create_ref with a message naming that branch. Deleting it on
    failure would need a delete credential this job should not hold, and would
    destroy the only evidence of what happened.
    """
    if not applied:
        raise Refusal("no patched content to commit: the plan changed no file's bytes")

    push = one_step(steps, "push_branch")
    open_pr = one_step(steps, "open_pr")
    branch = push["args"][BRANCH_ARGS["push_branch"]]
    opens_from = open_pr["args"][BRANCH_ARGS["open_pr"]]
    # check_write_class_targets proves these agree; re-checked because pushing to
    # one branch and opening from another delivers content no step described and
    # no frame bounded.
    if branch != opens_from:
        raise Refusal(
            f"the plan pushes {branch!r} but opens from {opens_from!r}; the follow-up pull "
            "request must open from the branch this plan pushed"
        )

    # Before any write: ADR-0007's refusal must land before a branch exists, or a
    # duplicate command leaves an orphan on its way to being refused.
    if existing := find_existing_fix(repo, base, key, bot_login=bot_login):
        # The message reaches the commander as a reply (ADR-0018), and the dedup
        # spans every PR state on purpose — so a closed match must say it is
        # closed, or "already exists" reads as deliverable evidence while the
        # remedy (reopening is the maintainer's move) stays in a docstring.
        # Measured in finding 0002's close-out, where the reply named a closed
        # pull request without saying so.
        if existing.get("state") == "closed":
            state_note = (
                " That pull request is merged, so the fix has already landed."
                if existing.get("merged_at") else
                " That pull request is closed without being merged: closing a fix is a "
                "maintainer's decision this command does not overrule, so reopening it "
                "is yours."
            )
        else:
            state_note = ""
        raise AlreadyDelivered(
            f"A follow-up pull request for this finding at this head already exists: "
            f"#{existing.get('number')} ({existing.get('html_url')}).{state_note} "
            "ADR-0007 deduplicates on (pull request, head SHA, finding), so this command "
            "is already delivered."
        )

    # The reviewed COMMIT's tree, because /git/trees takes a tree SHA. This is also
    # the anchor tree: basing on it is what makes the new tree a patch of the
    # reviewed head rather than a replacement of the repository.
    base_tree = read_commit(repo, reviewed_sha)["tree"]["sha"]

    blobs = {path: create_blob(repo, content) for path, content in applied.items()}
    tree = create_tree(repo, base_tree, blobs)
    commit = create_commit(repo, commit_message(open_pr["args"]["title"], metadata),
                           tree=tree, parent=reviewed_sha)
    try:
        create_ref(repo, branch, commit)
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise
        # The branch already exists, which the docstring above promises to report
        # as a refusal naming it. Reached where the dedup key cannot see the prior
        # effect — chiefly the deliberately-open window between 3 and 4, where a ref
        # exists with no pull request carrying the marker. StrandedDelivery, not
        # Refusal: a fix branch is standing (a PRIOR run's — this run's create_ref
        # failed and its commit is a dangling object), so the commander gets a
        # reply naming the state to clean up (ADR-0018); the bare HTTPError carried
        # neither branch nor commit, and dropped GitHub's own "Reference already
        # exists" body on the floor.
        raise StrandedDelivery(
            f"Branch {branch!r} already exists in {repo}, so this fix was already pushed "
            f"(possibly without its pull request); the commit built for it is {commit}. "
            "Delete the branch or close its follow-up pull request to retry"
        ) from exc
    print(f"created {branch!r} at {commit} ({len(blobs)} file(s) patched)")

    try:
        pull_request = open_pull_request(
            repo,
            head=branch,
            base=base,
            title=open_pr["args"]["title"],
            body=render_pr_body(open_pr["args"]["body"], key, metadata),
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        # The repository forbids Actions from opening pull requests, which is a
        # SETTING and not a scope: "Allow GitHub Actions to create and approve pull
        # requests" gates POST /pulls independently of the `pull-requests: write` this
        # job already holds, so the token has the scope and the call is refused anyway.
        #
        # Reported as a StrandedDelivery for the reason the 422 above is: the branch
        # and its commit already EXIST at this point, and a bare HTTPError reaches the
        # commander as a traceback that names neither — so the one piece of state they
        # have to clean up is the one thing the failure does not tell them. Measured in
        # production on artel PR #61, where this escaped as 24 lines of urllib stack,
        # and on the testbed (finding 0002's PR #17), where the Refusal it then was
        # left a pushed branch and a red run nothing pointed at.
        #
        # Not caught earlier as a precondition: the setting is not readable with the
        # permissions this job holds, and a delivery that checked it would be trusting
        # a second reader of what the API itself decides at the call.
        raise StrandedDelivery(
            f"The repository does not permit GitHub Actions to open pull requests, so the "
            f"fix was pushed to {branch!r} at {commit} and no follow-up pull request could "
            "be opened for it. Enable 'Allow GitHub Actions to create and approve pull "
            "requests' in Settings -> Actions -> General, then delete that branch and "
            "re-issue the command"
        ) from exc
    print(f"opened follow-up pull request #{pull_request.get('number')} "
          f"from {branch!r} into {base!r}")
    return pull_request
