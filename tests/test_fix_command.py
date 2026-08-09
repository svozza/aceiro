"""Tests for parsing the `/fix N[,M...]` command out of a comment body.

The comment body is attacker-controlled on a public repository: anyone may
comment, and trust is decided separately (author_trust, on the COMMENT author).
So this module's whole job is to decide whether a body is a command at all, and
which ordinals it names — before any credential-bearing step runs.

The ordinals are where the two numbering systems meet. A commander types the
positions they READ, which are 1-based; commanded_index.json is 0-based, because
it indexes a list. Every test here that names a number names both.

The command names a SET (ADR-0013), so three bounds are the set's own and not the
ordinal's: the empty set is not a command, duplicates collapse rather than naming
a finding twice, and the count is bounded by the same cap the ordinal is.
"""

from pathlib import Path

import pytest

from conftest import POLICY
from fix_command import MAX_ORDINAL, MAX_ORDINALS, parse_fix_command


class TestTheCapIsThePolicys:
    def test_the_cap_is_the_policys_finding_limit(self):
        # No review has more findings than the policy allows, so no ordinal above
        # that cap names one. Restated, this number would keep bounding at today's
        # value after an operator raised the cap, and every command for a
        # newly-enabled finding would read correctly and do nothing.
        assert MAX_ORDINAL == POLICY["artifact_schema"]["findings"]["max_items"]

    def test_the_cap_TRACKS_the_policy_rather_than_agreeing_with_it_today(self, tmp_path,
                                                                         monkeypatch):
        # The assertion above is satisfied by a hardcoded 10, because max_items IS 10
        # — so it detects a WRONG constant but never a RESTATED one, which is the
        # only failure the class exists to prevent. This reloads the module against a
        # policy with a different cap: a literal cannot follow.
        import importlib
        import json as _json

        import fix_command

        raised = _json.loads(Path(fix_command.POLICY_PATH).read_text())
        raised["artifact_schema"]["findings"]["max_items"] = 4
        policy = tmp_path / "policy.json"
        policy.write_text(_json.dumps(raised))

        monkeypatch.setattr(fix_command, "POLICY_PATH", policy)
        monkeypatch.setattr("artifact.POLICY_PATH", policy)
        reloaded = importlib.reload(fix_command)
        try:
            assert reloaded.MAX_ORDINAL == 4, (
                "MAX_ORDINAL did not follow the policy, so it is restated rather than "
                "derived and would keep bounding at the old cap"
            )
            # And the pattern follows it too, or the top ordinal is refused a layer
            # earlier than the range check.
            assert reloaded.parse_fix_command("/fix 4") == {3}
            # The COUNT bound follows as well. A pattern admitting more ordinals
            # than the policy allows findings carries a list no artifact can
            # resolve; one admitting fewer refuses a legal command.
            assert reloaded.MAX_ORDINALS == 4
            assert reloaded.parse_fix_command("/fix 1,2,3,4") == {0, 1, 2, 3}
            assert reloaded.parse_fix_command("/fix 1,2,3,4,4") is None
        finally:
            monkeypatch.undo()
            importlib.reload(fix_command)

    def test_the_pattern_can_express_the_highest_legal_ordinal(self):
        # The digit bound and the cap are one decision. A pattern narrower than the
        # cap refuses the top ordinal before the range check ever sees it — the
        # same silent no-op, one layer earlier.
        assert parse_fix_command(f"/fix {MAX_ORDINAL}") == {MAX_ORDINAL - 1}

    def test_the_count_bound_is_the_same_cap(self):
        # A review has at most max_items findings, so a command naming more
        # ordinals than that cannot name a distinct finding with each. Derived from
        # the one ceiling that already exists rather than a second number to keep
        # in step with it.
        assert MAX_ORDINALS == POLICY["artifact_schema"]["findings"]["max_items"]


