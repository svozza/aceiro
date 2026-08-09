"""Deterministic, fail-closed verifier for remediation PLAN artifacts.

The Python twin of ts/plan/schema.ts, for the executor's side of the boundary:
the prover (TypeScript, ADR-0003) decides whether a plan satisfies the ordering
and frame policies, but the process that holds the write token is Python, and
it re-verifies rather than trusting a claim from another job — the same posture
post.py takes toward the review job. The two implementations read the same
policy.json, so a plan the prover admitted and this module rejects (or the
reverse) is a defect in one of them, and the differential is worth a test.

This module carries ADR-0004's three reserved closures, same as the TS gate:
steps are {id, kind, args} typed records; argument_forms admits only literals,
so an execution-time binding ({"$ref": ...}) is an object where a scalar is
expected and rejects today; and there is no version field — a model-supplied
schema version is a model-selected policy, so "version" is just an unexpected
key like anything else the model invents.

Fail-closed, whole-plan, first violation wins. No partial acceptance, no
repair.

Checks, in order (mirroring verify.py's phase order):
1. Strict structural schema (check_plan_schema).
2. Cardinality (check_plan_cardinality) then the ordering policy
   (check_plan_ordering, mirroring proveOrdering) — both decidable from the plan
   alone, so they run before anything that reads a file.
3. ADR-0005's containment (check_plan_containment): frame, denylist,
   suggest.line provenance and placement, bounding, anchoring. Anchoring is why
   verify_plan grows a content source over verify()'s argument list — the ADR
   calls it the largest change to the verifier's shape.
4. Markdown allowlist on the args the policy marks markdown-bearing
   (check_plan_markdown, reusing verify.check_markdown_field).
5. Secret scan over the whole plan, raw and rendered (check_plan_secrets,
   mirroring verify.check_secrets).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from canonicalize import strip_invisible
from verify import (
    SCALAR_KEYS,
    Rejection,
    check_markdown_field,
    check_scalar,
    check_scalar_spec,
    fence_info_strings,
    parse_diff_hunks,
    scanned_representations,
)

# Ids exist so steps can be referred to; a duplicate makes a reference
# ambiguous, and a counterexample naming a step becomes unactionable. Kept
# conservative: this is what appears in audit output. Same expression as
# ts/plan/schema.ts's ID_RE — the two must agree or a plan can pass one gate
# and fail the other on shape alone.
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")

PLAN_KEYS = frozenset({"steps"})
STEP_KEYS = frozenset({"id", "kind", "args"})

# Every key of the policy's `plan` section, being exactly the ones this gate
# reads. Twin of ts/plan/policy.ts's PLAN_KEYS, whose loader refuses a policy
# carrying anything outside it — this gate read its keys ad hoc, so an unknown key
# was silently ignored here and rejected there: one policy meaning two things.
PLAN_POLICY_KEYS = frozenset({
    "max_steps",
    "control_flow",
    "argument_forms",
    "step_kinds",
    "ordering",
    "max_patched_files",
    "max_changed_lines",
    "max_changed_bytes",
    "max_plan_changed_bytes",
    "path_denylist",
    "branch_prefix",
    "label_allowlist",
})

# JSON permits \ud800 and both parsers accept it, so a plan can carry a string
# that is not encodable text: every later phase encodes it (containment for the
# anchor, the transcript for the audit record) and raises UnicodeEncodeError,
# which is not a Rejection. Any surrogate reaching here is UNPAIRED by
# construction — json.loads combines a valid pair into one astral code point — so
# the range test needs no pairing logic. ts/plan/schema.ts's twin does, since a
# JS string keeps UTF-16 units.
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def check_reserved_closures(policy_plan: dict) -> None:
    """Refuse a policy that widens ADR-0004's reservations past what this gate
    implements.

    control_flow and argument_forms are reservations, which means they have to
    REFUSE their shape today or they reserve nothing. This gate reasons about a
    straight-line plan throughout: ordering compares plan indices, containment
    simulates steps applying in sequence, and the prover's ∀-claims quantify over
    those same fixed positions. A policy declaring `branch` would be read by no
    code here — the branch step would verify as an ordinary straight-line step,
    and both gates would prove properties of a program neither had modelled.

    A policy error, not a Rejection about the plan: the fault is the deployment's,
    and reporting it as "the model produced something invalid" would send a reader
    to the generator. It is raised as Rejection only because that is the one
    failure channel this module has, with the message carrying the distinction —
    the prover's PolicyError is the same decision in a language that has a second
    exception type for it.
    """
    if control_flow := policy_plan["control_flow"]:
        raise Rejection(
            f"policy error: plan.control_flow declares {control_flow} — this gate implements "
            "straight-line plans only, and a branch it does not model is a branch nothing checks "
            "(ADR-0004: the reservation must refuse its shape until the semantics exist)"
        )
    forms = policy_plan["argument_forms"]
    if forms != ["literal"]:
        raise Rejection(
            f"policy error: plan.argument_forms declares {forms} — this gate implements "
            '["literal"] only, so an execution-time binding would pass a check that never '
            "looked at it"
        )


def check_plan_policy_keys(policy_plan: dict) -> None:
    """Refuse a plan policy carrying a key no reader consults, or missing one.

    Both directions, because both fail without saying so: an unknown key reads as
    a bound to whoever reviews policy.json while nothing enforces it, and a missing
    one raises KeyError from the middle of whichever check indexes it first rather
    than naming the absent field.

    A policy error rather than a claim about the plan, and decided before any step
    is read — the reasoning check_reserved_closures records.
    """
    if extra := sorted(set(policy_plan) - PLAN_POLICY_KEYS):
        raise Rejection(
            f"policy error: plan declares keys no reader consults {extra}; a bound this gate "
            f"does not enforce reads as a constraint while constraining nothing "
            f"(known: {sorted(PLAN_POLICY_KEYS)})"
        )
    if missing := sorted(PLAN_POLICY_KEYS - set(policy_plan)):
        raise Rejection(
            f"policy error: plan is missing keys {missing}, which this gate's checks index"
        )


def check_plan_arg_specs(policy_plan: dict) -> None:
    """Validate every step kind's arg specs, whether or not a plan uses that kind.

    Swept eagerly for the reason check_reserved_closures is: the policy this gate
    interprets must be one it can enforce, and that is settled before a step is
    read. A lazy check reaches only the kinds a plan happens to carry, so a
    malformed write-class spec — the ones deciding where `contents: write` points
    — stays latent until some later plan exercises it.

    Twin of ts/plan/policy.ts's loader, which validates the whole plan section up
    front. The two compile patterns with their OWN engines deliberately: each gate
    must refuse what it cannot enforce, and the two regex dialects disagree in
    both directions.
    """
    for kind, spec in policy_plan["step_kinds"].items():
        args = spec.get("args")
        if not isinstance(args, dict):
            raise Rejection(f"policy error: plan.step_kinds.{kind}.args is not an object")
        for name, arg_spec in args.items():
            where = f"plan.step_kinds.{kind}.args.{name}"
            if not isinstance(arg_spec, dict):
                raise Rejection(f"policy error: scalar spec at {where} is not an object")
            if arg_spec.get("type") not in ("string", "integer", "enum"):
                raise Rejection(
                    f"policy error: unknown scalar type {arg_spec.get('type')!r} at {where}"
                )
            if extra := set(arg_spec) - SCALAR_KEYS[arg_spec["type"]]:
                raise Rejection(
                    f"policy error: scalar spec at {where} carries keys no reader consults "
                    f"{sorted(extra)} (allowed for {arg_spec['type']}: "
                    f"{sorted(SCALAR_KEYS[arg_spec['type']])})"
                )
            check_scalar_spec(arg_spec, where)


def check_plan_schema(candidate, policy_plan: dict) -> None:
    """Raise Rejection on the first structural violation; return None if the
    plan is well-shaped. Shape only: containment (ADR-0005) and markdown are
    separate phases, same as verify.py's schema/provenance split."""
    # The policy this gate is about to interpret must be one it implements, and
    # that is decided before any step is read: a policy fault reported as a bad
    # plan sends a reader to the generator.
    # First of the three: check_reserved_closures and the sweep both index policy
    # keys, so a missing one must be named here rather than raising KeyError there.
    check_plan_policy_keys(policy_plan)
    check_reserved_closures(policy_plan)
    check_plan_arg_specs(policy_plan)
    if not isinstance(candidate, dict):
        raise Rejection("plan: expected a JSON object")

    extra = set(candidate) - PLAN_KEYS
    if extra:
        raise Rejection(f"plan: unexpected keys {sorted(extra)}")
    if "steps" not in candidate:
        raise Rejection("plan: missing steps")

    steps = candidate["steps"]
    if not isinstance(steps, list):
        raise Rejection("plan.steps: expected an array")
    if not steps:
        # An empty plan is not a safe no-op to wave through: something asked
        # for a remediation and nothing would happen, which is a failure the
        # commander has to see rather than a success with no effect.
        raise Rejection("plan.steps: empty, so the plan does nothing")
    if len(steps) > policy_plan["max_steps"]:
        raise Rejection(f"plan.steps: {len(steps)} steps exceeds max_steps {policy_plan['max_steps']}")

    step_kinds = policy_plan["step_kinds"]
    seen_ids = set()

    for index, step in enumerate(steps):
        where = f"plan.steps[{index}]"
        if not isinstance(step, dict):
            raise Rejection(f"{where}: expected an object")

        extra = set(step) - STEP_KEYS
        if extra:
            raise Rejection(f"{where}: unexpected keys {sorted(extra)}")
        missing = STEP_KEYS - set(step)
        if missing:
            raise Rejection(f"{where}: missing keys {sorted(missing)}")

        step_id = step["id"]
        if not isinstance(step_id, str) or not ID_RE.fullmatch(step_id):
            raise Rejection(f"{where}.id: expected a short lowercase identifier, got {step_id!r}")
        if step_id in seen_ids:
            raise Rejection(f"{where}.id: duplicate id {step_id!r}")
        seen_ids.add(step_id)

        kind = step["kind"]
        if not isinstance(kind, str):
            raise Rejection(f"{where}.kind: expected a string")
        if kind not in step_kinds:
            # Allowlist, not denylist: an unknown kind is not a no-op the
            # executor can skip. It is a request the harness does not
            # understand, and the only safe reading of it is to reject the
            # whole plan.
            raise Rejection(
                f"{where}.kind: {kind!r} is not a declared step kind ({', '.join(sorted(step_kinds))})"
            )

        args = step["args"]
        if not isinstance(args, dict):
            raise Rejection(f"{where}.args: expected an object")
        declared = step_kinds[kind]["args"]
        extra = set(args) - set(declared)
        if extra:
            raise Rejection(f"{where}.args: unexpected keys {sorted(extra)}")
        missing = set(declared) - set(args)
        if missing:
            raise Rejection(f"{where}.args: missing keys {sorted(missing)}")

        for arg_name, arg_spec in declared.items():
            value = args[arg_name]
            arg_where = f"{where}.args.{arg_name}"
            # ADR-0004's second closure lands exactly here. An execution-time
            # binding would arrive as {"$ref": "step1.output"} — an object
            # where a scalar is expected — so it rejects today with no
            # per-argument wrapper. Named explicitly because check_scalar's
            # bare "expected string" would send someone hunting a typo
            # instead of reading ADR-0004.
            if isinstance(value, (dict, list)):
                raise Rejection(
                    f"{arg_where}: expected a literal, got {type(value).__name__} — "
                    "argument_forms admits only [\"literal\"], so bindings are not accepted"
                )
            # Before check_scalar, which is total on a surrogate: len works and
            # NFC is a no-op, so the value would pass every declared bound and
            # reach a phase that encodes.
            if isinstance(value, str) and (found := SURROGATE_RE.search(value)):
                raise Rejection(
                    f"{arg_where}: contains an unpaired surrogate (U+{ord(found.group()):04X}) at "
                    f"position {found.start()}, which is not encodable text; a plan argument must "
                    "be a string this gate can write to a file and to the audit log"
                )
            check_scalar(value, arg_spec, arg_where)


