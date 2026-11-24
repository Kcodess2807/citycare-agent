"""The facts note and the text-only history.

Agents share one history that holds only the patient's text and the agent's final reply (design-log
2.4), so the ids a tool returned would be lost at a handoff. The tools record them per turn
(`ctx.data["facts"]`), the service merges them into `state["facts"]` (`merge_facts`), and each turn
the next agent gets them as a marked block in front of the patient's message (`with_note`). The
block is stripped when the history is committed (`text_only`), so it never piles up.

Facts are for continuity, not authority: a slot list can be stale and the hooks check ownership
and freshness themselves.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

START, END = "[[FACTS]]", "[[/FACTS]]"
KEEP_PER_KIND = 10  # most recent items kept per kind

_NOTE = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\s*", re.S)
_HEADER = "System context from earlier turns. The patient did not write this. Slots may be out of date: call free_slots again before offering one."


def _clean(text: str) -> str:
    """Nobody but this module may write a marker. A patient typing one gets it broken, so it never matches."""
    return text.replace("[[", "[ [").replace("]]", "] ]")


def merge_facts(old: dict[str, Any] | None, new: dict[str, Any] | None, keep: int = KEEP_PER_KIND) -> dict[str, Any]:
    """Add this turn's facts to the stored ones. A repeated id moves to the newest end. Only the last `keep` per kind stay."""
    out = {kind: dict(items) for kind, items in (old or {}).items()}
    for kind, items in (new or {}).items():
        bucket = out.setdefault(kind, {})
        for key, value in items.items():
            bucket.pop(key, None)
            bucket[key] = value
        while len(bucket) > keep:
            bucket.pop(next(iter(bucket)))
    return out


def _time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso)


def _day_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(iso)


def facts_note(facts: dict[str, Any] | None, now: dict[str, Any] | None = None) -> str:
    """The marked block, or "" when there is nothing to say. `now` is the hospital's /now answer."""
    facts = facts or {}
    lines: list[str] = []
    if now:
        lines.append(f"Hospital time now: {now.get('weekday', '')} {now.get('now', '')} ({now.get('timezone', '')})")
    if facts.get("doctors"):
        lines.append("Doctors: " + "; ".join(f"{k} {d.get('name', '')} ({d.get('specialty', '')})" for k, d in facts["doctors"].items()))
    for key, free in (facts.get("slots") or {}).items():
        doctor, _, day = key.partition("@")
        lines.append(f"Free slots seen for {doctor} on {day}: " + (", ".join(_time(s) for s in free) if free else "none"))
    for k, a in (facts.get("appointments") or {}).items():
        lines.append(f"Patient's appointment {k}: doctor {a.get('doctor_id', '')}, {_day_time(a.get('start', ''))}, {a.get('status', '')}")
    for k, b in (facts.get("bills") or {}).items():
        lines.append(f"Patient's bill {k}: Rs {b.get('amount_inr', '')}, {b.get('description', '')}, {b.get('date', '')}, {b.get('status', '')}")
    if not lines:
        return ""
    return START + "\n" + _clean(_HEADER + "\n" + "\n".join(lines)) + "\n" + END


def with_note(message: str, note: str) -> str:
    """The user message the model sees: the note, then the patient's words (with any marker of theirs broken)."""
    return f"{note}\n\n{_clean(message)}" if note else _clean(message)


# ---------------------------------------------------------------- text-only history


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content if isinstance(b, dict)]


def _user_text(content: Any) -> str | None:
    """The patient's text, note removed. None for a message that only carries tool results."""
    blocks = _blocks(content)
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not texts:
        return None
    return _NOTE.sub("", "\n".join(texts)).strip()


def _assistant_text(content: Any) -> str:
    return "\n".join(b.get("text", "") for b in _blocks(content) if b.get("type") == "text").strip()


def text_only(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History as stored: per turn, the patient's text and the reply the patient saw.

    Tool calls, tool results, thinking blocks and the facts note are dropped. Text the model wrote
    before a tool call is dropped too: it was never sent to the patient, so only the last assistant
    text of a turn is kept. Running it on an already text-only history changes nothing.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            text = _user_text(m["content"])
            if text:
                out.append({"role": "user", "content": text})
        else:
            text = _assistant_text(m["content"])
            if not text:
                continue
            if out and out[-1]["role"] == "assistant":
                out.pop()  # an earlier text of the same turn, never shown
            out.append({"role": "assistant", "content": text})
    return out
