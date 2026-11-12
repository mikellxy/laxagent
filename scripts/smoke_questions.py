"""Lima acceptance with real config/model: pause, answer, SSE and history.

Run after scripts/deploy_lima.sh and Alembic upgrade head. Uses an owned server
on temporary ports and retains the generated session as acceptance evidence.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx2

from laxcode.config import load_config
from smoke_resume import free_port, ready, report


async def events(api, run_id, attempt):
    result = []
    async with asyncio.timeout(240):
        async with api.stream('GET', f'/api/v1/runs/{run_id}/events?resume_time={attempt}') as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith('data: '):
                    event = json.loads(line[6:])
                    assert event['resume_time'] == attempt
                    result.append(event)
                    if event['type'] == 'sse_end':
                        break
    return result


async def smoke(args):
    config = load_config(args.config)
    with tempfile.TemporaryDirectory(prefix='laxcode-questions-smoke-') as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        api_port, router_port = free_port(), free_port()
        while api_port == router_port:
            router_port = free_port()
        raw['api_server']['port'], raw['llm_router']['port'] = api_port, router_port
        if config.frontend:
            raw['frontend']['dist_dir'] = config.frontend.dist_dir
        private_config = root / 'config.json'
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        with (root / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'laxcode.main', '--config', str(private_config)],
                                       stdout=log, stderr=log)
            try:
                async with httpx2.AsyncClient(base_url=f'http://127.0.0.1:{api_port}', trust_env=False, timeout=240) as api:
                    await ready(process, api)
                    login = await api.post('/api/v1/login', json={'user_id': str(uuid4()), 'from_page': 'guest_login'})
                    login.raise_for_status()
                    api.headers['Authorization'] = f"Bearer {login.json()['access_token']}"
                    session_id = login.json()['initial_session_id']
                    query = ('这是 ask_user_question 工具验收，请立即且只调用一次 ask_user_question，包含两个问题：'
                             '第一个问“采用哪个方案？”，choice 为 ["方案一","方案二"]；'
                             '第二个问“补充要求？”，choice 为 []。必须调用工具，不要仅用正文提问。'
                             '收到工具结果后，不再提问或调用其他工具，原样列出两个答案并回复 ASK_USER_QUESTION_OK。')
                    response = await api.post(f'/api/v1/sessions/{session_id}/chat',
                                              json={'user_query': query, 'model': config.llm.default_model})
                    assert response.status_code == 202, response.status_code
                    run_id = response.json()['run_id']
                    report(progress='submitted', run_id=run_id, session_id=session_id)
                    first = await events(api, run_id, 0)
                    questions = [item['content'] for item in first if item['type'] == 'ask_user_question']
                    assert len(questions) == 1, 'Model did not issue one question batch'
                    question = questions[0]
                    assert len(question['questions']) == 2
                    paused = (await api.get(f'/api/v1/runs/{run_id}')).json()
                    assert paused['status'] == 'paused' and paused['finished_at'] is None
                    assert first[-1]['content']['status'] == 'paused'
                    report(progress='paused and SSE ended', run_id=run_id)
                    answers = [{'question': item['question'], 'answer': '自定义方案-验收' if index == 0 else '用户未回答'}
                               for index, item in enumerate(question['questions'])]
                    body = {'resume_time': 0, 'interrupt_id': question['interrupt_id'], 'answers': answers}
                    submitted = await api.post(f'/api/v1/runs/{run_id}/answers', json=body)
                    assert submitted.status_code == 202 and submitted.json()['resume_time'] == 1
                    duplicate = await api.post(f'/api/v1/runs/{run_id}/answers', json=body)
                    assert duplicate.status_code == 409
                    second = await events(api, run_id, 1)
                    ended = (await api.get(f'/api/v1/runs/{run_id}')).json()
                    assert ended['status'] == 'success', ended['error_code']
                    result = [item for item in second if item['type'] == 'tool_end' and item['content']['tool_name'] == 'ask_user_question']
                    assert len(result) == 1 and result[0]['content']['result']['answers'] == answers
                    history = (await api.get(f'/api/v1/sessions/{session_id}/messages')).json()['items']
                    rows = [row for row in history if row.get('tool_name') == 'ask_user_question']
                    assert len(rows) == 1 and rows[0]['answers'] == answers
                    text = '\n'.join(row.get('content', '') for row in history if row['role'] == 'assistant')
                    assert 'ASK_USER_QUESTION_OK' in text and '自定义方案-验收' in text and '用户未回答' in text
                    report(progress='passed', run_id=run_id, session_id=session_id, resume_time=1,
                           checks=['interrupt', 'paused', 'answers', 'stale-answer-409', 'tool-end', 'history', 'model-continued'])
            finally:
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('config/config.json'))
    asyncio.run(smoke(parser.parse_args()))
