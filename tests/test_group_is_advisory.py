"""ADR-0013's load-bearing condition: no code in the fix lane reads `group`.

A group is a finding's CLAIM that it and its same-valued siblings are one defect,
and whether that claim is true is ADR-0005's unverifiable content question. So it
is advisory prose addressed to the commander choosing what to name, and what
authorises a write is the ordinals that human typed. `/fix 1` must not expand to
finding 1's group, because the resulting scope would be MODEL-CHOSEN — the
candidate ADR-0013 opens by refusing.

The drift from advisory to authorising is one convenience commit wide, so the ADR
enforces it the way ADR-0004's addendum enforced `control_flow`: by a coverage
assertion run in reverse. The ADR also states the stakes — "without that assertion
the disclosure should be refused outright" — which is why this file exists at all
and why its own calibration is asserted rather than assumed.

Written into test_plan_gate_differential.py originally, moved here when that
file's prover skip made this pure-Python scan's reach depend on a build it did
not use — and the differential file itself is gone now (the ADR superseding
ADR-0003), so this is where the guard lives, full stop.
"""

import ast
import json
import re
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HARNESS_DIR = REPO_ROOT / "src" / "aceiro"
POLICY_PATH = HARNESS_DIR / "policy.json"
FIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ai-pr-fix.yml"

# The disclosure field, restated rather than imported from verify.py. Importing it
# would make the guard agree with the thing it guards by construction; restating
# it plus test_the_group_field_is_the_one_the_policy_ships means a rename fails
# here instead of silently leaving the new field unasserted.
GROUP_FIELD = "group"

# The modules that may read the field, each with the reason it is not drift.
# SINGLE_GATE_KEYS' shape, and for its reason: an unexplained absence from the
# scan cannot be distinguished from an oversight, and test_every_exemption_is_
# load_bearing below refuses an entry that exempts nothing.
EXEMPT_MODULES = {
    # ADR-0013 requires a reader HERE and nowhere else: the cross-reference is
    # rendered by the harness, in post.render, because that is the only place
    # ordinals exist. post.py's own docstring records the exclusion; this records
    # that the test knows about it.
    "post": "ADR-0013's disclosure half — post.group_cross_reference is the required reader",
    # Where the field's name is defined and where the verifier bounds it. Reading
    # it to check its type, range and cardinality is the opposite of believing it.
    "verify": "the verifier bounds the field and never believes it (GROUP_FIELD, check_group_cardinality)",
}


def _entry_points() -> list[str]:
    """The harness modules ai-pr-fix.yml actually runs, read from the workflow.

    Derived rather than listed. A hand-kept list of "the fix lane" is what drifted:
    it named 8 modules under a comment claiming EVERY module between a `/fix`
    command and a delivery, and the closure below is 19.
    """
    found = sorted(set(re.findall(r"python\s+(?:\S*/)?src/aceiro/(\w+)\.py", FIX_WORKFLOW.read_text())))
    assert found, (
        f"no `python src/aceiro/*.py` step found in {FIX_WORKFLOW.name}; the fix lane "
        "cannot be derived from the workflow and this whole file would scan nothing"
    )
    return found


def fix_lane_modules() -> list[str]:
    """Every harness module reachable from a fix-lane entry point, by import.

    The transitive import closure IS the lane the comment claimed: a module that
    runs between a `/fix` command and a delivery is one the lane's entry points can
    reach. Deriving it means a new module joining the lane joins the scan, which is
    the property a literal list cannot have.
    """
    modules = {path.stem for path in HARNESS_DIR.glob("*.py")}
    seen: set[str] = set()
    pending = _entry_points()
    while pending:
        module = pending.pop()
        if module in seen or module not in modules:
            continue
        seen.add(module)
        for node in ast.walk(ast.parse((HARNESS_DIR / f"{module}.py").read_text())):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                pending.append(node.module)
    return sorted(seen)


