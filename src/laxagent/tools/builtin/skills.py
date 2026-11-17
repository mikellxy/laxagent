from pydantic import BaseModel, ConfigDict, Field

from laxagent import layout
from laxagent.skills import SkillError, file_tree, parse_metadata, read_skill_entry, skill_directory
from laxagent.tools.context import ToolContext
from laxagent.tools.workspace import file_error_code


class LoadSkillArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, description="Exact skill name, matching its directory and metadata.")


def load_skill(context: ToolContext, args: LoadSkillArgs) -> dict:
    try:
        with skill_directory(context.user_id, args.name) as directory:
            files = file_tree(directory)
            if "SKILL.md" not in files:
                raise SkillError("INVALID_SKILL_METADATA")
            content = read_skill_entry(directory)
            parse_metadata(args.name, content)
        root = layout.get_sandbox_skill_dir(args.name)
        return {"name": args.name, "content": content.decode("utf-8"),
                "files": [str(root / path) for path in files]}
    except FileNotFoundError:
        raise SkillError("SKILL_NOT_FOUND") from None
    except UnicodeError:
        raise SkillError("INVALID_SKILL_FILE") from None
    except OSError as exc:
        raise SkillError(file_error_code(exc, "READ_FAILED")) from None
