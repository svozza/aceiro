"""The architecture diagram is a living document, so it has to actually render.

docs/architecture.html carries its whole node graph as a JavaScript literal
inside one <script> block. Nothing checked that block parsed, and it did not: two
ASCII apostrophes inside a single-quoted `desc` string ("verifier's",
"ADR-0011's") were a syntax error, so the script threw on load and the page
rendered no diagram at all — every node, edge and lane silently absent while the
file looked correct in review, because prose containing an apostrophe is exactly
what prose contains.

Node here is a dev-box convenience, not a project dependency (the harness is all
Python): the check skips rather than fails where Node is absent, so CI's
Python-only test_verifier job is right to have no Node.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DIAGRAM = Path(__file__).parent.parent / "docs" / "architecture.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")


def script_source() -> str:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", DIAGRAM.read_text(encoding="utf-8"), re.S)
    assert scripts, "the diagram carries no <script> block, so its node graph cannot be there"
    return "\n".join(scripts)


def test_the_diagrams_script_parses(tmp_path):
    """A syntax error here means the page renders nothing, silently."""
    candidate = tmp_path / "diagram.js"
    candidate.write_text(script_source(), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(candidate)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "docs/architecture.html's script does not parse, so the page renders no diagram:\n"
        f"{result.stderr}"
    )


def test_every_node_has_the_keys_the_renderer_reads(tmp_path):
    """A missing key is the other way this page breaks without looking broken.

    Parsing is not enough: the renderer indexes these on every node, so an
    omitted one throws at draw time rather than at load time — later, and just as
    blank. Asserted by RUNNING the data through node, since that is the engine
    whose answer decides whether the page works.
    """
    harness = tmp_path / "check.js"
    harness.write_text(
        script_source().split("const canvas")[0]
        + """
const required = ['id', 'x', 'y', 'role', 'title', 'sub'];
const problems = [];
for (const node of NODES) {
  for (const key of required) {
    if (node[key] === undefined) problems.push(`${node.id ?? '<no id>'}: missing ${key}`);
  }
}
for (const edge of EDGES) {
  for (const end of ['from', 'to']) {
    if (!NODES.some((n) => n.id === edge[end])) problems.push(`edge ${end} names no node: ${edge[end]}`);
  }
}
for (const [flow, spec] of Object.entries(FLOWS)) {
  for (const id of spec.steps) {
    if (!NODES.some((n) => n.id === id)) problems.push(`flow ${flow} names no node: ${id}`);
  }
}
if (problems.length) { console.error(problems.join('\\n')); process.exit(1); }
console.log(`${NODES.length} nodes, ${EDGES.length} edges`);
""",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"the diagram's data is not renderable:\n{result.stderr}"
