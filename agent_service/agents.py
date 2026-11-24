"""The specialist agents. Each is a fixed model, a fixed prompt and a fixed set of tools.

`human` has no model: the router picking it ends in `prompts.human_reply`. All specialists run on
haiku for now; the haiku vs sonnet comparison happens at the end of Phase 2 with real prompts (2.1).
"""

from __future__ import annotations

from agent_service import prompts
from agent_service.gate import GATE_HOOK
from agentcore import HookRunner, ToolRegistry
from agentcore.loop import AgentSpec

SPECIALIST_MODEL = "claude-haiku-5-5"
SPECIALIST_MAX_TOKENS = 4096  # thinking counts inside it (1.3); a ceiling, not a target
SPECIALIST_EFFORT = "low"

TOOLS = {
    "info": ["contact_staff"],
    "booking": ["find_doctors", "free_slots", "list_appointments", "contact_staff"],
    "billing": ["list_bills", "contact_staff"],
}

# A write tool never runs on the model's call: the gate in `gate.py` turns it into a pending action
# (design-log 3.2). So `build_agents` refuses to hand them out unless that gate is installed.
WRITE_TOOLS = {
    "booking": ["book_appointment", "reschedule_appointment", "cancel_appointment"],
    "billing": ["create_payment_link"],
}


def _subset(reg: ToolRegistry, names: list[str]) -> ToolRegistry:
    sub = ToolRegistry()
    for name in names:  # a fixed order, so the tool list is byte-identical every call and caches
        tool = reg.get(name)
        if tool is None:
            raise KeyError(f"tool {name!r} is not registered")
        sub.register(tool)
    return sub


def build_agents(
    read_tools: ToolRegistry, write_tools: ToolRegistry | None = None, *, hooks: HookRunner | None = None
) -> dict[str, AgentSpec]:
    """`hooks` is the runner the agents will run under. With write tools it must have the confirmation gate."""
    if write_tools is not None and not (hooks is not None and hooks.has("before_tool", GATE_HOOK)):
        raise ValueError("write tools need the confirmation gate installed on the hook runner")
    pool = ToolRegistry()
    for source in (read_tools, write_tools):
        for name in source.names() if source else []:
            pool.register(source.get(name))
    names = {k: TOOLS[k] + (WRITE_TOOLS.get(k, []) if write_tools else []) for k in TOOLS}
    systems = {"info": prompts.INFO, "booking": prompts.BOOKING, "billing": prompts.BILLING}
    return {
        name: AgentSpec(
            name=name,
            model=SPECIALIST_MODEL,
            system=systems[name],
            tools=_subset(pool, names[name]),
            max_tokens=SPECIALIST_MAX_TOKENS,
            effort=SPECIALIST_EFFORT,
        )
        for name in TOOLS
    }
