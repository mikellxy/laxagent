from pydantic import BaseModel, ConfigDict, Field

from laxcode.sandbox.bubblewrap import SandboxError, run_bash
from laxcode.tools.errors import ToolError


class BashArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    command: str = Field(min_length=1, pattern="^[^\x00]+$", description="Bash command to execute once in workspace.")
    timeout_seconds: int | None = Field(ge=1, le=300, description="Time limit in seconds; null uses 120 seconds.")


async def bash(workspace: str, args: BashArgs) -> dict:
    try:
        return await run_bash(workspace, args.command, args.timeout_seconds or 120)
    except SandboxError as exc:
        raise ToolError(str(exc), result=exc.result) from None
