import asyncio
import json

import pytest
from pydantic import ValidationError

from laxagent.tools.builtin.ask_user_question import AskUserQuestionArgs
from laxagent.tools.executor import execute_tool
from laxagent.runtime.events import EventStoreError
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_chat import chat_api, chat_path, payload, wait_run
from test_tool_loop import tool_response

QUESTIONS = [{"question": "选择方式？", "choice": ["方案一", "方案二"]}, {"question": "补充说明？", "choice": []}]
ANSWERS = [{"question": "选择方式？", "answer": "自己的方案"}, {"question": "补充说明？", "answer": "用户未回答"}]


def question_response(names=("ask_user_question",)):
    response = tool_response(tuple("test.txt" for _ in names))
    for call, name in zip(response['response']['output'][1:], names):
        call['name'] = name
        call['arguments'] = json.dumps({"questions": QUESTIONS} if name == 'ask_user_question' else
                                       {"path": "test.txt", "content": "written"})
    return response


async def paused(h, run_id, attempt=0):
    async with asyncio.timeout(5):
        while True:
            run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
            if run.status == 'paused' and run.resume_time == attempt:
                execution = h.manager.executions.get((run_id, attempt))
                if execution:
                    await execution.task
                snapshot = await h.manager.graph.aget_state({'configurable': {'thread_id': run.thread_id}})
                return run, snapshot.interrupts[0].id
            assert run.status not in ('failed', 'success'), run.error
            await asyncio.sleep(.01)


def answer_body(interrupt_id, attempt=0):
    return {"resume_time": attempt, "interrupt_id": interrupt_id, "answers": ANSWERS}


