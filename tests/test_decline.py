"""Tests for the decline channel (ADR-0014).

A decline is neither an artifact kind nor a plan step kind: it is a refusal the
COMMAND CHANNEL reports, before any model runs, to the person who issued the
command. So the properties here are about WHICH refusals reply, WHERE the reply is
composed, and what the comment says about itself.

The security property carries the most weight and is the one to keep expecting:
**the untrusted-commander refusal must never be replied to.** Trust is resolved as
prepare_fix_context's SECOND step, so everything before it runs for an untrusted
commenter, and a reply there would let any passer-by make the harness post a comment
naming them.

Network is never touched: the upsert is stubbed.
"""

from pathlib import Path

import pytest

import decline
import post


METADATA_RUN = "https://github.com/o/r/actions/runs/99"


@pytest.fixture
def posted(monkeypatch):
    """Capture what reaches upsert_comment instead of GitHub."""
    calls = []
    monkeypatch.setattr(decline, "resolve_bot_login", lambda: "smtithy[bot]")
    monkeypatch.setattr(
        decline, "upsert_comment",
        lambda repo, pr, body, marker, *, bot_login: calls.append(
            {"repo": repo, "pr": pr, "body": body, "marker": marker, "bot_login": bot_login}),
    )
    return calls


def run_main(monkeypatch, **env):
    values = {
        "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "7",
        "REASON": "There is no delivery for this command on this pull request.",
        "HEAD_SHA": "reviewed-sha", "ORDINALS": "1,3", "RUN_URL": METADATA_RUN,
    } | env
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return decline.main()


class TestTheMarker:
    def test_the_marker_is_not_the_reviewers(self):
        # THE containment property. Sharing post.MARKER would make the two lanes
        # fight over ONE comment: the reviewer's next push overwriting the decline,
        # or the decline overwriting the review — supersede_previous_reviews'
        # unscoped-authority defect waiting to happen somewhere new.
        assert decline.MARKER != post.MARKER

    def test_the_marker_can_identify_one_comment(self):
        # post.check_marker's rules, which upsert_comment enforces on every call: an
        # empty marker matches EVERY comment, and a multi-line one can never be the
        # comment's first line, so the run would post another comment every time.
        post.check_marker(decline.MARKER)

    def test_the_marker_is_the_bodys_first_line(self, posted, monkeypatch):
        # By POSITION, like every other marker here, so nothing has to recognise a
        # pattern text could imitate.
        run_main(monkeypatch)
        assert posted[0]["body"].split("\n")[0] == decline.MARKER

    def test_the_posted_marker_is_the_one_the_body_carries(self, posted, monkeypatch):
        # They must be one value, or the upsert searches for a comment it never
        # posts and creates a new one every run.
        run_main(monkeypatch)
        assert posted[0]["marker"] == posted[0]["body"].split("\n")[0]


class TestTheBodyIsSelfDating:
    """ADR-0009's addendum B: an upsert destroys the previous decline's text, so a
    commander declined twice sees only the latest. That is only honest if the body
    says what it spoke for — an artefact that never claims a currency never needs a
    later run to correct it.
    """

    def test_the_body_carries_the_head_sha(self, posted, monkeypatch):
        run_main(monkeypatch, HEAD_SHA="abc123")
        assert "abc123" in posted[0]["body"]

    def test_the_body_carries_the_ordinals_it_spoke_for(self, posted, monkeypatch):
        # Without these, a decline of `/fix 1,3` would be indistinguishable from a
        # decline of `/fix 2,4` after the upsert replaced one with the other.
        run_main(monkeypatch, ORDINALS="2,4")
        assert "2,4" in posted[0]["body"]

    def test_the_body_names_the_command_as_the_commander_typed_it(self, posted, monkeypatch):
        run_main(monkeypatch, ORDINALS="1,3")
        assert "`/fix 1,3`" in posted[0]["body"]

    def test_the_body_carries_the_reason(self, posted, monkeypatch):
        run_main(monkeypatch, REASON="a very specific reason sentence")
        assert "a very specific reason sentence" in posted[0]["body"]

    def test_the_body_says_it_is_not_a_broken_run(self, posted, monkeypatch):
        # The reply exists because a red run on issue_comment appears in the Actions
        # tab and NOT on the pull request's timeline. Having gone to the trouble of
        # putting a comment where the commander is looking, it has to distinguish
        # "the harness declined" from "the harness crashed".
        run_main(monkeypatch)
        assert "declining to act" in posted[0]["body"]
        assert "Nothing was written" in posted[0]["body"]

    def test_the_body_links_the_run(self, posted, monkeypatch):
        run_main(monkeypatch)
        assert METADATA_RUN in posted[0]["body"]


class TestEveryValueIsRequired:
    """A decline naming no reason, or no head, is a comment that tells the commander
    nothing while looking like an answer. Empty is refused, not just absent — the
    values arrive as workflow outputs, and an output nobody wrote reads as "".
    """

    @pytest.mark.parametrize("name", ["REASON", "HEAD_SHA", "ORDINALS", "RUN_URL"])
    def test_an_empty_value_posts_nothing(self, posted, monkeypatch, name, capsys):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, **{name: ""})
        assert posted == [], f"a decline was posted with an empty {name}"
        assert name in capsys.readouterr().err

    @pytest.mark.parametrize("name", ["REASON", "HEAD_SHA", "ORDINALS", "RUN_URL"])
    def test_an_absent_value_posts_nothing(self, posted, monkeypatch, name):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, **{name: None})
        assert posted == []


