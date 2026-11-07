from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from laxcode.api.schemas import DebugInvokeRequest
from laxcode.models.errors import ModelError
from laxcode.runtime.events import EventStoreError, stream_events
from laxcode.runtime.runner import run_agent
from laxcode.storage.database import DatabaseError

router = APIRouter(prefix="/debug", tags=["debug"])
RunId = Annotated[int, Path(gt=0)]


def get_context(request: Request):
    context = getattr(request.app.state, "agent_context", None)
    if context is None:
        raise EventStoreError("Agent runtime is not initialized")
    return context


@router.post("/agent/invoke")
async def invoke(body: DebugInvokeRequest, request: Request):
    try:
        context = get_context(request)
        await context.repository.create_user(body.user_id)
        project = await context.repository.create_project(body.user_id, "debug-project")
        session = await context.repository.create_session(body.user_id, project.id, "debug-session")
        state = {
            "messages": [], "user_query": body.user_query,
            "model": body.model, "run_id": body.run_id, "next_msg_seq": session.next_msg_seq,
            "user_id": body.user_id, "project_id": project.id, "session_id": session.id,
        }
        return await run_agent(request.app.state.graph, state, context)
    except (ModelError, EventStoreError, DatabaseError) as exc:
        status = 502 if isinstance(exc, ModelError) else exc.status
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