# ------------------------------------------------- containment (ADR-0005) ---

# The step kinds whose path is a file the executor would modify. suggest joins
# patch (ADR-0009): an applied suggestion changes the file exactly as a patch
# would, so every containment check binds both. Mirrors proveFrame's filter in
# ts/plan/prove.ts.
ANCHORED_KINDS = ("patch", "suggest")


def glob_to_regexp(pattern: str) -> re.Pattern:
    """The denylist glob: ** spans separators, * does not, a dot is a dot.

    A deliberate port of ts/plan/prove.ts's globToRegExp, NOT fnmatch — fnmatch
    gives ** no special meaning (each * matches across separators, so
    `.github/**` and `.github/*` would silently mean the same thing) and its
    case behavior is platform-dependent. The §17 dotfile defect was a pattern
    enforced exactly as written where the written pattern was wrong; two
    languages enforcing the same policy.json must share one written-down
    semantics, and this is it. Everything that is not a * is a literal.
    """
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i + 1 : i + 2] == "*":
                # `**/` also matches zero directories, so `.github/**` catches
                # `.github/x` and `**/*.pem` catches a top-level `k.pem`.
                if pattern[i + 2 : i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_denylist(path: str, patterns: list[str]) -> str | None:
    """The first denylist pattern `path` matches, or None."""
    for pattern in patterns:
        if glob_to_regexp(pattern).fullmatch(path):
            return pattern
    return None


def tree_content_source(root: Path):
    """A path -> bytes reader confined to `root` — the pr_root quarantine tree,
    which IS the reviewed head, so reading from it is reading at the reviewed
    SHA with no further git plumbing.

    The tree is contributor-authored, so the requirement is stronger than
    confinement: the resolved path must BE the lexical join, false if any
    component is a symlink. An inward-pointing link is refused as well as an
    outward one — the frame and denylist checks are lexical, so they cannot see
    that a declared path names another file's bytes. Raises FileNotFoundError
    either way, so both read as missing to the caller.
    """
    resolved_root = root.resolve()

    def read(path: str) -> bytes:
        lexical = resolved_root / path
        target = lexical.resolve()
        if not target.is_relative_to(resolved_root):
            raise FileNotFoundError(f"{path!r} resolves outside the reviewed tree")
        # resolve() collapses every symlink, so a mismatch means one was present.
        # Compared against the join under the RESOLVED root, or a link on the way
        # to the quarantine (/tmp on macOS) would read as one inside it.
        if target != Path(os.path.normpath(lexical)):
            raise FileNotFoundError(
                f"{path!r} is or traverses a symlink inside the reviewed tree; "
                "the declared path does not name these bytes"
            )
        return target.read_bytes()

    return read


def count_occurrences(content: bytes, needle: bytes) -> int:
    """Every start offset at which `needle` occurs in `content`, INCLUDING
    overlaps.

    `bytes.count` counts non-overlapping occurrences: `aa` in `xaaay` counts 1
    although it begins at two offsets. Anchoring's exactly-once rule is about
    where a write LANDS, and `bytes.replace` picks the first of the overlapping
    pair — so the non-overlapping count would call a genuinely ambiguous write
    unique and hand the executor the guess the rule exists to refuse.
    """
    count = 0
    start = 0
    while (found := content.find(needle, start)) != -1:
        count += 1
        start = found + 1
    return count


def _line_count(text: str) -> int:
    """Lines in a patch fragment: a trailing newline ends the last line rather
    than starting an empty new one, and the empty string is zero lines."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def apply_patch_steps(anchored: list[tuple[int, dict]], content_source) -> dict[str, bytes]:
    """Apply the anchored steps in sequence and return the resulting file bytes.

    THE one model of what patching means, called by the verifier as its anchoring
    phase and by the stacked-PR delivery for the bytes it commits. Shared rather
    than reimplemented because a second replace() model is the divergence the
    chunk B review found eight instances of: the verifier proved a property about
    `original.replace(old, new)` while the write did something subtly else, so the
    bytes that were bounded, denylisted and secret-scanned were not the bytes that
    landed. One function means "verified" and "delivered" cannot come apart.

    Anchoring is part of the contract, not a precondition the caller supplies:
    `old` must byte-match the file at the reviewed SHA — the closest analogue to
    provenance a patch can have (ADR-0005) — RAW bytes against old's UTF-8, with no
    normalization on either side, so an NFD `old` over an NFC file is a fragment the
    model never saw and fails closed. Exactly-once is part of it too: zero matches
    means the model wasn't looking at this file, two means the replacement (and a
    suggestion's placement) is ambiguous, and an ambiguous write is refused rather
    than guessed at. Returning bytes a caller could commit is only safe while those
    rejections travel with them.

    Steps apply IN SEQUENCE, so a later step's anchor is checked against the content
    the earlier steps leave behind, not against the reviewed SHA. The exactly-once
    guarantee is per-step-at-apply-time or it is not a guarantee: two steps on one
    path can each be unique pre-plan while the first makes the second's anchor
    duplicated or absent. BOTH representations must admit the anchor, for different
    reasons — at the reviewed SHA because `old` is proof the model read the file, so
    an anchor matching only text an earlier step INVENTED is not anchored at all;
    against the pending content because that is where the write actually lands.

    Covers suggest as well as patch, so the sequential check is not a patch-only
    property. Two suggestions on one path are refused outright by
    check_plan_cardinality (ADR-0009: a suggestion is independently applicable, so a
    pair can be half-applied), which means pending content only ever differs from
    the original across DIFFERENT paths for suggestions — but the threading is
    deliberate rather than incidental: it is what makes the guarantee hold for
    whatever kind a future policy admits here.

    Takes (index, step) pairs rather than bare steps so every Rejection keeps
    naming plan.steps[N], which is the audit trail's coordinate system. Only paths
    an anchored step touched appear in the result, so the delivery uploads a blob
    per genuinely changed file and nothing else.
    """
    applied: dict[str, bytes] = {}
    for index, step in anchored:
        path = step["args"]["path"]
        where = f"plan.steps[{index}].args.old"
        try:
            original = content_source(path)
        except OSError as exc:
            raise Rejection(f"{where}: cannot read {path!r} at the reviewed SHA: {exc}")
        pending = applied.get(path, original)
        old_bytes = step["args"]["old"].encode("utf-8")

        at_reviewed_sha = count_occurrences(original, old_bytes)
        if at_reviewed_sha == 0:
            raise Rejection(f"{where}: does not byte-match the content of {path!r} at the reviewed SHA")
        if at_reviewed_sha > 1:
            raise Rejection(
                f"{where}: matches {path!r} {at_reviewed_sha} times at the reviewed SHA; "
                "an ambiguous anchor cannot be applied"
            )
        occurrences = count_occurrences(pending, old_bytes)
        if occurrences == 0:
            raise Rejection(
                f"{where}: no longer occurs in {path!r} once the earlier steps in this plan "
                "have applied; an anchor an earlier step destroys cannot be applied"
            )
        if occurrences > 1:
            raise Rejection(
                f"{where}: matches {path!r} {occurrences} times once the earlier steps in this "
                "plan have applied; an ambiguous anchor cannot be applied"
            )
        applied[path] = pending.replace(old_bytes, step["args"]["new"].encode("utf-8"), 1)
    return applied


BRANCH_ARGS = {"push_branch": "name", "open_pr": "branch"}


def check_write_class_targets(plan: dict, policy_plan: dict, head_branch: str | None) -> None:
    """Confine the arguments that decide WHERE a write-class step acts.

    Branches (push_branch.name, open_pr.branch) must sit under
    policy.plan.branch_prefix; a prefix rather than a denylist, so "not the
    default branch" is a property of the name rather than a list to keep current.
    The reviewed PR's own head branch is refused separately because the prefix
    cannot express it — a contributor could name their branch inside the
    namespace (ADR-0009 addendum). Labels must appear on
    policy.plan.label_allowlist exactly.

    `head_branch` is a plan input, not derivable here. The executor supplies it
    from the event (HEAD_REF, required there), which is what makes the refusal
    reachable; the generator lane passes what it has, since an eval fixture has no
    pull request. None means unknown and refuses nothing extra — an allowance for
    a caller with no value, never for one that has one and omits it.
    """
    prefix = policy_plan["branch_prefix"]
    allowed_labels = policy_plan["label_allowlist"]

    for index, step in enumerate(plan["steps"]):
        kind = step["kind"]
        if arg_name := BRANCH_ARGS.get(kind):
            branch = step["args"][arg_name]
            where = f"plan.steps[{index}].args.{arg_name}"
            # Segment-wise, so `smtithy-evil/x` cannot pass as `smtithy/`, and a
            # `..` segment cannot climb out of the namespace it matched.
            if not branch.startswith(prefix) or ".." in branch.split("/"):
                raise Rejection(
                    f"{where}: branch {branch!r} is not under the policy branch_prefix {prefix!r}; "
                    "a plan may only push inside the harness-owned namespace"
                )
            if head_branch is not None and branch == head_branch:
                raise Rejection(
                    f"{where}: branch {branch!r} is the reviewed pull request's own head branch; "
                    "the harness never pushes to the contributor's branch (ADR-0009 addendum)"
                )
        elif kind == "label":
            name = step["args"]["name"]
            if name not in allowed_labels:
                raise Rejection(
                    f"plan.steps[{index}].args.name: label {name!r} is not on the policy "
                    f"label_allowlist ({allowed_labels or 'empty — no label may be applied'})"
                )

    # A relation between two steps, so it cannot live in the loop above: both
    # branches passing confinement independently still leaves them free to name
    # DIFFERENT branches, and the executor would push the verified patch to one
    # and open the follow-up pull request from the other — whose content this plan
    # never described and whose bytes no frame bounded.
    #
    # After the per-step confinement, so an off-namespace branch is still reported
    # as one: that is the worse fault and the one a reader needs named. Only when
    # both values are strings, because shape belongs to the schema phase, and only
    # when both steps exist — cardinality admits a push with no open_pr.
    branches = {
        step["kind"]: step["args"][BRANCH_ARGS[step["kind"]]]
        for step in plan["steps"]
        if step["kind"] in BRANCH_ARGS
    }
    pushed, opened = branches.get("push_branch"), branches.get("open_pr")
    if isinstance(pushed, str) and isinstance(opened, str) and pushed != opened:
        raise Rejection(
            f"plan.steps: open_pr opens from {opened!r} but push_branch pushes {pushed!r}; "
            "the follow-up pull request must open from the branch this plan pushed, or its "
            "content is bytes no step of this plan described"
        )


def check_commanded_scope(plan: dict, commanded_findings: list[dict] | None) -> None:
    """ADR-0007/ADR-0013: the fix must touch the file of EVERY commanded finding.

    This scope was enforced by the PROMPT alone — verify_plan never saw the
    findings. A generator steered by text in the contributor-authored head tree
    (which it may Read in full) into patching the other files a PR changed, and
    none of the finding's own, produced a plan that verified: every path is in
    changed_files, none is denylisted, the chain is ordered. The commander asked
    for auth.py and got settings.py.

    SUBSET, not equality. ADR-0009 adds the stacked pull request precisely for a
    fix that only makes sense applied across several files, so requiring the path
    set to be exactly the commanded paths would refuse the case that ADR exists
    for. A fix touching the commanded files plus others is a judgement a human
    reviews; one that misses any commanded file is not the commanded fix.

    For a single commanded finding this is byte-for-byte the ∈ check it replaces:
    one path, one membership test. That is what makes ADR-0013's widening a
    generalisation of the existing gate rather than a replacement for it — the
    multi-finding case adds conjuncts and weakens none.

    None means no command (the review lane, and every test that passes no
    finding), which refuses nothing extra. A plan with no fix step has no path
    set to compare — check_plan_cardinality is what refuses a plan doing nothing.
    """
    if not commanded_findings:
        return
    paths = {step["args"]["path"] for step in plan["steps"] if step["kind"] in ANCHORED_KINDS}
    if not paths:
        return
    # Sorted and deduplicated so the message is stable and names each missing file
    # once, however many commanded findings share it.
    missing = sorted({
        finding.get("path") for finding in commanded_findings
    } - paths)
    if missing:
        raise Rejection(
            f"plan: the commanded finding(s) are on {missing} but the fix touches "
            f"{sorted(paths)}; a plan that never touches a commanded file is not the "
            "commanded fix (ADR-0013: the command names a set of findings, and the fix "
            "must touch every path they name)"
        )


def check_plan_containment(plan: dict, diff_text: str, changed_files: list[str],
                           policy_plan: dict, content_source, head_branch: str | None = None,
                           commanded_findings: list[dict] | None = None) -> None:
    """ADR-0005: frame, denylist, suggest.line provenance, bounding, anchoring
    (which for a suggest step includes PLACEMENT — that `old` begins at the
    addressed line and ends at a line end, so the anchored region and the region
    GitHub's suggestion block replaces are the same region).

    Phase-by-phase across all steps, first violation wins, mirroring
    verify.py's structure. Anchoring runs LAST because it is the only check
    that touches the filesystem: everything cheaper (and everything decidable
    from data the verifier already holds) fails first, so a file is only ever
    read for a path that already passed the frame and the denylist.
    """
    steps = plan["steps"]
    changed = set(changed_files)
    anchored = [(index, step) for index, step in enumerate(steps) if step["kind"] in ANCHORED_KINDS]

    # Write-class targets first: decidable from the plan alone, and it is the
    # phase that bounds where the write credential is pointed.
    check_write_class_targets(plan, policy_plan, head_branch)

    # Frame: every modified path is a file the PR touched (§20 as written,
    # the Python re-verification of what proveFrame proves — the executor
    # trusts no other job). Exact string identity, same as the prover's
    # intern table: a path that merely shares a prefix is a different file.
    for index, step in anchored:
        path = step["args"]["path"]
        if path not in changed:
            raise Rejection(f"plan.steps[{index}].args.path: {path!r} is not a file this PR touched")

    # Denylist: a narrowing of the already-closed changed_files set (ADR-0005
    # on why a denylist is acceptable here despite allowlisting being the rule
    # everywhere else).
    for index, step in anchored:
        path = step["args"]["path"]
        pattern = matches_denylist(path, policy_plan["path_denylist"])
        if pattern is not None:
            raise Rejection(f"plan.steps[{index}].args.path: {path!r} is on the policy path denylist ({pattern!r})")

    # Scope, after the frame: a path the PR never touched is out of frame
    # whatever was commanded, so that is the reason a reader should get. Here
    # rather than later because it is still decidable from the plan plus the
    # command, before anything is read off disk.
    check_commanded_scope(plan, commanded_findings)

    # suggest.line provenance (ADR-0009 addendum): the same in-hunk check a
    # finding's line gets, against the same SHA-anchored diff, in the verifier
    # rather than as a GitHub 422 in the executor.
    hunks = parse_diff_hunks(diff_text)
    for index, step in anchored:
        if step["kind"] != "suggest":
            continue
        path, line = step["args"]["path"], step["args"]["line"]
        if line not in hunks.get(path, set()):
            raise Rejection(f"plan.steps[{index}].args.line: line {line} of {path!r} is not inside any diff hunk")

    # Bounding. Distinct files across the whole plan; changed lines and changed
    # BYTES per step (ADR-0005 caps "changed lines per patch", and ADR-0009
    # applies the same caps per suggestion), then changed bytes over the plan.
    # Every count is diff --stat's reading: the old side plus the new side.
    #
    # Two dimensions because a line count bounds nothing about line LENGTH: a
    # 20000-character single-line `old` and `new` — a minified or generated line
    # is a plausible real target — scores 2 changed lines and passed a cap of 120
    # while substituting 40 KB. The plan total is separate from the per-step cap
    # because several steps may share one file, so max_patched_files does not
    # bound the sum.
    patched_paths = {step["args"]["path"] for _, step in anchored}
    if len(patched_paths) > policy_plan["max_patched_files"]:
        raise Rejection(
            f"plan: {len(patched_paths)} patched files exceeds max_patched_files "
            f"{policy_plan['max_patched_files']}"
        )
    plan_bytes = 0
    for index, step in anchored:
        changed_lines = _line_count(step["args"]["old"]) + _line_count(step["args"]["new"])
        if changed_lines > policy_plan["max_changed_lines"]:
            raise Rejection(
                f"plan.steps[{index}]: {changed_lines} changed lines exceeds max_changed_lines "
                f"{policy_plan['max_changed_lines']}"
            )
        # UTF-8 bytes, not code points: the budget bounds what reaches the file,
        # and a file holds bytes. Measured in code points a 3-byte code point
        # would cost a third of its real size.
        changed_bytes = len(step["args"]["old"].encode("utf-8")) + len(step["args"]["new"].encode("utf-8"))
        if changed_bytes > policy_plan["max_changed_bytes"]:
            raise Rejection(
                f"plan.steps[{index}]: {changed_bytes} changed bytes exceeds max_changed_bytes "
                f"{policy_plan['max_changed_bytes']}"
            )
        plan_bytes += changed_bytes
    if plan_bytes > policy_plan["max_plan_changed_bytes"]:
        raise Rejection(
            f"plan: {plan_bytes} changed bytes across all steps exceeds max_plan_changed_bytes "
            f"{policy_plan['max_plan_changed_bytes']}"
        )

    # Anchoring, via the applier the DELIVERY also calls (ADR-0005). Shared rather
    # than duplicated here: the stacked PR commits what this returns, and a second
    # replace() model is what the chunk B review found eight instances of. Every
    # anchor rejection — byte-match at the reviewed SHA, exactly-once there and
    # again at apply time — is apply_patch_steps's contract, so it is documented
    # once beside the code that enforces it.
    apply_patch_steps(anchored, content_source)

    # Placement is checked HERE and not in the applier, because it is a provenance
    # claim about the reviewed SHA rather than a fact about applying anything: it
    # asks whether the region GitHub's suggestion block will replace is the region
    # `old` anchored. `original` is therefore read again from the content source —
    # the reviewed-SHA bytes, which is the diff the model read — never the applied
    # result.
    for index, step in anchored:
        if step["kind"] != "suggest":
            continue
        path = step["args"]["path"]
        where = f"plan.steps[{index}].args.old"
        # Already proved readable and unambiguously anchored by the applier above,
        # so this cannot fail where that succeeded.
        original = content_source(path)
        old_bytes = step["args"]["old"].encode("utf-8")

        # Placement (ADR-0009 addendum: "`old` IS the anchored line"). GitHub's
        # suggestion block replaces the commented line range, not the text in
        # `old`, so the two must name the same region. Here rather than in the
        # in-hunk phase above because only the file content decides it.
        offset = original.index(old_bytes)
        if offset > 0 and not original[offset - 1:offset] == b"\n":
            raise Rejection(
                f"{where}: does not start at the beginning of a line in {path!r}; a suggestion "
                "replaces whole lines, so a sub-line anchor cannot describe what it overwrites"
            )
        # And it must END at one. GitHub replaces the whole addressed range, so
        # any byte left on the anchor's last line is overwritten without having
        # been anchored. Three ways to be at a line end and no others: the anchor
        # consumed the terminator, the terminator is the next byte, or the file
        # stops there — a last line with no final newline must still verify.
        end = offset + len(old_bytes)
        at_line_end = (
            old_bytes.endswith(b"\n") or end == len(original) or original[end:end + 1] == b"\n"
        )
        if not at_line_end:
            raise Rejection(
                f"{where}: does not end at the end of a line in {path!r}; a suggestion replaces "
                "whole lines, so the rest of the anchor's last line would be overwritten unanchored"
            )
        start_line = original.count(b"\n", 0, offset) + 1
        line = step["args"]["line"]
        if start_line != line:
            raise Rejection(
                f"plan.steps[{index}].args.line: suggestion addresses line {line} of {path!r} but "
                f"old is anchored at line {start_line}; the addressed and anchored regions must be one"
            )
        # A multi-line `old` has no start_line/end_line pair to declare (ADR-0004
        # keeps the step shape closed), so its extent is derived from the anchor
        # and every line it spans must be in the hunk set — the same provenance
        # the addressed line already got, applied to the whole replaced range.
        end_line = start_line + _line_count(step["args"]["old"]) - 1
        for spanned in range(start_line, end_line + 1):
            if spanned not in hunks.get(path, set()):
                raise Rejection(
                    f"plan.steps[{index}].args.old: spans line {spanned} of {path!r}, which "
                    "is not inside any diff hunk"
                )
        # And `new` must terminate the way the applier will terminate it. A
        # suggestion block's lines are what GitHub commits, and a block line
        # always arrives with its newline — there is no way to express "join this
        # to the following line" in one. So a `new` whose last line drops the
        # terminator `old` carried is proved above as a JOIN (the replace() model)
        # that the applier will not perform: the bytes checked for bounds,
        # denylist and secrets are not the bytes committed. Exempt where `old`
        # ends the file unterminated too, because then there is no following line
        # to join to and the two models agree.
        new = step["args"]["new"]
        if new and not new.endswith("\n") and old_bytes.endswith(b"\n"):
            raise Rejection(
                f"{where.replace('.old', '.new')}: drops the line terminator `old` carried, which "
                "a suggestion block cannot express — its lines are always committed terminated, so "
                "the join this describes would be verified and then not performed"
            )
        # The complementary shape, and the same divergence from the other side. The
        # placement rule above admits an `old` that stops just BEFORE a mid-file
        # terminator without consuming it (its "the terminator is the next byte"
        # arm), and the join rule cannot fire because `old` carries no terminator to
        # drop. The applier then leaves that newline in place — modelling an extra
        # blank line, or an empty one for a deletion — while the suggestion block
        # replaces the whole addressed line and commits neither. Requiring `old` to
        # consume the terminator whenever one follows makes the applier's model and
        # the committed bytes the same object again, which is what ADR-0005's single
        # applier is for. Exempt at end of file, where there is no terminator.
        if not old_bytes.endswith(b"\n") and original[end:end + 1] == b"\n":
            raise Rejection(
                f"{where}: stops just before the line terminator of {path!r} without consuming "
                "it, so the applier would model an extra line where the suggestion block replaces "
                "the whole addressed line — include the terminator in `old`"
            )


# -------------------------------------------------------------- cardinality --


def check_plan_cardinality(plan: dict, policy_plan: dict) -> None:
    """At most one of each write-class kind; at most one suggestion per file; no
    chain at all on a suggest plan.

    Ordering constrains the chain's ORDER, this its COUNT — one commanded finding
    produces one effect. write_kinds is read from the policy rather than listed
    here. A suggest plan is forbidden a chain because ADR-0009 makes a suggestion
    the delivery applied in place, with nothing to push.

    One suggestion per file per finding (ADR-0009), matching GitHub's
    one-hunk-per-suggestion mechanics. A suggestion is INDEPENDENTLY APPLICABLE —
    its own one-click commit, appliable in any subset — so two on one file can be
    half-applied, which is the state ADR-0009's atomicity argument refuses. Patch
    steps are deliberately not bounded this way: they become one atomic commit on
    the stacked branch, so several coordinated hunks in a file are exactly what
    that delivery is for.

    The gate cannot tell a coordinated pair from an independent one — that is a
    judgement about the code, not a property of the plan — so it refuses the
    shape. Here rather than in decide_delivery because this phase is where the
    retry is: a Rejection is feedback the session can act on, while a Refusal at
    delivery time is a run that already spent its budget.

    A plan with no fix step at all is refused for the same reason: it has no path
    set, so check_commanded_scope has nothing to compare and every containment
    check passes vacuously, leaving a fixless write chain (push + open_pr) verified
    whole. decide_delivery refuses it, but a gate that admits a plan whose only
    steps are write-class makes delivery the sole guard on the one shape that
    reaches `contents: write` while remediating nothing.
    """
    kinds = [step["kind"] for step in plan["steps"]]
    step_kinds = policy_plan["step_kinds"]
    write_kinds = [kind for kind, spec in step_kinds.items() if spec["write_class"]]

    if not any(kind in ANCHORED_KINDS for kind in kinds):
        raise Rejection(
            f"plan.steps: no fix step ({' or '.join(ANCHORED_KINDS)}); a plan whose steps are "
            "all delivery scaffolding remediates nothing, and has no path for scope or "
            "containment to check"
        )

    for kind in sorted(write_kinds):
        count = kinds.count(kind)
        if count > 1:
            raise Rejection(
                f"plan.steps: {count} {kind} steps; a write-class kind may appear at most once, "
                "so one commanded finding produces one effect"
            )

    # Grouped by path so the message names the file and both steps: a count alone
    # leaves a reader (and the model) guessing which two.
    by_path: dict[str, list[str]] = {}
    for step in plan["steps"]:
        if step["kind"] == "suggest":
            by_path.setdefault(step["args"]["path"], []).append(step["id"])
    for path, ids in by_path.items():
        if len(ids) > 1:
            raise Rejection(
                f"plan.steps: {len(ids)} suggest steps on {path!r} ({', '.join(repr(i) for i in ids)}); "
                "a suggestion is applied on its own, so two on one file can be half-applied — "
                "coordinated edits go to the stacked pull request as patch steps (ADR-0009)"
            )

    # A suggestion is applied by the contributor, so it needs no branch to push.
    if "suggest" in kinds:
        present = sorted(kind for kind in write_kinds if kind in kinds and kind != "label")
        if present:
            raise Rejection(
                f"plan.steps: a suggest plan carries {present}; a suggestion is applied in place "
                "and has nothing to push"
            )

    if "open_pr" in kinds and "push_branch" not in kinds:
        raise Rejection("plan.steps: open_pr with no push_branch; there is no branch to open it from")


# ----------------------------------------------------------------- ordering --


def check_plan_ordering(plan: dict, policy_plan: dict) -> None:
    """policy.plan.ordering: no `after`-kind step may precede a `before`-kind one.

    ADR-0009's legal write chain (patch → push_branch → open_pr). The Python twin
    of ts/plan/prove.ts proveOrdering, and semantics must stay identical to it:
    pairs at their plan indices, so relative order matters and not adjacency; a
    plan with no orderable pair holds vacuously; first violation wins.
    """
    steps = plan["steps"]
    for rule in policy_plan["ordering"]:
        for j, second in enumerate(steps):
            if second["kind"] != rule["after"]:
                continue
            for i, first in enumerate(steps):
                if i == j or first["kind"] != rule["before"]:
                    continue
                if j < i:
                    raise Rejection(
                        f"plan.steps[{j}]: {second['kind']} ({second['id']!r}) precedes "
                        f"{first['kind']} ({first['id']!r}) at plan.steps[{i}], which the "
                        f"ordering policy forbids"
                    )


# -------------------------------------------------- markdown + secret scan --


def plan_markdown_args(args_spec: dict, kind: str) -> list[str]:
    """Args the policy marks markdown-bearing, with verify.markdown_fields'
    fail-closed rule: a string arg that is neither markdown-checked nor
    pattern-constrained is a policy error — except patch/suggest old and new,
    which are file bytes, never rendered as prose, and gated instead by
    anchoring and the human merge (pinned in TestShippedPolicyAgreement)."""
    fields = []
    for name, spec in args_spec.items():
        if spec.get("markdown"):
            fields.append(name)
        elif spec["type"] == "string" and "pattern" not in spec and name not in ("old", "new"):
            raise Rejection(
                f"policy error: {kind}.{name} is a string arg that is neither "
                "markdown-checked nor pattern-constrained"
            )
    return fields


def _iter_plan_markdown(plan: dict, policy: dict):
    step_kinds = policy["plan"]["step_kinds"]
    for index, step in enumerate(plan["steps"]):
        kind = step["kind"]
        for arg_name in plan_markdown_args(step_kinds[kind]["args"], kind):
            yield f"plan.steps[{index}].args.{arg_name}", step["args"][arg_name]


# The info string that makes a fenced block APPLIABLE rather than quoted. Read as
# the first word, since GitHub takes the language from there and ignores the rest.
_SUGGESTION_INFO = "suggestion"

# Bytes CommonMark rewrites before a renderer ever sees them: CR and CRLF become
# LF, and NUL becomes U+FFFD. Nothing else in the harness cares, because nothing
# else delivers file content THROUGH markdown.
_MANGLED_BY_COMMONMARK = {"\r": "carriage return", "\x00": "NUL"}


def check_suggestion_new_survives_markdown(new: str, where: str) -> None:
    """Refuse a `new` a suggestion block cannot carry unchanged.

    `new` reaches the contributor's file through a markdown code fence, so
    CommonMark's preprocessing is part of this delivery: it normalises CR and CRLF
    to LF and substitutes U+FFFD for NUL. Bytes that do not survive it would be
    proved here — for bounds, the denylist, the secret scan — and then silently
    rewritten on the way in, so what the contributor's click commits is not what
    was checked.

    Refused rather than normalised, ADR-0011's posture: the checked bytes must BE
    the delivered bytes, and quietly folding a CRLF file's line endings to LF
    would rewrite the file's own convention on the model's behalf.

    A `patch` step's `new` is exempt and deliberately not routed here — the
    executor writes that file directly, where a CR is just a byte.
    """
    for char, name in _MANGLED_BY_COMMONMARK.items():
        if char in new:
            raise Rejection(
                f"{where}: a suggestion block cannot carry a {name} — markdown rewrites it "
                "before the applier sees it, so the committed bytes would not be the "
                "checked ones; deliver this as a patch instead"
            )


def check_note_carries_no_suggestion(note: str, where: str) -> None:
    """Refuse a `note` whose own fence GitHub would offer to apply.

    The note is prose, and prose is ALL it is checked as. Every check that makes
    `new` safe to hand a contributor — the byte-match against the reviewed tree,
    hunk containment, the bounding caps, one-suggestion-per-file — binds `new`
    alone. A ```suggestion fence inside the note is therefore an appliable block
    of bytes nothing anchored, bounded or compared, rendered ABOVE the real one
    and so reached first; ADR-0005's human gate stays intact (the contributor
    still clicks) but what the click commits was never verified.

    Not refused for open_pr.body: a suggestion fence in a pull-request body
    applies to nothing, and refusing prose for a hazard the surface does not have
    would be refusing it for the shape of its syntax.
    """
    for info in fence_info_strings(note):
        if info.split()[0] == _SUGGESTION_INFO:
            raise Rejection(
                f"{where}: contains a suggestion block, which GitHub offers to apply — "
                "the note is checked as prose only, so its bytes are anchored against "
                "nothing; the replacement belongs in `new`, which is"
            )


def check_plan_markdown(plan: dict, policy: dict) -> None:
    """Markdown-bearing args (suggest.note, open_pr.body) through the same
    allowlist gate a finding's body gets: they render in a posted comment or
    PR body, so nothing reaches GitHub's renderer that verify.py would not
    have let a review comment carry.

    The note gets one check more than the allowlist, because it lands in the one
    place a fence is more than code — see check_note_carries_no_suggestion.
    """
    for where, value in _iter_plan_markdown(plan, policy):
        check_markdown_field(value, policy["markdown"], where)
    for index, step in enumerate(plan["steps"]):
        if step["kind"] == "suggest":
            check_note_carries_no_suggestion(
                step["args"]["note"], f"plan.steps[{index}].args.note"
            )
            check_suggestion_new_survives_markdown(
                step["args"]["new"], f"plan.steps[{index}].args.new"
            )


def check_plan_secrets(plan: dict, policy: dict) -> None:
    """The whole plan through the secret scan, mirroring check_secrets.

    Four representations, each also scanned invisible-stripped: raw JSON (any
    arg, old and new included), rendered markdown args, and old FUSED with new —
    a rendered suggestion shows those adjacent, so a credential split across the
    boundary reads complete there while neither fragment nor the syntax-separated
    JSON matches.

    Stripping is a scan representation only. ADR-0005's anchor comparison stays
    raw, or an `old` the model never saw verbatim would start matching.

    What this cannot see, recorded so it is not rediscovered as a defect: a
    credential that exists only in the file AFTER a patch applies. The scan reads
    the plan, and the plan's `new` is a fragment — a value completed by the bytes
    already around it in the head tree is not present in any representation here.
    Scanning the applied result would mean scanning contributor content the PR
    already contains, which is the reviewed input rather than something the plan
    introduced. Inherent to anchoring a scan to the plan, not a gap in these four
    representations.
    """
    texts = [json.dumps(plan, ensure_ascii=False)]
    for step in plan["steps"]:
        if step["kind"] in ANCHORED_KINDS:
            texts.append(step["args"]["old"] + step["args"]["new"])
    # Keeping the raw forms alongside means stripping can only ADD matches: it
    # cannot fuse two innocent runs into a false negative.
    texts.extend(strip_invisible(text) for text in list(texts))
    # scanned_representations already carries its own stripped forms, and it is
    # the artifact gate's corpus builder: a markdown arg the two gates scan
    # differently is one credential with two verdicts.
    for _, value in _iter_plan_markdown(plan, policy):
        texts.extend(scanned_representations(value))
    for pattern in policy["secret_scan_patterns"]:
        for text in texts:
            if re.search(pattern, text):
                raise Rejection(f"secret scan: plan content matches pattern {pattern!r}")


# ----------------------------------------------------------------- driver --


def verify_plan(plan: dict, diff_text: str, changed_files: list[str], policy: dict,
                content_source, head_branch: str | None = None,
                commanded_findings: list[dict] | None = None) -> None:
    """Raise Rejection on the first policy violation; return None if verified.

    Mirrors verify()'s phase order (schema, provenance-shaped checks, markdown,
    secrets). `content_source` is a path -> bytes callable for file content at
    the reviewed SHA — production passes tree_content_source(pr_root), tests
    pass whatever mapping the case needs. It is an argument rather than a Path
    because the read discipline (confinement, what counts as missing) is the
    caller's trust decision, and a callable keeps this module free of any
    filesystem assumption beyond it.

    `commanded_findings` are the findings the command names (ADR-0007, ADR-0013),
    making the plan's scope a CHECKED property rather than a prompt instruction.
    None (or empty) means no command and refuses nothing extra.
    """
    check_plan_schema(plan, policy["plan"])
    check_plan_cardinality(plan, policy["plan"])
    check_plan_ordering(plan, policy["plan"])
    check_plan_containment(
        plan, diff_text, changed_files, policy["plan"], content_source, head_branch, commanded_findings
    )
    check_plan_markdown(plan, policy)
    check_plan_secrets(plan, policy)
