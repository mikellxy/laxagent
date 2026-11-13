import asyncio
import json
from typing import get_args
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.context.prompts import system_prompt
from laxagent.memory import MEMORY_CATEGORY_DESCRIPTIONS, MemoryCategory, MemoryUnavailableError
from laxagent.run_errors import RunError
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Memory, SessionMessage
from laxagent.tools.context import ToolContext
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
from tool_helpers import tool_context
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot, run_agent
from test_agent import MemoryEvents, state


def write_args(**changes):
    return {"abstract": "回答偏好", "detail": "普通问题简洁回答，架构问题展开说明。",
            "category": "preference", "merge_ids": [], **changes}


async def memories(repository):
    async with repository.db.transaction() as transaction:
        return list((await transaction.execute(select(
            Memory.id, Memory.user_id, Memory.abstract, Memory.detail, Memory.category,
        ).order_by(Memory.id))).all())


def tool_response(name, arguments, call_id="call_memory"):
    response = completed("")
    response["response"]["output"] = [{
        "type": "function_call", "id": f"fc_{call_id}", "call_id": call_id,
        "name": name, "arguments": json.dumps(arguments),
    }]
    return response


def test_memory_schema_has_no_identity_and_matches_prompt_categories():
    categories = set(get_args(MemoryCategory))
    assert categories == set(MEMORY_CATEGORY_DESCRIPTIONS)
    definitions = {item["name"]: item for item in tool_definitions({"enable_memory": "1"})}
    for name in ("write_memory", "list_memory_abstracts", "get_memory_details"):
        schema = definitions[name]["parameters"]
        assert definitions[name]["strict"] is True
        assert schema["additionalProperties"] is False
        assert "user_id" not in schema["properties"]
        assert set(schema["required"]) == set(schema["properties"])
    assert set(definitions["write_memory"]["parameters"]["properties"]["category"]["enum"]) == categories
    assert set(definitions["list_memory_abstracts"]["parameters"]["properties"]["categories"]["items"]["enum"]) == categories
    prompt = system_prompt("/workspace", {"enable_memory": "1"})
    for category in categories:
        assert f"- {category}:" in prompt


@pytest.mark.anyio
@pytest.mark.parametrize("name,args", [
    ("write_memory", write_args(user_id="forged")),
    ("write_memory", write_args(category="other")),
    ("write_memory", write_args(category=["profile", "preference"])),
    ("write_memory", write_args(abstract=" \n\t")),
    ("write_memory", write_args(detail="\u3000")),
    ("write_memory", write_args(merge_ids=[True])),
    ("write_memory", write_args(merge_ids=["1"])),
    ("write_memory", write_args(merge_ids=[0])),
    ("write_memory", write_args(merge_ids=None)),
    ("write_memory", {"abstract": "a", "detail": "d", "category": "profile"}),
    ("list_memory_abstracts", {"categories": []}),
    ("list_memory_abstracts", {"categories": ["preference", "other"]}),
    ("list_memory_abstracts", {"categories": ["profile"], "user_id": "forged"}),
    ("list_memory_abstracts", {"categorys": ["profile"]}),
    ("get_memory_details", {"ids": []}),
    ("get_memory_details", {"ids": [-1]}),
    ("get_memory_details", {"ids": [2**31]}),
    ("get_memory_details", {"ids": [1], "user_id": "forged"}),
])
async def test_invalid_memory_arguments_are_rejected_before_execution(name, args):
    assert await execute_tool(name, json.dumps(args), context=tool_context("/unused"), user_configs={"enable_memory": "1"}) == {
        "error_code": "INVALID_ARGUMENTS", "result": {},
    }


@pytest.mark.anyio
async def test_memory_migration_schema_and_all_categories(repository, conversation):
    def check(connection):
        schema = inspect(connection)
        columns = {column["name"]: column for column in schema.get_columns("memories")}
        assert set(columns) == {"id", "user_id", "abstract", "detail", "category"}
        assert all(not column["nullable"] for column in columns.values())
        assert columns["id"]["identity"]["always"]
        assert schema.get_foreign_keys("memories") == []
        assert any(index["column_names"] == ["user_id", "category", "id"]
                   for index in schema.get_indexes("memories"))

    async with repository.db.engine.connect() as connection:
        await connection.run_sync(check)
    for category in get_args(MemoryCategory):
        await repository.write_memory(user_id=conversation["user_id"], **write_args(category=category))
    assert len(await memories(repository)) == 6


