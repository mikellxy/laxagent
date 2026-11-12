import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as ResourcePath, UploadFile
from fastapi.responses import FileResponse

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import AppendixReference
from laxcode.appendices import InvalidAppendix, MAX_FILE_BYTES, remove_files, save_files

router = APIRouter()


@router.post("/appendices", status_code=201, response_model=AppendixReference)
async def upload(file: UploadFile, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "单个附件不能超过 10 MiB")
    try:
        values = await asyncio.to_thread(save_files, user_id, name, data)
    except InvalidAppendix as exc:
        raise HTTPException(415, str(exc)) from None
    except OSError:
        raise HTTPException(503, "附件保存失败，请重试") from None
    try:
        row = await repository.create_appendix(**values)
    except BaseException:
        await asyncio.to_thread(remove_files, values)
        raise
    return {"id": row.id, "name": row.name}


@router.get("/appendices/{appendix_id}")
async def content(appendix_id: Annotated[int, ResourcePath(gt=0)],
                  user_id=Depends(get_current_user), repository=Depends(get_repository)):
    row = (await repository.get_appendices(user_id, [appendix_id])).get(appendix_id)
    if row is None or not await asyncio.to_thread(Path(row.path).is_file):
        raise HTTPException(404, "附件不存在")
    return FileResponse(row.path, media_type=row.media_type, filename=row.name,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
