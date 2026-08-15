"""Tests for the reply channel (ADR-0014, widened by ADR-0018).

A reply is neither an artifact kind nor a plan step kind: it is the COMMAND
CHANNEL reporting a command's terminal state to the person who issued it — a
decline or a delivered receipt. So the properties here are about WHICH runs
reply, WHERE the reply is composed, and what the comment says about itself.

The security property carries the most weight and is the one to keep expecting:
**the untrusted-commander refusal must never be replied to.** Trust is resolved as
prepare_fix_context's SECOND step, so everything before it runs for an untrusted
commenter, and a reply there would let any passer-by make the harness post a comment
naming them.

Network is never touched: the upsert is stubbed.
"""

from pathlib import Path

import pytest

import post
import reply
from conftest import POLICY


METADATA_RUN = "https://github.com/o/r/actions/runs/99"


@pytest.fixture
def posted(monkeypatch):
    """Capture what reaches upsert_comment instead of GitHub."""
    calls = []
    monkeypatch.setattr(reply, "resolve_bot_login", lambda: "smtithy[bot]")
    monkeypatch.setattr(
        reply, "upsert_comment",
        lambda repo, pr, body, marker, *, bot_login: calls.append(
            {"repo": repo, "pr": pr, "body": body, "marker": marker, "bot_login": bot_login}),
    )
    return calls


def run_main(monkeypatch, **env):
    values = {
        "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "7",
        "REASON": "There is no delivery for this command on this pull request.",
        "KIND": "declined",
        "HEAD_SHA": "reviewed-sha", "ORDINALS": "1,3", "RUN_URL": METADATA_RUN,
    } | env
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return reply.main()


class TestTheMarker:
    def test_no_reply_marker_is_the_reviewers(self):
        # THE containment property. Sharing post.MARKER would make the two lanes
        # fight over ONE comment: the reviewer's next push overwriting the reply,
        # or the reply overwriting the review — supersede_previous_reviews'
        # unscoped-authority defect waiting to happen somewhere new. Every reply
        # marker starts with the prefix, so it is asserted on the prefix rather
        # than on one sample marker.
        assert not post.MARKER.startswith(reply.MARKER_PREFIX)
        assert reply.marker("sha", "1,3") != post.MARKER

    def test_the_marker_can_identify_one_comment(self):
        # post.check_marker's rules, which upsert_comment enforces on every call: an
        # empty marker matches EVERY comment, and a multi-line one can never be the
        # comment's first line, so the run would post another comment every time.
        post.check_marker(reply.marker("reviewed-sha", "1,3"))

    def test_the_marker_is_per_command(self):
        # ADR-0018: one marker per pull request let a second command's receipt
        # upsert away the first's cross-link — recreating, for the first command,
        # the invisible-delivery gap the receipt exists to close. Keyed by head SHA
        # and ordinals: a retry of ONE command upserts one comment, a distinct
        # command gets its own.
        assert reply.marker("sha", "1,3") == reply.marker("sha", "1,3")
        assert reply.marker("sha", "1,3") != reply.marker("sha", "2,4")
        assert reply.marker("sha", "1,3") != reply.marker("other", "1,3")

    def test_one_commands_marker_is_not_a_prefix_of_anothers(self):
        # Ownership matches the whole stripped first line, and the trailing ` -->`
        # seals the key — `/fix 1,2`'s marker must not match `/fix 1,22`'s comment.
        assert not reply.marker("sha", "1,2").startswith(reply.marker("sha", "1,22"))
        assert reply.marker("sha", "1,2") not in reply.marker("sha", "1,22")

    def test_the_marker_is_the_bodys_first_line(self, posted, monkeypatch):
        # By POSITION, like every other marker here, so nothing has to recognise a
        # pattern text could imitate.
        run_main(monkeypatch)
        assert posted[0]["body"].split("\n")[0] == reply.marker("reviewed-sha", "1,3")

    def test_the_posted_marker_is_the_one_the_body_carries(self, posted, monkeypatch):
        # They must be one value, or the upsert searches for a comment it never
        # posts and creates a new one every run.
        run_main(monkeypatch)
        assert posted[0]["marker"] == posted[0]["body"].split("\n")[0]


