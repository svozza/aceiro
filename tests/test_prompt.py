"""Mechanical checks on the generator prompt.

Whether the prompt STEERS the model is not testable without a model — that is
what the eval suite is for, and no assertion here is a substitute for it. What is
testable is drift: the prompt names fields and shows examples, and those must
agree with policy.json, or the model is being told to produce something the
verifier will reject. Two live defects motivate each half:

- `Links only to hosts: .` reached a real model because an empty allowlist was
  interpolated into the appended constraints. Nothing checked the ASSEMBLED
  prompt for holes.
- The prompt now embeds example artifacts. An example that does not verify is
  worse than none: it teaches the model a shape the verifier rejects, and it
  would rot silently the first time a cap or field name changed in policy.json.
"""

import json
import re
import sys
from pathlib import Path

import pytest

from artifact import (
    DEFAULT_PROJECT_DESCRIPTION,
    PROMPT_PATH,
    apply_project_description,
    render_constraints,
)
from conftest import POLICY
from verify import Rejection, check_schema

PROMPT = PROMPT_PATH.read_text()
SHIPPED_POLICY = json.loads((Path(__file__).parent.parent / "src" / "aceiro" / "policy.json").read_text())
EXAMPLES = re.findall(r"```json\n(.*?)```", PROMPT, re.S)


class TestPromptFile:
    def test_the_prompt_resolves_and_is_not_empty(self):
        # PROMPT_PATH is module-relative (see artifact.py). If the layout moves,
        # cc_loop dies at runtime holding a credential, which is a bad place to
        # discover a path bug.
        assert PROMPT_PATH.exists(), f"prompt not found at {PROMPT_PATH}"
        assert len(PROMPT) > 500

    def test_it_names_every_field_the_schema_requires(self):
        # Rename a field in policy.json and the prompt silently disagrees with
        # the schema the CLI enforces.
        for field in SHIPPED_POLICY["artifact_schema"]["required"]:
            assert f"`{field}`" in PROMPT, f"prompt never mentions {field!r}"

    def test_it_shows_every_finding_field(self):
        # In an example, or named in prose. The prompt discusses `path` and
        # `severity` descriptively ("anchored to a changed file", "Severity:
        # critical = ...") rather than by field name, which is fine as long as
        # the examples demonstrate them -- but a field appearing in NEITHER means
        # the model is never shown a field the schema requires.
        for field in SHIPPED_POLICY["artifact_schema"]["properties"]["findings"]["items"]["properties"]:
            in_prose = f"`{field}`" in PROMPT
            in_example = any(field in f for e in EXAMPLES for f in json.loads(e)["findings"])
            assert in_prose or in_example, f"findings.{field} appears in neither prose nor an example"

    def test_it_names_every_severity(self):
        for severity in SHIPPED_POLICY["artifact_schema"]["properties"]["findings"]["items"]["properties"]["severity"]["enum"]:
            assert f"`{severity}`" in PROMPT


class TestAssembledPromptHasNoHoles:
    """The prompt the model actually receives is file + constraints + roots.

    Checked as a whole because the defect that reached production lived in the
    JOIN, not in either part: the file was fine and render_constraints was fine
    for a populated allowlist.
    """

    def test_no_empty_interpolation_under_the_shipped_policy(self):
        assembled = PROMPT + render_constraints(SHIPPED_POLICY)
        for line in assembled.splitlines():
            stripped = line.rstrip()
            assert not stripped.endswith(": ."), f"empty interpolation: {line!r}"
            assert not stripped.endswith(": ,"), f"empty list element: {line!r}"
            if "host" in line:
                assert not re.search(r"`\s*`", line), f"empty backticks: {line!r}"

    def test_no_empty_interpolation_with_a_populated_allowlist(self):
        import copy

        policy = copy.deepcopy(SHIPPED_POLICY)
        policy["markdown"]["link_host_allowlist"] = ["docs.example.com"]
        assembled = PROMPT + render_constraints(policy)
        assert "docs.example.com" in assembled
        for line in assembled.splitlines():
            assert not line.rstrip().endswith(": .")

    def test_it_states_the_canonical_text_rule(self):
        # ADR-0011: the verifier REJECTS invisible/bidi controls and non-NFC
        # text, so the prompt has to say so — an enforced rule the model can only
        # discover by burning a submission is a rule stated in the wrong place.
        assembled = PROMPT + render_constraints(SHIPPED_POLICY)
        assert "NFC" in assembled
        assert "bidirectional" in assembled
        assert "zero-width" in assembled


