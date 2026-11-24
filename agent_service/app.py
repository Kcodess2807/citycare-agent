"""The agent service. Run: uvicorn agent_service.app:app --port 8000"""

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402

from agentcore.trace import summarize  # noqa: E402

from .schemas import ChatRequest, ChatResponse  # noqa: E402
from .service import ChatService, build_service  # noqa: E402

app = FastAPI(title="City Care front desk agent", version="0.2")

_service: ChatService | None = None


def service() -> ChatService:
    """Built on first use, so importing the app needs neither a key nor a running hospital."""
    global _service
    if _service is None:
        _service = build_service()
    return _service


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    return await service().handle(req)  # never raises: a failure is still a 200 with a reply


@app.get("/trace/{session_id}")
async def trace(session_id: str):
    """Every model call, tool call, hook decision and state save of this session, plus the totals."""
    events = await service().traces.get(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="No trace for this session")
    return {
        "session_id": session_id,
        "events": [e.model_dump(mode="json") for e in events],
        "summary": summarize(events).model_dump(mode="json"),
    }