class TestTheBodyIsSelfDating:
    """ADR-0009's addendum B: an upsert destroys the previous text under the SAME
    marker, and the marker is per-command — so each comment is one command's
    CURRENT terminal state. That is only honest if the body says what it spoke
    for — an artefact that never claims a currency never needs a later run to
    correct it.
    """

    def test_the_body_carries_the_head_sha(self, posted, monkeypatch):
        run_main(monkeypatch, HEAD_SHA="abc123")
        assert "abc123" in posted[0]["body"]

    def test_the_body_carries_the_ordinals_it_spoke_for(self, posted, monkeypatch):
        # Without these, a decline of `/fix 1,3` would be indistinguishable from a
        # decline of `/fix 2,4` to a reader of either comment.
        run_main(monkeypatch, ORDINALS="2,4")
        assert "2,4" in posted[0]["body"]

    def test_the_body_names_the_command_as_the_commander_typed_it(self, posted, monkeypatch):
        run_main(monkeypatch, ORDINALS="1,3")
        assert "`/fix 1,3`" in posted[0]["body"]

    def test_the_body_carries_the_reason(self, posted, monkeypatch):
        run_main(monkeypatch, REASON="a very specific reason sentence")
        assert "a very specific reason sentence" in posted[0]["body"]

    def test_the_declined_body_says_it_is_not_a_broken_run(self, posted, monkeypatch):
        # The reply exists because a red run on issue_comment appears in the Actions
        # tab and NOT on the pull request's timeline. Having gone to the trouble of
        # putting a comment where the commander is looking, it has to distinguish
        # "the harness declined" from "the harness crashed".
        run_main(monkeypatch, KIND="declined")
        assert "declining to act" in posted[0]["body"]
        assert "Nothing was written" in posted[0]["body"]

    def test_the_body_links_the_run(self, posted, monkeypatch):
        run_main(monkeypatch)
        assert METADATA_RUN in posted[0]["body"]


class TestTheTwoKinds:
    """ADR-0018: a decline and a receipt are the two message kinds of one channel.
    The kind selects the rendered claim, so the same posting job cannot say
    "was not performed" over a delivered fix or the reverse.
    """

    def test_the_declined_body_says_not_performed(self, posted, monkeypatch):
        run_main(monkeypatch, KIND="declined")
        assert "was not performed" in posted[0]["body"]
        assert "was delivered" not in posted[0]["body"]
        assert "declined" in posted[0]["body"]

    def test_the_delivered_body_says_delivered(self, posted, monkeypatch):
        run_main(monkeypatch, KIND="delivered",
                 REASON="Opened follow-up pull request #18 (https://github.com/o/r/pull/18).")
        assert "was delivered" in posted[0]["body"]
        assert "was not performed" not in posted[0]["body"]
        assert "#18" in posted[0]["body"]

    def test_the_receipt_does_not_excuse_a_red_run(self, posted, monkeypatch):
        # The receipt's run is green; "not a run that broke" explains a red X the
        # commander can see, and on a receipt it would explain one that is not
        # there.
        run_main(monkeypatch, KIND="delivered", REASON="Opened follow-up pull request #18.")
        assert "declining to act" not in posted[0]["body"]
        assert "Nothing was written" not in posted[0]["body"]

    def test_a_kind_nobody_defined_posts_nothing(self, posted, monkeypatch, capsys):
        # A kind outside the pair is a producer bug: posting a comment whose
        # headline nobody wrote is worse than the red run that gets the bug fixed.
        with pytest.raises(SystemExit):
            run_main(monkeypatch, KIND="performed")
        assert posted == []
        assert "KIND" in capsys.readouterr().err


class TestEveryValueIsRequired:
    """A reply naming no reason, or no head, is a comment that tells the commander
    nothing while looking like an answer. Empty is refused, not just absent — the
    values arrive as workflow outputs, and an output nobody wrote reads as "".
    """

    @pytest.mark.parametrize("name", ["REASON", "KIND", "HEAD_SHA", "ORDINALS", "RUN_URL"])
    def test_an_empty_value_posts_nothing(self, posted, monkeypatch, name, capsys):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, **{name: ""})
        assert posted == [], f"a reply was posted with an empty {name}"
        assert name in capsys.readouterr().err

    @pytest.mark.parametrize("name", ["REASON", "KIND", "HEAD_SHA", "ORDINALS", "RUN_URL"])
    def test_an_absent_value_posts_nothing(self, posted, monkeypatch, name):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, **{name: None})
        assert posted == []


