"""An async face for tests/fake_claude.py, so the async loop can run against the scripted fake.

fake_claude.py stays as provided (it is sync). This wraps it:

    fake = AsyncFake([text("hi")])
    response = await fake.messages.create(model=..., messages=[...])
    fake.messages.calls        # every request, as FakeClaude records them

`delays` makes the Nth call (0-based, counting every call that was started) wait that many seconds
first, which is how a test makes a model call time out.
"""

from __future__ import annotations

import asyncio

from tests.fake_claude import FakeClaude


class _AsyncMessages:
    def __init__(self, inner, delays):
        self._inner = inner
        self._delays = list(delays or [])
        self._started = 0

    @property
    def calls(self) -> list[dict]:
        return self._inner.calls

    @property
    def script(self) -> list:
        return self._inner.script

    async def create(self, **kwargs):
        n, self._started = self._started, self._started + 1
        if n < len(self._delays) and self._delays[n]:
            await asyncio.sleep(self._delays[n])
        return self._inner.create(**kwargs)


class AsyncFake:
    def __init__(self, script: list, delays: list[float] | None = None):
        self.messages = _AsyncMessages(FakeClaude(script).messages, delays)
