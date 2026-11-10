import json
from types import SimpleNamespace

import pytest
from langgraph.errors import GraphRecursionError
from psycopg import OperationalError

from laxcode.agent.context import AgentContext
from laxcode.models.errors import ModelError
from laxcode.run_errors import RUN_FAILURES, RunError, make_run_error, registered_run_error
from laxcode.runtime.events import EventStoreError
from laxcode.runtime.runner import classify_run_error, run_agent
from laxcode.runtime.task_manager import TaskManager
from laxcode.storage.database import DatabaseError
from test_agent import MemoryEvents


@pytest.mark.parametrize("code", RUN_FAILURES)
def test_catalog_constructs_safe_typed_failures(code):
    definition = RUN_FAILURES[code]
    error = make_run_error(code)
    assert isinstance(error, RunError)
    assert type(error) is definition["exception"]
    assert error.code == code
    assert str(error).strip()
    assert str(error) == (definition["message"] if definition["message"] is not None
                          else str(definition["exception"]()))
    assert error.incomplete == definition.get("incomplete", False)
    assert any("\u4e00" <= char <= "\u9fff" for char in definition["desc"])


def test_factory_defaults_and_rejects_unregistered_codes_and_overrides():
    assert RUN_FAILURES["agent_execution_failed"]["message"] is None
    assert str(make_run_error("agent_execution_failed")) == "Agent execution failed"
    with pytest.raises(KeyError):
        make_run_error("not_registered")
    with pytest.raises(TypeError):
        make_run_error("model_request_failed", message="secret-key")


def test_empty_default_message_is_not_a_valid_definition(monkeypatch):
    class EmptyError(RunError):
        def __init__(self):
            super().__init__("")

    monkeypatch.setitem(RUN_FAILURES, "empty", {
        "exception": EmptyError, "message": None, "desc": "无效定义",
    })
    with pytest.raises(ValueError, match="must not be empty"):
        make_run_error("empty")


def test_registered_error_uses_catalog_payload_not_mutable_exception_fields():
    original = make_run_error("model_request_failed")
    original.args = ("secret-key",)
    original.incomplete = True
    actual = registered_run_error(original)
    assert actual.code == original.code
    assert str(actual) == "Model request failed"
    assert not actual.incomplete
    original.code = "not_registered"
    assert registered_run_error(original).code == "agent_execution_failed"


FAILURES = [
    (OperationalError("checkpoint-secret-key"), "run_storage_failed"),
    (DatabaseError("secret-key"), "run_storage_failed"),
    (DatabaseError("Session not found", 404), "run_storage_failed"),
    (DatabaseError("Session message sequence changed", 409), "run_storage_failed"),
    (EventStoreError("secret-key"), "run_event_failed"),
    (GraphRecursionError("secret-key"), "graph_step_limit_reached"),
    (OSError("secret-key"), "agent_execution_failed"),
    (ModelError("secret-key", incomplete=True), "agent_execution_failed"),
    (RunError("secret-key"), "agent_execution_failed"),
    (make_run_error("model_incomplete_max_output_tokens"), "model_incomplete_max_output_tokens"),
    (make_run_error("model_incomplete_content_filter"), "model_incomplete_content_filter"),
    (make_run_error("model_incomplete_unspecified"), "model_incomplete_unspecified"),
]


@pytest.mark.parametrize("original,code", FAILURES)
def test_execution_boundary_classification(original, code):
    error = classify_run_error(original)
    assert error.code == code
    assert "secret-key" not in str(error)


@pytest.mark.anyio
@pytest.mark.parametrize("original,code", FAILURES)
async def test_worker_persists_catalog_failure_and_preserves_internal_cause(
    original, code, repository, conversation, caplog,
):
    async def fail(*args, **kwargs):
        raise original

    run = await repository.create_run(
        user_id=conversation["user_id"], session_id=conversation["session_id"],
        user_query="hello", model="example:model-name", context_limit=100, output_limit=10,
    )
    events = MemoryEvents()
    with pytest.raises(RunError) as caught:
        await run_agent(SimpleNamespace(ainvoke=fail), run.id, AgentContext(None, events, repository))
    error = caught.value
    assert error.code == code
    assert error.__cause__ is original
    stored = await repository.get_run(run.id, run.user_id)
    assert stored.status == "failed" and stored.finished_at is not None
    assert stored.error == str(make_run_error(code))
    assert events.items[0]["type"] == ("response_incomplete" if error.incomplete else "error")
    assert events.items[-1]["content"] == {"status": "incomplete" if error.incomplete else "failed"}
    assert all(item["error"] == stored.error for item in events.items)
    TaskManager.consume(SimpleNamespace(cancelled=lambda: False, exception=lambda: error))
    assert "secret-key" not in stored.error + json.dumps(events.items) + caplog.text


@pytest.mark.anyio
async def test_repository_does_not_persist_unregistered_failure_payload(repository, conversation):
    run = await repository.create_run(
        user_id=conversation["user_id"], session_id=conversation["session_id"],
        user_query="hello", model="example:model-name", context_limit=100, output_limit=10,
    )
    await repository.finish_run(run.id, error=RunError("secret-key", incomplete=True))
    stored = await repository.get_run(run.id, run.user_id)
    assert stored.status == "failed" and stored.error == "Agent execution failed"
