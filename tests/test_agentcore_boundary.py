"""agentcore must stay generic: no imports of the service or the mock, no hospital words anywhere.

The word scan covers code, comments, docstrings and strings, so text shown to the model
(tool_result messages and so on) can't carry domain wording either. Wording comes from hooks.
"""

import ast
import re
from pathlib import Path

AGENTCORE = Path(__file__).resolve().parent.parent / "agentcore"
SOURCES = sorted(AGENTCORE.rglob("*.py"))

FORBIDDEN_MODULES = {"agent_service", "mock_hospital", "evals", "tests"}
# Domain words. Whole words only, case-insensitive.
FORBIDDEN_WORDS = re.compile(
    r"\b(patients?|doctors?|appointments?|bills?|billing|payments?|hospital|citycare|clinic|"
    r"phones?|slots?|book|books|booking|reschedule|whatsapp|emergency|staff|handoff)\b",
    re.IGNORECASE,
)


def test_agentcore_has_sources():
    assert SOURCES, "agentcore/ is empty, so the boundary checks below would pass for nothing"


def test_agentcore_imports_no_service_code():
    bad = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            bad += [f"{path.name}: {n}" for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
    assert not bad, bad


def test_agentcore_uses_no_domain_words():
    hits = []
    for path in SOURCES:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits += [f"{path.name}:{no}: {m.group(0)}" for m in FORBIDDEN_WORDS.finditer(line)]
    assert not hits, hits
