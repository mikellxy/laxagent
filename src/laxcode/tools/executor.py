import asyncio
import json

from laxcode.tools.errors import ToolError
from laxcode.tools.registry import TOOLS


async def execute_tool(name: str, arguments: str, *, workspace: str) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"error_code": "UNKNOWN_TOOL", "result": {}}
    schema, execute = tool
    try:
        args = schema.model_validate(json.loads(arguments))
    except ValueError:
        return {"error_code": "INVALID_ARGUMENTS", "result": {}}
    try:
        result = await asyncio.to_thread(execute, workspace, args)
        return {"error_code": None, "result": result}
    except ToolError as exc:
        return {"error_code": str(exc), "result": {}}
