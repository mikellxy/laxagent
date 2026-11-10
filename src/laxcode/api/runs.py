from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_config, get_context, get_repository
from laxcode.api.schemas import ChatRequest, RunResponse, SubmittedRunResponse
from laxcode.runtime.events import stream_events
from laxcode.storage.database import DatabaseError

router = APIRouter()
ResourceId = Annotated[int, Path(gt=0)]


@router.post("/sessions/{session_id}/chat", status_code=202, response_model=SubmittedRunResponse)
async def chat(session_id: ResourceId, body: ChatRequest, request: Request,
               user_id=Depends(get_current_user), config=Depends(get_config)):
    model = next((model for provider in config.llm.provider_list for model in provider.model_list
                  if f"{provider.name}:{model.name}" == body.model), None)
    if model is None:
        raise HTTPException(422, "A configured provider:model is required")
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise DatabaseError("Agent runtime is not initialized")
    run = await manager.submit(user_id=user_id, session_id=session_id, user_query=body.user_query,
                               model=body.model, context_limit=model.limit.context, output_limit=model.limit.output)
    return {"run_id": run.id, "session_id": run.session_id, "status": "waiting"}


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: ResourceId, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.get_run(run_id, user_id)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: ResourceId, request: Request, user_id=Depends(get_current_user)):
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise DatabaseError("Agent runtime is not initialized")
    return await manager.cancel(user_id, run_id)


async def subscription(run_id: ResourceId, user_id=Depends(get_current_user), context=Depends(get_context)):
    await context.repository.get_run(run_id, user_id)
    async with context.events.subscribe(run_id) as sub:
        yield sub


@router.get("/runs/{run_id}/events")
async def events(run_id: ResourceId, sub=Depends(subscription, scope="request")):
    return StreamingResponse(stream_events(sub, run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
