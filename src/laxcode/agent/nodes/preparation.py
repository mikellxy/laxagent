import asyncio
import os
from pathlib import Path
from uuid import UUID

from langgraph.runtime import Runtime

from laxcode import layout
from laxcode.context.prompts import system_prompt
from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.run_errors import make_run_error
from laxcode.models.responses import messages_to_input
from laxcode.skills import initialize_skills, skill_catalog, SkillError


def _ensure_directory(home: Path, path: Path) -> None:
    parts = path.relative_to(home).parts
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("Invalid data path")
    # Walk from the trusted home using descriptors, never following data symlinks.
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
    finally:
        os.close(directory)


def initialize_workspace(state: AgentState, builtin_skills_dir: Path | None = None) -> str:
    try:
        user_id = state["user_id"]
        if str(UUID(user_id)) != user_id:
            raise ValueError("Invalid user ID")
        if any(type(state[key]) is not int or state[key] <= 0 for key in ("project_id", "session_id")):
            raise ValueError("Invalid resource ID")
        user_dir = layout.get_user_data_dir(user_id)
        session_dir = layout.get_session_config_dir(user_id, state["session_id"])
        workspace = layout.get_project_workdir(user_id, state["project_id"])
        session_dir.relative_to(user_dir)
        workspace.relative_to(user_dir)
        for path in (user_dir, session_dir, workspace):
            _ensure_directory(Path.home(), path)
        initialize_skills(user_id, builtin_skills_dir)
        return str(workspace)
    except (OSError, ValueError, SkillError):
        raise make_run_error("workspace_initialization_failed") from None


async def prepare(state: AgentState, runtime: Runtime[AgentContext]):
    workspace = await asyncio.to_thread(initialize_workspace, state, runtime.context.builtin_skills_dir)
    skills = await asyncio.to_thread(skill_catalog, state["user_id"])
    next_seq, messages = await runtime.context.repository.append_message(
        user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
        role="user", content=state["user_query"], load_context=True,
        appendix=state.get("appendix", []),
        expected_seq=state.get("next_msg_seq"), replay_safe=True,
    )
    messages = [{"role": "system", "content": system_prompt(workspace, state.get("user_configs"), skills)}, *messages]
    return {
        "workspace": workspace, "pending_tool_calls": [],
        "model_input": messages_to_input(messages),
        "next_msg_seq": next_seq,
        "messages": messages,
        "loaded_skills": {},
    }
