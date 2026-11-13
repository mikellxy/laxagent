import asyncio
import json

import httpx2
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.storage.checkpoints import open_checkpointer
from laxcode.web_search import WebSearchService
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot, run_agent
from test_agent import MemoryEvents, state
from test_chat import chat_api, chat_path, payload, wait_run
from test_questions import QUESTIONS, answer_body, paused
from test_written_paths import calls


def searches(count):
    # Image and web requests share the same per-run budget.
    return [("web_search", {"query": f"query {i}", "search_type": "image" if i % 2 else "web"})
            for i in range(count)]


def outputs(request):
    return [item["output"] for item in request["input"] if item.get("type") == "function_call_output"]


@pytest.mark.anyio
async def test_search_boundaries_failures_and_new_run(repository, conversation):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(401 if len(requests) == 2 else 200, json={"Result": {"ResultCount": 0}})

    batch = searches(31)
    batch[2] = ("web_search", {"query": ""})  # Invalid arguments still consume an attempt.
    batch.insert(19, ("list_written_files", {}))
    captured, events = [], MemoryEvents()
    graph = build_graph(InMemorySaver())
    streams = [ModelStream([calls(*batch)]), ModelStream([completed("done")]),
               ModelStream([calls(*searches(1))]), ModelStream([completed("new run")])]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as search_client:
        async with client_for(streams, captured) as client:
            context = AgentContext(client, events, repository, web_search=WebSearchService(search_client, 1))
            result = await run_agent(graph, state(conversation), context)
            assert len(requests) == 28  # Calls 30+ are blocked; call 3 failed validation.
            history = [name for name, _ in batch]
            assert result["tool_call_history"] == history
            run = await repository.get_run(result["run_id"], conversation["user_id"])
            saved = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
            assert saved.values["tool_call_history"] == history
            next_events = MemoryEvents()
            context.events = next_events
            next_result = await run_agent(graph, state(conversation), context)
    assert next_result["tool_call_history"] == ["web_search"] and len(requests) == 29
    assert "收敛联网搜索流程" not in outputs(captured[-1])[-1]
    first_outputs = outputs(captured[1])
    assert len(first_outputs) == 32
    assert "SEARCH_AUTH_FAILED" in first_outputs[1]
    assert "INVALID_ARGUMENTS" in first_outputs[2]
    search_outputs = first_outputs[:19] + first_outputs[20:]
    assert all("收敛联网搜索流程" not in content for content in search_outputs[:19])
    for count in range(20, 30):
        assert f"已调用 web_search {count} 次" in search_outputs[count - 1]
    assert all("SEARCH_LIMIT_REACHED" in content for content in search_outputs[29:])
    assert "收敛联网搜索流程" not in first_outputs[19]
    tool_events = [item for item in [*events.items, *next_events.items] if item["type"] == "tool_end"]
    assert [item["error"] for item in tool_events[-3:]] == ["SEARCH_LIMIT_REACHED", "SEARCH_LIMIT_REACHED", None]
    assert [item["content"]["result"]["call_count"] for item in tool_events[-3:-1]] == [30, 31]
    _, tables = await message_snapshot(repository, conversation["session_id"])
    assert tables[0] == tables[1]
    assert [row.content for row in tables[0] if row.role == "tool"][:32] == first_outputs


@pytest.mark.anyio
async def test_unknown_searches_count_after_postgres_restart(chat_api, migrated_database):
    h = chat_api
    entered = asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 28:
            entered.set()
            await asyncio.Future()
        return httpx2.Response(200, json={"Result": {"ResultCount": 0}})

    captured = []
    streams = [ModelStream([calls(*searches(29))]),
               ModelStream([calls(*searches(2), ("list_written_files", {}))]),
               ModelStream([completed("done")])]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as search_client:
        h.context.web_search = WebSearchService(search_client, 30)
        async with client_for(streams, captured) as client:
            h.context.client = client
            async with open_checkpointer(migrated_database) as saver:
                h.manager.graph = build_graph(saver)
                run_id = (await h.api.post(chat_path(h), json=payload())).json()["run_id"]
                await asyncio.wait_for(entered.wait(), 10)
                assert (await h.api.post(f"/api/v1/runs/{run_id}/cancel")).json()["status"] == "cancelled"
                run = await h.context.repository.get_run(run_id, h.conversation["user_id"])
                config = {"configurable": {"thread_id": run.thread_id}}
                saved = await h.manager.graph.aget_state(config)
                assert saved.values["tool_call_history"] == ["web_search"] * 27
            async with open_checkpointer(migrated_database) as saver:
                h.manager.graph = build_graph(saver)
                assert (await h.api.post(f"/api/v1/runs/{run_id}/resume", json={"resume_time": 0})).status_code == 202
                assert (await wait_run(h, run_id))["status"] == "success"
                saved = await h.manager.graph.aget_state(config)
                assert saved.values["tool_call_history"] == ["web_search"] * 31 + ["list_written_files"]
                await saver.adelete_thread(run.thread_id)
    assert len(requests) == 28  # Neither unknown call is replayed; new searches hit the limit.
    recovered = outputs(captured[1])
    assert len(recovered) == 29
    for count in (28, 29):
        assert "TOOL_STATE_UNCERTAIN" in recovered[count - 1]
        assert f"已调用 web_search {count} 次" in recovered[count - 1]
    final_outputs = outputs(captured[-1])
    assert all("SEARCH_LIMIT_REACHED" in content for content in final_outputs[-3:-1])
    assert json.loads(final_outputs[-1])["error_code"] is None


@pytest.mark.anyio
async def test_question_continuation_preserves_search_count_without_duplicate_history(chat_api):
    h = chat_api
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json={"Result": {"ResultCount": 0}})

    captured = []
    batch = [*searches(19), ("ask_user_question", {"questions": QUESTIONS}), *searches(2)]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as search_client:
        h.context.web_search = WebSearchService(search_client, 1)
        async with client_for([ModelStream([calls(*batch)]), ModelStream([completed("done")])], captured) as client:
            h.context.client = client
            run_id = (await h.api.post(chat_path(h), json=payload())).json()["run_id"]
            run, interrupt_id = await paused(h, run_id)
            config = {"configurable": {"thread_id": run.thread_id}}
            saved = await h.manager.graph.aget_state(config)
            assert saved.values["tool_call_history"] == ["web_search"] * 19
            assert (await h.api.post(f"/api/v1/runs/{run_id}/answers", json=answer_body(interrupt_id))).status_code == 202
            assert (await wait_run(h, run_id))["status"] == "success"
            saved = await h.manager.graph.aget_state(config)
    assert len(requests) == 21
    assert saved.values["tool_call_history"] == [name for name, _ in batch]
    assert "已调用 web_search 20 次" in outputs(captured[-1])[-2]
    assert "已调用 web_search 21 次" in outputs(captured[-1])[-1]
