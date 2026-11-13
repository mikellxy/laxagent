import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from laxagent import layout
from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.agent.nodes.preparation import initialize_workspace
from laxagent.models.errors import ModelError
from laxagent.runtime.runner import run_agent
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_agent import MemoryEvents


def identity(**changes):
    return {"user_id": str(uuid4()), "project_id": 1, "session_id": 1, **changes}


def test_layout_is_pure_and_uses_home(isolated_home):
    state = identity()
    user = layout.get_user_data_dir(state["user_id"])
    assert user == isolated_home / "laxcode" / state["user_id"]
    assert layout.get_project_workdir(state["user_id"], 1) == user / "project/1"
    assert layout.get_session_config_dir(state["user_id"], 1) == user / "session/1"
    assert not user.exists()


@pytest.mark.anyio
async def test_initialization_repairs_missing_directories_and_preserves_shared_files():
    state = identity()
    workspace = Path(initialize_workspace(state))
    user = layout.get_user_data_dir(state["user_id"])
    session = layout.get_session_config_dir(state["user_id"], 1)
    assert workspace.is_dir() and session.is_dir()
    assert list(workspace.iterdir()) == list(session.iterdir()) == []
    assert (user / "skills").is_dir()
    (workspace / "keep.txt").write_text("keep")
    session.rmdir()
    results = await asyncio.gather(*[
        asyncio.to_thread(initialize_workspace, {**state, "session_id": n}) for n in (1, 2, 2)
    ])
    assert results == [str(workspace)] * 3
    assert session.is_dir() and (user / "session/2").is_dir()
    assert (workspace / "keep.txt").read_text() == "keep"
    other_project = Path(initialize_workspace({**state, "project_id": 2, "session_id": 3}))
    other_user = Path(initialize_workspace(identity()))
    assert len({workspace, other_project, other_user}) == 3
    assert not (other_project / "keep.txt").exists()
    assert not (other_user / "keep.txt").exists()
    other_project.rmdir()
    assert Path(initialize_workspace({**state, "project_id": 2, "session_id": 3})).is_dir()


@pytest.mark.parametrize("key,value", [("user_id", "../escape"), ("project_id", "../escape"),
                                      ("session_id", 0), ("project_id", True)])
def test_invalid_identity_creates_nothing(key, value, isolated_home):
    with pytest.raises(ModelError, match="Workspace initialization failed"):
        initialize_workspace(identity(**{key: value}))
    assert list(isolated_home.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("obstruction", ["file", "symlink", "permission"])
async def test_initialization_failure_stops_before_messages_and_model(
    repository, conversation, tmp_path, monkeypatch, obstruction,
):
    user = layout.get_user_data_dir(conversation["user_id"])
    user.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if obstruction == "file":
        (user / "project").write_text("preserve")
    elif obstruction == "symlink":
        (user / "project").symlink_to(outside, target_is_directory=True)
    else:
        def denied(*args, **kwargs):
            raise PermissionError("private path")
        monkeypatch.setattr("laxagent.agent.nodes.preparation.os.mkdir", denied)
    run = await repository.create_run(
        user_id=conversation["user_id"], session_id=conversation["session_id"], user_query="hello",
        model="test:model", context_limit=100, output_limit=10,
    )
    captured, events = [], MemoryEvents()
    async with client_for(ModelStream([completed()]), captured) as client:
        with pytest.raises(ModelError, match="Workspace initialization failed"):
            await run_agent(build_graph(), run.id, AgentContext(client, events, repository))
    stored = await repository.get_run(run.id, run.user_id)
    assert stored.status == "failed" and stored.error == "Workspace initialization failed"
    assert captured == []
    assert await message_snapshot(repository, conversation["session_id"]) == (1, [[], []])
    assert events.items[-1]["content"] == {"status": "failed"}
    assert list(outside.iterdir()) == []