@pytest.mark.anyio
@pytest.mark.parametrize("changes", [{"category": "other"}, {"abstract": " \n\t"}, {"detail": "\n"}])
async def test_database_constraints_reject_bypassed_tool_validation(repository, conversation, changes):
    with pytest.raises(DatabaseError):
        async with repository.db.transaction() as transaction:
            transaction.add(Memory(
                **{"user_id": conversation["user_id"], "abstract": "a", "detail": "d",
                   "category": "profile", **changes},
            ))
    assert await memories(repository) == []


@pytest.mark.anyio
async def test_reads_are_user_scoped_ordered_and_complete(repository, conversation):
    user = conversation["user_id"]
    other_user = str(uuid4())
    await repository.create_user(other_user)
    first = await repository.write_memory(user_id=user, **write_args(category="project", abstract="项目 A"))
    second = await repository.write_memory(user_id=user, **write_args(category="profile"))
    third = await repository.write_memory(user_id=user, **write_args(category="profile", abstract="其他背景"))
    foreign = await repository.write_memory(user_id=other_user, **write_args(category="profile", detail="private"))
    context = ToolContext(user, repository, workspace="/unused")
    result = await execute_tool("list_memory_abstracts", json.dumps({"categories": ["project", "profile", "profile"]}),
                                user_configs={"enable_memory": "1"}, context=context)
    assert result == {"error_code": None, "result": {"memory_abstracts": [
        {key: memory[key] for key in ("category", "id", "abstract")} for memory in (second, third, first)
    ]}}
    details = await execute_tool("get_memory_details", json.dumps({"ids": [third["id"], first["id"], third["id"]]}),
                                user_configs={"enable_memory": "1"}, context=context)
    assert details == {"error_code": None, "result": {"memory_list": [
        {key: memory[key] for key in ("category", "id", "detail")} for memory in (third, first)
    ]}}
    for unavailable in (foreign["id"], 2**31 - 1):
        result = await execute_tool("get_memory_details", json.dumps({"ids": [first["id"], unavailable]}),
                                    user_configs={"enable_memory": "1"}, context=context)
        assert result == {"error_code": "MEMORY_UNAVAILABLE", "result": {}}
    assert await repository.list_memory_abstracts(user_id=user, categories=["workflow"]) == []


@pytest.mark.anyio
async def test_replacement_reclassifies_and_merge_deduplicates_ids(repository, conversation):
    user = conversation["user_id"]
    context = ToolContext(user, repository, workspace="/unused")
    async def write(**changes):
        response = await execute_tool("write_memory", json.dumps(write_args(**changes)),
                                      user_configs={"enable_memory": "1"}, context=context)
        assert response["error_code"] is None
        return response["result"]

    first = await write()
    updated = await write(category="instruction", merge_ids=[first["id"]], detail="新规则")
    assert updated["id"] != first["id"] and updated["category"] == "instruction"
    second = await write(detail="规则的例外")
    unrelated = await write(category="profile")
    merged = await write(category="instruction", abstract="完整规则", detail="新规则及例外",
                         merge_ids=[second["id"], updated["id"], second["id"]])
    assert set(merged) == {"id", "category", "abstract", "detail"}
    assert {row.id for row in await memories(repository)} == {unrelated["id"], merged["id"]}
    with pytest.raises(MemoryUnavailableError):
        await repository.get_memory_details(user_id=user, ids=[first["id"]])


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["foreign", "missing", "replaced"])
async def test_invalid_merge_changes_nothing(repository, conversation, target):
    user = conversation["user_id"]
    other = str(uuid4())
    await repository.create_user(other)
    owned = await repository.write_memory(user_id=user, **write_args())
    foreign = await repository.write_memory(user_id=other, **write_args())
    bad_id = foreign["id"] if target == "foreign" else 2**31 - 1
    if target == "replaced":
        old = await repository.write_memory(user_id=user, **write_args())
        await repository.write_memory(user_id=user, **write_args(merge_ids=[old["id"]]))
        bad_id = old["id"]
    before = await memories(repository)
    result = await execute_tool("write_memory", json.dumps(write_args(merge_ids=[owned["id"], bad_id])),
                                user_configs={"enable_memory": "1"}, context=ToolContext(user, repository, workspace="/unused"))
    assert result == {"error_code": "MEMORY_UNAVAILABLE", "result": {}}
    assert await memories(repository) == before


