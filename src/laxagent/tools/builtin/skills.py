from pydantic import BaseModel, ConfigDict, Field

from laxagent.skills import SkillError, skill_directory, snapshot
from laxagent.tools.context import ToolContext
from laxagent.tools.workspace import file_error_code


class LoadSkillArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, description="Exact skill name, matching its directory and metadata.")


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
