"""Workflow-shape assertions: the properties a green suite otherwise cannot see.

Two confirmed findings lived here, and both are invisible to every other test in
this repo because they are facts about YAML, not about Python: the evals job
executed PR-head code with Bedrock credentials without asserting the approval
gate, and it wrote a pip cache into the BASE BRANCH's cache scope under a key the
untrusted PR controlled.

Parsed with a deliberately small hand-rolled reader rather than PyYAML: adding a
dependency to the hash-pinned lockfiles to test three properties is a worse trade
than a parser that only understands the subset these files use. It is not a
general YAML implementation and does not need to be — it reads step lists, step
keys, and scalar values, and every assertion below is written against that.

The rule these encode, from ADR-0006: a job that checks out
`pull_request.head.sha` executes untrusted code, so it must assert the gate from
trusted code BEFORE that checkout, hold no credential before the assertion, and
never write a cache entry keyed on or influenced by the untrusted tree.
"""

from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"


def parse_steps(text: str, job: str) -> list[dict[str, str]]:
    """The `steps:` list of one job, as dicts of top-level scalar keys.

    Nested mappings (a step's `with:` / `env:`) are flattened in as
    ``with.cache``-style dotted keys, which is all the assertions need. Block
    scalars (``run: |``) collapse to their first line plus a marker, since no
    assertion here reads a script body.
    """
    lines = text.splitlines()
    # Find "  <job>:" then its "    steps:" block.
    job_indent = None
    in_job = False
    steps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    step_indent = None
    prefix: list[str] = []

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if not in_job:
            if stripped == f"{job}:":
                in_job, job_indent = True, indent
            continue
        if indent <= job_indent:
            break  # next job

        if stripped == "steps:":
            step_indent = indent
            continue
        if step_indent is None:
            continue

        if stripped.startswith("- ") and indent == step_indent + 2:
            current = {}
            steps.append(current)
            prefix = []
            stripped = stripped[2:]
            indent += 2
        if current is None:
            continue

        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if not value or value in ("|", ">-", ">", "|-"):
            # A nested mapping (with:/env:) or a block scalar.
            prefix = [key] if not value else []
            if value:
                current[".".join([key])] = "<block>"
            continue
        dotted = ".".join(prefix + [key]) if prefix and indent > step_indent + 4 else key
        current[dotted] = value.strip("'\"")
    return steps


@pytest.fixture(scope="module")
def evals_steps():
    return parse_steps((WORKFLOWS / "evals.yml").read_text(), "evals")


def untrusted_checkout_index(steps) -> int:
    """Index of the step checking out the PR's own head, or -1.

    Matched on any key's value, since `ref:` arrives as the dotted `with.ref`.
    """
    for index, step in enumerate(steps):
        if any("pull_request.head.sha" in value for value in step.values()):
            return index
    return -1


class TestParser:
    """The parser is test infrastructure, so it gets its own assertions —
    a reader that silently found nothing would make every check below vacuous."""

    def test_it_finds_the_evals_steps(self, evals_steps):
        assert len(evals_steps) >= 5

    def test_it_finds_the_untrusted_checkout(self, evals_steps):
        assert untrusted_checkout_index(evals_steps) >= 0

    def test_it_reads_nested_with_keys(self, evals_steps):
        setup = [s for s in evals_steps if "setup-python" in s.get("uses", "")]
        assert setup, "no setup-python step found"
        assert any("python-version" in key for step in setup for key in step)


class TestEvalsApprovalGate:
    """ADR-0006: the gate is asserted inside the gated job, from trusted code,
    before any credential exists."""

    def test_the_gate_is_asserted(self, evals_steps):
        assert any(
            "environment_gate.py" in step.get("run", "") for step in evals_steps
        ), "the evals job executes PR-head code with Bedrock credentials and never asserts the gate"

    def test_the_gate_runs_before_the_untrusted_checkout(self, evals_steps):
        gate = next(i for i, s in enumerate(evals_steps) if "environment_gate.py" in s.get("run", ""))
        checkout = untrusted_checkout_index(evals_steps)
        assert gate < checkout, (
            "the gate is asserted after the PR head is on disk; asking the PR's own code "
            "whether the PR was approved is no check at all"
        )

    def test_the_gate_runs_from_a_trusted_checkout(self, evals_steps):
        gate = next(s for s in evals_steps if "environment_gate.py" in s.get("run", ""))
        assert gate.get("working-directory", "").startswith("trusted-base"), (
            "the gate must run from the base-branch checkout, not the workspace root "
            "the PR head is written into"
        )

    def test_the_gate_runs_before_any_credential(self, evals_steps):
        gate = next(i for i, s in enumerate(evals_steps) if "environment_gate.py" in s.get("run", ""))
        creds = [
            i for i, s in enumerate(evals_steps) if "configure-aws-credentials" in s.get("uses", "")
        ]
        assert creds, "no credential step found; this assertion has gone stale"
        assert gate < min(creds), "a credential exists in the job before the gate is asserted"

    def test_the_gate_names_the_environment_and_trust_inputs(self, evals_steps):
        gate = next(s for s in evals_steps if "environment_gate.py" in s.get("run", ""))
        joined = " ".join(f"{k}={v}" for k, v in gate.items())
        assert "ai-pr-review" in joined
        assert "trusted" in joined  # AUTHOR_TRUSTED from eval_author_trust
        assert "draft" in joined.lower()  # PR_DRAFT