class TestTheEmittedOutputs:
    """One reason format, TWO producers (ADR-0014): `command` for the
    undeliverable case, `stack` for AlreadyDelivered. They share this emitter rather
    than each formatting their own, because two implementations that must agree on
    their text is the defect ADR-0009's addendum B was written about.
    """

    def emit(self, tmp_path, monkeypatch, reason="the reason", head_sha="sha", ordinals="1,3"):
        output = tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        decline.emit(reason, head_sha=head_sha, ordinals=ordinals)
        return output.read_text()

    def test_every_output_the_posting_job_reads_is_emitted(self, tmp_path, monkeypatch):
        written = self.emit(tmp_path, monkeypatch)
        for name in decline.OUTPUTS:
            assert name in written, f"{name} is not emitted; the posting job reads it as empty"

    def test_the_declined_flag_is_written_LAST(self, tmp_path, monkeypatch):
        # The flag the job's `if:` reads. Written after every value the job needs, so
        # a decline job firing on a partial write cannot post a comment with holes in
        # it. Asserted as a POSITION rather than as a suffix: a write that emitted
        # the flag first and then repeated it at the end would satisfy `endswith`
        # while a run cancelled mid-write would still leave the flag set with no
        # values behind it.
        written = self.emit(tmp_path, monkeypatch)
        lines = written.splitlines()
        assert lines.count("declined=true") == 1, (
            "the flag is written more than once, so an interrupted write can leave it set "
            "with no values behind it"
        )
        assert lines.index("declined=true") == len(lines) - 1
        for name in decline.OUTPUTS:
            assert written.index(name) < written.index("declined=true"), (
                f"{name} is written after the flag, so the posting job can fire before it exists"
            )

    def test_a_multi_line_reason_is_not_truncated(self, tmp_path, monkeypatch):
        # GITHUB_OUTPUT's `name=value` form ends at the first newline, so a
        # multi-line reason would truncate — and the REMAINDER would be parsed as
        # further outputs, which is how an output write becomes an output injection.
        # Nothing model-controlled reaches here, so this is defence in depth: the
        # reason text is the part most likely to be reworded, and a rewording that
        # introduced a newline would be a silent truncation nobody would connect to
        # this.
        written = self.emit(tmp_path, monkeypatch, reason="first line\nsecond line")
        assert "second line" in written
        assert "\nsecond line=" not in written, "the reason's second line reads as another output"

    def test_a_reason_carrying_the_delimiter_emits_nothing(self, tmp_path, monkeypatch, capsys):
        # Refused rather than escaped: a value carrying the heredoc delimiter could
        # close the block early and have its remainder read as more outputs.
        written = self.emit(tmp_path, monkeypatch, reason=f"x {decline._DELIMITER} y")
        assert "declined=true" not in written, (
            "a value containing the output delimiter was emitted, so its remainder is read "
            "as further outputs"
        )
        assert "delimiter" in capsys.readouterr().err

    @pytest.mark.parametrize("field", ["reason", "head_sha", "ordinals"])
    def test_an_empty_value_emits_nothing(self, tmp_path, monkeypatch, field):
        # Fail closed at the producer too, not only at the poster: a `declined=true`
        # with a missing value would start the posting job for nothing.
        written = self.emit(tmp_path, monkeypatch, **{field: ""})
        assert "declined=true" not in written

    def test_nothing_is_emitted_without_a_github_output(self, monkeypatch):
        # Running the executor by hand is legitimate and must not raise.
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        decline.emit("r", head_sha="s", ordinals="1")


class TestOrdinalsAreOneBasedForTheHuman:
    """fix_command owns the 1-based-to-0-based conversion; this is the one place it
    runs BACK, because the comment is addressed to the human who typed the numbers.
    """

    def test_the_indices_are_rendered_as_the_ordinals_typed(self):
        assert decline.ordinals_of([0]) == "1"
        assert decline.ordinals_of([0, 2]) == "1,3"

    def test_the_ordinals_are_sorted_however_they_arrive(self):
        # Canonical, like every other identity in this lane: two runs of ONE command
        # must not produce two different comments.
        assert decline.ordinals_of([2, 0]) == decline.ordinals_of([0, 2])

    def test_a_set_is_accepted_since_that_is_what_the_command_is(self):
        assert decline.ordinals_of(frozenset({2, 0})) == "1,3"


class TestTheReasonIsHarnessAuthored:
    def test_the_body_is_composed_here_and_not_taken_from_a_model(self):
        # A decline names a limitation of the CHANNEL: a fork having no branch to
        # base a pull request on is not a fact the generator knows or should
        # narrate. That is one of the three reasons ADR-0014 refuses a `decline`
        # plan step, and it holds only if nothing here reads an artifact.
        source = Path(decline.__file__).read_text(encoding="utf-8")
        for artifact_input in ("review.json", "plan.json", "commanded_index.json"):
            assert artifact_input not in source, (
                f"decline.py reads {artifact_input}; the reason text must be the harness's, "
                "with no field a generator writes"
            )
