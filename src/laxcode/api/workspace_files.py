from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_repository
from laxcode.workspace_files import WorkspaceFileError, open_file, preview, preview_content

router = APIRouter()
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class WorkspaceFilePreview(BaseModel):
    path: str
    name: str
    size: int
    kind: Literal["text", "image", "download"]
    language: str | None
    text: str | None
    truncated: bool
    can_expand: bool


class FileDownload(StreamingResponse):
    """Own the already-authorized descriptor through response completion/disconnect."""
    def __init__(self, file, name: str):
        self.file = file
        super().__init__(self.chunks(), media_type="application/octet-stream", headers={
            **HEADERS, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
        })

    async def chunks(self):
        while chunk := await run_in_threadpool(self.file.read, 64 * 1024):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.file.close()


async def owned_file(session_id, user_id, repository, path):
    session, _ = await repository.session_detail(user_id, session_id)
    return await run_in_threadpool(open_file, user_id, session.project_id, path)


@router.get("/sessions/{session_id}/workspace-files/preview", response_model=WorkspaceFilePreview)
async def file_preview(session_id: Annotated[int, Path(gt=0)], response: Response,
                       path: Annotated[str, Query(min_length=1)], user_id=Depends(get_current_user),
                       repository=Depends(get_repository)):
    try:
        canonical, file = await owned_file(session_id, user_id, repository, path)
        with file:
            result = await run_in_threadpool(preview, file, canonical)
        response.headers.update(HEADERS)
        return result
    except WorkspaceFileError as exc:
        raise HTTPException(exc.status, exc.message) from None
    except OSError:
        raise HTTPException(503, "文件读取失败，请重试") from None


@router.get("/sessions/{session_id}/workspace-files/content")
async def file_content(session_id: Annotated[int, Path(gt=0)], path: Annotated[str, Query(min_length=1)],
                       download: bool = False, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    try:
        canonical, file = await owned_file(session_id, user_id, repository, path)
        if download:
            return FileDownload(file, canonical.rsplit("/", 1)[-1])
        with file:
            data, media_type = await run_in_threadpool(preview_content, file, canonical)
        return Response(data, media_type=media_type, headers=HEADERS)
    except WorkspaceFileError as exc:
        raise HTTPException(exc.status, exc.message) from None
    except OSError:
        raise HTTPException(503, "文件读取失败，请重试") from None
