"""The specialist agents. Each is a fixed model, a fixed prompt and a fixed set of tools.

`human` has no model: the router picking it ends in `prompts.human_reply`. All specialists run on
haiku for now; the haiku vs sonnet comparison happens at the end of Phase 2 with real prompts (2.1).
"""

from __future__ import annotations

from agent_service import prompts
from agentcore import ToolRegistry
from agentcore.loop import AgentSpec

SPECIALIST_MODEL = "claude-haiku-5-5"
SPECIALIST_MAX_TOKENS = 4096  # thinking counts inside it (1.3); a ceiling, not a target
SPECIALIST_EFFORT = "low"

TOOLS = {
    "info": [],
    "booking": ["find_doctors", "free_slots", "list_appointments"],
    "billing": ["list_bills"],
}


def _subset(reg: ToolRegistry, names: list[str]) -> ToolRegistry:
    sub = ToolRegistry()
    for name in names:  # a fixed order, so the tool list is byte-identical every call and caches
        tool = reg.get(name)
        if tool is None:
            raise KeyError(f"tool {name!r} is not registered")
        sub.register(tool)
    return sub


def build_agents(read_tools: ToolRegistry) -> dict[str, AgentSpec]:
    systems = {"info": prompts.INFO, "booking": prompts.BOOKING, "billing": prompts.BILLING}
    return {
        name: AgentSpec(
            name=name,
            model=SPECIALIST_MODEL,
            system=systems[name],
            tools=_subset(read_tools, TOOLS[name]),
            max_tokens=SPECIALIST_MAX_TOKENS,
            effort=SPECIALIST_EFFORT,
        )
        for name in TOOLS
    }
