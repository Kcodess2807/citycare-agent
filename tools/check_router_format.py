"""One real call: does haiku-5-5 accept structured output (output_config.format) together with effort?

    uv run python tools/check_router_format.py

Prints only status, tokens, cost and the parsed answer. Hard stop at LIMIT_USD (one call costs well under a cent).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from agentcore.trace import TokenUsage, clip, cost_usd  # noqa: E402

LIMIT_USD = 0.10
MODEL = "claude-haiku-5-5"
SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "enum": ["booking", "billing", "info", "human"]},
        "language": {"type": "string", "enum": ["en", "hi", "hinglish"]},
    },
    "required": ["agent", "language"],
    "additionalProperties": False,
}


async def main() -> None:
    load_dotenv(REPO / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("No ANTHROPIC_API_KEY in .env or the environment.")
    client = anthropic.AsyncAnthropic(max_retries=0)
    try:
        r = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system="Route the patient's message. agent: booking, billing, info or human. language: en, hi or hinglish.",
            messages=[{"role": "user", "content": "mera bill kitna hai?"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.APIStatusError as e:
        raise SystemExit(f"REJECTED status={e.status_code} message={clip(str(getattr(e, 'message', '')), 300)}")
    usage = TokenUsage.from_sdk(r.usage)
    cost = cost_usd(MODEL, usage) or 0.0
    if cost > LIMIT_USD:
        raise SystemExit(f"over the limit: ${cost:.4f}")
    print(f"ACCEPTED stop={r.stop_reason} in={usage.input_tokens} out={usage.output_tokens} think={usage.thinking_tokens} cost=${cost:.5f}")
    print("blocks:", [b.type for b in r.content])
    text = "".join(b.text for b in r.content if b.type == "text")
    print("text:", clip(text, 300))
    print("parsed:", json.loads(text))


asyncio.run(main())
