from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from laxcode.api.schemas import DebugInvokeRequest
from laxcode.models.responses import ModelError
from laxcode.runtime.events import EventStoreError, stream_events
from laxcode.runtime.runner import run_agent

router = APIRouter(prefix="/debug", tags=["debug"])
RunId = Annotated[int, Path(gt=0)]


def get_context(request: Request):
    context = getattr(request.app.state, "agent_context", None)
    if context is None:
        raise EventStoreError("Agent runtime is not initialized")
    return context


@router.post("/agent/invoke")
async def invoke(body: DebugInvokeRequest, request: Request):
    state = {
        "messages": [
            {"role": "system", "content": "你是一个有帮助的 AI 助手，请准确、简洁地回答用户的问题。"},
            {"role": "user", "content": body.user_query},
        ],
        "model": body.model, "run_id": body.run_id, "next_msg_id": 0,
        "user_id": body.user_id, "project_id": body.project_id, "session_id": body.session_id,
    }
    try:
        context = get_context(request)
        return await run_agent(request.app.state.graph, state, context)
    except (ModelError, EventStoreError) as exc:
        status = exc.status if isinstance(exc, EventStoreError) else 502
        raise HTTPException(status, {"run_id": body.run_id, "error": str(exc)}) from None


async def subscription(run_id: RunId, request: Request):
    try:
        async with get_context(request).events.subscribe(run_id) as sub:
            yield sub
    except EventStoreError as exc:
        raise HTTPException(exc.status, {"run_id": run_id, "error": str(exc)}) from None


@router.get("/runs/{run_id}/events")
async def events(run_id: RunId, sub=Depends(subscription, scope="request")):
    return StreamingResponse(
        stream_events(sub, run_id), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
