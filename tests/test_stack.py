"""Tests for stack.py — the stacked follow-up pull request delivery.

ADR-0009's fallback for what a suggestion structurally cannot carry: a
coordinated multi-file fix, whose merge must be atomic where per-file
suggestions are independently applicable and can be half-applied.

Two properties carry most of the weight here, and both are pinned rather than
described:

- ADR-0007's deduplication key on (pr, head_sha, finding). Nothing in src/
  implemented it before this delivery; suggest.py deliberately does not, because
  the head churns exactly when a suggestion does not, while a stacked PR's whole
  premise dies with the head. The key rides a marker on LINE 1 of the follow-up
  PR's body, and matching it requires the marker AND the authenticated author.
- ADR-0009's addendum: the base is the reviewed PR's own head BRANCH, taken from
  the live PR context and never from the plan.

Network is never touched: the github_api helpers are stubbed.
"""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

import stack
from diff_map import anchor_signatures

from test_plan_verify import PLAN_DIFF, tree_source

POLICY = json.loads((Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text())

SIGNATURES = anchor_signatures(PLAN_DIFF, content_source=tree_source())

BOT = "smtithy[bot]"

METADATA = {
    "model": "global.anthropic.claude-opus-4-8",
    "policy": "0011223344ff",
    "sha": "reviewed-sha",
    "run_url": "https://github.com/o/r/actions/runs/1",
}

FINDING = {"path": "src/app.py", "line": 2, "severity": "high", "group": 1, "title": "t", "body": "b"}


def key(pr_number=7, head_sha="reviewed-sha", finding=None, findings=None, signatures=None):
    """A fix key. `finding` is the one-finding shorthand; `findings` names a set.

    Both spellings exist because ADR-0013 made the key set-valued while every
    single-ordinal property below is unchanged: the one-finding case must stay
    exactly what it was, or the widening would be a replacement.
    """
    if findings is None:
        findings = [FINDING if finding is None else finding]
    return stack.fix_key(
        pr_number, head_sha, findings,
        SIGNATURES if signatures is None else signatures,
    )


class TestTheDedupKey:
    """ADR-0007: the remediator refuses when a follow-up PR for
    (pr, head_sha, finding) already exists — "the equivalent of the reviewer's
    marker-keyed sticky comment", and "the kind of gap that only appears in
    production"."""

    def test_the_key_is_stable_across_calls(self):
        assert key() == key()

    def test_a_different_pull_request_is_a_different_key(self):
        # Two PRs can carry byte-identical findings on the same path; a fix for
        # one must not dedup against the other's.
        assert key(pr_number=7) != key(pr_number=8)

    def test_a_different_head_sha_is_a_different_key(self):
        # The premise dies with the head (ADR-0009 addendum). A new head means the
        # anchors were re-verified against different bytes, so the earlier fix PR
        # does not speak for it and a fresh command must be honoured.
        assert key(head_sha="reviewed-sha") != key(head_sha="pushed-sha")

    def test_a_different_finding_is_a_different_key(self):
        # Per-finding scoping is the whole point of /fix N: two commands naming
        # two findings must produce two PRs.
        other = FINDING | {"path": "src/util.py", "line": 1}
        assert key() != key(finding=other)

    def test_the_path_alone_distinguishes_two_findings(self):
        # Varying path AND line together lets the anchored component carry the
        # difference on its own, so `path` is never independently pinned. Two files
        # sharing an anchor line is routine for boilerplate.
        sigs = {("src/app.py", 1): "def handle(request):",
                ("src/util.py", 1): "def handle(request):"}
        here = FINDING | {"path": "src/app.py", "line": 1}
        there = FINDING | {"path": "src/util.py", "line": 1}
        assert key(finding=here, signatures=sigs) != key(finding=there, signatures=sigs)

    def test_the_line_alone_distinguishes_two_findings(self):
        # The collision that made a delivered fix refuse a DIFFERENT finding: two
        # copy-pasted blocks give two anchors the same window=1 signature, so
        # without the line the key cannot tell the two findings apart.
        window = "    log.info(token)"
        sigs = {("svc.py", 3): window, ("svc.py", 8): window}
        first = FINDING | {"path": "svc.py", "line": 3}
        second = FINDING | {"path": "svc.py", "line": 8}
        assert key(finding=first, signatures=sigs) != key(finding=second, signatures=sigs), (
            "two findings on repeated code share one key, so the follow-up pull "
            "request for the first refuses every /fix for the second at this head"
        )

    def test_the_key_ignores_the_findings_prose(self):
        # The measured lesson (ADR-0009 addendum): the model rewords every finding
        # on essentially every run over a byte-identical diff. A key that moves
        # with the wording never matches twice, so every repeat command would open
        # another PR -- exactly the duplication ADR-0007 forbids.
        reworded = FINDING | {"title": "completely different wording",
                              "body": "and a different body too"}
        assert key() == key(finding=reworded)

    def test_the_key_ignores_the_severity(self):
        # A re-graded finding is the same defect. Severity is deliberately out of
        # the anchor signature for findings, and it stays out here.
        assert key() == key(finding=FINDING | {"severity": "low"})

    def test_the_key_tracks_the_anchored_code(self):
        # Identity is the CODE, so a signature map describing different content at
        # the anchor yields a different key even with the finding untouched.
        moved = dict(SIGNATURES)
        moved[("src/app.py", 2)] = "something else entirely"
        assert key() != key(signatures=moved)

    def test_a_missing_signature_degrades_rather_than_crashing(self):
        # Provenance makes this unreachable for a verified plan (the finding's line
        # must be in a hunk), but identity must not crash if it happens: the same
        # posture suggestion_fingerprint takes.
        assert key(signatures={}) == key(signatures={})

    def test_a_missing_signature_is_not_the_same_key_as_a_present_one(self):
        # The fallback must not collide with the real thing, or an unanchorable
        # finding would dedup against an anchored one.
        assert key(signatures={}) != key()

    @pytest.mark.parametrize("impersonation", [
        "2",                # the bare line number: collides if the fallback is str(line)
        "unanchored:2",     # a `tag:line` fallback spelling
        "unanchored\x002",  # the NUL-separated spelling
        "anchored\x002",    # the anchored tag itself, if only the fallback is tagged
        "",                 # the empty signature
    ])
    def test_no_signature_text_can_impersonate_the_fallback(self, impersonation):
        # The collision an untagged branch allows, tested over the shapes a
        # fallback might plausibly take rather than one guess -- a single
        # hardcoded candidate passes against every fallback spelling except the
        # one it was written for, which is a test that reads as enforcement while
        # enforcing nothing.
        #
        # It matters because a signature is CONTRIBUTOR CODE: its text is not ours
        # to choose. An anchored finding is a real defect and an unanchorable one
        # is a degraded case, so a signature colliding with the fallback means one
        # silently dedups against the other -- refusing a command that should have
        # been honoured, or honouring one that should have been refused.
        impersonating = dict(SIGNATURES)
        impersonating[("src/app.py", FINDING["line"])] = impersonation
        assert key(signatures=impersonating) != key(signatures={}), (
            f"a signature of {impersonation!r} keys identically to no signature at all"
        )


class TestTheKeyIsOverTheSet:
    """ADR-0013: `fix_key` folds the SORTED per-finding identities, so the command
    a key identifies is the SET of findings the commander named.

    Two consequences that pull in opposite directions and are both wanted:
    `/fix 3,1` and `/fix 1,3` are ONE command and must not open two pull requests,
    while `/fix 1` and `/fix 1,3` are TWO — the second read as a widening, because
    refusing it would mean a commander who narrowed too far could never widen.
    """

    OTHER = FINDING | {"path": "src/util.py", "line": 1}

    def test_the_order_the_commander_typed_is_not_in_the_key(self):
        # The load-bearing one. `/fix 3,1` and `/fix 1,3` name the same findings, so
        # a key that moved with the typing order would let one commander's two
        # spellings open two branches and two pull requests — precisely the
        # duplication ADR-0007 forbids.
        assert key(findings=[FINDING, self.OTHER]) == key(findings=[self.OTHER, FINDING]), (
            "the key depends on the order the ordinals were typed, so /fix 3,1 and "
            "/fix 1,3 dedup as two different commands"
        )

    def test_a_wider_command_is_a_different_key(self):
        # `/fix 1` then `/fix 1,3` is a WIDENING and must be honoured: a different
        # scope is a different fix and a different artefact. A key that collided
        # would refuse the second with AlreadyDelivered, pointing the commander at a
        # pull request delivering half of what they asked for.
        assert key(findings=[FINDING]) != key(findings=[FINDING, self.OTHER])

    def test_no_two_sets_of_findings_fold_to_one_key(self):
        # Every non-empty subset of three findings, so the fold is pinned over sizes
        # as well as members: a fold reading only its first component, or losing the
        # count, collides here.
        third = FINDING | {"path": "src/other.py", "line": 3}
        sets = [[FINDING], [self.OTHER], [third],
                [FINDING, self.OTHER], [FINDING, third], [self.OTHER, third],
                [FINDING, self.OTHER, third]]
        keys = [key(findings=members) for members in sets]
        assert len(set(keys)) == len(sets), "two different commanded sets share one key"

    @pytest.mark.parametrize("separator", ["\x00", "\x01", "\x1f", "|", "\n", "\t"])
    def test_no_signature_can_forge_a_second_component(self, separator):
        # The fold folds several components into one string, and the LAST field of a
        # component is the anchor signature — CONTRIBUTOR CODE, whose alphabet
        # normalize_signature_line does not restrict (it composes NFC and folds
        # indentation; every other byte survives). So a contributor can put any
        # separator in a signature, and if the fold joined on one, a file crafted to
        # carry that separator plus a well-formed second component would make
        # `/fix 1` key identically to `/fix 1,3`.
        #
        # That direction is the dangerous one: a key match REFUSES with
        # AlreadyDelivered, so the collision is a denial of service on every later
        # command for those findings, and the refusal points the commander at a
        # follow-up pull request delivering a different scope. Parametrized over
        # separators rather than one guess, because a single candidate passes against
        # every delimiter except the one it was written for — the shape that reads as
        # enforcement while enforcing nothing.
        signatures = dict(SIGNATURES)
        second = stack.finding_component(self.OTHER, signatures)
        signatures[(FINDING["path"], FINDING["line"])] = (
            f"{SIGNATURES[(FINDING['path'], FINDING['line'])]}{separator}{second}"
        )
        assert key(findings=[FINDING], signatures=signatures) != \
            key(findings=[FINDING, self.OTHER]), (
                f"a signature carrying {separator!r} folds to the key of a WIDER command, "
                "so every later /fix for that pair refuses as already delivered"
            )

    def test_a_repeated_finding_does_not_change_the_key(self):
        # The parse collapses duplicates, and the key must agree: a set of one folds
        # one component however many times the ordinal was typed. Otherwise `/fix 1`
        # and a `/fix 1,1` that slipped through would be two commands.
        assert key(findings=[FINDING]) == key(findings=[FINDING, FINDING])

    def test_a_set_of_one_is_byte_for_byte_the_single_finding_key(self):
        # ADR-0013's widening must not change the one-finding case, or every
        # in-flight follow-up pull request stops deduplicating against its own
        # repeat command. Driven through fix_key both ways rather than compared
        # against a stored constant, so the property is "the shapes agree" and not
        # "the hash is this".
        assert stack.fix_key(7, "reviewed-sha", [FINDING], SIGNATURES) == key()


class TestTheMarkerCarriesTheKey:
    def test_the_marker_is_the_first_line_of_the_body(self):
        body = stack.render_pr_body("the model's body text", key(), METADATA)
        assert body.split("\n")[0] == stack.fix_marker(key())

    def test_the_body_carries_the_not_a_human_review_notice(self):
        # ADR-0005's visibility requirement, which ADR-0009 extends to this body:
        # patch content is unverified by construction and that has to be visible
        # to whoever merges, not only recorded in an ADR.
        body = stack.render_pr_body("b", key(), METADATA)
        assert "no approval" in body

    def test_the_body_carries_the_policy_hash_and_reviewed_sha(self):
        body = stack.render_pr_body("b", key(), METADATA)
        assert METADATA["policy"] in body
        assert METADATA["sha"] in body

    def test_the_models_body_survives_verbatim(self):
        # open_pr.body passed check_plan_markdown, so it is inserted as-is.
        body = stack.render_pr_body("a **bold** claim", key(), METADATA)
        assert "a **bold** claim" in body

    def test_the_key_is_read_from_line_one_only(self):
        # Same containment as suggest.marker_line: model text can legally contain
        # the marker's literal text inside a fence, and open_pr.body is
        # model-authored. A body-wide scan would let crafted content present
        # itself as a fix PR for any key -- which, since a match REFUSES, is a
        # denial of service on every future command for that finding.
        pr = {"body": f"innocent first line\n{stack.fix_marker(key())}",
              "user": {"login": BOT}}
        assert stack.owned_fix_key(pr, BOT) is None

    def test_a_marker_on_line_one_is_read(self):
        pr = {"body": f"{stack.fix_marker(key())}\nthe rest", "user": {"login": BOT}}
        assert stack.owned_fix_key(pr, BOT) == key()

    def test_another_authors_pr_is_never_ours(self):
        # The marker is copyable, so it alone would let anyone's PR be read as a
        # fix of ours. Both halves are load-bearing, exactly as in
        # suggest.owned_fingerprint.
        pr = {"body": f"{stack.fix_marker(key())}\nx", "user": {"login": "someone-else"}}
        assert stack.owned_fix_key(pr, BOT) is None

    def test_an_empty_bot_login_matches_nothing(self):
        # resolve_bot_login fails closed upstream; an unresolved identity must not
        # become ownership of a deleted author's PR.
        pr = {"body": f"{stack.fix_marker(key())}\nx", "user": None}
        assert stack.owned_fix_key(pr, "") is None
        # A null author already fails the author comparison, so it cannot reach the
        # `not bot_login` guard. This is the case that does: GitHub reports the
        # login as the empty string, which an unresolved bot_login would EQUAL.
        emptied = {"body": f"{stack.fix_marker(key())}\nx", "user": {"login": ""}}
        assert stack.owned_fix_key(emptied, "") is None, (
            "an unresolved identity claimed ownership, so every future /fix for "
            "that finding would be refused against a PR nobody can prove is ours"
        )


def patch_plan(*paths):
    steps = [
        {"id": f"p{i}", "kind": "patch",
         "args": {"path": path, "old": "o", "new": "n"}}
        for i, path in enumerate(paths or ("src/app.py",))
    ]
    return steps + [
        {"id": "push", "kind": "push_branch", "args": {"name": "smtithy/fix-7"}},
        {"id": "open", "kind": "open_pr",
         "args": {"branch": "smtithy/fix-7", "title": "Fix it", "body": "the body"}},
    ]


@pytest.fixture
def calls(monkeypatch):
    """Record every github_api write the delivery makes, in order."""
    log = []

    def blob(repo, content):
        log.append(("blob", repo, content))
        return f"blob-{len(log)}"

    def tree(repo, base_tree, blobs):
        log.append(("tree", repo, base_tree, dict(blobs)))
        return "tree-sha"

    def commit(repo, message, *, tree, parent):
        log.append(("commit", repo, message, tree, parent))
        return "commit-sha"

    def ref(repo, branch, sha):
        log.append(("ref", repo, branch, sha))
        return {"ref": f"refs/heads/{branch}"}

    def pull(repo, *, head, base, title, body):
        log.append(("pull", repo, head, base, title, body))
        return {"number": 99, "html_url": "https://github.com/o/r/pull/99"}

    def commit_tree(repo, sha):
        log.append(("read-commit", repo, sha))
        return {"tree": {"sha": "reviewed-tree-sha"}}

    monkeypatch.setattr(stack, "create_blob", blob)
    monkeypatch.setattr(stack, "create_tree", tree)
    monkeypatch.setattr(stack, "create_commit", commit)
    monkeypatch.setattr(stack, "create_ref", ref)
    monkeypatch.setattr(stack, "open_pull_request", pull)
    monkeypatch.setattr(stack, "read_commit", commit_tree)
    monkeypatch.setattr(stack, "find_existing_fix", lambda *a, **k: None)
    return log


def deliver(applied=None, steps=None, **overrides):
    kwargs = dict(
        repo="o/r",
        steps=patch_plan() if steps is None else steps,
        applied={"src/app.py": b"patched\n"} if applied is None else applied,
        base="feature/x",
        reviewed_sha="reviewed-sha",
        key=key(),
        metadata=METADATA,
        bot_login=BOT,
    )
    kwargs.update(overrides)
    return stack.deliver_stacked_pr(**kwargs)


class TestTheDelivery:
    def test_the_sequence_is_blobs_then_tree_then_commit_then_ref_then_pull(self, calls):
        deliver()
        assert [c[0] for c in calls] == [
            "read-commit", "blob", "tree", "commit", "ref", "pull",
        ]

    def test_the_ref_is_created_before_the_pull_request(self, calls):
        # Necessarily so -- a PR cannot open from a branch that does not exist --
        # but pinned because the failure is asymmetric: everything before the ref
        # is unreferenced and collectable, so this ordering is what makes a
        # partial failure leave nothing behind.
        deliver()
        kinds = [c[0] for c in calls]
        assert kinds.index("ref") < kinds.index("pull")

    def test_every_writing_call_happens_after_the_dedup_check(self, monkeypatch, calls):
        # The refusal must land before anything is created, or a duplicate command
        # leaves an orphan branch behind on its way to being refused.
        monkeypatch.setattr(
            stack, "find_existing_fix",
            lambda *a, **k: {"number": 12, "html_url": "u"},
        )
        with pytest.raises(stack.AlreadyDelivered):
            deliver()
        assert calls == []

    def test_the_refusal_names_the_existing_pull_request(self, monkeypatch, calls):
        monkeypatch.setattr(
            stack, "find_existing_fix",
            lambda *a, **k: {"number": 12, "html_url": "https://github.com/o/r/pull/12"},
        )
        with pytest.raises(stack.AlreadyDelivered, match="12"):
            deliver()

    def test_one_blob_per_patched_path(self, calls):
        deliver(applied={"src/app.py": b"a\n", "src/util.py": b"b\n"},
                steps=patch_plan("src/app.py", "src/util.py"))
        blobs = [c for c in calls if c[0] == "blob"]
        assert sorted(c[2] for c in blobs) == [b"a\n", b"b\n"]

    def test_the_tree_is_based_on_the_reviewed_commits_tree(self, calls):
        # Not on the reviewed SHA itself: /git/trees wants a TREE sha, and a
        # commit sha names a different object. Read from the reviewed commit,
        # which is also what proves the base tree is the anchor tree.
        deliver()
        tree = next(c for c in calls if c[0] == "tree")
        assert tree[2] == "reviewed-tree-sha"

    def test_the_commit_parent_is_the_reviewed_head(self, calls):
        # ADR-0005: every `old` byte-matches the file at the reviewed SHA, so the
        # patch applies cleanly on that tree and nowhere else.
        deliver()
        commit = next(c for c in calls if c[0] == "commit")
        assert commit[4] == "reviewed-sha"

    def test_the_branch_is_the_plans_push_branch_name(self, calls):
        # Model-supplied but confined by both gates to policy.plan.branch_prefix
        # and refused from being the reviewed head branch, so by here it is a
        # verified value rather than a trusted one.
        deliver()
        ref = next(c for c in calls if c[0] == "ref")
        assert ref[2] == "smtithy/fix-7"

    def test_the_pull_request_opens_from_that_same_branch(self, calls):
        deliver()
        pull = next(c for c in calls if c[0] == "pull")
        assert pull[2] == "smtithy/fix-7"

    def test_the_base_comes_from_the_context_not_the_plan(self, calls):
        # ADR-0009 addendum, the load-bearing one: the base is the reviewed PR's
        # own head BRANCH, so the fix merges INTO the open pull request and broken
        # code never lands on the default branch. The plan cannot express it --
        # open_pr has no base argument -- and this asserts the executor's value is
        # what reaches the API.
        deliver(base="feature/x")
        pull = next(c for c in calls if c[0] == "pull")
        assert pull[3] == "feature/x"

    def test_the_base_is_never_defaulted_to_a_branch_name(self, calls):
        # A different base must produce a different target: a constant would pass
        # the test above while ignoring the argument entirely.
        deliver(base="release/2")
        pull = next(c for c in calls if c[0] == "pull")
        assert pull[3] == "release/2"

    def test_the_title_comes_from_the_plan(self, calls):
        deliver()
        pull = next(c for c in calls if c[0] == "pull")
        assert pull[4] == "Fix it"

    def test_the_body_carries_the_marker_and_the_models_body(self, calls):
        deliver()
        pull = next(c for c in calls if c[0] == "pull")
        assert pull[5].split("\n")[0] == stack.fix_marker(key())
        assert "the body" in pull[5]

    def test_the_delivered_pull_request_is_returned(self, calls):
        assert deliver()["number"] == 99

    def test_a_plan_with_no_push_branch_step_refuses(self, calls):
        # decide_delivery already requires exactly one of each, but this module
        # fails closed rather than assuming a gate ran -- the posture the whole
        # executor takes.
        steps = [s for s in patch_plan() if s["kind"] != "push_branch"]
        with pytest.raises(stack.Refusal, match="push_branch"):
            deliver(steps=steps)
        assert calls == []

    def test_a_plan_with_no_open_pr_step_refuses(self, calls):
        steps = [s for s in patch_plan() if s["kind"] != "open_pr"]
        with pytest.raises(stack.Refusal, match="open_pr"):
            deliver(steps=steps)
        assert calls == []

    def test_no_applied_bytes_refuses_before_any_write(self, calls):
        # An empty map means nothing was patched, so there is nothing to commit
        # and a PR would be an empty diff a commander cannot act on.
        with pytest.raises(stack.Refusal, match="no patched content"):
            deliver(applied={})
        assert calls == []

    def test_the_branch_and_the_open_pr_branch_must_agree(self, calls):
        # check_write_class_targets proves this, and it is re-checked here for the
        # same fail-closed reason: pushing to one branch and opening from another
        # delivers content this plan never described.
        steps = patch_plan()
        steps[-1]["args"]["branch"] = "smtithy/somewhere-else"
        with pytest.raises(stack.Refusal, match="branch"):
            deliver(steps=steps)
        assert calls == []

    def test_an_existing_branch_is_a_stranded_delivery_naming_it(self, calls, monkeypatch):
        # The docstring promises "a re-run refuses at create_ref with a message
        # naming that branch". GitHub answers 422 there, and a bare HTTPError is
        # neither StrandedDelivery nor AlreadyDelivered — execute_plan caught
        # neither, so the run ended in a traceback naming no branch and dropping
        # GitHub's own "Reference already exists" body. Reachable through the
        # deliberately-open window between create_ref and open_pull_request, where a
        # ref exists with no pull request carrying the marker for find_existing_fix
        # to see. StrandedDelivery, so the commander gets a reply naming the
        # standing branch (ADR-0018) — a PRIOR run's; this run pushed nothing.
        def exists(repo, branch, sha):
            raise urllib.error.HTTPError(
                f"https://api/repos/{repo}/git/refs", 422, "Unprocessable Content",
                {}, None)

        monkeypatch.setattr(stack, "create_ref", exists)
        with pytest.raises(stack.StrandedDelivery, match="smtithy/fix-7") as caught:
            deliver()
        assert "commit-sha" in str(caught.value), "the orphaned commit is not named"
        assert not any(c[0] == "pull" for c in calls)

    def test_a_non_422_from_create_ref_is_not_swallowed(self, calls, monkeypatch):
        # Only the branch-exists case is a refusal. A 500 means the push may or may
        # not have happened, which is not something to report as "already pushed".
        def broken(repo, branch, sha):
            raise urllib.error.HTTPError(
                f"https://api/repos/{repo}/git/refs", 500, "Server Error", {}, None)

        monkeypatch.setattr(stack, "create_ref", broken)
        with pytest.raises(urllib.error.HTTPError):
            deliver()
        assert not any(c[0] == "pull" for c in calls)

    def test_a_forbidden_pull_request_is_a_refusal_naming_the_pushed_branch(
            self, calls, monkeypatch):
        """MEASURED IN PRODUCTION on artel PR #61: this escaped as 24 lines of urllib
        stack, in the first stacked delivery ever to run for real.

        403 here is a repository SETTING and not a scope — "Allow GitHub Actions to
        create and approve pull requests" gates POST /pulls independently of the
        `pull-requests: write` the job already holds, so the token has the scope and
        the call is refused anyway. Nothing earlier can see it: the setting is not
        readable with this job's permissions.

        The branch and its commit already exist by this point, so the failure has to
        name them — they are the state a commander must clean up before retrying, and
        the traceback named neither. Same reasoning as the 422 above, one call along.
        """
        def forbidden(repo, **kwargs):
            raise urllib.error.HTTPError(
                f"https://api/repos/{repo}/pulls", 403, "Forbidden", {}, None)

        monkeypatch.setattr(stack, "open_pull_request", forbidden)
        with pytest.raises(stack.StrandedDelivery, match="smtithy/fix-7") as caught:
            deliver()
        message = str(caught.value)
        assert "commit-sha" in message, (
            "the pushed commit is not named, so the commander cannot tell what was left behind"
        )
        assert "Actions" in message, "the message does not name the setting that refused"

    def test_a_stranded_delivery_is_not_a_refusal(self):
        # ADR-0018's taxonomy, pinned where the classes live. Refusal promises
        # "raised before any write, so a refused plan leaves nothing behind", and
        # the two post-push raises were the only ones breaking that promise. And
        # the subclass relation is the silent-regression path: were StrandedDelivery
        # a Refusal, a lost except arm in execute_plan would swallow it into the
        # reply-less fail() — recreating finding 0002's orphan-plus-silence
        # invisibly, instead of as a loud traceback.
        assert not issubclass(stack.StrandedDelivery, stack.Refusal)
        assert not issubclass(stack.AlreadyDelivered, stack.Refusal)

    def test_a_non_403_from_open_pull_request_is_not_swallowed(self, calls, monkeypatch):
        # Same boundary as create_ref's: only the permission case is a refusal. A 422
        # here means something else entirely (no commits between base and head, say),
        # and reporting it as a settings problem would send the commander to the wrong
        # place.
        def broken(repo, **kwargs):
            raise urllib.error.HTTPError(
                f"https://api/repos/{repo}/pulls", 422, "Unprocessable Content", {}, None)

        monkeypatch.setattr(stack, "open_pull_request", broken)
        with pytest.raises(urllib.error.HTTPError):
            deliver()


class TestFindingAnExistingFix:
    def existing(self, monkeypatch, prs):
        monkeypatch.setattr(stack, "pull_requests_for_base", lambda repo, base: iter(prs))

    def test_a_matching_pr_is_found(self, monkeypatch):
        self.existing(monkeypatch, [
            {"number": 12, "html_url": "u", "body": f"{stack.fix_marker(key())}\nx",
             "user": {"login": BOT}},
        ])
        found = stack.find_existing_fix("o/r", "feature/x", key(), bot_login=BOT)
        assert found and found["number"] == 12

    def test_a_pr_for_another_key_is_not_a_match(self, monkeypatch):
        other = key(finding=FINDING | {"path": "src/util.py", "line": 1})
        self.existing(monkeypatch, [
            {"number": 12, "body": f"{stack.fix_marker(other)}\nx", "user": {"login": BOT}},
        ])
        assert stack.find_existing_fix("o/r", "feature/x", key(), bot_login=BOT) is None

    def test_no_prs_at_all_is_no_match(self, monkeypatch):
        self.existing(monkeypatch, [])
        assert stack.find_existing_fix("o/r", "feature/x", key(), bot_login=BOT) is None

    def test_a_human_pr_carrying_a_copied_marker_is_not_a_match(self, monkeypatch):
        # A contributor can paste our marker into their own PR body. Reading that
        # as ours would refuse every future command for that finding.
        self.existing(monkeypatch, [
            {"number": 12, "body": f"{stack.fix_marker(key())}\nx", "user": {"login": "them"}},
        ])
        assert stack.find_existing_fix("o/r", "feature/x", key(), bot_login=BOT) is None
