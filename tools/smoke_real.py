"""Real-API smoke test for agentcore. Run by hand. It is not part of pytest and spends a little money.

    uv run python tools/smoke_real.py

Check A: one turn each on haiku and sonnet through the real loop with a toy tool. Does the API
         accept our request shape and the blocks we store from real responses?
Check B: thinking blocks and history trimming. Build a real history with thinking blocks, then
         send it back as it is (V0), trimmed at a turn boundary like trim_history does (V1), and
         with the thinking blocks stripped (V2). Which of those does the API accept?

It prints only: model, step, stop reason, token counts, cost, time, and for a rejected request
the status code and a short scrubbed error message. It never prints the client, a response
object, headers or the key. It stops if the running cost passes LIMIT_USD.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

from agentcore import Tool, ToolRegistry  # noqa: E402
from agentcore.loop import AgentLoop, AgentSpec  # noqa: E402
from agentcore.session import InMemorySessionStore, check_history, trim_history  # noqa: E402
from agentcore.trace import InMemoryTraceStore, TokenUsage, clip, cost_usd  # noqa: E402

LIMIT_USD = 0.40
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


class Budget:
    def __init__(self) -> None:
        self.spent = 0.0

    def add(self, usd: float | None) -> None:
        self.spent += usd or 0.0
        if self.spent > LIMIT_USD:
            raise SystemExit(f"Stopped: spent ${self.spent:.3f}, over the ${LIMIT_USD:.2f} limit.")


BUDGET = Budget()


class AddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


async def _add(inp: AddIn, ctx) -> dict:
    return {"sum": inp.a + inp.b}


def toy_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(name="add_numbers", description="Adds two whole numbers and returns their sum.", input_model=AddIn, run=_add))
    return reg


def safe(text: object) -> str:
    return clip(str(text).replace("\n", " "), 300)  # clip() also scrubs anything shaped like a key


def row(label: str, step: int | str, stop: str, usage: TokenUsage, cost: float | None, ms: float) -> None:
    c = "n/a" if cost is None else f"${cost:.5f}"
    print(
        f"  {label:<10} step={step} stop={stop:<10} in={usage.input_tokens} out={usage.output_tokens} "
        f"think={usage.thinking_tokens} cache_r={usage.cache_read_tokens} cache_w={usage.cache_write_tokens} cost={c} ms={ms:.0f}"
    )


async def run_turn(loop: AgentLoop, traces: InMemoryTraceStore, agent: AgentSpec, sid: str, message: str, label: str):
    result = await loop.run_turn(agent, sid, message)
    events = [e for e in await traces.get(sid) if e.trace_id == result.trace_id and e.type == "model_call"]
    for e in events:
        row(label, e.extra.get("step", "?"), e.extra.get("stop_reason", e.status), e.usage or TokenUsage(), e.cost_usd, e.duration_ms)
        BUDGET.add(e.cost_usd)
    print(f"  {label:<10} -> status={result.status} steps={result.steps}" + (f" reason={result.reason_code}: {safe(result.reason)}" if result.status == "failed" else ""))
    return result


def history_facts(messages: list[dict]) -> str:
    blocks = [b for m in messages if m["role"] == "assistant" and isinstance(m["content"], list) for b in m["content"]]
    thinking = [b for b in blocks if b.get("type") in ("thinking", "redacted_thinking")]
    return (
        f"messages={len(messages)} thinking_blocks={len(thinking)} "
        f"with_text={sum(1 for b in thinking if b.get('thinking'))} tool_use_blocks={sum(1 for b in blocks if b.get('type') == 'tool_use')}"
    )


async def try_history(client, agent: AgentSpec, label: str, messages: list[dict]) -> bool | None:
    """Send `messages` as they are. Returns True if accepted, False if the API rejected them."""
    check_history(messages)  # our own shape check first, so a rejection is the API's and not ours
    request = AgentLoop._request(agent, messages, agent.max_tokens)
    t0 = time.perf_counter()
    try:
        response = await client.messages.create(**request)
    except anthropic.APIStatusError as e:
        print(f"  {label:<10} REJECTED status={e.status_code} message={safe(getattr(e, 'message', ''))}")
        return False
    except Exception as e:
        print(f"  {label:<10} ERROR {type(e).__name__}")
        return None
    usage = TokenUsage.from_sdk(response.usage)
    cost = cost_usd(agent.model, usage)
    BUDGET.add(cost)
    row(label, "-", str(response.stop_reason), usage, cost, (time.perf_counter() - t0) * 1000)
    print(f"  {label:<10} ACCEPTED")
    return True


async def check_a(client) -> None:
    print("\nCheck A: request shape through the real loop, toy tool, effort low")
    for model in (HAIKU, SONNET):
        store, traces = InMemorySessionStore(), InMemoryTraceStore()
        loop = AgentLoop(client, store, traces)
        agent = AgentSpec("toy", model, "You are a calculator assistant. Use the add_numbers tool for any addition.", toy_tools(), max_tokens=1024, effort="low")
        result = await run_turn(loop, traces, agent, "a", "What is 17 + 25? Use the tool.", model.split("-")[1])
        print(f"  {model.split('-')[1]:<10} answer_mentions_42={'42' in result.text}")


async def check_b(client, model: str, full: bool) -> None:
    short = model.split("-")[1]
    print(f"\nCheck B on {short}: thinking blocks and history trimming, effort high")
    store, traces = InMemorySessionStore(), InMemoryTraceStore()
    loop = AgentLoop(client, store, traces)
    system = "You are a careful calculator assistant. Use the add_numbers tool for every single addition, one at a time."
    agent = AgentSpec("toy", model, system, toy_tools(), max_tokens=4096, effort="high")
    first = await run_turn(loop, traces, agent, "b", "A box holds 17 red and 25 blue balls, then 38 green balls are added. How many balls now? Add step by step with the tool.", f"{short}-t1")
    second = await run_turn(loop, traces, agent, "b", "Now 19 balls of each of the three colours are added. How many balls in total? Use the tool.", f"{short}-t2")
    if first.status != "done" or second.status != "done":
        print("  could not build a two-turn history, skipping the variants")
        return
    history = (await store.load("b")).messages
    print("  stored history:", history_facts(history))

    turn2_start = [i for i, m in enumerate(history) if m["role"] == "user" and isinstance(m["content"], str)][1]
    new_user = {"role": "user", "content": "Remind me, what was the very first total you worked out?"}

    await try_history(client, agent, f"{short}-V0", history + [new_user])  # as stored
    trimmed = trim_history(history, len(history) - turn2_start)  # what trim_history would store
    print(f"  V1 trims {turn2_start} of {len(history)} messages (the whole first turn)")
    v1 = await try_history(client, agent, f"{short}-V1", trimmed + [new_user])
    if full:
        stripped = [
            {**m, "content": [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]}
            if m["role"] == "assistant" and isinstance(m["content"], list)
            else m
            for m in history
        ]
        await try_history(client, agent, f"{short}-V2", stripped + [new_user])
    return v1


async def main() -> None:
    load_dotenv(REPO / ".env")
    print("key present:", "yes" if os.environ.get("ANTHROPIC_API_KEY") else "no")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("No ANTHROPIC_API_KEY in .env or the environment.")
    client = anthropic.AsyncAnthropic(max_retries=0)  # the loop owns retries
    await check_a(client)
    v1_sonnet = await check_b(client, SONNET, full=True)
    v1_haiku = await check_b(client, HAIKU, full=False)
    print(f"\nTotal spent: ${BUDGET.spent:.4f} (limit ${LIMIT_USD:.2f})")
    print(f"V1 (trim at a turn boundary) accepted: sonnet={v1_sonnet} haiku={v1_haiku}")


if __name__ == "__main__":
    asyncio.run(main())