@pytest.mark.anyio
async def test_ordered_questions_checkpoint_each_tool_and_expose_answers(chat_api, monkeypatch):
    h = chat_api
    calls = []
    async def track(name, arguments, **kwargs):
        calls.append(name)
        return await execute_tool(name, arguments, **kwargs)
    monkeypatch.setattr('laxagent.agent.nodes.loop.execute_tool', track)
    captured = []
    async with client_for([ModelStream([question_response(('write_file', 'ask_user_question', 'ask_user_question', 'write_file'))]),
                           ModelStream([completed('done')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        run, first = await paused(h, run_id)
        assert run.finished_at is None
        assert calls == ['write_file', 'ask_user_question']
        assert (await h.api.post(chat_path(h), json=payload())).status_code == 409
        assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 409
        stream = (await h.api.get(f'/api/v1/runs/{run_id}/events')).text
        assert 'event: ask_user_question' in stream and '"status": "paused"' in stream
        assert first in stream and '"questions"' in stream
        assert (await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(first))).status_code == 202
        _, second = await paused(h, run_id, 1)
        assert second != first and calls.count('write_file') == 1
        assert (await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(first))).status_code == 409
        body = answer_body(second, 1)
        body['answers'] = [{**item, 'answer': '用户未回答'} for item in ANSWERS]
        assert (await h.api.post(f'/api/v1/runs/{run_id}/answers', json=body)).status_code == 202
        assert (await wait_run(h, run_id))['resume_time'] == 2
        assert calls.count('write_file') == 2
        _, tables = await message_snapshot(h.context.repository, run.session_id)
        assert tables[0] == tables[1]
        assert [row.role for row in tables[0]] == ['user', 'assistant', 'tool', 'tool', 'tool', 'tool', 'assistant']
        history = (await h.api.get(f'/api/v1/sessions/{run.session_id}/messages')).json()['items']
        questions = [row for row in history if row.get('tool_name') == 'ask_user_question']
        assert [row['answers'] for row in questions] == [ANSWERS, body['answers']]
        assert all('answers' not in row for row in history if row.get('tool_name') != 'ask_user_question')
        stream = (await h.api.get(f'/api/v1/runs/{run_id}/events?resume_time=1')).text
        assert 'event: tool_end' in stream and '自己的方案' in stream
        assert '"resume_time": 0' not in stream


@pytest.mark.anyio
async def test_answer_validation_and_concurrent_admission(chat_api):
    h = chat_api
    async with client_for([ModelStream([question_response()]), ModelStream([completed()])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        _, interrupt_id = await paused(h, run_id)
        path = f'/api/v1/runs/{run_id}/answers'
        body = answer_body(interrupt_id)
        for changed in ({'answers': []}, {'answers': ANSWERS[::-1]}, {'answers': [ANSWERS[0]]},
                        {'answers': [{**ANSWERS[0], 'answer': ' '}, ANSWERS[1]]}):
            assert (await h.api.post(path, json={**body, **changed})).status_code == 422
        assert (await h.api.post(path, json={**body, 'interrupt_id': 'stale'})).status_code == 409
        response = await asyncio.gather(h.api.post(path, json=body), h.api.post(path, json=body))
        assert sorted(item.status_code for item in response) == [202, 409]
        assert (await wait_run(h, run_id))['status'] == 'success'


@pytest.mark.anyio
async def test_paused_cancel_then_resume_skips_remaining_batch(chat_api):
    h = chat_api
    captured = []
    async with client_for([ModelStream([question_response(('ask_user_question', 'ask_user_question'))]),
                           ModelStream([completed()])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        run, interrupt_id = await paused(h, run_id)
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        assert (await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(interrupt_id))).status_code == 409
        assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
        outputs = [item['output'] for item in captured[1]['input'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 2 and all('TOOL_STATE_UNCERTAIN' in item for item in outputs)
        history = (await h.api.get(f'/api/v1/sessions/{run.session_id}/messages')).json()['items']
        assert all('answers' not in row for row in history)


@pytest.mark.anyio
async def test_delete_paused_session_without_execution_task(chat_api):
    h = chat_api
    async with client_for([ModelStream([question_response()])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        run, _ = await paused(h, run_id)
        assert (run_id, 0) not in h.manager.executions
        assert (await h.api.delete(f'/api/v1/sessions/{run.session_id}')).status_code == 204
        assert (await h.api.get(f'/api/v1/runs/{run_id}')).status_code == 404


@pytest.mark.anyio
async def test_question_event_failure_uses_existing_run_error(chat_api, monkeypatch):
    h = chat_api
    publish = h.context.events.publish
    async def fail(event):
        if event['type'] == 'ask_user_question':
            raise EventStoreError()
        await publish(event)
    monkeypatch.setattr(h.context.events, 'publish', fail)
    async with client_for([ModelStream([question_response()])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        assert (await wait_run(h, run_id))['error_code'] == 'run_event_failed'


@pytest.mark.parametrize('questions', [[], QUESTIONS * 3, [{'question': ' ', 'choice': []}],
                                       [{'question': 'q', 'choice': ['a'] * 4}], [{'question': 'q', 'choice': [3]}]])
def test_question_argument_bounds(questions):
    with pytest.raises(ValidationError):
        AskUserQuestionArgs.model_validate({'questions': questions})

@pytest.mark.anyio
async def test_answer_and_cancel_are_serialized(chat_api):
    h = chat_api
    async with client_for([ModelStream([question_response()]), ModelStream([completed()])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        _, interrupt_id = await paused(h, run_id)
        answer, cancel = await asyncio.gather(
            h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(interrupt_id)),
            h.api.post(f'/api/v1/runs/{run_id}/cancel'),
        )
        assert answer.status_code in (202, 409)
        assert cancel.status_code in (200, 409)
        assert 200 == cancel.status_code or 202 == answer.status_code
        async with asyncio.timeout(5):
            while True:
                run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
                if run.status not in ('running', 'waiting'):
                    break
                await asyncio.sleep(.01)
        assert run.status in ('success', 'cancelled')
        assert run.resume_time == int(answer.status_code == 202)


@pytest.mark.anyio
async def test_other_user_cannot_answer(chat_api):
    from uuid import uuid4
    h = chat_api
    async with client_for([ModelStream([question_response()])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        _, interrupt_id = await paused(h, run_id)
        token = (await h.api.post('/api/v1/login', json={'user_id': str(uuid4())})).json()['access_token']
        result = await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(interrupt_id),
                                 headers={'Authorization': f'Bearer {token}'})
        assert result.status_code == 404
