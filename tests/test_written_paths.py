import json

import pytest

from laxcode.storage.checkpoints import open_checkpointer
from laxcode.agent.graph import build_graph
from model_helpers import ModelStream, client_for, completed
from test_chat import chat_api, chat_path, payload, wait_run
from test_questions import QUESTIONS, answer_body, paused


def calls(*items):
    response = completed('')
    response['response']['output'] = [
        {'type': 'function_call', 'call_id': f'call_{i}', 'id': f'fc_{i}', 'name': name, 'arguments': json.dumps(args)}
        for i, (name, args) in enumerate(items)
    ]
    return response


def query():
    return calls(('list_written_files', {}))


def last_paths(request):
    return json.loads([item['output'] for item in request['input'] if item.get('type') == 'function_call_output'][-1])['result']['paths']


@pytest.mark.anyio
async def test_paths_survive_postgres_resume_and_reset_for_next_run(chat_api, migrated_database):
    h = chat_api
    captured = []
    streams = [ModelStream([calls(('write_file', {'path': 'result.txt', 'content': 'done'}))]),
               ModelStream([]), ModelStream([query()]), ModelStream([completed('recovered')]),
               ModelStream([query()]), ModelStream([completed('new run')])]
    async with client_for(streams, captured) as client:
        h.context.client = client
        async with open_checkpointer(migrated_database) as saver:
            h.manager.graph = build_graph(saver)
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            assert (await wait_run(h, run_id))['status'] == 'failed'
        async with open_checkpointer(migrated_database) as saver:
            h.manager.graph = build_graph(saver)
            assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
            assert (await wait_run(h, run_id))['status'] == 'success'
            assert last_paths(captured[3]) == ['result.txt']
            new_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            assert (await wait_run(h, new_id))['status'] == 'success'
            assert last_paths(captured[-1]) == []
            for id in [run_id, new_id]:
                run = await h.context.repository.get_run(id, h.conversation['user_id'])
                await saver.adelete_thread(run.thread_id)


@pytest.mark.anyio
async def test_query_after_question_sees_preceding_writes_and_noop_edits(chat_api):
    h = chat_api
    captured = []
    first = calls(('write_file', {'path': './a.txt', 'content': 'same'}),
                  ('ask_user_question', {'questions': QUESTIONS}),
                  ('edit_file', {'path': 'a.txt', 'edits': [{'old_text': 'same', 'new_text': 'same'}]}),
                  ('write_file', {'path': 'empty.txt', 'content': ''}), ('list_written_files', {}))
    async with client_for([ModelStream([first]), ModelStream([completed('done')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        run, interrupt = await paused(h, run_id)
        snapshot = await h.graph.aget_state({'configurable': {'thread_id': run.thread_id}})
        assert snapshot.values['written_workspace_paths'] == ['a.txt']
        assert (await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(interrupt))).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
        assert last_paths(captured[-1]) == ['a.txt', 'empty.txt']
