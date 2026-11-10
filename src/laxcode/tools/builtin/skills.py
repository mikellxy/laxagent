from pathlib import Path
import shlex
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from laxcode.sandbox.bubblewrap import run_bash
from laxcode.skills import SkillError, skill_directory, snapshot
from laxcode.tools.builtin.bash import run_cancellable_sandbox
from laxcode.tools.context import ToolContext
from laxcode.tools.errors import ToolError
from laxcode.tools.workspace import file_path, file_error_code


class LoadSkillArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, description="Exact skill name, matching its directory and metadata.")


class RunSkillScriptsArgs(LoadSkillArgs):
    runner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", description="One executable name, e.g. python3 or bash; no options.")
    script: str = Field(min_length=1, description="Exact relative script path returned by load_skill.")
    args: list[str] = Field(description="Literal arguments; {workspace} expands to the project workspace. No shell syntax.")
    timeout_seconds: int | None = Field(default=None, ge=1, le=300, description="null uses 120 seconds; maximum 300.")


def _snapshot(context, name):
    try:
        with skill_directory(context.user_id, name) as directory:
            return snapshot(directory, name)
    except FileNotFoundError:
        raise SkillError("SKILL_NOT_FOUND") from None
    except UnicodeError:
        raise SkillError("INVALID_SKILL_FILE") from None
    except OSError as exc:
        raise SkillError(file_error_code(exc, "READ_FAILED")) from None


def load_skill(context: ToolContext, args: LoadSkillArgs) -> dict:
    skill = _snapshot(context, args.name)
    context.loaded_skills[args.name] = {"digest": skill.digest, "files": skill.files}
    return {"name": args.name, "content": skill.contents["SKILL.md"].decode("utf-8"), "files": skill.files}


def _run_skill(context: ToolContext, args: RunSkillScriptsArgs, *, stop_event) -> dict:
    loaded = context.loaded_skills.get(args.name)
    if loaded is None:
        raise SkillError("SKILL_NOT_LOADED")
    relative = file_path(args.script)
    if relative.as_posix() != args.script or args.script not in loaded["files"]:
        raise SkillError("INVALID_SKILL_SCRIPT")
    try:
        for arg in args.args:
            arg.encode("utf-8")
            if "\x00" in arg:
                raise ValueError
    except (ValueError, UnicodeError):
        raise SkillError("INVALID_ARGUMENTS") from None
    skill = _snapshot(context, args.name)
    if skill.digest != loaded["digest"]:
        raise SkillError("SKILL_CHANGED")
    # The immutable copy pins all resources, not just the entry script, while
    # leaving user edits free to proceed during a long-running invocation.
    try:
        with tempfile.TemporaryDirectory(prefix="laxcode-skill-") as temporary:
            root = Path(temporary).resolve()
            for path, content in skill.contents.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            arguments = [arg.replace("{workspace}", context.workspace) for arg in args.args]
            command = shlex.join([args.runner, "./" + args.script, *arguments])
            return run_bash(context.workspace, command, args.timeout_seconds or 120,
                            stop_event=stop_event, skill_dir=str(root))
    except OSError as exc:
        raise ToolError(file_error_code(exc, "READ_FAILED")) from None


async def run_skill_scripts(context: ToolContext, args: RunSkillScriptsArgs) -> dict:
    return await run_cancellable_sandbox(_run_skill, context, args)
