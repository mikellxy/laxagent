"""Lima acceptance: two API processes, shared Router, real LLM/bash and remote cancellation.

Run after scripts/deploy_lima.sh and Alembic upgrade head. Uses config/config.json,
allocates temporary listening ports, and retains the fresh session as evidence.
"""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx2

from laxagent.config import load_config
from laxagent.layout import get_project_workdir


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def report(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


async def ready(process, api, *, expected_status=200):
    async with asyncio.timeout(30):
        while True:
            assert process.poll() is None, 'Test process exited during startup'
            try:
                if (await api.get('/openapi.json', timeout=1)).status_code == expected_status:
                    return
            except httpx2.HTTPError:
                pass
            await asyncio.sleep(.1)


async def wait_bash(api, run_id, path):
    async with asyncio.timeout(180):
        while not path.exists():
            response = await api.get(f'/api/v1/runs/{run_id}')
            response.raise_for_status()
            assert response.json()['status'] in ('waiting', 'running'), 'Run ended before starting bash'
            await asyncio.sleep(.2)


async def smoke(args):
    config = load_config(args.config)
    ports = set()
    while len(ports) < 3:
        ports.add(free_port())
    router_port, first_port, second_port = ports
    user_id, marker = str(uuid4()), 'cancel-smoke-' + uuid4().hex
    processes = []
    with tempfile.TemporaryDirectory(prefix='laxcode-cancel-smoke-') as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        raw['api_server']['port'] = first_port
        raw['llm_router']['port'] = router_port
        raw['cancel_timeout_seconds'] = 30
        if config.frontend:
            raw['frontend']['dist_dir'] = config.frontend.dist_dir
        private_config = root / 'config.json'
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        if config.builtin_skills_dir.exists():
            shutil.copytree(config.builtin_skills_dir, root / 'builtin_skills')
        with (root / 'servers.log').open('w') as log:
            def launch(kind, port):
                # Test-only independent processes; production startup remains unchanged.
                code = ('import sys, uvicorn; from pathlib import Path; from laxagent.config import load_config; '
                        'from laxagent.api.app import create_app; from laxagent.llm_router.app import create_router_app; '
                        'c=load_config(Path(sys.argv[1])); '
                        f'a=create_router_app(c.llm) if {kind!r} == "router" else create_app(c); '
                        'uvicorn.run(a, host="127.0.0.1", port=int(sys.argv[2]), timeout_graceful_shutdown=10)')
                process = subprocess.Popen([sys.executable, '-c', code, str(private_config), str(port)],
                                           stdout=log, stderr=log)
                processes.append(process)
                return process
            try:
                router = launch('router', router_port)
                first = launch('api', first_port)
                async with httpx2.AsyncClient(base_url=f'http://127.0.0.1:{first_port}', trust_env=False, timeout=45) as a, \
                        httpx2.AsyncClient(base_url=f'http://127.0.0.1:{second_port}', trust_env=False, timeout=45) as b, \
                        httpx2.AsyncClient(base_url=f'http://127.0.0.1:{router_port}', trust_env=False) as router_api:
                    # The internal Router deliberately disables its OpenAPI endpoint.
                    await ready(router, router_api, expected_status=404)
                    await ready(first, a)
                    report(progress='router and first API ready')
                    login = await a.post('/api/v1/login', json={'user_id': user_id, 'from_page': 'guest_login'})
                    login.raise_for_status()
                    a.headers['Authorization'] = b.headers['Authorization'] = f"Bearer {login.json()['access_token']}"
                    session_id = login.json()['initial_session_id']
                    detail = (await a.get(f'/api/v1/sessions/{session_id}')).json()
                    workspace = get_project_workdir(user_id, detail['project_id'])
                    async def submit(api, suffix):
                        command = f"printf 'started\\n' > {marker}.{suffix}.started; sleep 120; touch {marker}.{suffix}.finished"
                        query = ('这是任务取消验收。请首先只调用一次 bash 工具，timeout_seconds=180，完整执行以下命令，不要修改或拆分：\n'
                                 + command + '\n若恢复后收到工具执行状态未知，不要重跑命令；直接简短回复已停止并结束。')
                        response = await api.post(f'/api/v1/sessions/{session_id}/chat',
                                                  json={'user_query': query, 'model': config.llm.default_model})
                        assert response.status_code == 202, f'Submit rejected: {response.status_code}'
                        return response.json()['run_id']
                    run_id = await submit(a, 'cancel')
                    report(progress='waiting for real bash tool', run_id=run_id, session_id=session_id)
                    await wait_bash(a, run_id, workspace / f'{marker}.cancel.started')
                    # Starting B while A executes must not change A's run status.
                    second = launch('api', second_port)
                    await ready(second, b)
                    assert (await b.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'running'
                    rejected = await b.post(f'/api/v1/sessions/{session_id}/chat',
                                           json={'user_query': 'must conflict', 'model': config.llm.default_model})
                    assert rejected.status_code == 409
                    results = await asyncio.gather(*(b.post(f'/api/v1/runs/{run_id}/cancel') for _ in range(3)))
                    assert all(r.status_code == 200 and r.json()['status'] == 'cancelled' for r in results)
                    assert not (workspace / f'{marker}.cancel.finished').exists()
                    assert subprocess.run(['pgrep', '-f', marker], capture_output=True).returncode == 1, 'Tool process survived cancellation'
                    report(progress='remote cancel PASS', run_id=run_id, session_id=session_id,
                           owner_pid=first.pid, requester_pid=second.pid)
                    # The second process resumes from shared PostgreSQL checkpoints.
                    resumed = await b.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})
                    assert resumed.status_code == 202 and resumed.json()['resume_time'] == 1
                    async with asyncio.timeout(180):
                        while True:
                            ended = (await a.get(f'/api/v1/runs/{run_id}')).json()
                            if ended['status'] not in ('waiting', 'running'):
                                break
                            await asyncio.sleep(.2)
                    assert ended['status'] == 'success', f'Resumed run ended: {ended["status"]}'
                    assert (workspace / f'{marker}.cancel.started').read_text() == 'started\n'
                    assert not (workspace / f'{marker}.cancel.finished').exists()
                    report(progress='cross-process resume PASS', run_id=run_id, resume_time=1)
                    deletion_run = await submit(b, 'delete')
                    await wait_bash(b, deletion_run, workspace / f'{marker}.delete.started')
                    assert (await a.delete(f'/api/v1/sessions/{session_id}')).status_code == 204
                    assert (await b.get(f'/api/v1/sessions/{session_id}')).status_code == 404
                    assert not (workspace / f'{marker}.delete.finished').exists()
                    assert subprocess.run(['pgrep', '-f', marker], capture_output=True).returncode == 1, 'Tool process survived deletion'
                    report(status='PASS', user_id=user_id, session_id=session_id,
                           cancelled_run=run_id, deleted_run=deletion_run,
                           checks=['new instance startup', 'session admission', 'remote cancel',
                                   'duplicate cancel', 'tool cleanup', 'remote resume', 'remote delete'])
            except Exception:
                log.flush()
                diagnostic = (root / 'servers.log').read_text()
                secrets = [config.database.password.get_secret_value(), config.auth.jwt_secret.get_secret_value()]
                secrets.extend(provider.api_key.get_secret_value() for provider in config.llm.provider_list)
                secrets.extend(str(provider.base_url) for provider in config.llm.provider_list)
                for secret in secrets:
                    diagnostic = diagnostic.replace(secret, '[redacted]')
                report(progress='smoke diagnostic', log_tail=diagnostic[-4000:])
                raise
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            await asyncio.to_thread(process.wait, timeout=20)
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
        message = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        parser.exit(1, f'Cancellation smoke failed: {message}\n')
