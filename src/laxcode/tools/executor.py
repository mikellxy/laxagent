import asyncio
import inspect
import json

from laxcode.tools.errors import ToolError
from laxcode.tools.registry import TOOLS
from laxcode.tools.context import ToolContext
from laxcode.memory import MemoryUnavailableError
from laxcode.storage.database import DatabaseError


async def run_sync_tool(execute, target, args):
    # asyncio cancellation cannot stop a file-operation thread. Drain it before
    # allowing the run to finish, while preserving cancellation over its result.
    worker = asyncio.create_task(asyncio.to_thread(execute, target, args))
    interrupted = False
    try:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                interrupted = True
        return worker.result()
    finally:
        if interrupted:
            raise asyncio.CancelledError


async def execute_tool(name: str, arguments: str, *, workspace: str, context: ToolContext | None = None) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"error_code": "UNKNOWN_TOOL", "result": {}}
    try:
        args = tool.schema.model_validate(json.loads(arguments))
    except ValueError:
        return {"error_code": "INVALID_ARGUMENTS", "result": {}}
    try:
        if tool.requires_context and context is None:
            raise ToolError("TOOL_CONTEXT_UNAVAILABLE")
        target = context if tool.requires_context else workspace
        if inspect.iscoroutinefunction(tool.execute):
            result = await tool.execute(target, args)
        else:
            result = await run_sync_tool(tool.execute, target, args)
        return {"error_code": None, "result": result}
    except MemoryUnavailableError:
        return {"error_code": "MEMORY_UNAVAILABLE", "result": {}}
    except DatabaseError:
        return {"error_code": "MEMORY_STORAGE_FAILED", "result": {}}
    except ToolError as exc:
        return {"error_code": str(exc), "result": exc.result}