@pytest.mark.anyio
async def test_failure_after_delete_rolls_back_new_memory_and_old_deletions(repository, conversation):
    user = conversation["user_id"]
    old = await repository.write_memory(user_id=user, **write_args())
    before = await memories(repository)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM memories"):
            raise IntegrityError("private-query", {}, RuntimeError("secret-password"))

    event.listen(repository.db.engine.sync_engine, "after_cursor_execute", fail)
    try:
        result = await execute_tool("write_memory", json.dumps(write_args(merge_ids=[old["id"]])),
                                    user_configs={"enable_memory": "1"}, context=ToolContext(user, repository, workspace="/unused"))
    finally:
        event.remove(repository.db.engine.sync_engine, "after_cursor_execute", fail)
    assert result == {"error_code": "MEMORY_STORAGE_FAILED", "result": {}}
    assert await memories(repository) == before


@pytest.mark.anyio
async def test_concurrent_overlapping_merges_have_one_winner(repository, conversation):
    user = conversation["user_id"]
    first = await repository.write_memory(user_id=user, **write_args())
    second = await repository.write_memory(user_id=user, **write_args())
    results = await asyncio.wait_for(asyncio.gather(
        repository.write_memory(user_id=user, **write_args(merge_ids=[first["id"], second["id"]], detail="A")),
        repository.write_memory(user_id=user, **write_args(merge_ids=[second["id"], first["id"]], detail="B")),
        return_exceptions=True,
    ), 5)
    assert sum(isinstance(result, MemoryUnavailableError) for result in results) == 1
    winner = next(result for result in results if isinstance(result, dict))
    stored = await memories(repository)
    assert len(stored) == 1 and stored[0].id == winner["id"] and stored[0].detail == winner["detail"]


@pytest.mark.anyio
async def test_graph_reads_updates_and_reuses_memory_across_sessions(repository, conversation):
    await repository.update_user_config(conversation["user_id"], "enable_memory", "1")
    old = await repository.write_memory(user_id=conversation["user_id"], **write_args())
    events, captured = MemoryEvents(), []
    streams = [ModelStream([response]) for response in [
        tool_response("list_memory_abstracts", {"categories": ["preference"]}, "list"),
        tool_response("get_memory_details", {"ids": [old["id"]]}, "get"),
        tool_response("write_memory", write_args(merge_ids=[old["id"]], detail="架构讨论简洁回答"), "write"),
        completed("已更新记忆"),
    ]]
    async with client_for(streams, captured) as client:
        await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    stored = await memories(repository)
    assert len(stored) == 1 and stored[0].user_id == conversation["user_id"]
    assert stored[0].id != old["id"] and stored[0].detail == "架构讨论简洁回答"
    assert old["detail"] not in json.dumps(captured[0]["input"], ensure_ascii=False)
    _, tables = await message_snapshot(repository, conversation["session_id"])
    tools = [message for message in tables[0] if message.role == "tool"]
    assert [message.tool_name for message in tools] == ["list_memory_abstracts", "get_memory_details", "write_memory"]
    assert all(json.loads(message.content)["error_code"] is None for message in tools)
    for message in tools:
        assert "user_id" not in json.loads(message.tool_call["arguments"])
    # The next session can read the same memory even in another project.
    project = await repository.create_project(conversation["user_id"], "second")
    session = await repository.create_session(conversation["user_id"], project.id)
    next_conversation = {**conversation, "project_id": project.id, "session_id": session.id}
    captured = []
    streams = [ModelStream([tool_response("get_memory_details", {"ids": [stored[0].id]})]),
               ModelStream([completed("已参考记忆")])]
    async with client_for(streams, captured) as client:
        await run_agent(build_graph(), state(next_conversation), AgentContext(client, MemoryEvents(), repository))
    output = json.loads(captured[1]["input"][-1]["output"])
    assert output["result"]["memory_list"][0]["detail"] == "架构讨论简洁回答"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["database", "event"])
async def test_committed_memory_survives_message_or_event_failure(repository, conversation, failure):
    await repository.update_user_config(conversation["user_id"], "enable_memory", "1")
    def fail(mapper, connection, message):
        if message.role == "tool":
            raise IntegrityError("forced failure", {}, RuntimeError("test"))

    events = MemoryEvents("tool_end" if failure == "event" else None)
    if failure == "database":
        event.listen(SessionMessage, "before_insert", fail)
    captured = []
    try:
        async with client_for(ModelStream([tool_response("write_memory", write_args())]), captured) as client:
            message = "Run data operation failed" if failure == "database" else "Event storage unavailable"
            with pytest.raises(RunError, match=message):
                await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    finally:
        if failure == "database":
            event.remove(SessionMessage, "before_insert", fail)
    assert len(await memories(repository)) == 1
    assert len(captured) == 1
    assert sum(item["type"] == "tool_start" for item in events.items) == 1
