from pathlib import Path, PurePosixPath
from unittest.mock import Mock

from laxagent import layout
from laxagent.sandbox.backend import BubblewrapBackend
from laxagent.storage.repositories import Repository
from laxagent.tools.context import ToolContext


def tool_context(workspace: str | Path, user_id="test-user") -> ToolContext:
    workspace = str(layout.get_sandbox_project_workdir(Path(workspace).name))
    return ToolContext(user_id=user_id, repository=Mock(spec=Repository), workspace=workspace,
                       backend=BubblewrapBackend(layout.get_user_sandbox_mounts(user_id), workspace),
                       skill_dir=layout.get_skills_dir(user_id), sandbox_skill_dir=PurePosixPath("/user/skills"))
