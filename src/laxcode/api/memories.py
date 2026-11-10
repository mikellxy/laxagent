from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import MemoriesResponse
from laxcode.memory import MEMORY_CATEGORY_LABELS

router = APIRouter()


@router.get("/memories", response_model=MemoriesResponse)
async def list_memories(user_id=Depends(get_current_user), repository=Depends(get_repository)):
    groups = {category: {"category": category, "label": label, "items": []}
              for category, label in MEMORY_CATEGORY_LABELS.items()}
    for memory in await repository.list_memories(user_id):
        groups[memory["category"]]["items"].append(
            {key: memory[key] for key in ("id", "abstract", "detail")},
        )
    return {"categories": list(groups.values())}


@router.delete("/memories", status_code=204)
async def delete_all_memories(user_id=Depends(get_current_user), repository=Depends(get_repository)):
    await repository.delete_memories(user_id)
    return Response(status_code=204)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: Annotated[int, Path(gt=0, le=2**31 - 1)],
                        user_id=Depends(get_current_user), repository=Depends(get_repository)):
    await repository.delete_memories(user_id, memory_id)
    return Response(status_code=204)