def _names_bound_to(field: str) -> dict[str, set[str]]:
    """Per module, the module-level names holding `field`'s value.

    This is the arm the scan did not have, and its absence is what made the
    assertion pass on the spelling this repository already uses:

        verify.py   GROUP_FIELD = "group"
        post.py     from verify import GROUP_FIELD
        post.py     group = findings[index][GROUP_FIELD]

    `finding[GROUP_FIELD]` contains no STRING equal to the field and no NAME equal
    to it, so a developer adding a convenience widening by copying the only
    existing reader writes the evading spelling BY DEFAULT. Not an obfuscation
    argument — the house idiom.

    Resolved to a fixpoint so a re-export chains: an alias imported from a module
    that imported it is the same constant under a third name.
    """
    trees = {path.stem: ast.parse(path.read_text()) for path in HARNESS_DIR.glob("*.py")}
    bound: dict[str, set[str]] = {stem: set() for stem in trees}
    changed = True
    while changed:
        changed = False
        for stem, tree in trees.items():
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value == field:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in bound[stem]:
                            bound[stem].add(target.id)
                            changed = True
                elif isinstance(node, ast.ImportFrom) and node.module in bound:
                    for alias in node.names:
                        local = alias.asname or alias.name
                        if alias.name in bound[node.module] and local not in bound[stem]:
                            bound[stem].add(local)
                            changed = True
    return bound


def readers_of(field: str, paths: list[Path]) -> list[str]:
    """Every file in `paths` whose CODE could read `field` off a finding.

    Tokenized rather than grepped, in two directions, because both kinds of false
    answer make the assertion lie.

    False POSITIVES would make it unmaintainable, and there are two sources. The
    ADRs and this repo's prose discuss the field constantly — including the
    sentence explaining why nothing reads it — so comments and docstrings are
    excluded. And `match.group(1)` and "concurrency group" are not readers of a
    finding's field, so a NAME reached through `.` is excluded UNLESS the thing
    before the dot is a module that holds the field's value: that is what
    distinguishes `re.Match.group` from `verify.GROUP_FIELD`. Dropping the skip
    instead would have cost the `re` exclusion, which is load-bearing.

    False NEGATIVES would make it worthless. A finding is a dict, so the way to
    read the field is a subscript or a `.get`, and the key can be spelled four
    ways: a STRING literal, an f-string (Python 3.12+ emits FSTRING_MIDDLE and
    never STRING, so `finding[f"group"]` carries no STRING token at all), a bare
    name holding the value, or the imported constant this repository actually
    uses. All four count, wherever they appear in code, including inside a tuple
    of field names a loop iterates.

    An `import` of the constant counts as a reader on its own. A fix-lane module
    has no use for the name that is not reading the field, and counting the import
    puts the failure on the line a reviewer would have to justify.
    """
    bound = _names_bound_to(field)
    holders: dict[str, set[str]] = {}
    for module, names in bound.items():
        for name in names:
            holders.setdefault(name, set()).add(module)

    found = []
    for path in paths:
        local = set(bound.get(path.stem, set())) | {field}
        with path.open("rb") as handle:
            tokens = [
                token for token in tokenize.tokenize(handle.readline)
                if token.type not in (tokenize.COMMENT, tokenize.NL)
            ]
        # A STRING whose whole statement is that string is a docstring: it follows
        # INDENT, NEWLINE or the file's ENCODING marker and nothing else.
        docstring_after = (tokenize.INDENT, tokenize.NEWLINE, tokenize.ENCODING, tokenize.DEDENT)
        for index, token in enumerate(tokens):
            previous = tokens[index - 1] if index else None
            if token.type == tokenize.STRING:
                if previous is not None and previous.type in docstring_after:
                    continue
                try:
                    value = ast.literal_eval(token.string)
                except (ValueError, SyntaxError):
                    continue
                if value == field:
                    found.append(path.name)
                    break
            elif token.type == tokenize.FSTRING_MIDDLE and token.string == field:
                found.append(path.name)
                break
            elif token.type == tokenize.NAME and token.string in local | set(holders):
                if previous is not None and previous.string == ".":
                    qualifier = tokens[index - 2] if index >= 2 else None
                    reached_through_a_holder = (
                        qualifier is not None
                        and qualifier.type == tokenize.NAME
                        and qualifier.string in holders.get(token.string, set())
                    )
                    if not reached_through_a_holder:
                        continue  # an attribute (match.group), not this field
                found.append(path.name)
                break
    return found


def fix_lane_readers(field: str) -> list[str]:
    scanned = [
        HARNESS_DIR / f"{module}.py"
        for module in fix_lane_modules()
        if module not in EXEMPT_MODULES
    ]
    return readers_of(field, scanned)


# --------------------------------------------------------- the assertion itself ---


