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

    def test_the_execute_job_is_the_only_writer(self):
        text = (WORKFLOWS / self.FIX).read_text()
        writers = [job for job in job_names(text) if "pull-requests: write" in job_block(text, job)]
        assert writers == ["execute"], (
            f"exactly one job may hold the write token, got {writers}"
        )

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
        assert len(self.lines_containing("npm ")) >= 2

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

    def test_every_npm_install_is_ci(self):
        # `npm install` may resolve outside the lockfile; `npm ci` may not.
        for workflow, number, text in self.lines_containing("npm install"):
            raise AssertionError(
                f"{workflow}:{number} runs `npm install`, which may resolve outside "
                f"the lockfile; use `npm ci`: {text!r}"
            )
