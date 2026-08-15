"""Workflow-shape assertions: properties that are facts about YAML, not Python,
and so invisible to every other test here.

The rule, from ADR-0006: a job checking out `pull_request.head.sha` executes
untrusted code, so it must assert the gate from trusted code BEFORE that
checkout, hold no credential before the assertion, and never write a cache entry
the untrusted tree influenced.

Hand-parsed rather than adding PyYAML to the hash-pinned lockfiles for three
properties. Not a general YAML implementation: it reads step lists, step keys and
scalar values, which is all the assertions below use.
"""

import re
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


def job_condition(text: str, job: str) -> str:
    """One job's `if:` expression, whitespace-collapsed.

    Block scalars (`if: >-`) are the norm for these, so the continuation lines
    are joined; the assertions below test for substrings, not layout. A block
    ends at the first line back at the key's own indentation.
    """
    indent = None
    key_indent = None
    parts: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent is None:
            if stripped == f"{job}:":
                indent = current
            continue
        if current <= indent:
            break
        if key_indent is not None:
            if current <= key_indent:
                break
            parts.append(stripped)
            continue
        if stripped.startswith("if:"):
            inline = stripped[3:].strip()
            if inline in (">-", ">", "|", "|-"):
                key_indent = current
            else:
                return " ".join(inline.split())
    return " ".join(" ".join(parts).split())


def job_block(text: str, job: str) -> str:
    """One job's whole body, comments stripped.

    For properties about what a job does NOT contain — a permission scope, a
    credential step. Comments are dropped because this file's workflows document
    the very things these assertions forbid, and a prose mention must not read as
    the thing itself.
    """
    indent = None
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = len(raw) - len(raw.lstrip())
        if indent is None:
            if raw.strip() == f"{job}:":
                indent = current
            continue
        if current <= indent:
            break
        lines.append(raw)
    return "\n".join(lines)


def job_needs(text: str, job: str) -> list[str]:
    """One job's `needs:` as a list, in the order declared.

    Both spellings: the inline `needs: [a, b]` flow sequence this file's workflows
    use, and a bare `needs: a`. Job-level only, at the job's own key depth, so a
    step's keys cannot be mistaken for it.
    """
    indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent is None:
            if stripped == f"{job}:":
                indent = current
            continue
        if current <= indent:
            break
        if stripped.startswith("needs:"):
            value = stripped[len("needs:"):].strip()
            return [name.strip() for name in value.strip("[]").split(",") if name.strip()]
    return []


def job_environment(text: str, job: str) -> str | None:
    """One job's `environment:` value, or None.

    Job-level only: a `steps:` entry is more deeply indented and a step has no
    environment key, so the first match at the job's own key depth is the job's.
    """
    indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent is None:
            if stripped == f"{job}:":
                indent = current
            continue
        if current <= indent:
            break
        if stripped.startswith("environment:"):
            return stripped[len("environment:"):].strip()
    return None


def workflow_input(text: str, name: str) -> dict[str, str]:
    """One `workflow_call` input's own scalar keys, as declared.

    Block scalars (`description: >-`) collapse to their marker, since no assertion
    here reads prose — `type` and `default` are what a consumer's value is bound by.
    """
    inputs_indent = None
    input_indent = None
    keys: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if inputs_indent is None:
            if stripped == "inputs:":
                inputs_indent = current
            continue
        if current <= inputs_indent:
            break  # past the inputs block (secrets:, permissions:, ...)
        if input_indent is None:
            if stripped == f"{name}:":
                input_indent = current
            continue
        if current <= input_indent:
            break  # the next input
        key, _, value = stripped.partition(":")
        keys[key.strip()] = value.strip()
    return keys