class TestParseFixCommand:
    def test_the_ordinal_is_one_based_for_humans(self):
        # The conversion, stated once and directly: `/fix 1` is the FIRST rendered
        # finding, which is index 0. Off by one here means every command
        # remediates the neighbour of the finding the commander pointed at — a
        # real defect on the wrong file, with every gate passing.
        assert parse_fix_command("/fix 1") == {0}
        assert parse_fix_command("/fix 2") == {1}
        assert parse_fix_command("/fix 10") == {9}

    def test_zero_is_not_a_command(self):
        # There is no zeroth finding in a comment a human read. Accepting it would
        # silently mean "the first", which is a guess about intent.
        assert parse_fix_command("/fix 0") is None

    def test_a_negative_ordinal_is_not_a_command(self):
        assert parse_fix_command("/fix -1") is None

    def test_an_absurd_ordinal_is_not_a_command(self):
        # Bounded before it reaches the gate rather than by the gate's range
        # check: the policy caps findings at 10, and this is the parse refusing to
        # carry an unbounded integer from an untrusted body into an int().
        assert parse_fix_command(f"/fix {MAX_ORDINAL}") == {MAX_ORDINAL - 1}
        assert parse_fix_command(f"/fix {MAX_ORDINAL + 1}") is None
        assert parse_fix_command("/fix 999999999999999999999999") is None

    def test_a_body_that_is_not_a_command_is_not_one(self):
        for body in ["", "looks good to me", "/fixup 1", "/fix", "/fix all",
                     "/fix one", "fix 1", "/FIXES 1", "/fix 1.5", "/fix 1x"]:
            assert parse_fix_command(body) is None, body

    def test_the_command_may_be_the_whole_comment_with_surrounding_space(self):
        # GitHub bodies arrive with CRLF and trailing whitespace.
        assert parse_fix_command("  /fix 3  ") == {2}
        assert parse_fix_command("/fix 3\r\n") == {2}

    def test_the_command_must_be_the_comments_only_content(self):
        # The strict reading, and it is deliberate. A body that MENTIONS the
        # command — quoting someone else's, or discussing it — must not fire one:
        # the effect is a real remediation and a maintainer typing prose about
        # `/fix 2` has not commanded anything. Refusing here costs the commander a
        # second, dedicated comment; accepting would make every quotation of a
        # command a command.
        for body in [
            "I think we should run /fix 2 here",
            "> /fix 2",
            "```\n/fix 2\n```",
            "/fix 2 — but check the other one first",
            "/fix 2\n/fix 3",
        ]:
            assert parse_fix_command(body) is None, body

    def test_case_and_spacing_of_the_verb_are_exact(self):
        # No normalisation: one spelling is a command. A looser match is a wider
        # attack surface on a body anyone can write, for no benefit a second
        # comment does not provide.
        for body in ["/Fix 1", "/FIX 1", "/ fix 1", "/fix  1"]:
            assert parse_fix_command(body) is None, body

    @pytest.mark.parametrize("space", [" ", " ", " "])
    def test_a_unicode_space_is_not_the_separator(self, space):
        # `\s` in Python matches these, so a permissive pattern would accept a
        # body that does not read as the command in any renderer. The separator is
        # one ASCII space.
        assert parse_fix_command(f"/fix{space}1") is None

    def test_an_invisible_code_point_cannot_hide_inside_the_command(self):
        # canonicalize's threat, on this channel: a zero-width joiner between the
        # verb and the ordinal renders as `/fix 1` while defeating an exact match.
        # The pattern is exact, so this simply is not a command — no stripping,
        # which would mean accepting a body that is not the one displayed.
        assert parse_fix_command("/fix​ 1") is None
        assert parse_fix_command("/fix 1​") is None


class TestTheCommandNamesASet:
    """ADR-0013: `/fix N,M` names several findings, and naming them asserts they
    take ONE remediation. The assertion is the commander's; what this module owes
    is a bounded, unambiguous reading of the SHAPE they typed.
    """

    def test_several_ordinals_are_one_command(self):
        assert parse_fix_command("/fix 1,3") == {0, 2}
        assert parse_fix_command("/fix 2,4,6") == {1, 3, 5}

    def test_order_is_not_part_of_the_command(self):
        # A set, because `/fix 3,1` and `/fix 1,3` name the same findings. The
        # ordering is the commander's typing; stack.fix_key rests on this, so two
        # spellings of one command cannot open two follow-up pull requests.
        assert parse_fix_command("/fix 3,1") == parse_fix_command("/fix 1,3")

    def test_a_repeated_ordinal_collapses_rather_than_naming_a_finding_twice(self):
        # `/fix 1,1` names ONE finding. Read as two, the same finding would be
        # remediated twice inside one plan's caps, and the scope check would be
        # satisfied twice by one path.
        assert parse_fix_command("/fix 1,1") == {0}
        assert parse_fix_command("/fix 2,1,2") == {0, 1}

    def test_the_empty_set_is_not_a_command(self):
        # There is no command naming no finding. `/fix` and `/fix ,` have no honest
        # reading as "all of them" — that is `/fix all`, refused — and the empty set
        # would reach a plan session with nothing to fix.
        for body in ["/fix", "/fix ", "/fix ,", "/fix ,1", "/fix 1,", "/fix 1,,2"]:
            assert parse_fix_command(body) is None, body

    def test_more_ordinals_than_the_policy_allows_findings_is_not_a_command(self):
        # The count is bounded by the same cap the ordinal is, so an untrusted body
        # cannot make the parse walk an arbitrarily long list. At the cap it is a
        # command; one past it is not.
        at_cap = ",".join(str(n) for n in range(1, MAX_ORDINALS + 1))
        assert parse_fix_command(f"/fix {at_cap}") == set(range(MAX_ORDINALS))
        over_cap = ",".join(str(n) for n in range(1, MAX_ORDINALS + 2))
        assert parse_fix_command(f"/fix {over_cap}") is None

    def test_the_count_bound_is_on_the_ordinals_typed_not_the_set_they_collapse_to(self):
        # A repeated ordinal collapses to a smaller SET, and the bound is still on
        # the list. Otherwise `/fix 1,1,1,...` past the cap would parse: the count
        # exists to bound the untrusted body's length, and a set of one says nothing
        # about how long the body was.
        over_cap = ",".join(["1"] * (MAX_ORDINALS + 1))
        assert parse_fix_command(f"/fix {over_cap}") is None

    def test_one_out_of_range_ordinal_refuses_the_whole_command(self):
        # NOT the subset it could resolve. A commander who typed `/fix 1,0` asked
        # for two findings; delivering one of them would be a scope the harness
        # chose, which is the one thing ADR-0013 refuses to let anything but the
        # commander do.
        for body in ["/fix 1,0", "/fix 0,1", f"/fix 1,{MAX_ORDINAL + 1}"]:
            assert parse_fix_command(body) is None, body

    def test_the_separator_is_exactly_one_ascii_comma(self):
        # Decided as narrowly as the verb's own separator: every spelling admitted
        # here is a spelling an untrusted body may use. A space-separated list is
        # additionally indistinguishable from the trailing prose the
        # whole-comment rule refuses.
        for body in ["/fix 1, 3", "/fix 1 ,3", "/fix 1 3", "/fix 1;3", "/fix 1/3",
                     "/fix 1+3", "/fix 1，3", "/fix 1&3", "/fix 1\t3", "/fix 1\n3"]:
            assert parse_fix_command(body) is None, body
