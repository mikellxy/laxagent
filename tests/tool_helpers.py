from pathlib import Path
from unittest.mock import Mock

from laxagent.storage.repositories import Repository
from laxagent.tools.context import ToolContext


def tool_context(workspace: str | Path) -> ToolContext:
    return ToolContext(user_id="test-user", repository=Mock(spec=Repository), workspace=str(workspace))
