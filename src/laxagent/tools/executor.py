import asyncio
import inspect
import json

from laxagent.tools.errors import ToolError
from laxagent.tools.registry import available_tools
from laxagent.tools.context import ToolContext
from laxagent.memory import MemoryUnavailableError
from laxagent.storage.database import DatabaseError


async def run_sync_tool(execute, context: ToolContext, args):
    # asyncio cancellation cannot stop a file-operation thread. Drain it before
    # allowing the run to finish, while preserving cancellation over its result.
    worker = asyncio.create_task(asyncio.to_thread(execute, context, args))
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


async def execute_tool(name: str, arguments: str, *, context: ToolContext,
                       user_configs: dict[str, str] | None = None) -> dict:
    tool = available_tools(user_configs, web_search_enabled=context.web_search is not None).get(name)
    if tool is None:
        return {"error_code": "UNKNOWN_TOOL", "result": {}}
    try:
        args = tool.schema.model_validate(json.loads(arguments))
    except ValueError:
        return {"error_code": "INVALID_ARGUMENTS", "result": {}}
    try:
        if inspect.iscoroutinefunction(tool.execute):
            result = await tool.execute(context, args)
        else:
            result = await run_sync_tool(tool.execute, context, args)
        return {"error_code": None, "result": result}
    except MemoryUnavailableError:
        return {"error_code": "MEMORY_UNAVAILABLE", "result": {}}
    except DatabaseError:
        return {"error_code": "MEMORY_STORAGE_FAILED", "result": {}}
    except ToolError as exc:
        return {"error_code": str(exc), "result": exc.result}
