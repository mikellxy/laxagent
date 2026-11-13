import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from laxagent.api.auth import get_current_user
from laxagent.api.schemas import SkillsResponse
from laxagent.skills import user_skill_list

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/skills", response_model=SkillsResponse)
async def list_skills(user_id: str = Depends(get_current_user)):
    try:
        skills = await asyncio.to_thread(user_skill_list, user_id)
    except OSError:
        logger.exception("Unable to read user skills")
        raise HTTPException(503, "技能列表读取失败，请重试") from None
    return {"skills": skills}
