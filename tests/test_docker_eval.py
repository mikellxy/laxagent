"""Mode composition, persisted bindings, and unmodified tool paths."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.api.app import create_app
from laxagent.config import AppConfig
from laxagent.eval_api.app import create_eval_app
from laxagent.layout import get_project_workdir, get_skills_dir
from laxagent.runtime.profile import DockerEvalProfile, StandardProfile
from laxagent.runtime.task_manager import TaskManager
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Project
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
from model_helpers import ModelStream, client_for, completed
from storage_helpers import run_agent
from test_agent import MemoryEvents
from tool_helpers import tool_context


def test_eval_schema_has_no_host_paths_or_unavailable_tools():
    tools = tool_definitions({"enable_memory": "1"}, web_search_enabled=True, tool_names=DockerEvalProfile.tool_names)
    assert {t["name"] for t in tools} == DockerEvalProfile.tool_names
    assert "/user" not in json.dumps(tools)
    assert "bubblewrap" not in json.dumps(tools)


@pytest.mark.anyio
async def test_tool_dispatch_cannot_call_hidden_tools():
    context = replace(tool_context("/testbed"), tool_names=DockerEvalProfile.tool_names)
    for name in ("load_skill", "read_appendix", "ask_appendices", "ask_user_question", "write_memory"):
        result = await execute_tool(name, "{}", context=context, user_configs={"enable_memory": "1"})
        assert result["error_code"] == "UNKNOWN_TOOL"


@pytest.mark.anyio
async def test_project_api_binds_only_after_preparation(repository, config_data, monkeypatch):
    prepared = []
    def install(container, workspace):
        prepared.append((container, workspace))
        if container == "broken":
            from laxagent.sandbox.bubblewrap import SandboxError
            raise SandboxError("DOCKER_CONTAINER_NOT_RUNNING")
        return "a" * 64
    monkeypatch.setattr("laxagent.runtime.profile.prepare_container", install)
    app = create_app(eval_in_docker=True)
    app.state.config = AppConfig.model_validate(config_data)
    app.state.agent_context = AgentContext(None, None, repository, profile=app.state.profile)
    app.state.task_manager = SimpleNamespace(submit=AsyncMock())
    user = str(uuid4())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test") as api:
        login = await api.post("/api/v1/login", json={"user_id": user, "from_page": "guest_login"})
        assert login.status_code == 200 and login.json()["initial_session_id"] is None
        assert not get_skills_dir(user).exists()
        api.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        for body in ({"name":"x"}, {"name":"x","container_id":"c","container_workspace":"relative"},
                     {"name":"x","container_id":"-bad","container_workspace":"/testbed"}):
            assert (await api.post("/api/v1/projects", json=body)).status_code == 422
        assert prepared == []
        for container, status in (("broken",503),("case",201)):
            response = await api.post("/api/v1/projects", json={"name":"case", "container_id":container,
                                                               "container_workspace":"/testbed"})
            assert response.status_code == status
        project = await repository.get_project(user, response.json()["id"])
        assert project.container_id == "a" * 64 and project.container_workspace == "/testbed"
        async with repository.db.transaction() as tx:
            assert len(list(await tx.scalars(select(Project).where(Project.user_id == user)))) == 1
        for route in ("/api/v1/skills", "/api/v1/appendices/1", "/api/v1/sessions/1/workspace-files/preview"):
            assert (await api.get(route)).status_code == 404
    eval_app = create_eval_app(app)
    async with eval_app.router.lifespan_context(eval_app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(eval_app), base_url="http://eval") as api:
            created = await api.post("/eval/create_session", json={"session_name":"case", "container_id":"case",
                                                                  "container_workspace":"/another"})
            assert created.status_code == 201
            record = created.json()
            project = await repository.get_project(record["user_id"], record["project_id"])
            assert project.container_workspace == "/another"
            assert (await api.post("/eval/collect", json={})).status_code == 404
            assert (await api.post("/eval/init_session", json={})).status_code == 404
    with pytest.raises(DatabaseError, match="Container binding"):
        StandardProfile().prepare_project("case", "/testbed")


@pytest.mark.anyio
async def test_eval_graph_uses_container_context(repository, monkeypatch):
    user = str(uuid4())
    await repository.create_user(user)
    project = await repository.create_project(user, "case", container_id="a"*64, container_workspace="/testbed")
    session = await repository.create_session(user, project.id)
    captured, calls = [], []
    class Backend:
        def __init__(self, container_id, workspace):
            assert container_id == "a"*64 and workspace == "/testbed"
        def run_file_tool(self, name, request, *, stop_event):
            calls.append((name, request))
            return {"error_code": None, "result": {"path": request["path"], "bytes_written": 1}}
    monkeypatch.setattr("laxagent.runtime.profile.DockerBackend", Backend)
    first = completed("")
    first["response"]["output"] = [{"type":"function_call", "id":"fc_1", "call_id":"c_1",
                                      "name":"write_file", "arguments":json.dumps({"path":"/testbed/a.txt", "operation":"write", "content":"a"})}]
    graph = build_graph(InMemorySaver())
    async with client_for([ModelStream([first]),ModelStream([completed("done")])], captured) as client:
        context = AgentContext(client, MemoryEvents(), repository, profile=DockerEvalProfile())
        result = await run_agent(graph, {"user_id":user,"project_id":project.id,"session_id":session.id,
                                         "user_query":"do it","model":"example:model-name"}, context)
    assert context.workspace == "" and context.backend is None  # shared dependencies were not mutated
    assert calls[0][1]["path"] == "/testbed/a.txt"
    assert result["workspace"] == "/testbed" and result["written_workspace_paths"] == ["a.txt"]
    assert not get_project_workdir(user, project.id).exists() and not get_skills_dir(user).exists()
    prompt = captured[0]["input"][0]["content"]
    assert "/testbed" in prompt and "/user" not in prompt and "skill" not in prompt.lower()
    assert {t["name"] for t in captured[0]["tools"]} == DockerEvalProfile.tool_names - {"web_search"}
    manager = TaskManager(graph, context)
    with pytest.raises(DatabaseError, match="Attachments"):
        await manager._submit({"user_id":user,"session_id":session.id,"appendix":[{"id":1,"name":"x"}]})