class TestProjectDescription:
    """The one consumer-substituted region of the prompt (ADR-0002's last
    coupling). The substitution is anchored to a verbatim constant, so the
    load-bearing assertions are: the anchor still matches the file, absence
    of a description changes nothing, and a supplied description that cannot
    land raises rather than silently reviewing under the wrong identity."""

    def test_the_prompt_contains_the_anchor_verbatim(self):
        # Reword the prompt's opening without updating the constant and the
        # substitution would match nothing; this failure names the pair.
        assert DEFAULT_PROJECT_DESCRIPTION in PROMPT

    def test_no_description_is_byte_identical(self):
        # The shipped default carries the eval history; an unset consumer
        # description must not perturb a single byte of it.
        assert apply_project_description(PROMPT, None) == PROMPT
        assert apply_project_description(PROMPT, "") == PROMPT

    def test_description_replaces_exactly_the_anchor(self):
        swapped = apply_project_description(PROMPT, "`svozza/artel`, a Rust peer-to-peer file syncer")
        assert "`svozza/artel`, a Rust peer-to-peer file syncer" in swapped
        assert DEFAULT_PROJECT_DESCRIPTION not in swapped
        # Everything outside the anchor is untouched.
        assert swapped.replace(
            "`svozza/artel`, a Rust peer-to-peer file syncer", DEFAULT_PROJECT_DESCRIPTION
        ) == PROMPT

    def test_unmatched_anchor_raises_not_silently_skips(self):
        with pytest.raises(ValueError, match="default project description"):
            apply_project_description("a prompt that was reworded", "some description")


class TestEmbeddedExamples:
    """The example artifacts must be artifacts the verifier accepts.

    Added because the model was writing the whole artifact into one parameter as
    text (docs/findings/0001), and a concrete correct example is one candidate
    fix. An example is only worth showing if it is actually valid.
    """

    def test_there_is_at_least_one_example(self):
        assert EXAMPLES, "no ```json example in the prompt"

    @pytest.mark.parametrize("index", range(len(EXAMPLES)))
    def test_every_example_is_valid_json(self, index):
        json.loads(EXAMPLES[index])

    @pytest.mark.parametrize("index", range(len(EXAMPLES)))
    def test_every_example_passes_the_real_schema_check(self, index):
        # check_schema, not a hand-rolled comparison: the example is graded by
        # the same code that will grade the model's output.
        check_schema(json.loads(EXAMPLES[index]), POLICY)

    def test_no_example_shows_an_empty_findings_list(self):
        """Deliberately the INVERSE of what this test first asserted.

        An empty-findings example was added on the reasoning that the empty case
        was where the field went missing, so it should be demonstrated. Measured,
        that reasoning was wrong and expensive: zero-finding artifacts rose from
        3/25 to 9/31 of verified runs, and provenance_boundary_adjacent_bug went
        from finding the planted defect 3/3 to reporting nothing 6/6 -- the model
        reasoned correctly about the defect and then filed it in residual_risk.

        A worked example of "nothing to report" is an invitation to report
        nothing. The schema requires the key, and step 4 says so in prose; that
        is enough. Do not re-add an empty example without measuring the
        zero-finding rate before and after."""
        assert not any(json.loads(e)["findings"] == [] for e in EXAMPLES), (
            "an empty-findings example measurably raises the zero-finding rate"
        )

    def test_an_example_shows_anchoring_a_defect_in_unchanged_code(self):
        # The behaviour provenance_boundary_adjacent_bug grades: a defect in
        # unchanged code, anchored to the changed line that triggers it. The
        # model's failure was not misunderstanding provenance -- its reasoning was
        # correct -- but concluding that out-of-hunk meant unreportable.
        assert any("unchanged" in json.loads(e)["findings"][0]["body"] for e in EXAMPLES if json.loads(e)["findings"]), (
            "no example demonstrates anchoring an unchanged-code defect to its trigger line"
        )

    def test_one_example_shows_a_populated_finding(self):
        assert any(json.loads(e)["findings"] for e in EXAMPLES)

    @pytest.mark.parametrize("index", range(len(EXAMPLES)))
    def test_examples_respect_the_policy_caps(self, index):
        # An example longer than a cap would teach the model to exceed it.
        example = json.loads(EXAMPLES[index])
        schema = SHIPPED_POLICY["artifact_schema"]["properties"]
        assert len(example["summary"]) <= schema["summary"]["maxLength"]
        assert len(example["residual_risk"]) <= schema["residual_risk"]["maxLength"]
        assert len(example["findings"]) <= schema["findings"]["maxItems"]
        for finding in example["findings"]:
            for field, spec in schema["findings"]["items"]["properties"].items():
                if spec.get("type") == "string":
                    assert len(finding[field]) <= spec["maxLength"], f"{field} exceeds its cap"


