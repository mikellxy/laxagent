"""Lima acceptance: real LLM + bash, SIGKILL the owned server, restart and resume.

Uses config/config.json credentials with temporary listener ports. Run after
scripts/deploy_lima.sh and Alembic upgrade head. Retains the test session as evidence.
"""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx2
from sqlalchemy import select

from laxcode.config import load_config
from laxcode.layout import get_project_workdir
from laxcode.storage.checkpoints import open_checkpointer
from laxcode.storage.database import Database
from laxcode.storage.models import Message, Run, SessionMessage


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def report(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


async def ready(process, api):
    async with asyncio.timeout(30):
        while True:
            assert process.poll() is None, 'Test server exited during startup'
            try:
                if (await api.get('/openapi.json', timeout=1)).status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            await asyncio.sleep(.1)


async def smoke(args):
    config = load_config(args.config)
    # Running another active task while starting a supervisor would mark it interrupted.
    async with Database.open(config.database) as db:
        async with db.transaction() as tx:
            assert await tx.scalar(select(Run.id).where(Run.status.in_(['waiting', 'running'])).limit(1)) is None, \
                'Wait for existing active runs before this smoke test'
    api_port, router_port = free_port(), free_port()
    while api_port == router_port:
        router_port = free_port()
    user_id = str(uuid4())
    marker = f'resume-smoke-{uuid4().hex}'
    command = f"printf 'started\\n' >> {marker}.started; sleep 120; printf 'finished\\n' > {marker}.finished"
    query = (
        '这是服务中断恢复验收。请首先只调用一次 bash 工具，timeout_seconds=180，完整执行以下命令，不要修改或拆分：\n'
        f'{command}\n'
        '如果后续工具结果提示“执行状态未知”，不要重跑该命令或 sleep；只用 read_file 检查 '
        f'{marker}.started 的第1行，然后简短说明检查结果并结束。'
    )
    with tempfile.TemporaryDirectory(prefix='laxcode-resume-smoke-') as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        raw['api_server']['port'] = api_port
        raw['llm_router']['port'] = router_port
        if config.frontend:
            raw['frontend']['dist_dir'] = config.frontend.dist_dir
        private_config = root / 'config.json'
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        process = None
        with (root / 'server.log').open('w') as log:
            def start():
                return subprocess.Popen([sys.executable, '-m', 'laxcode.main', '--config', str(private_config)],
                                        stdout=log, stderr=log)
            try:
                process = start()
                async with httpx2.AsyncClient(base_url=f'http://127.0.0.1:{api_port}', trust_env=False, timeout=300) as api:
                    await ready(process, api)
                    login = await api.post('/api/v1/login', json={'user_id': user_id, 'from_page': 'guest_login'})
                    login.raise_for_status()
                    api.headers['Authorization'] = f"Bearer {login.json()['access_token']}"
                    session_id = login.json()['initial_session_id']
                    detail = (await api.get(f'/api/v1/sessions/{session_id}')).json()
                    workspace = get_project_workdir(user_id, detail['project_id'])
                    submitted = await api.post(f'/api/v1/sessions/{session_id}/chat',
                                               json={'user_query': query, 'model': config.llm.default_model})
                    assert submitted.status_code == 202
                    run_id = submitted.json()['run_id']
                    report(progress='submitted', session_id=session_id, run_id=run_id, workspace=str(workspace))
                    async with asyncio.timeout(180):
                        while not (workspace / f'{marker}.started').exists():
                            status = (await api.get(f'/api/v1/runs/{run_id}')).json()['status']
                            assert status in ('waiting', 'running'), f'Run ended before starting bash: {status}'
                            await asyncio.sleep(.2)
                    assert not (workspace / f'{marker}.finished').exists()
                    async with Database.open(config.database) as db:
                        async with db.transaction() as tx:
                            original = await tx.get(Run, run_id)
                            assert original.status == 'running'
                    async with open_checkpointer(config.database) as saver:
                        checkpoint = await saver.aget_tuple({'configurable': {'thread_id': original.thread_id}})
                        assert checkpoint and checkpoint.checkpoint['channel_values']['pending_tool_calls']
                    report(progress='bash running; killing owned server', run_id=run_id, pid=process.pid)
                    process.kill()  # SIGKILL: no application cleanup/checkpoint-on-exit.
                    await asyncio.to_thread(process.wait, timeout=10)
                    process = start()
                    await ready(process, api)
                    failed = (await api.get(f'/api/v1/runs/{run_id}')).json()
                    assert failed['status'] == 'failed' and failed['error_code'] == 'service_execution_interrupted'
                    resumed = await api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})
                    assert resumed.status_code == 202, f'Resume rejected: {resumed.status_code}'
                    assert resumed.json()['run_id'] == run_id and resumed.json()['resume_time'] == 1
                    report(progress='restarted and resumed', run_id=run_id, resume_time=1)
                    events = []
                    async with asyncio.timeout(300):
                        async with api.stream('GET', f'/api/v1/runs/{run_id}/events?resume_time=1') as stream:
                            stream.raise_for_status()
                            async for line in stream.aiter_lines():
                                if line.startswith('data: '):
                                    event = json.loads(line[6:])
                                    assert event['resume_time'] == 1
                                    events.append(event)
                                    if event['type'] == 'sse_end':
                                        break
                    ended = (await api.get(f'/api/v1/runs/{run_id}')).json()
                    assert ended['status'] == 'success', f"Resumed run ended: {ended['status']} ({ended['error_code']})"
                    assert events[-1]['type'] == 'sse_end'
                    assert sum(e['type'] == 'tool_end' and e['error'] == 'TOOL_STATE_UNCERTAIN' for e in events) == 1
                    async with Database.open(config.database) as db:
                        async with db.transaction() as tx:
                            current = await tx.get(Run, run_id)
                            assert current.thread_id == original.thread_id and current.resume_time == 1
                            tables = []
                            for table in (Message, SessionMessage):
                                tables.append((await tx.execute(select(table.seq, table.role, table.content,
                                    table.tool_name, table.tool_call_id).where(table.session_id == session_id)
                                    .order_by(table.seq))).all())
                    assert tables[0] == tables[1]
                    assert sum(row.role == 'user' for row in tables[0]) == 1
                    unknown = [row for row in tables[0] if row.role == 'tool' and row.tool_name == 'bash']
                    assert len(unknown) == 1 and '异常中断，工具执行状态未知，请先检查工作区状态，不要盲目重试' in unknown[0].content
                    assert (workspace / f'{marker}.started').read_text() == 'started\n', 'Long command was re-executed'
                    assert not (workspace / f'{marker}.finished').exists()
                    assert any(row.role == 'tool' and row.tool_name == 'read_file' for row in tables[0])
                    report(status='PASS', run_id=run_id, session_id=session_id, resume_time=1,
                           tool_call_id=unknown[0].tool_call_id, unknown_tool_seq=unknown[0].seq,
                           unknown_tool_message=unknown[0].content, message_count=len(tables[0]),
                           command_start_count=1)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config/config.json'))
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args))
    except Exception as exc:
        # No raw provider/connection exceptions or credential-bearing config in output.
        message = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        parser.exit(1, f'Resume smoke failed: {message}\n')