class TestNoUntrustedInfluencedCache:
    """A pull_request_target job runs under the BASE ref, so a cache it saves
    lands in the base branch's scope — reachable from main and from every other
    PR. setup-python's cache save is an implicit POST step, so it always runs
    after the untrusted code has executed; reordering steps cannot fix it."""

    def test_the_evals_job_caches_nothing(self, evals_steps):
        for step in evals_steps:
            if "setup-python" in step.get("uses", ""):
                assert not any(key.endswith("cache") for key in step), (
                    "the evals job checks out and executes PR-head code, so any cache entry it "
                    "saves is written into the base branch's cache scope"
                )
                assert not any("cache-dependency-path" in key for key in step)

    def test_any_job_checking_out_pr_head_caches_nothing(self):
        # The general rule, over every workflow: the two findings were one job's
        # instance of it, and a new job would reintroduce it silently.
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text()
            for job in job_names(text):
                steps = parse_steps(text, job)
                if untrusted_checkout_index(steps) < 0:
                    continue
                for step in steps:
                    if "setup-python" not in step.get("uses", ""):
                        continue
                    caching = [key for key in step if key.endswith("cache")]
                    assert not caching, (
                        f"{path.name} job {job!r} checks out pull_request.head.sha and caches "
                        f"({caching}); the entry is saved by an implicit post step into the "
                        "base ref's scope, after the untrusted code has run"
                    )


def job_names(text: str) -> list[str]:
    """Top-level keys under `jobs:` — two-space indented, colon-terminated."""
    names, in_jobs = [], False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.rstrip() == "jobs:":
            in_jobs = True
            continue
        if in_jobs:
            indent = len(raw) - len(raw.lstrip())
            if indent == 0:
                break
            if indent == 2 and raw.rstrip().endswith(":"):
                names.append(raw.strip().rstrip(":"))
    return names


class TestQuarantineIsStrippedOfSymlinks:
    """git materialises mode-120000 entries from the head tree verbatim, and the
    generator is granted Read/Grep/Glob over the quarantine. Stripping happens
    where the tree is materialised; cc_loop asserts it independently."""

    def test_the_quarantine_step_strips_symlinks(self):
        text = (WORKFLOWS / "ai-pr-review.yml").read_text()
        quarantine = text.split("Quarantine-fetch PR head")[1].split("- name:")[0]
        assert "-type l" in quarantine, (
            "the quarantine step does not strip symlinks; a PR can plant a link to "
            "~/.aws/credentials inside a directory the generator may Read"
        )
        assert "-delete" in quarantine


class TestOtherWorkflowsStayCorrect:
    """The two workflows that got this right, pinned so a copy-paste of the
    evals fix does not 'fix' them into something worse."""

    def test_quality_check_is_pull_request_scoped(self):
        text = (WORKFLOWS / "quality_check.yml").read_text()
        assert "pull_request_target" not in text, (
            "quality_check.yml may cache because it is `pull_request` (PR-scoped cache); "
            "switching it to pull_request_target would put its cache in the base scope"
        )

    def test_ai_pr_review_keys_its_cache_on_the_trusted_harness(self):
        steps = parse_steps((WORKFLOWS / "ai-pr-review.yml").read_text(), "review")
        for step in steps:
            for key, value in step.items():
                if "cache-dependency-path" in key:
                    assert value.startswith("harness/"), (
                        "ai-pr-review.yml's cache key must come from the pinned harness "
                        f"checkout, not the consumer's tree; got {value!r}"
                    )
