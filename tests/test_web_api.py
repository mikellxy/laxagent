import asyncio
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from laxcode.agent.context import AgentContext
from laxcode.api.app import create_app
from laxcode.config import AppConfig
from laxcode.layout import get_user_data_dir
from laxcode.storage.models import Message, Project, Session, SessionMessage, User


@pytest.fixture
async def web_api(repository, config_data):
    app = create_app(AppConfig.model_validate(config_data))
    app.state.agent_context = AgentContext(None, None, repository)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
        yield api


async def login(api, user_id, guest=False):
    response = await api.post("/api/v1/login", json={"user_id": user_id, **({"from_page": "guest_login"} if guest else {})})
    assert response.status_code == 200
    api.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return response.json()


@pytest.mark.anyio
async def test_guest_initialization_is_atomic_idempotent_and_concurrent(web_api, repository):
    user_id = str(uuid4())
    results = await asyncio.gather(*[login(web_api, user_id, True) for _ in range(6)])
    assert len({r["initial_session_id"] for r in results}) == 1
    session_id = results[0]["initial_session_id"]
    projects = (await web_api.get("/api/v1/projects")).json()
    assert len(projects) == 1 and projects[0]["name"] == "LaxCode"
    project_id = projects[0]["id"]
    sessions = (await web_api.get(f"/api/v1/projects/{project_id}/sessions")).json()
    assert len(sessions) == 1 and sessions[0]["name"] == "LaxCode"
    detail = (await web_api.get(f"/api/v1/sessions/{session_id}")).json()
    assert detail["latest_run"] is None and detail["next_msg_seq"] == 1
    assert not get_user_data_dir(user_id).exists()
    responses = await asyncio.gather(*[web_api.post(f"/api/v1/projects/{project_id}/sessions") for _ in range(4)])
    assert all(r.status_code == 201 for r in responses)
    assert {r.json()["name"] for r in responses} == {f"会话 {n}" for n in range(2, 6)}
    renamed = await web_api.patch(f"/api/v1/sessions/{session_id}", json={"name": " renamed "})
    assert renamed.json()["name"] == "renamed"
    assert (await login(web_api, user_id))["initial_session_id"] is None
    assert len((await web_api.get(f"/api/v1/projects/{project_id}/sessions")).json()) == 5


@pytest.mark.anyio
async def test_guest_failure_rolls_back_user_project_session(web_api, repository):
    user_id = str(uuid4())
    def fail(*args):
        raise IntegrityError("forced", {}, RuntimeError())
    event.listen(Session, "before_insert", fail)
    try:
        response = await web_api.post("/api/v1/login", json={"user_id": user_id, "from_page": "guest_login"})
    finally:
        event.remove(Session, "before_insert", fail)
    assert response.status_code == 409 and "access_token" not in response.json()
    async with repository.db.transaction() as tx:
        for model in (User, Project, Session):
            assert await tx.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.anyio
async def test_model_catalog_and_resource_ownership(web_api, repository, conversation):
    await login(web_api, str(uuid4()))
    catalog = (await web_api.get("/api/v1/models")).json()
    assert catalog == {"default_model": "example:model-name", "models": [
        {"id": "example:model-name", "provider": "example", "name": "model-name", "supports_multimodal": False},
    ]}
    assert (await web_api.get("/api/v1/projects")).json() == []
    sid, pid = conversation["session_id"], conversation["project_id"]
    for path in (f"/api/v1/projects/{pid}/sessions", f"/api/v1/sessions/{sid}", f"/api/v1/sessions/{sid}/messages"):
        assert (await web_api.get(path)).status_code == 404
    assert (await web_api.patch(f"/api/v1/sessions/{sid}", json={"name": "no"})).status_code == 404
    assert (await web_api.post(f"/api/v1/projects/{pid}/sessions")).status_code == 404
    web_api.headers.pop("Authorization")
    for path in ("/api/v1/models", "/api/v1/projects", f"/api/v1/sessions/{sid}", f"/api/v1/sessions/{sid}/messages"):
        assert (await web_api.get(path)).status_code == 401


@pytest.mark.anyio
async def test_history_uses_messages_and_exclusive_sequence_cursor(web_api, repository, conversation):
    await login(web_api, conversation["user_id"])
    for n in range(6):
        await repository.append_message(**conversation, role="user", content=str(n))
    call = {"call_id": "c1", "name": "read_file", "arguments": '{"path":"hello.txt"}'}
    await repository.append_message(**conversation, role="tool", content="private result", tool_name="read_file", tool_call=call)
    async with repository.db.transaction() as tx:
        row = await tx.scalar(select(SessionMessage).where(SessionMessage.session_id == conversation["session_id"], SessionMessage.seq == 1))
        row.content = "compressed context"
    path = f"/api/v1/sessions/{conversation['session_id']}"
    detail = (await web_api.get(path)).json()
    assert detail["next_msg_seq"] == 8
    # A later commit must not leak into an earlier detail snapshot's bounded page.
    await repository.append_message(**conversation, role="assistant", content="newer")
    page = (await web_api.get(path + "/messages", params={"before_seq": 8, "limit": 2})).json()
    assert [m["seq"] for m in page["items"]] == [6, 7]
    assert page["items"][-1] == {"seq": 7, "role": "tool", "tool_name": "read_file", "arguments": call["arguments"]}
    assert page["has_more"] and page["next_before_seq"] == 6
    older = (await web_api.get(path + "/messages", params={"before_seq": 6})).json()
    assert older["items"][0]["content"] == "0"
    assert [m["seq"] for m in older["items"]] == [1, 2, 3, 4, 5]
    assert not older["has_more"] and older["next_before_seq"] is None
    assert (await web_api.get(path + "/messages", params={"before_seq": 1})).json()["items"] == []
    assert (await web_api.get(path + "/messages", params={"limit": 101})).status_code == 422
    run = await repository.create_run(user_id=conversation["user_id"], session_id=conversation["session_id"],
                                      user_query="hi", model="example:model-name", context_limit=100, output_limit=10)
    assert (await web_api.get(path)).json()["latest_run"]["status"] == "waiting"
    await repository.claim_run(run.id)
    assert (await web_api.get(path)).json()["latest_run"]["status"] == "running"
    await repository.finish_run(run.id)
    detail = (await web_api.get(path)).json()
    assert detail["latest_run"]["run_id"] == run.id and detail["latest_run"]["status"] == "success"
    assert "thread_id" not in detail["latest_run"]