def test_the_group_field_has_NO_reader_in_the_fix_lane():
    """ADR-0013's load-bearing condition, and the ADR says the disclosure should be
    refused outright without it.

    What authorises a write is the ordinals the commander typed — never the group.
    This is ADR-0004's policy-coverage assertion run in reverse: instead of "every
    key has a reader", it is "this key has none", in the lane where a reader would
    matter.
    """
    readers = fix_lane_readers(GROUP_FIELD)
    assert not readers, (
        f"the fix lane reads {GROUP_FIELD!r} in {readers}. A group is advisory prose to a "
        "commander; a reader in this lane makes it authorise a write, which is the "
        "model-chosen scope ADR-0013 refuses. If a group must genuinely be read here, "
        "that is an ADR decision and not a test to update."
    )


def test_the_group_field_is_the_one_the_policy_ships():
    # Restated, this constant would keep guarding `group` after a rename, and the
    # new field would have no assertion at all.
    item_fields = json.loads(POLICY_PATH.read_text())["artifact_schema"]["properties"]["findings"]["items"]["properties"]
    assert GROUP_FIELD in item_fields, (
        f"policy.json's findings carry no {GROUP_FIELD!r} field, so this assertion guards nothing"
    )


# ------------------------------------------------ the lane the assertion covers ---


def test_the_lane_covers_the_three_modules_the_ADR_names():
    """ADR-0013's floor, stated verbatim: "this field has NO READER in
    `prepare_fix_context`, `plan_loop` or `plan_verify`".

    The derivation above is broader than the ADR demands, which is the intent — but
    a derivation that lost one of the three named modules would be narrower than
    the ADR while looking more thorough.
    """
    named = {"prepare_fix_context", "plan_loop", "plan_verify"}
    missing = named - set(fix_lane_modules())
    assert not missing, (
        f"the derived fix lane is missing {sorted(missing)}, which ADR-0013 names explicitly; "
        "the derivation no longer reaches the modules the condition is written about"
    )


def test_the_lane_reaches_the_modules_a_reader_could_widen_a_scope_in():
    """The modules where a reader would be both invisible and effective.

    `artifact.py` is the one reproduced: it hosts `rendered_findings`, which is how
    `read_commanded_findings` resolves ordinals, so a widening helper placed there
    and called from `plan_loop` was scanned by nothing and changed the commanded set.
    `execute_plan`, `route_delivery`, `stack` and `suggest` hold the write token, so
    a reader there would be the group deciding what gets written.
    """
    effective = {
        "artifact", "prepare_fix_context", "plan_loop", "plan_verify",
        "execute_plan", "route_delivery", "stack", "suggest", "fix_command",
    }
    missing = effective - set(fix_lane_modules())
    assert not missing, f"the derived fix lane no longer reaches {sorted(missing)}"


def test_the_lane_is_derived_and_not_a_list_that_can_drift():
    # The defect this derivation replaces: a literal list of 8 modules under a
    # comment claiming every module in the lane. The closure is much larger than
    # any list a person would maintain, which is the whole argument for deriving it.
    modules = fix_lane_modules()
    assert len(modules) > 8, (
        f"the fix lane derives to only {len(modules)} modules, which is the size of the "
        "hand-kept list this replaced — the closure walk has probably stopped following imports"
    )


def test_every_exemption_is_load_bearing():
    # An exemption whose removal changes nothing is documentation wearing an
    # assertion's clothes. Each entry must be the thing keeping a real reader
    # named, so an exempt module that stops reading the field stops being exempt.
    for module in EXEMPT_MODULES:
        assert readers_of(GROUP_FIELD, [HARNESS_DIR / f"{module}.py"]), (
            f"EXEMPT_MODULES[{module!r}] exempts nothing: the module has no {GROUP_FIELD!r} "
            "reader, so the entry states a licence nobody is using and hides the module "
            "from the scan for free"
        )


def test_no_exemption_names_a_module_outside_the_lane():
    # The opposite direction: an exemption for a module the lane does not reach
    # licenses a reader nothing was ever going to look at, and reads as though the
    # scan considered it.
    stray = set(EXEMPT_MODULES) - set(fix_lane_modules())
    assert not stray, (
        f"EXEMPT_MODULES names {sorted(stray)}, which the fix lane does not reach; "
        "an exemption from a scan that never covered the module states nothing"
    )


# ------------------------------------------------------------- the calibration ---