class TestTheEmittedOutputs:
    """One reason format, one emitter, several producers: `command` for the
    undeliverable case, `stack` for AlreadyDelivered, the stranded deliveries and
    the receipt. They share this emitter rather than each formatting their own,
    because two implementations that must agree on their text is the defect
    ADR-0009's addendum B was written about.
    """

    def emit(self, tmp_path, monkeypatch, reason="the reason", kind="declined",
             head_sha="sha", ordinals="1,3"):
        output = tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        reply.emit(reason, kind=kind, head_sha=head_sha, ordinals=ordinals)
        return output.read_text()

    def test_every_output_the_posting_job_reads_is_emitted(self, tmp_path, monkeypatch):
        written = self.emit(tmp_path, monkeypatch)
        for name in reply.OUTPUTS:
            assert name in written, f"{name} is not emitted; the posting job reads it as empty"

    def test_the_replied_flag_is_written_LAST(self, tmp_path, monkeypatch):
        # The flag the job's `if:` reads. Written after every value the job needs, so
        # a reply job firing on a partial write cannot post a comment with holes in
        # it. Asserted as a POSITION rather than as a suffix: a write that emitted
        # the flag first and then repeated it at the end would satisfy `endswith`
        # while a run cancelled mid-write would still leave the flag set with no
        # values behind it.
        written = self.emit(tmp_path, monkeypatch)
        lines = written.splitlines()
        assert lines.count("replied=true") == 1, (
            "the flag is written more than once, so an interrupted write can leave it set "
            "with no values behind it"
        )
        assert lines.index("replied=true") == len(lines) - 1
        for name in reply.OUTPUTS:
            assert written.index(name) < written.index("replied=true"), (
                f"{name} is written after the flag, so the posting job can fire before it exists"
            )

    def test_both_kinds_emit(self, tmp_path, monkeypatch):
        # The receipt travels the same outputs the declines do (ADR-0018): one
        # posting path, with the kind as data rather than a second mechanism.
        for kind in reply.KINDS:
            written = self.emit(tmp_path, monkeypatch, kind=kind)
            assert f"\n{kind}\n" in written
            assert "replied=true" in written

    def test_a_kind_nobody_defined_emits_nothing(self, tmp_path, monkeypatch, capsys):
        written = self.emit(tmp_path, monkeypatch, kind="performed")
        assert "replied=true" not in written
        assert "kind" in capsys.readouterr().err

    def test_a_multi_line_reason_is_not_truncated(self, tmp_path, monkeypatch):
        # GITHUB_OUTPUT's `name=value` form ends at the first newline, so a
        # multi-line reason would truncate — and the REMAINDER would be parsed as
        # further outputs, which is how an output write becomes an output injection.
        # Nothing MODEL-controlled reaches here — but the reason interpolates a
        # contributor-authored path, so this guard runs over contributor content and
        # is not defence in depth. See
        # test_no_policy_legal_path_can_suppress_the_reply.
        written = self.emit(tmp_path, monkeypatch, reason="first line\nsecond line")
        assert "second line" in written
        assert "\nsecond line=" not in written, "the reason's second line reads as another output"

    def test_a_reason_carrying_the_delimiter_emits_nothing(self, tmp_path, monkeypatch, capsys):
        # Refused rather than escaped: a value carrying the heredoc delimiter could
        # close the block early and have its remainder read as more outputs.
        written = self.emit(tmp_path, monkeypatch, reason=f"x {reply._DELIMITER} y")
        assert "replied=true" not in written, (
            "a value containing the output delimiter was emitted, so its remainder is read "
            "as further outputs"
        )
        assert "delimiter" in capsys.readouterr().err

    # Every untrusted alphabet a reply reason interpolates. The undeliverable
    # reason carries the commanded PATHS (contributor-authored,
    # path_must_be_changed_file); the stranded reason carries the plan's BRANCH
    # NAME (generator-authored, ADR-0018, both the pushed name and the branch
    # open_pr opens from). A grammar joining this list must join this test, or a
    # value in it that can spell the delimiter suppresses the reply.
    INTERPOLATED_GRAMMARS = [
        ("finding path",
         lambda policy: policy["artifact_schema"]["findings"]["item_fields"]["path"]["pattern"],
         "src/a{c}b.py"),
        ("push_branch name",
         lambda policy: policy["plan"]["step_kinds"]["push_branch"]["args"]["name"]["pattern"],
         "smtithy/a{c}b"),
        ("open_pr branch",
         lambda policy: policy["plan"]["step_kinds"]["open_pr"]["args"]["branch"]["pattern"],
         "smtithy/a{c}b"),
    ]

    @pytest.mark.parametrize("label,pattern_of,template",
                             INTERPOLATED_GRAMMARS,
                             ids=[g[0] for g in INTERPOLATED_GRAMMARS])
    def test_no_legal_value_of_an_interpolated_grammar_can_suppress_the_reply(
            self, label, pattern_of, template):
        """The guard above runs over UNTRUSTED content, so the delimiter must be a
        string none of the interpolated grammars can write.

        Measured for the path half: the delimiter was `SMTITHY_DECLINE_EOF`, the
        policy path pattern admits it as a substring, so `src/SMTITHY_DECLINE_EOF.py`
        refused the emit and left the commander with no comment — the "declined and
        told nobody" state ADR-0014 exists to prevent, reached through the mechanism
        built to prevent it, and self-serve since the contributor controls both the
        fork-ness and the filename. The branch grammars joined the alphabet when
        ADR-0018 put the plan-authored branch name into the stranded reason: a
        hostile diff steering the plan session's branch choice must not be able to
        spell the delimiter either.

        Asserted against the PATTERN rather than against a list of guesses, and in
        the direction that matters: it is not that known spellings are refused, it
        is that NO legal value can contain the delimiter at all.
        """
        import re

        pattern = pattern_of(POLICY)
        for character in reply._DELIMITER:
            if re.fullmatch(pattern, template.format(c=character)):
                continue
            break
        else:
            pytest.fail(
                f"every character of {reply._DELIMITER!r} is legal in a {label}, so an "
                "untrusted author can spell the delimiter and suppress the reply — the "
                "state ADR-0014 exists to prevent, through the mechanism built to prevent it"
            )

    @pytest.mark.parametrize("hostile", [
        "SMTITHY_REPLY_EOF",
        "src/SMTITHY_REPLY_EOF.py",
        "tests/fixtures/SMTITHY_REPLY_EOF_data.json",
        # The original measured suppressors, kept from before the rename: legal
        # paths carrying the delimiter's WORD must never suppress again whatever
        # the word currently is.
        "src/SMTITHY_DECLINE_EOF.py",
    ])
    def test_the_reproduced_suppressing_paths_no_longer_suppress(
            self, tmp_path, monkeypatch, hostile):
        # Spellings of the delimiter's word that ARE policy-legal. They are here as
        # the reproduction, with the pattern assertion above as the general
        # property: a delimiter change that only defeated these spellings would
        # pass here and fail there.
        import re

        pattern = POLICY["artifact_schema"]["findings"]["item_fields"]["path"]["pattern"]
        assert re.fullmatch(pattern, hostile), (
            f"{hostile!r} is no longer a legal path, so this case reproduces nothing — the "
            "suppression would now be blocked by the schema rather than by the delimiter"
        )
        reason = f"The command names findings on 2 files (`{hostile}`, `src/app.py`), so the fix..."
        written = self.emit(tmp_path, monkeypatch, reason=reason)
        assert "replied=true" in written, (
            f"a command naming {hostile!r} emitted no reply, so a contributor suppressed their "
            "own decline by choosing a filename"
        )
        assert hostile in written

    @pytest.mark.parametrize("field", ["reason", "head_sha", "ordinals"])
    def test_an_empty_value_emits_nothing(self, tmp_path, monkeypatch, field):
        # Fail closed at the producer too, not only at the poster: a `replied=true`
        # with a missing value would start the posting job for nothing.
        written = self.emit(tmp_path, monkeypatch, **{field: ""})
        assert "replied=true" not in written

    def test_nothing_is_emitted_without_a_github_output(self, monkeypatch):
        # Running the executor by hand is legitimate and must not raise.
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        reply.emit("r", kind="declined", head_sha="s", ordinals="1")