class TestResidualRiskIsDefinedOnce:
    """`residual_risk` is a schema field, graded by 5 of 11 eval scenarios, and
    referenced three times in the prompt for three different purposes — and it was
    absent from CONTEXT.md, the glossary whose whole job is fixing terms.

    That gap produced two defensible-but-conflicting readings in review: "what you
    could not determine" (too narrow — it excludes the injection-note use) and
    "stuff not in the PR" (too broad — it licenses demoting a confirmed defect).
    Between them, a scenario description and its own graded assertion contradicted
    each other and the model was marked wrong 6/6 for taking the documented
    alternative.

    These tests bind the three places the term is defined so they cannot drift
    apart again silently.
    """

    CONTEXT = (Path(__file__).parent.parent / "CONTEXT.md").read_text()

    def test_the_glossary_defines_it(self):
        assert "**Residual risk**" in self.CONTEXT, "residual_risk is a load-bearing term with no glossary entry"

    def test_the_glossary_gives_the_decision_test(self):
        # The dividing line, not a list of examples: examples invite reasoning by
        # analogy, which is how "could not anchor" crept in.
        assert "could I confirm this?" in self.CONTEXT
        assert "could I anchor it?" in self.CONTEXT

    def test_the_glossary_covers_all_three_prompt_uses(self):
        entry = self.CONTEXT.split("**Residual risk**")[1].split("_Avoid_")[0]
        assert "confirm" in entry, "missing the suspected-but-unconfirmed use"
        assert "capability" in entry or "running tests" in entry, "missing the missing-capability use"
        assert "manipulation" in entry or "attempted" in entry, "missing the injection-note use"

    def test_the_prompt_states_the_same_decision_test(self):
        assert "could I confirm it?" in PROMPT
        assert "could I anchor it?" in PROMPT

    def test_the_prompt_says_it_is_not_for_confirmed_defects(self):
        # The specific error the model made: reasoning about provenance correctly,
        # then filing a confirmed defect in residual_risk because it could not
        # anchor it to the defective line.
        assert "not** the place for a defect you have established" in PROMPT

    def test_the_prompt_does_not_redefine_it_narrowly(self):
        # The wording I invented and had to remove. It excluded the injection use
        # and contradicted a scenario that permits a note there.
        assert "only for what you could not determine" not in PROMPT