def github_env_writes(block: str) -> set[str]:
    """Every name a job's steps export to $GITHUB_ENV.

    The reverse direction of a wiring pin needs the whole set and not a membership
    test: an exported name no module reads is either dead or a name something reads
    under a different spelling, and both are silent.
    """
    return set(re.findall(r"([A-Z_][A-Z0-9_]*)=[^\n]*>> \"\$GITHUB_ENV\"", block))


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

    def test_the_trusted_checkout_holds_trusted_code(self, evals_steps):
        # Every assertion above locates the gate by the PATH it runs from, and
        # none establishes what that path CONTAINS. Adding
        # `repository: ...head.repo.full_name` + `ref: ...head.ref` to the
        # trusted checkout leaves all of them passing while trusted-base/ holds
        # the fork's own code — so the gate deciding whether the PR was approved
        # is code the PR supplied.
        #
        # Asserted as the ABSENCE of head references, not as the presence of
        # `repository:`/`ref:`: the correct workflow supplies neither, defaulting
        # to the base branch, so requiring them would fail against green code and
        # pressure someone into editing the workflow to satisfy a test.
        gate = next(s for s in evals_steps if "environment_gate.py" in s.get("run", ""))
        workdir = gate["working-directory"].split("/")[0]
        checkouts = [
            s for s in evals_steps
            if "actions/checkout" in s.get("uses", "")
            and s.get("with.path", "").split("/")[0] == workdir
        ]
        assert checkouts, f"no checkout writes into {workdir}, so this assertion has gone stale"
        for step in checkouts:
            named = {k: v for k, v in step.items() if "pull_request.head" in str(v)}
            assert not named, (
                f"the checkout supplying {workdir} names the PR head in {sorted(named)}; "
                "the gate would be asserted by code the pull request controls"
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


class TestTheGateJobWaitsAtTheEnvironmentTheWorkerVerifies:
    """The in-code assertion and the job that actually waits must name ONE
    environment.

    environment_gate.py asks whether GATE_ENVIRONMENT carries required
    reviewers. It cannot ask whether THIS run waited at one -- so if the gate
    job's `environment:` is some other environment, the check passes on the
    reviewed environment's rules while the run waited at an environment with
    none. That is failure 4 of the ADR-0006 addendum, and every step-ordering
    assertion in this file stays green through it: they read the worker's step
    list, and this is a fact about a different job.
    """

    # (workflow, gate job, worker job). The worker is where GATE_ENVIRONMENT is
    # set; the gate job is the one whose `environment:` makes the run wait.
    LANES = [
        ("evals.yml", "eval_approve", "evals"),
        ("ai-pr-review.yml", "approve", "review"),
        ("ai-pr-fix.yml", "approve", "plan"),
    ]

    def test_every_gated_lane_is_listed(self):
        # This list is hand-maintained, and a lane missing from it is a lane whose
        # gate nothing checks — silently, since every assertion below is
        # parametrised over the list itself. So the list is checked against the
        # workflows: any job asserting a GATE_ENVIRONMENT must appear here as a
        # worker. That is how a new credential-bearing lane gets this coverage by
        # existing rather than by being remembered.
        found = set()
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text()
            for job in job_names(text):
                if any(key.endswith("GATE_ENVIRONMENT") for step in parse_steps(text, job) for key in step):
                    found.add((path.name, job))
        listed = {(workflow, worker) for workflow, _gate, worker in self.LANES}
        assert found == listed, (
            f"lanes asserting a gate but not listed here: {sorted(found - listed)}; "
            f"listed but no longer asserting one: {sorted(listed - found)}"
        )

    @pytest.mark.parametrize("workflow,gate_job,worker_job", LANES)
    def test_the_gate_job_environment_is_the_asserted_one(self, workflow, gate_job, worker_job):
        text = (WORKFLOWS / workflow).read_text()
        declared = [
            step[key]
            for step in parse_steps(text, worker_job)
            for key in step
            if key.endswith("GATE_ENVIRONMENT")
        ]
        assert declared, f"{workflow} job {worker_job!r} sets no GATE_ENVIRONMENT"
        waited_at = job_environment(text, gate_job)
        assert waited_at == declared[0], (
            f"{workflow}: {worker_job!r} asserts protection rules on {declared[0]!r} while "
            f"{gate_job!r} waits at {waited_at!r}. A run can then satisfy needs.{gate_job}.result "
            "without any human having approved it, and the in-code gate still passes."
        )

    @pytest.mark.parametrize("workflow,gate_job,worker_job", LANES)
    def test_the_worker_does_not_wait_at_the_gate_environment(self, workflow, gate_job, worker_job):
        # The two must differ: the worker holds the credential, so if it waited
        # at the reviewed environment every run would need a second approval,
        # and a maintainer would "fix" that by loosening the gate's rules.
        text = (WORKFLOWS / workflow).read_text()
        assert job_environment(text, worker_job) != job_environment(text, gate_job)

    # ADR-0006's gate is asserted INSIDE the gated job and, per its own comment,
    # "before any credential exists in it". Deleting the step is caught by
    # test_every_gated_lane_is_listed; RELOCATING it was caught by nothing — the
    # whole suite stayed green with the gate moved below the quarantine fetch, below
    # configure-aws-credentials, and even below the generator itself. These are the
    # two assertions TestEvalsApprovalGate already makes, parametrised over every
    # lane so a new one gets them by existing rather than by being remembered.

    def gate_index(self, steps, workflow, worker_job):
        for index, step in enumerate(steps):
            if "environment_gate.py" in step.get("run", ""):
                return index
        raise AssertionError(f"{workflow} job {worker_job!r} never runs environment_gate.py")

    @pytest.mark.parametrize("workflow,gate_job,worker_job", LANES)
    def test_the_gate_runs_before_any_untrusted_content(self, workflow, gate_job, worker_job):
        # The agent lanes never CHECK OUT the head — they quarantine-fetch it as
        # bytes — so the untrusted content arrives by a different step than the
        # evals lane's checkout. Either way it must not be on disk before the gate:
        # asking a pull request's own content whether it was approved is no check.
        text = (WORKFLOWS / workflow).read_text()
        steps = parse_steps(text, worker_job)
        gate = self.gate_index(steps, workflow, worker_job)
        untrusted = [
            index for index, step in enumerate(steps)
            if "quarantine" in step.get("name", "").lower()
            or any("pull_request.head.sha" in str(value) for value in step.values())
        ]
        assert untrusted, f"{workflow} job {worker_job!r}: no untrusted content step found"
        assert gate < min(untrusted), (
            f"{workflow}: the gate is asserted at step {gate}, after untrusted content "
            f"lands at step {min(untrusted)}"
        )

    @pytest.mark.parametrize("workflow,gate_job,worker_job", LANES)
    def test_the_gate_runs_before_any_credential(self, workflow, gate_job, worker_job):
        # A credential minted before the gate is a credential an ungated run held,
        # whatever the gate then decides.
        text = (WORKFLOWS / workflow).read_text()
        steps = parse_steps(text, worker_job)
        gate = self.gate_index(steps, workflow, worker_job)
        credentials = [
            index for index, step in enumerate(steps)
            if "configure-aws-credentials" in step.get("uses", "")
            or any(key.endswith(("ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY")) for key in step)
        ]
        assert credentials, f"{workflow} job {worker_job!r}: no credential step found"
        assert gate < min(credentials), (
            f"{workflow}: the gate is asserted at step {gate}, after a credential is "
            f"minted at step {min(credentials)}"
        )


class TestNoUntrustedInfluencedCache:
    """A pull_request_target job runs under the BASE ref, so a cache it saves
    lands in the base branch's scope. The save is an implicit POST step that runs
    after the untrusted code, so reordering steps cannot fix it."""

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
    """Stripping happens where the tree is materialised; cc_loop asserts it
    independently."""

    def test_the_tree_is_capped_before_it_is_materialised(self):
        # prepare_context caps the head tree's blob and aggregate size from the
        # tree API. That only bounds the checkout if it runs FIRST: reordered,
        # the fetch has already pulled the bytes the cap exists to refuse.
        steps = parse_steps((WORKFLOWS / "ai-pr-review.yml").read_text(), "review")
        names = [step.get("name", "") for step in steps]
        context = next(i for i, n in enumerate(names) if "SHA-anchored context" in n)
        quarantine = next(i for i, n in enumerate(names) if "Quarantine-fetch" in n)
        assert context < quarantine, (
            "the quarantine fetch runs before the head-tree size cap, so an oversized tree is "
            "transferred before anything refuses it"
        )

    def test_the_quarantine_step_strips_symlinks(self):
        text = (WORKFLOWS / "ai-pr-review.yml").read_text()
        quarantine = text.split("Quarantine-fetch PR head")[1].split("- name:")[0]
        assert "-type l" in quarantine, (
            "the quarantine step does not strip symlinks; a PR can plant a link to "
            "~/.aws/credentials inside a directory the generator may Read"
        )
        assert "-delete" in quarantine


class TestNoOidcMintingWhereUntrustedCodeRuns:
    """`id-token: write` injects ACTIONS_ID_TOKEN_REQUEST_URL and
    ...REQUEST_TOKEN into every step of the job, and the evals job EXECUTES the
    PR's own code. Those two are a standing capability to mint a fresh OIDC
    token, so PR-controlled code can assume the role again with NO inline session
    policy — obtaining whatever the role's identity policy permits and bypassing
    the Bedrock-only bound the credential step applies.

    smtithy's own role grants nothing but two Bedrock actions, so nothing leaks
    here today. A consumer supplying a wider role (relying on the workflow's
    inline policy, as its comment invites) is the exposure. The capability is
    only needed by configure-aws-credentials, which runs before the PR's code, so
    shadowing the pair for the steps that run it costs nothing.
    """

    MINTING = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")

    def eval_running_steps(self, steps):
        """The steps that execute PR-head code: everything from the untrusted
        checkout onward that runs something."""
        checkout = untrusted_checkout_index(steps)
        assert checkout >= 0, "no untrusted checkout found; this assertion has gone stale"
        return [s for s in steps[checkout:] if "run" in s]

    def test_the_steps_running_pr_code_shadow_the_minting_pair(self, evals_steps):
        for step in self.eval_running_steps(evals_steps):
            for name in self.MINTING:
                key = f"env.{name}"
                assert key in step, (
                    f"step {step.get('name')!r} runs PR-head code with {name} still in scope, so "
                    "that code can mint another OIDC token and re-assume the role with no session "
                    "policy"
                )
                assert step[key] in ("", "''", '""'), (
                    f"step {step.get('name')!r} sets {name} to {step[key]!r}; it must be shadowed empty"
                )

    def test_the_credential_step_itself_is_not_shadowed(self, evals_steps):
        # The one step that NEEDS the pair. Shadowing it too would leave the role
        # unassumable and the whole suite failing on a 403 — a fail-closed
        # outcome, but the wrong one, and the shape a copy-paste of the env block
        # onto every step would produce.
        creds = next(s for s in evals_steps if "configure-aws-credentials" in s.get("uses", ""))
        for name in self.MINTING:
            assert f"env.{name}" not in creds

    def test_the_shadowing_is_per_step_not_job_wide(self, evals_steps):
        # A job-level `env:` would also cover the credential step, so the pair is
        # shadowed on the steps that run PR code and nowhere else. Asserted here
        # because a reader's obvious simplification breaks the job.
        shadowed = [s.get("name") for s in evals_steps if any(f"env.{n}" in s for n in self.MINTING)]
        assert len(shadowed) >= 3, f"expected every PR-code step shadowed, got {shadowed}"


class TestDraftSemanticsAgree:
    """ADR-0008: "untrusted and draft authors wait at the environment's required
    reviewer before their code runs with the credential, whatever the trigger."
    So a draft is EVALUATED behind the gate.

    Two ways to break that, both of which the code had. A gate job that fires
    for a draft while the worker it gates excludes drafts parks an approval
    request that can produce no run — ADR-0006 addendum §4: "a gate that fires
    for everyone carries no information about anyone", and the cost lands on the
    routine path, which is where people learn to click without reading. And
    excluding drafts at all leaves the draft-to-ready transition covered by
    nothing, since every run while the PR was a draft was skipped.
    """

    GATED = [
        ("evals.yml", "eval_approve", "evals"),
        ("ai-pr-review.yml", "approve", "review"),
    ]

    @pytest.mark.parametrize(("workflow", "gate", "worker"), GATED)
    def test_a_gate_that_fires_for_drafts_gates_a_job_that_runs_them(self, workflow, gate, worker):
        text = (WORKFLOWS / workflow).read_text()
        gate_asks_for_drafts = "pull_request.draft" in job_condition(text, gate)
        worker_excludes_drafts = "!github.event.pull_request.draft" in job_condition(text, worker)
        assert not (gate_asks_for_drafts and worker_excludes_drafts), (
            f"{workflow}: {gate!r} requests approval for a draft that {worker!r} will skip "
            "regardless of what the reviewer clicks"
        )

    @pytest.mark.parametrize(("workflow", "gate", "worker"), GATED)
    def test_the_worker_does_not_exclude_drafts(self, workflow, gate, worker):
        # The other half: with drafts excluded, the ready_for_review transition
        # would need its own trigger, because every draft-era run was skipped.
        text = (WORKFLOWS / workflow).read_text()
        assert "!github.event.pull_request.draft" not in job_condition(text, worker), (
            f"{workflow}: {worker!r} excludes drafts, so a PR whose last push happened while it "
            "was a draft merges with no coverage (ADR-0008 says drafts run behind the gate)"
        )

    def test_every_draft_push_creates_a_run(self):
        # What closes the draft-to-ready coverage hole. With drafts evaluated,
        # `opened` covers the first commit and `synchronize` every push after,
        # so the state at the moment a PR leaves draft has always been graded
        # and no `ready_for_review` trigger is needed. Remove either type and
        # the hole reopens.
        text = (WORKFLOWS / "evals.yml").read_text()
        types = next(line for line in text.splitlines() if "types:" in line)
        assert "opened" in types and "synchronize" in types, (
            f"evals.yml must run on opened and synchronize or a draft's pushes go ungraded; got {types!r}"
        )

    @pytest.mark.parametrize(("workflow", "gate", "worker"), GATED)
    def test_a_draft_still_waits_for_a_human(self, workflow, gate, worker):
        # Evaluating drafts must not mean evaluating them unapproved: a draft
        # from a fork author executes PR-head code against a live credential.
        text = (WORKFLOWS / workflow).read_text()
        assert "pull_request.draft" in job_condition(text, gate), (
            f"{workflow}: {gate!r} no longer requests approval for drafts, so a draft would run "
            "PR-head code with no human gate"
        )


class TestTheCommandLaneHoldsNoCredential:
    """`issue_comment` runs the DEFAULT BRANCH's workflow and always carries a
    write token, whoever commented — the same trap as pull_request_target, reached
    from a channel anyone can write to.

    So the job that decides whether a command is honoured must hold neither a
    model credential nor write scope: every refusal (not a command, commander
    untrusted, no posted review, drift) has to land before either exists.
    """

    FIX = "ai-pr-fix.yml"

    def test_the_command_job_has_no_write_scope(self):
        text = (WORKFLOWS / self.FIX).read_text()
        block = job_block(text, "command")
        for scope in ("pull-requests: write", "contents: write", "id-token: write"):
            assert scope not in block, (
                f"the command job holds {scope!r}; it runs on an event anyone can trigger and "
                "decides whether the command is honoured at all"
            )

    def test_the_command_job_mints_no_model_credential(self):
        block = job_block((WORKFLOWS / self.FIX).read_text(), "command")
        assert "configure-aws-credentials" not in block
        assert "ANTHROPIC_API_KEY" not in block

    def test_the_comment_body_is_never_interpolated_into_a_shell(self):
        # A comment body is attacker-authored free text. Inside a `run:` block,
        # `${{ github.event.comment.body }}` is substituted before the shell sees
        # it, so a body containing $(...) executes in the runner. It reaches the
        # parser through env instead — and the same holds for the author login.
        text = (WORKFLOWS / self.FIX).read_text()
        in_run = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("run:"):
                in_run = True
            elif stripped.startswith("- name:") or stripped.startswith("env:"):
                in_run = False
            if in_run:
                assert "github.event.comment" not in raw, (
                    f"the comment body is interpolated into a run: block ({stripped!r}); "
                    "it is attacker-authored text and would be shell-expanded"
                )

    def test_only_the_two_delivery_jobs_and_the_decline_write(self):
        # Two deliveries need two credentials (ADR-0009: suggestions need only
        # pull-requests: write, a stacked PR also needs contents: write), and
        # ADR-0014 adds a third writer that delivers nothing: `decline` posts the
        # harness's reply to a command it will not perform. Hand-maintained on
        # purpose — a FOURTH writer appearing is a named decision, not a quiet one,
        # and this list is exactly where that decision gets made.
        text = (WORKFLOWS / self.FIX).read_text()
        writers = [job for job in job_names(text) if "pull-requests: write" in job_block(text, job)]
        assert writers == ["execute", "stack", "decline"], (
            f"only the delivery jobs and the decline may hold a write token, got {writers}"
        )

    def test_contents_write_lives_in_exactly_one_job(self):
        # The scope that can push a branch. ADR-0009 records with approval that
        # suggestions made the write scope SHRINK for the common case, and this is
        # what keeps that true: a suggestion run must never mint it.
        text = (WORKFLOWS / self.FIX).read_text()
        holders = [job for job in job_names(text) if "contents: write" in job_block(text, job)]
        assert holders == ["stack"], (
            f"contents: write must live only in the stacked-PR job, got {holders}"
        )

    def test_the_suggestion_job_cannot_push_a_branch(self):
        # The shrink, asserted from the other side.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "execute")
        assert "contents: write" not in block

    def test_the_router_holds_no_credential(self):
        # It reads an UNVERIFIED plan and its output selects which credential gets
        # minted, so it must hold none itself: no write scope, no model key, no
        # token beyond the runtime's own artifact access.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "route")
        assert "permissions: {}" in block
        for scope in ("contents: write", "pull-requests: write", "id-token: write"):
            assert scope not in block, f"the router holds {scope!r}; it decides which job writes"
        assert "configure-aws-credentials" not in block
        assert "ANTHROPIC_API_KEY" not in block

    def test_each_delivery_job_declares_the_mode_it_may_deliver(self):
        # --allow is what bounds the router's unverified input to "which job
        # starts": a plan misrepresenting its mode reaches a job that refuses it
        # after verification. Without it on BOTH jobs, the concession is unbounded
        # in one direction.
        text = (WORKFLOWS / self.FIX).read_text()
        assert "--allow suggestions" in job_block(text, "execute"), (
            "the suggestion job must refuse to deliver a stacked PR"
        )
        assert "--allow stacked_pr" in job_block(text, "stack"), (
            "the stacked-PR job must refuse to deliver suggestions even though its "
            "token could"
        )

    def test_the_delivery_jobs_are_mutually_exclusive(self):
        # One command, one effect. Both jobs gate on the router's mode, so a plan
        # cannot be delivered twice -- and the two deliveries are alternatives, not
        # a fallback chain.
        text = (WORKFLOWS / self.FIX).read_text()
        assert "mode == 'suggestions'" in job_condition(text, "execute")
        assert "mode == 'stacked_pr'" in job_condition(text, "stack")

    def test_a_router_that_fails_delivers_nothing(self):
        # Both jobs carry always(), so they are REACHED when the router fails; what
        # stops them is that each requires an EQUALITY against a specific mode, and
        # a failed router emits no output at all. Asserted because the fail-closed
        # direction here is a property of how the condition is written: a
        # `!= 'stacked_pr'` spelling on the suggestion job would read as equivalent
        # and would deliver on an empty mode.
        text = (WORKFLOWS / self.FIX).read_text()
        for job, mode in (("execute", "suggestions"), ("stack", "stacked_pr")):
            condition = job_condition(text, job)
            assert f"== '{mode}'" in condition, (
                f"{job} must require the mode to EQUAL {mode!r}; a negated condition would "
                "run when the router produced no mode at all"
            )
            assert "!=" not in condition.split("mode")[-1], (
                f"{job} gates on a negation, so an absent mode would deliver"
            )

    def test_the_stacked_job_runs_its_own_full_gate(self):
        # It holds the broadest credential in the lane, so it must not trust
        # another job's verification: the posture execute_plan.py is built on. A
        # thin writer here would put contents: write behind the weakest link.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "stack")
        assert "Quarantine-fetch PR head" in block, "the anchor tree is this job's own fetch"
        assert "execute_plan.py" in block, "verification happens in the job that writes"

    def test_the_plan_job_holds_no_write_scope(self):
        # The generator reads contributor-authored content with a model credential
        # in scope. It must not also be able to write: that is the split the review
        # lane makes between `review` and `post`.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "plan")
        assert "pull-requests: write" not in block
        assert "contents: write" not in block

    def test_the_command_job_can_read_the_review_artifact(self):
        # review.json comes from the review run's Actions artifact, which needs
        # `actions: read`. Without it the lane cannot derive the commanded finding
        # at all, and the failure would look like an expired artifact.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "command")
        assert "actions: read" in block

    def test_the_lane_is_serialized_but_not_superseded(self):
        # cancel-in-progress must be FALSE here where the review lane sets it true:
        # a command is a discrete human instruction, so two maintainers commanding
        # different findings must not have the first cancelled by the second.
        text = (WORKFLOWS / self.FIX).read_text()
        assert "cancel-in-progress: false" in text, (
            "the fix lane must not cancel a running command; newer-push-wins is the review "
            "lane's rule and would silently discard a maintainer's instruction"
        )


class TestTheDeclineLaneRepliesWithoutWideningTheCommandJob:
    """ADR-0014: a decline is a reply from the command channel, and the whole
    decision turns on WHERE the write scope for it lives.

    `command` is reached directly from `issue_comment`, upstream of the trust check,
    on a channel anyone can write to. A `pull-requests: write` there is one minted
    for anybody who types `/fix 1`, whatever it is used for — so the credential-free
    job DERIVES the refusal and a fourth job posts it, which is `route` →
    `execute`/`stack` reused.
    """

    FIX = "ai-pr-fix.yml"

    def test_the_command_job_still_holds_no_write_scope(self):
        # The property ADR-0014 turns on, asserted where the ADR puts it rather than
        # only alongside the other command-job scopes: the decline exists BECAUSE
        # this job may not post, so if it ever could, the fourth job has no reason
        # to exist and the ADR's argument is gone.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "command")
        assert "pull-requests: write" not in block, (
            "the command job gained the write scope the decline job exists to keep out of it; "
            "it is reached directly from issue_comment, upstream of the trust check"
        )

    def test_the_decline_job_holds_only_pull_requests_write(self):
        block = job_block((WORKFLOWS / self.FIX).read_text(), "decline")
        assert "pull-requests: write" in block
        for scope in ("contents: write", "id-token: write", "actions: read"):
            assert scope not in block, f"the decline job holds {scope!r}; it posts one comment"

    def test_the_decline_job_mints_no_model_credential(self):
        # The reason text is HARNESS-authored: no model runs to produce a decline,
        # which is one of the three reasons ADR-0014 refuses a `decline` plan step.
        block = job_block((WORKFLOWS / self.FIX).read_text(), "decline")
        assert "configure-aws-credentials" not in block
        assert "ANTHROPIC_API_KEY" not in block

    def test_the_decline_needs_both_producers(self):
        # Two refusals, knowable in two places: the fork-plus-multi-path case in
        # `command` (the PR object it already fetches) and AlreadyDelivered in
        # `stack` (the quarantine tree plus a live PR listing). A job needing only
        # one would silently drop the other refusal.
        text = (WORKFLOWS / self.FIX).read_text()
        needs = job_needs(text, "decline")
        assert needs == ["command", "stack"], (
            f"the decline job needs {needs}; both producers must be named or one refusal "
            "can never reach the commander"
        )

    def test_the_decline_fires_on_either_producer(self):
        condition = job_condition((WORKFLOWS / self.FIX).read_text(), "decline")
        assert "needs.command.outputs.declined == 'true'" in condition
        assert "needs.stack.outputs.declined == 'true'" in condition
        # always(), because BOTH producers FAIL their own job — `command` exits
        # non-zero on the refusal it derived, `stack` fails after emitting. Without
        # it the posting job is skipped and the refusal is invisible, which is the
        # "declined to fix something and told nobody" case ADR-0007's third addendum
        # forbids.
        assert "always()" in condition, (
            "the decline job does not run when its producer failed, and both producers fail "
            "by design; the reply would never be posted"
        )

    def test_the_decline_fires_on_an_EQUALITY_not_a_negation(self):
        # The fail-closed direction is a property of how the condition is written, as
        # it is for the delivery jobs: a `!= 'false'` spelling would post a decline
        # whenever the output was absent — which is every ordinary successful run.
        condition = job_condition((WORKFLOWS / self.FIX).read_text(), "decline")
        assert "!=" not in condition, (
            "the decline gates on a negation, so an absent output would post a decline on a "
            "run that declined nothing"
        )

    def test_the_decline_marker_is_not_the_reviewers(self):
        # Sharing post.MARKER would make the two lanes fight over ONE comment: the
        # reviewer's next push overwriting the decline, or the decline overwriting
        # the review. That is supersede_previous_reviews' unscoped-authority defect
        # waiting to happen somewhere new. Asserted against the modules rather than
        # the workflow because that is where the values live.
        import decline
        import post

        assert decline.MARKER != post.MARKER
        assert decline.MARKER.strip(), "an empty marker matches every comment"

    def test_the_decline_reason_reaches_the_poster_through_env(self):
        # Not inline interpolation. It is harness prose today, and that is exactly
        # why this must be pinned: a `run:` block would shell-expand whatever the
        # reason becomes later, and the whole lane's rule is that nothing reaches a
        # shell by substitution.
        text = (WORKFLOWS / self.FIX).read_text()
        steps = parse_steps(text, "decline")
        poster = next(s for s in steps if "decline.py" in s.get("run", ""))
        assert any(key.endswith("REASON") for key in poster), "the reason is not passed via env"
        assert "needs." not in poster["run"], (
            "the poster's run: block interpolates a job output; every value must arrive via env"
        )


class TestTheDeclineIsWiredEndToEnd:
    """The producers write job OUTPUTS and the poster reads ENVIRONMENT VARIABLES,
    and the only thing joining the two is this workflow's `env:` block.

    Nothing else could check that join, and a one-character typo in either name is
    silent: the poster reads an empty value, `main()` refuses, and the decline becomes
    a red run that posts nothing — the exact "declined to fix something and told
    nobody" case ADR-0007's third addendum forbids, arriving through the mechanism
    ADR-0014 built to prevent it. Verified reachable: renaming `decline_reason` to
    `decline_rason` in the workflow left the whole suite green before this existed.

    So both directions are asserted against decline.OUTPUT_ENV, which is the one
    place the pairing is declared.
    """

    FIX = "ai-pr-fix.yml"

    def poster(self):
        text = (WORKFLOWS / self.FIX).read_text()
        return next(s for s in parse_steps(text, "decline") if "decline.py" in s.get("run", ""))

    def test_every_output_the_poster_needs_is_mapped_to_its_env_var(self):
        import decline

        poster = self.poster()
        for output, env_var in decline.OUTPUT_ENV.items():
            value = poster.get(f"env.{env_var}")
            assert value is not None, (
                f"the poster reads {env_var} but the workflow sets no env.{env_var}, so "
                f"decline.py refuses and the commander is told nothing"
            )
            assert f"outputs.{output}" in value, (
                f"env.{env_var} is {value!r}, which does not read outputs.{output} — the "
                "producer writes that name, so the poster would see an empty string"
            )

    def test_every_mapped_env_var_reads_BOTH_producers(self):
        # Two producers, and the decline fires on either (ADR-0014). A value reading
        # only `command` leaves every AlreadyDelivered decline empty — and since that
        # is the refusal a maintainer is most likely to want an answer to, it is the
        # one that must not silently post a blank.
        import decline

        poster = self.poster()
        for output, env_var in decline.OUTPUT_ENV.items():
            value = poster[f"env.{env_var}"]
            for producer in ("command", "stack"):
                assert f"needs.{producer}.outputs.{output}" in value, (
                    f"env.{env_var} does not read {producer}'s {output}, so a decline derived "
                    f"by {producer} posts an empty value there"
                )

    def test_the_poster_reads_no_env_var_no_producer_writes(self):
        # The reverse direction, which is what makes this a mapping rather than a
        # checklist: an env var the workflow sets from an output nobody writes is a
        # value that is always empty, and the poster refuses on it — so a decline that
        # should have posted never does.
        import decline

        poster = self.poster()
        declared = set(decline.OUTPUT_ENV.values()) | {decline.RUN_URL_ENV}
        # GITHUB_TOKEN and PR_NUMBER come from the event and the runtime, not a producer.
        from_the_event = {"GITHUB_TOKEN", "PR_NUMBER"}
        for key in poster:
            if not key.startswith("env."):
                continue
            name = key.removeprefix("env.")
            assert name in declared | from_the_event, (
                f"the workflow sets env.{name}, which decline.py declares no reader for; "
                "either it is dead or the module reads a name nothing sets"
            )

    def test_the_flag_the_condition_reads_is_the_flag_the_writer_writes(self):
        # The `if:` gates the whole job, so a mismatch here means the poster never
        # runs at all — the loudest version of the same silence.
        import decline

        condition = job_condition((WORKFLOWS / self.FIX).read_text(), "decline")
        for producer in ("command", "stack"):
            assert f"needs.{producer}.outputs.{decline.DECLINED_OUTPUT} == 'true'" in condition, (
                f"the decline's condition does not read {producer}'s "
                f"{decline.DECLINED_OUTPUT!r} output as the writer spells it"
            )

    def test_both_producers_declare_every_output_the_poster_reads(self):
        # A job cannot expose an output it does not declare in its own `outputs:`
        # block — the value would be empty however correctly the step wrote it. This
        # is the third place the same name has to appear, and the only one the two
        # tests above cannot see.
        import decline

        text = (WORKFLOWS / self.FIX).read_text()
        for producer in ("command", "stack"):
            block = job_block(text, producer)
            for output in (*decline.OUTPUT_ENV, decline.DECLINED_OUTPUT):
                assert f"{output}:" in block, (
                    f"job {producer!r} does not declare the output {output!r}, so it is empty "
                    "downstream whatever the step wrote to GITHUB_OUTPUT"
                )


class TestRunsAreSerializedPerPullRequest:
    """Two runs for one PR share the sticky comment's marker and bot login, so
    without a per-PR group they contend for one comment and the loser writes
    last. post.py's withdrawal is revision-scoped for the same reason; this is
    the half that stops the contention arising."""

    def test_the_review_workflow_serializes_per_pull_request(self):
        text = (WORKFLOWS / "ai-pr-review.yml").read_text()
        group = next(
            (line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("  group:")),
            None,
        )
        assert group is not None, "ai-pr-review.yml declares no concurrency group"
        assert "pull_request.number" in group, (
            f"the concurrency group must be per-PR, got {group!r}; a repository-wide or "
            "ref-wide group would let two head SHAs' runs contend for one sticky comment"
        )


class TestOtherWorkflowsStayCorrect:
    """The two workflows that got this right, pinned so a copy-paste of the
    evals fix does not 'fix' them into something worse."""

    def test_quality_check_is_pull_request_scoped(self):
        text = (WORKFLOWS / "quality_check.yml").read_text()
        assert "pull_request_target" not in text, (
            "quality_check.yml may cache because it is `pull_request` (PR-scoped cache); "
            "switching it to pull_request_target would put its cache in the base scope"
        )

    def test_the_type_checker_is_its_own_job_beside_the_test_job(self):
        # ADR-0017: ty is a compiled artifact acceptable only as a CI-only dev
        # tool, so it gets its own job — folding it into test_verifier as a step
        # would put the binary in the job whose contract is deterministic pytest,
        # and folding pytest into it would do the reverse.
        text = (WORKFLOWS / "quality_check.yml").read_text()
        assert job_names(text) == ["test_verifier", "typecheck"]
        typecheck = parse_steps(text, "typecheck")
        commands = " ".join(value for step in typecheck for value in step.values())
        assert "ty check" in commands
        assert "pytest" not in commands
        verifier = parse_steps(text, "test_verifier")
        assert "ty" not in " ".join(
            value for step in verifier for value in step.values() if "run" in step
        ).split()

    def test_ai_pr_review_keys_its_cache_on_the_trusted_harness(self):
        steps = parse_steps((WORKFLOWS / "ai-pr-review.yml").read_text(), "review")
        for step in steps:
            for key, value in step.items():
                if "cache-dependency-path" in key:
                    assert value.startswith("harness/"), (
                        "ai-pr-review.yml's cache key must come from the pinned harness "
                        f"checkout, not the consumer's tree; got {value!r}"
                    )



class TestSupplyChainPinning:
    """Every third-party action and every install is pinned, in every workflow.

    A raw-text scan rather than the step parser: the property is about all four
    files including their reusable-workflow `uses:`, and a step-level reader would
    miss a line the parser's job filter skips. Comment lines are excluded, since
    the documentation of how to pin necessarily shows an unpinned form.
    """

    SHA_LENGTH = 40

    @staticmethod
    def lines_containing(needle: str):
        """Every non-comment line holding `needle`, as (workflow, line number, text).

        By content rather than by leading key, because a `run:` block scalar puts
        its commands on continuation lines that begin with neither.
        """
        found = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for number, raw in enumerate(path.read_text().splitlines(), start=1):
                stripped = raw.strip()
                if stripped.startswith("#") or needle not in stripped:
                    continue
                found.append((path.name, number, stripped))
        return found

    def test_the_scan_finds_something(self):
        # A filter that matched nothing would make every assertion below vacuous.
        assert len(self.lines_containing("uses:")) >= 5
        assert len(self.lines_containing("pip install")) >= 5

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        # A moving tag is a supply-chain hole wherever it appears, but especially
        # in the jobs holding a Bedrock role or a write token: @v6 re-resolves on
        # every run, so a compromised tag executes in a credentialed job.
        for workflow, number, text in self.lines_containing("uses:"):
            reference = text.partition("uses:")[2].strip().split()[0]
            if reference.startswith("./"):
                continue  # a local path, not a fetched third party
            assert "@" in reference, f"{workflow}:{number} uses {reference!r} with no pin"
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == self.SHA_LENGTH and all(
                character in "0123456789abcdef" for character in revision
            ), (
                f"{workflow}:{number} pins {reference!r} to a moving reference; "
                "a tag or branch re-resolves on every run"
            )

    def test_every_pip_install_requires_hashes(self):
        installs = self.lines_containing("pip install")
        assert installs, "no pip install found; this assertion has gone stale"
        for workflow, number, text in installs:
            assert "--require-hashes" in text, (
                f"{workflow}:{number} installs without --require-hashes, so a compromised "
                f"index can substitute a dependency: {text!r}"
            )

    def test_no_workflow_runs_node(self):
        # The harness is all Python (the ADR superseding ADR-0003): a Node step
        # reappearing in a workflow means a second toolchain crept back into a
        # credentialed lane without the decision being revisited.
        assert self.lines_containing("npm ") == []
        assert self.lines_containing("setup-node") == []


AGENT_JOBS = [("ai-pr-review.yml", "review"), ("ai-pr-fix.yml", "plan")]


class TestTheConsumerSetsTheGeneratorBudget:
    """ADR-0015's wiring: ONE `workflow_call` input governs the agent job's
    `timeout-minutes` AND the deadline cc_loop sizes each attempt from.

    The value crosses a job boundary, which YAML joins and nothing else checks —
    renaming a `decline` output once left all 1,951 tests green while the poster read
    nothing. And cc_loop TOLERATES an absent deadline, for the eval runners, so a
    workflow that forgot the variable would not fail: it would quietly run every
    consumer's review on the harness's own budget. Nothing at runtime distinguishes
    those two cases.
    """

    @pytest.mark.parametrize(("workflow", "job"), AGENT_JOBS)
    def test_the_agent_job_timeout_is_the_consumers_input(self, workflow, job):
        # A literal here is the defect ADR-0015 exists for: `timeout-minutes` on a
        # called workflow's job is the CALLEE's, so the consumer has no remedy.
        import cc_loop

        block = job_block((WORKFLOWS / workflow).read_text(), job)
        line = next(line.strip() for line in block.splitlines() if "timeout-minutes" in line)
        assert f"inputs.{cc_loop.TIMEOUT_INPUT}" in line, (
            f"{workflow}:{job} sets {line!r}; the agent job's ceiling must read the "
            f"{cc_loop.TIMEOUT_INPUT} input, or it is the harness's number and not the "
            "consumer's"
        )

    @pytest.mark.parametrize(("workflow", "job"), AGENT_JOBS)
    def test_both_workflows_declare_the_input_the_module_names(self, workflow, job):
        import cc_loop

        declared = workflow_input((WORKFLOWS / workflow).read_text(), cc_loop.TIMEOUT_INPUT)
        assert declared, (
            f"{workflow} declares no {cc_loop.TIMEOUT_INPUT} input, so `inputs.` reads "
            "of it are empty and the job has no ceiling at all"
        )
        assert declared.get("type") == "number", (
            f"{workflow}'s {cc_loop.TIMEOUT_INPUT} is type {declared.get('type')!r}; a "
            "string would reach `timeout-minutes` unvalidated"
        )
        assert declared.get("default"), (
            f"{workflow}'s {cc_loop.TIMEOUT_INPUT} has no default, so every existing "
            "consumer's calls break on a required input"
        )

    def test_the_two_workflows_default_to_the_same_budget(self):
        # Both lanes run the same generator loop over the same measurement, so two
        # defaults would mean one was chosen by nobody. Also the figure the
        # arithmetic below reads.
        import cc_loop

        defaults = {
            workflow: workflow_input((WORKFLOWS / workflow).read_text(),
                                     cc_loop.TIMEOUT_INPUT)["default"]
            for workflow, _ in AGENT_JOBS
        }
        assert len(set(defaults.values())) == 1, (
            f"the two agent jobs default to different budgets: {defaults}"
        )

    @pytest.mark.parametrize(("workflow", "job"), AGENT_JOBS)
    def test_the_deadline_is_stamped_from_the_same_input(self, workflow, job):
        # ONE number: a deadline computed from a second input or a constant is the
        # coupling this replaced, moved somewhere new, and it drifts the same way.
        import cc_loop

        block = job_block((WORKFLOWS / workflow).read_text(), job)
        assert cc_loop.DEADLINE_ENV in block, (
            f"{workflow}:{job} never writes {cc_loop.DEADLINE_ENV}, so cc_loop falls "
            "back to its own constants and the consumer's input governs the job "
            "ceiling only"
        )
        # Scoped to the stamping STEP: the job's own `timeout-minutes` reads the
        # input two lines up, so a job-wide window is satisfied by that alone.
        write = next(line for line in block.splitlines() if cc_loop.DEADLINE_ENV in line)
        step = block[block.rindex("- name:", 0, block.index(cc_loop.DEADLINE_ENV)):]
        bound = re.findall(
            r"([A-Z_][A-Z0-9_]*): \$\{\{ inputs\." + re.escape(cc_loop.TIMEOUT_INPUT) + r" \}\}",
            step,
        )
        assert bound, (
            f"{workflow}:{job} stamps the deadline in a step that never reads the "
            f"{cc_loop.TIMEOUT_INPUT} input; the job ceiling and the generator's "
            "deadline must be one number"
        )
        assert any(name in write for name in bound), (
            f"{workflow}:{job} binds the input to {bound} and then computes the "
            f"deadline from something else: {write.strip()!r}"
        )

    @pytest.mark.parametrize(("workflow", "job"), AGENT_JOBS)
    def test_the_deadline_is_stamped_before_the_job_spends_anything(self, workflow, job):
        # POSITION, not presence: stamped after setup, the consumer's number would
        # start when setup ENDED, so the job and the generator would each be allowed
        # the whole timeout and GitHub would kill the step with the transcript
        # unwritten. Relocating the ADR-0006 gate step once left 56 tests green.
        import cc_loop

        block = job_block((WORKFLOWS / workflow).read_text(), job)
        assert block.index(cc_loop.DEADLINE_ENV) < block.index("uses:"), (
            f"{workflow}:{job} stamps the deadline after its first action, so setup "
            "time lands on top of the consumer's budget instead of inside it"
        )

    @pytest.mark.parametrize(("workflow", "job"), AGENT_JOBS)
    def test_the_job_exports_no_name_the_module_declares_no_reader_for(self, workflow, job):
        # The reverse direction, which makes this a pairing rather than a checklist:
        # an exported name cc_loop does not read is either dead or the same value
        # misspelled, and the misspelling is silent — the module finds nothing under
        # its own name and tolerates the absence exactly as designed.
        import cc_loop

        block = job_block((WORKFLOWS / workflow).read_text(), job)
        assert github_env_writes(block) == {cc_loop.DEADLINE_ENV}, (
            f"{workflow}:{job} exports {github_env_writes(block)} to $GITHUB_ENV; "
            f"cc_loop declares a reader for {cc_loop.DEADLINE_ENV} alone"
        )


class TestTheGeneratorBudgetLeavesRoomForTheJob:
    """What the arithmetic pin BECAME (ADR-0015).

    It used to assert that WALL_CLOCK_SECONDS x MAX_ATTEMPTS plus backoff fits the
    agent job's `timeout-minutes`, because those two numbers lived in different
    files and nothing connected them. The input now IS the connection: it sets the
    job ceiling and the deadline both, so a budget raised without the job following
    is no longer expressible.

    What still needs asserting is the reserve: each attempt is sized from the time
    remaining, so left alone the generator spends the whole job and the failure path
    has nowhere to write the transcript. The floor stays a MEASUREMENT — two
    production runs on one pull request, one completing a review in 397s of API time
    over 11 tool calls, one exhausting 600s over 21 without submitting.
    """

    # A review that completed, scaled to the exploration that did not: 758s.
    COMPLETED_API_SECONDS = 397
    CALLS_WHEN_IT_FIT = 11
    CALLS_WHEN_IT_DID_NOT = 21

    @property
    def floor(self) -> float:
        return self.COMPLETED_API_SECONDS * self.CALLS_WHEN_IT_DID_NOT / self.CALLS_WHEN_IT_FIT

    def test_the_default_budget_covers_a_review_that_explores_harder(self):
        # The consumer-facing half, and what stops the reserve growing until the
        # default stops working.
        import cc_loop

        default_minutes = int(workflow_input(
            (WORKFLOWS / AGENT_JOBS[0][0]).read_text(), cc_loop.TIMEOUT_INPUT
        )["default"])
        usable = default_minutes * 60 - cc_loop.POST_GENERATOR_RESERVE_SECONDS
        assert usable >= self.floor, (
            f"a {default_minutes}-minute default leaves {usable:.0f}s after the "
            f"{cc_loop.POST_GENERATOR_RESERVE_SECONDS}s reserve, which does not cover the "
            f"measured {self.floor:.0f}s of a review that explores harder"
        )

    def test_the_reserve_leaves_room_for_the_steps_after_the_generator(self):
        # The bundle steps run `if: always()` precisely so a FAILED generator's
        # transcript still lands, and uploading it is a network round trip.
        import cc_loop

        assert cc_loop.POST_GENERATOR_RESERVE_SECONDS >= 60, (
            f"{cc_loop.POST_GENERATOR_RESERVE_SECONDS}s is not enough for the bundle "
            "assembly and the artifact upload that follow the generator"
        )

    def test_an_attempt_too_small_to_use_is_not_started(self):
        # So that a spent budget reports itself as one, rather than as a timeout from
        # an attempt the loop knew could not submit.
        import cc_loop

        assert 0 < cc_loop.MIN_ATTEMPT_SECONDS < self.floor, (
            "the minimum attempt must be positive and well below the cost of a real "
            f"review; {cc_loop.MIN_ATTEMPT_SECONDS}s against a {self.floor:.0f}s floor"
        )

    def test_the_budget_is_not_the_original_development_value(self):
        # 150s was a fast-feedback figure carried over from a different repository
        # and a simpler agent flow, never a measured production budget. It timed out
        # a real review of a FOUR FILE, 5.5 KB diff -- the ceiling bounds a whole
        # agent session (process spawn, model latency, and every tool call it makes
        # exploring the tree), not the diff, so a reviewer chasing a symbol across
        # crates spends most of it on investigation.
        import cc_loop

        assert cc_loop.WALL_CLOCK_SECONDS > 150, (
            "the wall-clock budget is still the development-loop default, which is "
            "measured to time out real reviews"
        )

    # A repository whose reviews are more Grep than Read makes more calls in the same
    # clock, so the ordering must hold at a per-call cost BELOW the measured one. 0.7
    # is the margin the hand-set pair already had (45 turns against the ~31 that 900s
    # buys), rather than a fresh number.
    CHEAPER_CALLS = 0.7

    def calls_permitted(self, budget: float) -> float:
        import cc_loop

        measured = cc_loop.MEASURED_TOOL_CALL_SECONDS / cc_loop.MEASURED_TOOL_CALLS
        return budget / (measured * self.CHEAPER_CALLS)

    def test_the_fallback_turn_ceiling_does_not_bind_before_the_fallback_clock(self):
        """The two ceilings are co-limits and only the clock has a measured
        rationale, so raising the clock alone relocates the failure: at 900s and ~29s
        per call a review may make ~31, which the previous MAX_TURNS of 30 refused
        first. This pair is the FALLBACK and is still set by hand, so its ordering is
        asserted rather than derived.
        """
        import cc_loop

        permitted = self.calls_permitted(cc_loop.WALL_CLOCK_SECONDS)
        assert cc_loop.MAX_TURNS > permitted, (
            f"MAX_TURNS={cc_loop.MAX_TURNS} binds before the "
            f"{cc_loop.WALL_CLOCK_SECONDS}s clock, which permits {permitted:.0f} tool "
            "calls -- so a review would fail on the turn ceiling and report the wrong "
            "constraint"
        )

    # The smallest attempt the loop will start, the two figures the fallback has
    # held, what the default input buys after the reserve, and GitHub's own limit.
    @pytest.mark.parametrize("budget", [90, 600, 900, 2820, 21600])
    def test_the_derived_turn_ceiling_does_not_bind_before_the_clock_either(self, budget):
        """The same ordering over the derivation, at every budget one input can
        produce rather than at the one pair the constants were chosen for."""
        import cc_loop

        permitted = self.calls_permitted(budget)
        assert cc_loop.turn_ceiling(budget) > permitted, (
            f"a {budget}s attempt permits {permitted:.0f} tool calls, but its derived "
            f"ceiling is {cc_loop.turn_ceiling(budget)} turns -- so the review would "
            "fail on the turn count and report a constraint the consumer did not set"
        )

    def test_the_budget_covers_a_review_that_explores_harder(self):
        """The floor, stated as the measurement rather than as a number.

        REPLACES an assertion that required the budget to absorb "two measured
        provider stalls" of 160s. That framing was wrong: it read a per-event
        `usage.output_tokens`, which is a streaming snapshot, and concluded turns
        emitting three to nine tokens were stalling. Measured against the
        ResultMessage, the same run emitted 29,533 output tokens with
        duration_api_ms >= duration_ms -- all of the wall clock was the provider
        generating, at 74 tok/s. Nothing stalled, so an assertion built on stalls
        enforced a cause that does not exist.

        What the budget actually bounds is reasoning volume, and the measurement is
        two production runs on one pull request: one COMPLETED a review in 397s of
        API time making 11 tool calls; one EXHAUSTED 600s making 21 tool calls and
        had still not submitted, because a landed fix had removed the obvious defect
        and the reviewer explored harder for one. So the budget must cover a review
        that investigates at the higher rate.

        Expressed as the arithmetic rather than as `> 600`, for the reason the
        replaced assertion had right: if either figure is re-measured the floor moves
        with it, where a bare number would only echo whatever was set.

        Applied to the FALLBACK, which is the budget a run with no deadline gets —
        the eval runners and local invocation. The consumer-facing half of the same
        measurement is test_the_default_budget_covers_a_review_that_explores_harder.
        """
        import cc_loop

        assert cc_loop.WALL_CLOCK_SECONDS >= self.floor, (
            f"{cc_loop.WALL_CLOCK_SECONDS}s does not cover {self.floor:.0f}s, the measured "
            f"cost of a {self.COMPLETED_API_SECONDS}s review scaled to the "
            f"{self.CALLS_WHEN_IT_DID_NOT}-tool-call exploration that exhausted 600s in "
            "production without submitting"
        )
