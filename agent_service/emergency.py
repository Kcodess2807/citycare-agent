"""The emergency check. It runs in `ChatService.handle` before the router and before anything else.

Tonight this is a stub that never fires: the call site, and what a hit does, are built and tested
so the shape of `handle` does not change. Phase 3 replaces the body with code that recognises an
emergency in English, Hindi and Hinglish (design-log 2.2).
"""

from __future__ import annotations


def check_emergency(message: str) -> str | None:
    """The fixed reply to send if `message` describes an emergency, else None."""
    return None
