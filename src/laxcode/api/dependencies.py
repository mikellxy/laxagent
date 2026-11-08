from fastapi import Request

from laxcode.storage.database import DatabaseError


def get_config(request: Request):
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise DatabaseError("Application runtime is not initialized")
    return config


def get_context(request: Request):
    context = getattr(request.app.state, "agent_context", None)
    if context is None:
        raise DatabaseError("Database runtime is not initialized")
    return context


def get_repository(request: Request):
    return get_context(request).repository
