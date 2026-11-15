from contextlib import aclosing
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from laxagent.agent.context import AgentContext
from laxagent.agent.nodes.loop import call_model, save_model
from laxagent.models.diagnostics import ModelDiagnostics
from laxagent.models.errors import ModelError
from laxagent.models.responses import stream_response
from model_helpers import ModelStream, client_for, completed


def diagnostic(**kwargs):
    return ModelDiagnostics(user_id="test-user", session_id=40, run_id=50, msg_seq=8,
                            resume_time=0, model="ark:test-model", **kwargs)


def read(log):
    return json.loads(log.path.read_text())


@pytest.mark.anyio
async def test_missing_assistant_retains_full_terminal_before_validation():
    final = completed()
    final["response"]["output"] = [{"type": "reasoning", "summary": [], "provider_field": "retained"}]
    final["response"]["provider_extension"] = {"phase": "unknown", "nullable": None}
    log = diagnostic()
    source = ModelStream([
        {"type": "response.output_text.delta", "delta": "earlier text"},
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "fc1", "call_id": "call1", "name": "read_file", "arguments": ""}},
        final,
    ])
    async with client_for(source, []) as client:
        with pytest.raises(ModelError) as exc:
            async for _ in stream_response(client, model="ark:test-model", messages=[], diagnostics=log):
                pass
    assert exc.value.code == "missing_assistant_message"
    data = read(log)
    assert data["terminal_event"] == final
    assert data["error"]["code"] == "missing_assistant_message"
    assert data["parsed"] is None and data["routing"] is None
    assert data["event_counts"]["response.output_text.delta"] == 1
    assert data["stream_tool_calls"][0]["call_id"] == "call1"
    assert source.closed


@pytest.mark.anyio
@pytest.mark.parametrize("with_tool", [False, True])
async def test_loop_records_actual_response_and_routing_without_text_heuristics(with_tool):
    final = completed("我先实现，再测试。")
    final["response"]["output"][0]["phase"] = "commentary"
    if with_tool:
        final["response"]["output"].append({"type": "function_call", "id": "fc1", "call_id": "c1",
                                           "name": "write_file", "arguments": "{}"})
    state = {"user_id": "test-user", "session_id": 40, "project_id": 3, "run_id": 50,
             "next_msg_seq": 8, "model": "ark:test-model", "model_input": [], "messages": []}
    events = SimpleNamespace(publish=AsyncMock())
    repository = SimpleNamespace(append_message=AsyncMock(return_value=(9, [])))
    async with client_for(ModelStream([final]), []) as client:
        runtime = SimpleNamespace(context=AgentContext(client, events, repository))
        update = await call_model(state, runtime)
        path = Path(update["pending_model_response"]["diagnostic_path"])
        result = await save_model({**state, **update}, runtime)
    data = json.loads(path.read_text())
    assert data["terminal_event"] == final
    assert data["parsed"]["tool_call_count"] == int(with_tool)
    assert data["routing"] == {"next_node": "tool_node" if with_tool else "END",
                               "reason": "tool_calls" if with_tool else "no_tool_calls"}
    assert bool(result["pending_tool_calls"]) == with_tool
    assert "diagnostic" not in repr(events.publish.call_args_list)


@pytest.mark.anyio
async def test_redaction_and_aggregation_do_not_change_provider_response():
    secret = 'provider-secret-"key'
    final = completed('echo ' + secret)
    final["response"]["authorization"] = "unconfigured-credential"
    final["response"]["metadata"] = {"nested": json.dumps({"key": secret})}
    log = diagnostic(secrets=(secret,))
    source = ModelStream([*[{"type": "response.reasoning_text.delta", "delta": "thinking"}] * 100, final])
    async with client_for(source, []) as client:
        output = [item async for item in stream_response(client, model="ark:test-model", messages=[], diagnostics=log)]
    raw = log.path.read_text()
    data = read(log)
    assert secret not in raw and "provider-secret" not in raw and "unconfigured-credential" not in raw
    assert data["terminal_event"]["response"]["authorization"] == "[REDACTED]"
    assert output[-1].text == 'echo ' + secret
    assert data["event_counts"]["response.reasoning_text.delta"] == 100
    assert data["delta_characters"]["response.reasoning_text.delta"] == 800
    assert "thinking" not in raw
    assert len(list(log.path.parent.iterdir())) == 1
    assert log.path.stat().st_mode & 0o777 == 0o600
    another = diagnostic()
    assert another.path != log.path and read(log)["status"] == "parsed"


@pytest.mark.anyio
async def test_diagnostic_io_failure_does_not_break_model_or_expose_response(monkeypatch, caplog):
    def denied(*args, **kwargs):
        raise PermissionError("secret-in-exception")
    monkeypatch.setattr(Path, "mkdir", denied)
    log = diagnostic()
    async with client_for(ModelStream([completed("private-answer")]), []) as client:
        result = [item async for item in stream_response(client, model="ark:test-model", messages=[], diagnostics=log)]
    assert result[-1].text == "private-answer"
    assert "write failed" in caplog.text
    assert "secret-in-exception" not in caplog.text and "private-answer" not in caplog.text


@pytest.mark.anyio
async def test_early_close_preserves_interrupted_statistics():
    log = diagnostic()
    source = ModelStream([{"type": "response.created", "response": {}}, completed()])
    async with client_for(source, []) as client:
        async with aclosing(stream_response(client, model="ark:test-model", messages=[], diagnostics=log)) as stream:
            await anext(stream)
    assert read(log)["status"] == "interrupted"
    assert read(log)["terminal_event"] is None
    assert source.closed