# Every spelling the scan must see, one arm each, as SOURCE. Calibrated against
# fixtures rather than against the live corpus, which is what let the STRING arm
# drift: `commanded_findings` — the field the calibration used — appears as a bare
# NAME in all eight scanned files and as a string literal in NONE, so the STRING
# arm was uncalibrated and killing it left every test green.
SEEN_SPELLINGS = {
    "a string subscript": 'def widen(finding):\n    return finding["group"]\n',
    "a string .get": 'def widen(finding):\n    return finding.get("group")\n',
    "an f-string subscript": 'def widen(finding):\n    return finding[f"group"]\n',
    "a local alias": 'def widen(finding):\n    key = "group"\n    return finding[key]\n',
    "a tuple of field names": 'FIELDS = ("path", "group")\n\ndef widen(finding):\n    return [finding[f] for f in FIELDS]\n',
    "a bare name": 'def widen(findings, commanded):\n    group = commanded[0]["x"]\n    return group\n',
    "the imported constant, this repo's own idiom": (
        "from verify import GROUP_FIELD\n\n\ndef widen(finding):\n    return finding[GROUP_FIELD]\n"
    ),
    "the constant under a rename": (
        "from verify import GROUP_FIELD as FIELD\n\n\ndef widen(finding):\n    return finding[FIELD]\n"
    ),
    "the module attribute": "import verify\n\n\ndef widen(finding):\n    return finding[verify.GROUP_FIELD]\n",
    "the constant re-exported through post": (
        "from post import GROUP_FIELD\n\n\ndef widen(finding):\n    return finding[GROUP_FIELD]\n"
    ),
}

# Every spelling the scan must NOT see. False positives make the assertion
# unmaintainable, and `re.Match.group` is the one that would fire constantly.
IGNORED_SPELLINGS = {
    "a regex match group": 'import re\n\n\ndef read(text):\n    return re.match("(x)", text).group(1)\n',
    "a docstring naming the field": '"""Nothing here may read group."""\n\n\ndef widen(finding):\n    return finding["path"]\n',
    "a function docstring naming the field": 'def widen(finding):\n    """No reader of group lives here."""\n    return finding["path"]\n',
    "a comment naming the field": 'def widen(finding):\n    # never read group\n    return finding["path"]\n',
    "an unrelated attribute": 'def read(spec):\n    return spec.group\n',
    "a different field entirely": 'def widen(finding):\n    return finding["severity"]\n',
}


def _fixture(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "candidate_reader.py"
    path.write_text(source)
    return path


@pytest.mark.parametrize("spelling", sorted(SEEN_SPELLINGS))
def test_the_scan_sees_each_spelling_of_a_reader(spelling, tmp_path):
    """One arm per case, so killing an arm fails a test that names it.

    The three that evaded the scan as shipped are here by name: the imported
    constant (the house idiom), the module attribute, and the f-string — none of
    which carries a STRING token equal to the field or a NAME equal to it.
    """
    path = _fixture(tmp_path, SEEN_SPELLINGS[spelling])
    assert readers_of(GROUP_FIELD, [path]) == [path.name], (
        f"the scan does not see {spelling}, so a reader written that way passes the "
        "assertion ADR-0013 makes the disclosure conditional on"
    )


@pytest.mark.parametrize("spelling", sorted(IGNORED_SPELLINGS))
def test_the_scan_ignores_each_non_reader(spelling, tmp_path):
    path = _fixture(tmp_path, IGNORED_SPELLINGS[spelling])
    assert readers_of(GROUP_FIELD, [path]) == [], (
        f"the scan reports {spelling} as a reader; false positives make this assertion "
        "unmaintainable, and the first one to be waved through takes the assertion with it"
    )


def test_the_scan_reports_the_file_a_reader_is_in(tmp_path):
    # The assertion's message names files, and a scan that found readers but
    # reported an empty list would read as "no readers" at every call site.
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    reader = first / "reads.py"
    reader.write_text('def widen(finding):\n    return finding["group"]\n')
    clean = second / "clean.py"
    clean.write_text('def widen(finding):\n    return finding["path"]\n')
    assert readers_of(GROUP_FIELD, [reader, clean]) == ["reads.py"]


def test_the_field_value_is_resolved_from_the_module_that_defines_it():
    # The imported-constant arm works by resolving GROUP_FIELD to its value, so it
    # only works while verify.py is where that value lives. If the constant moves
    # or its value changes, the arm silently stops resolving and the house idiom
    # evades again.
    bound = _names_bound_to(GROUP_FIELD)
    assert "GROUP_FIELD" in bound["verify"], (
        f"no module-level name in verify.py holds {GROUP_FIELD!r}, so the scan cannot resolve "
        "the imported constant and this repository's own spelling of the read evades it"
    )
