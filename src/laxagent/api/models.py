from fastapi import APIRouter, Depends

from laxagent.api.auth import get_current_user
from laxagent.api.dependencies import get_config
from laxagent.api.schemas import ModelCatalogResponse

router = APIRouter()


@router.get("/models", response_model=ModelCatalogResponse, dependencies=[Depends(get_current_user)])
async def models(config=Depends(get_config)):
    return {"default_model": config.llm.default_model, "models": [
        {"id": f"{provider.name}:{model.name}", "provider": provider.name, "name": model.name,
         "supports_multimodal": model.supports_multimodal}
        for provider in config.llm.provider_list for model in provider.model_list
    ]}
