from fastapi import Request

from laxcode.storage.database import DatabaseError


def get_repository(request: Request):
    context = getattr(request.app.state, "agent_context", None)
    if context is None:
        raise DatabaseError("Database runtime is not initialized")
    return context.repository
