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
2. ADR-0005's containment (check_plan_containment): frame, denylist,
   suggest.line provenance, bounding, anchoring. Anchoring is why verify_plan
   grows a content source over verify()'s argument list — the ADR calls it
   the largest change to the verifier's shape.
3. Markdown allowlist on the args the policy marks markdown-bearing
   (check_plan_markdown, reusing verify.check_markdown_field).
4. Secret scan over the whole plan, raw and rendered (check_plan_secrets,
   mirroring verify.check_secrets).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from verify import Rejection, check_markdown_field, check_scalar, parse_diff_hunks, rendered_markdown

# Ids exist so steps can be referred to; a duplicate makes a reference
# ambiguous, and a counterexample naming a step becomes unactionable. Kept
# conservative: this is what appears in audit output. Same expression as
# ts/plan/schema.ts's ID_RE — the two must agree or a plan can pass one gate
# and fail the other on shape alone.
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")

PLAN_KEYS = frozenset({"steps"})
STEP_KEYS = frozenset({"id", "kind", "args"})


def check_plan_schema(candidate, policy_plan: dict) -> None:
    """Raise Rejection on the first structural violation; return None if the
    plan is well-shaped. Shape only: containment (ADR-0005) and markdown are
    separate phases, same as verify.py's schema/provenance split."""
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

    Confinement is checked on the RESOLVED path: the tree is contributor-
    authored data, so a symlink inside it pointing outside is an expected
    hostile shape, and it reads as missing rather than following the link out.
    """
    resolved_root = root.resolve()

    def read(path: str) -> bytes:
        target = (resolved_root / path).resolve()
        if not target.is_relative_to(resolved_root):
            raise FileNotFoundError(f"{path!r} resolves outside the reviewed tree")
        return target.read_bytes()

    return read


def _line_count(text: str) -> int:
    """Lines in a patch fragment: a trailing newline ends the last line rather
    than starting an empty new one, and the empty string is zero lines."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def check_plan_containment(plan: dict, diff_text: str, changed_files: list[str],
                           policy_plan: dict, content_source) -> None:
    """ADR-0005: frame, denylist, suggest.line provenance, bounding, anchoring.

    Phase-by-phase across all steps, first violation wins, mirroring
    verify.py's structure. Anchoring runs LAST because it is the only check
    that touches the filesystem: everything cheaper (and everything decidable
    from data the verifier already holds) fails first, so a file is only ever
    read for a path that already passed the frame and the denylist.
    """
    steps = plan["steps"]
    changed = set(changed_files)
    anchored = [(index, step) for index, step in enumerate(steps) if step["kind"] in ANCHORED_KINDS]

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

    # Bounding. Distinct files across the whole plan; changed lines PER STEP
    # (ADR-0005 caps "changed lines per patch", and ADR-0009 applies the same
    # caps per suggestion). A step's changed-line count is diff --stat's:
    # lines removed plus lines added.
    patched_paths = {step["args"]["path"] for _, step in anchored}
    if len(patched_paths) > policy_plan["max_patched_files"]:
        raise Rejection(
            f"plan: {len(patched_paths)} patched files exceeds max_patched_files "
            f"{policy_plan['max_patched_files']}"
        )
    for index, step in anchored:
        changed_lines = _line_count(step["args"]["old"]) + _line_count(step["args"]["new"])
        if changed_lines > policy_plan["max_changed_lines"]:
            raise Rejection(
                f"plan.steps[{index}]: {changed_lines} changed lines exceeds max_changed_lines "
                f"{policy_plan['max_changed_lines']}"
            )

    # Anchoring: old must byte-match the file at the reviewed SHA — the
    # closest analogue to provenance a patch can have (ADR-0005). RAW bytes
    # against old's UTF-8, no normalization on either side: an NFD `old` over
    # an NFC file is a fragment the model never saw, and it fails closed.
    # Exactly-once is part of the anchor: zero matches means the model wasn't
    # looking at this file, two means the executor's replacement (and a
    # suggestion's placement) is ambiguous, and an ambiguous write is refused
    # rather than guessed at.
    for index, step in anchored:
        path = step["args"]["path"]
        where = f"plan.steps[{index}].args.old"
        try:
            content = content_source(path)
        except OSError as exc:
            raise Rejection(f"{where}: cannot read {path!r} at the reviewed SHA: {exc}")
        occurrences = content.count(step["args"]["old"].encode("utf-8"))
        if occurrences == 0:
            raise Rejection(f"{where}: does not byte-match the content of {path!r} at the reviewed SHA")
        if occurrences > 1:
            raise Rejection(
                f"{where}: matches {path!r} {occurrences} times; an ambiguous anchor cannot be applied"
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


def check_plan_markdown(plan: dict, policy: dict) -> None:
    """Markdown-bearing args (suggest.note, open_pr.body) through the same
    allowlist gate a finding's body gets: they render in a posted comment or
    PR body, so nothing reaches GitHub's renderer that verify.py would not
    have let a review comment carry."""
    for where, value in _iter_plan_markdown(plan, policy):
        check_markdown_field(value, policy["markdown"], where)


def check_plan_secrets(plan: dict, policy: dict) -> None:
    """The whole plan through the secret scan, raw and rendered, mirroring
    check_secrets. Raw JSON catches a secret in any arg (old and new
    included); rendered text catches one reassembled by markdown formatting.
    The third representation is patch/suggest old FUSED with new: a rendered
    suggestion shows old and new adjacent, so a credential split across the
    boundary reads complete there while neither the raw JSON (which separates
    them with syntax) nor either fragment alone ever matches."""
    texts = [json.dumps(plan, ensure_ascii=False)]
    for _, value in _iter_plan_markdown(plan, policy):
        texts.append(rendered_markdown(value))
    for step in plan["steps"]:
        if step["kind"] in ANCHORED_KINDS:
            texts.append(step["args"]["old"] + step["args"]["new"])
    for pattern in policy["secret_scan_patterns"]:
        for text in texts:
            if re.search(pattern, text):
                raise Rejection(f"secret scan: plan content matches pattern {pattern!r}")


# ----------------------------------------------------------------- driver --


def verify_plan(plan: dict, diff_text: str, changed_files: list[str], policy: dict,
                content_source) -> None:
    """Raise Rejection on the first policy violation; return None if verified.

    Mirrors verify()'s phase order (schema, provenance-shaped checks, markdown,
    secrets). `content_source` is a path -> bytes callable for file content at
    the reviewed SHA — production passes tree_content_source(pr_root), tests
    pass whatever mapping the case needs. It is an argument rather than a Path
    because the read discipline (confinement, what counts as missing) is the
    caller's trust decision, and a callable keeps this module free of any
    filesystem assumption beyond it.
    """
    check_plan_schema(plan, policy["plan"])
    check_plan_containment(plan, diff_text, changed_files, policy["plan"], content_source)
    check_plan_markdown(plan, policy)
    check_plan_secrets(plan, policy)