class TestOrdinalsAreOneBasedForTheHuman:
    """fix_command owns the 1-based-to-0-based conversion; this is the one place it
    runs BACK, because the comment is addressed to the human who typed the numbers.
    """

    def test_the_indices_are_rendered_as_the_ordinals_typed(self):
        assert reply.ordinals_of([0]) == "1"
        assert reply.ordinals_of([0, 2]) == "1,3"

    def test_the_ordinals_are_sorted_however_they_arrive(self):
        # Canonical, like every other identity in this lane: two runs of ONE command
        # must not produce two different comments — the ordinals are in the MARKER
        # now, so an unsorted spelling would give `/fix 3,1` and `/fix 1,3` two
        # comments for one command.
        assert reply.ordinals_of([2, 0]) == reply.ordinals_of([0, 2])

    def test_a_set_is_accepted_since_that_is_what_the_command_is(self):
        assert reply.ordinals_of(frozenset({2, 0})) == "1,3"


class TestTheReasonIsHarnessAuthored:
    def test_the_body_is_composed_here_and_not_taken_from_a_model(self):
        # A reply names a fact about the CHANNEL: a fork having no branch to
        # base a pull request on is not a fact the generator knows or should
        # narrate. That is one of the three reasons ADR-0014 refuses a `decline`
        # plan step, and it holds only if nothing here reads an artifact.
        source = Path(reply.__file__).read_text(encoding="utf-8")
        for artifact_input in ("review.json", "plan.json", "commanded_index.json"):
            assert artifact_input not in source, (
                f"reply.py reads {artifact_input}; the reason text must be the harness's, "
                "with no field a generator writes"
            )
