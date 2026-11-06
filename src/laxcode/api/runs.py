from fastapi import APIRouter

from laxcode.api.schemas import ChatRequest

router = APIRouter()


@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatRequest) -> dict[str, str]:
    query = body.query
    # shortcut: 仅回显解析结果；接入业务逻辑时替换为 run 响应。
    return {"session_id": session_id, "query": query}
