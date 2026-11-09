import asyncio
from threading import Event

from pydantic import BaseModel, ConfigDict, Field

from laxcode.sandbox.bubblewrap import SandboxError, run_bash
from laxcode.tools.errors import ToolError


class BashArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    command: str = Field(min_length=1, pattern="^[^\x00]+$", description="Bash command to execute once in workspace.")
    timeout_seconds: int | None = Field(ge=1, le=300, description="Time limit in seconds; null uses 120 seconds.")


async def bash(workspace: str, args: BashArgs) -> dict:
    stop_event = Event()
    worker = asyncio.create_task(asyncio.to_thread(
        run_bash, workspace, args.command, args.timeout_seconds or 120, stop_event=stop_event,
    ))
    interrupted = False
    try:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                interrupted = True
                stop_event.set()
        return worker.result()
    except SandboxError as exc:
        raise ToolError(str(exc), result=exc.result) from None
    finally:
        # Cancelling the await cannot stop a thread; wait for its cleanup first.
        if interrupted:
            raise asyncio.CancelledError
