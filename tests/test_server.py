import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from openai import DefaultHttpx2Client, OpenAI

from laxagent.runtime.events import EventStore


@pytest.fixture(autouse=True)
def database_ready(migrated_database):
    pass


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def launch(config_data, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "laxagent.main", "--config", str(config_path)],
            cwd=Path(__file__).parents[1], stdout=log, stderr=log,
        )
        try:
            yield process, log_path
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def wait_for_api(process, url, log_path):
    with httpx2.Client(trust_env=False, timeout=0.3) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, log_path.read_text()
            try:
                response = client.get(url)
                if response.status_code == 200:
                    return response
            except httpx2.HTTPError:
                pass
            time.sleep(0.02)
    raise AssertionError(log_path.read_text())


def assert_port_closed(port):
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_two_servers_stream_to_openai_sdk_and_shutdown(config_data, tmp_path):
    release = threading.Event()
    captured = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello","sequence_number":0,"output_index":0,"content_index":0,"item_id":"msg_test"}\n\n')
            self.wfile.flush()
            release.wait(timeout=5)
            self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed","sequence_number":1,"response":{"id":"resp_test","model":"model-name","status":"completed","output":[]}}\n\n')
            self.wfile.flush()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            config_data["api_server"]["port"] = api_port = available_port()
            config_data["llm_router"]["port"] = router_port = available_port()
            config_data["llm"]["provider_list"][0]["base_url"] = f"http://127.0.0.1:{upstream.server_port}/v1"
            with launch(config_data, tmp_path) as (process, log_path):
                response = wait_for_api(process, f"http://127.0.0.1:{api_port}/openapi.json", log_path)
                assert "/api/v1/login" in response.json()["paths"]
                with OpenAI(
                    api_key="placeholder", base_url=f"http://127.0.0.1:{router_port}/v1",
                    max_retries=0, timeout=2,
                    http_client=DefaultHttpx2Client(trust_env=False),
                ) as client:
                    with client.responses.create(model="example:model-name", input="hi", stream=True, max_output_tokens=20) as stream:
                        assert next(stream).delta == "hello"
                        # The first event arrives before the upstream finishes.
                        assert not release.is_set()
                        release.set()
                        assert list(stream)[-1].type == "response.completed"
                assert captured[0][0:2] == ("/v1/responses", "Bearer sk-private-test")
                assert captured[0][2]["model"] == "model-name"
                assert captured[0][2]["max_output_tokens"] == 16000
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=15) == 0, log_path.read_text()
                assert "sk-private-test" not in log_path.read_text()
            assert_port_closed(api_port)
            assert_port_closed(router_port)
        finally:
            release.set()
            upstream.shutdown()
            thread.join(timeout=2)


def test_api_bind_failure_stops_router(config_data, tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        config_data["api_server"]["port"] = occupied.getsockname()[1]
        config_data["llm_router"]["port"] = router_port = available_port()
        with launch(config_data, tmp_path) as (process, log_path):
            assert process.wait(timeout=15) != 0
            assert "sk-private-test" not in log_path.read_text()
        assert_port_closed(router_port)


def test_router_bind_failure_never_starts_api(config_data, tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        config_data["llm_router"]["port"] = occupied.getsockname()[1]
        config_data["api_server"]["port"] = api_port = available_port()
        with launch(config_data, tmp_path) as (process, _):
            assert process.wait(timeout=15) != 0
        assert_port_closed(api_port)


def test_nats_unavailable_stops_both_listeners(config_data, tmp_path):
    config_data["api_server"]["port"] = api_port = available_port()
    config_data["llm_router"]["port"] = router_port = available_port()
    config_data["nats"] = {"url": f"nats://127.0.0.1:{available_port()}"}
    with launch(config_data, tmp_path) as (process, log_path):
        assert process.wait(timeout=15) != 0
        assert "sk-private-test" not in log_path.read_text()
    assert_port_closed(api_port)
    assert_port_closed(router_port)


def test_chat_returns_before_model_finishes_and_sse_disconnect_does_not_cancel(config_data, tmp_path):
    # The same listener serves static assets while retaining streaming API behavior.
    dist = tmp_path / "web"
    dist.mkdir()
    (dist / "index.html").write_text("<html>chat frontend</html>")
    config_data["frontend"] = {"dist_dir": str(dist)}
    from laxagent.config import DatabaseConfig
    from laxagent.storage.database import Database
    from laxagent.storage.repositories import Repository

    release = threading.Event()
    run_id = None
    captured = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def event(data):
                self.wfile.write(f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            event({"type": "response.created", "response": {"id": "resp_test"}})
            event({"type": "response.output_text.delta", "delta": "hello"})
            release.wait(timeout=10)
            event({"type": "response.completed", "response": {
                "status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}]}],
            }})

        def log_message(self, *args):
            pass

    async def seed():
        from sqlalchemy import text
        async with Database.open(DatabaseConfig.model_validate(config_data["database"])) as db:
            async with db.transaction() as tx:
                await tx.execute(text("SELECT setval(pg_get_serial_sequence('runs', 'id'), :start, false)"),
                                 {"start": 1_000_000_000 + uuid4().int % 900_000_000})
            repository = Repository(db)
            user_id = str(uuid4())
            await repository.create_user(user_id)
            project = await repository.create_project(user_id, "http-test")
            session = await repository.create_session(user_id, project.id, "http-test")
            return user_id, session.id

    async def cleanup():
        async with EventStore.connect("nats://127.0.0.1:4222") as store:
            await store.js.delete_stream(store.stream_name(run_id))

    with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            config_data["api_server"]["port"] = api_port = available_port()
            config_data["llm_router"]["port"] = available_port()
            config_data["llm"]["provider_list"][0]["base_url"] = f"http://127.0.0.1:{upstream.server_port}/v1"
            base = f"http://127.0.0.1:{api_port}"
            user_id, session_id = asyncio.run(seed())
            with launch(config_data, tmp_path) as (process, log_path):
                wait_for_api(process, f"{base}/openapi.json", log_path)
                with httpx2.Client(trust_env=False, timeout=10) as api:
                    assert api.get(base + "/").text == "<html>chat frontend</html>"
                    login = api.post(f"{base}/api/v1/login", json={"user_id": user_id})
                    api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
                    response = api.post(f"{base}/api/v1/sessions/{session_id}/chat", json={
                        "model": "example:model-name", "user_query": "hi",
                    })
                    assert response.status_code == 202, response.text
                    run_id = response.json()["run_id"]
                    assert not release.is_set()
                    url = f"{base}/api/v1/runs/{run_id}/events"
                    with api.stream("GET", url) as response:
                        assert response.status_code == 200
                        for line in response.iter_lines():
                            if line.startswith("data: ") and json.loads(line[6:])["type"] == "message_chunk":
                                break
                    # Disconnecting the SSE HTTP request leaves the model execution running.
                    assert api.get(f"{base}/api/v1/runs/{run_id}").json()["status"] == "running"
                    release.set()
                    replay = api.get(url)
                    seen = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
                    assert seen[-1]["content"] == {"status": "completed"}
                    assert api.get(f"{base}/api/v1/runs/{run_id}").json()["status"] == "success"
                    assert captured[0]["max_output_tokens"] == 16000
                    assert captured[0]["model"] == "model-name"
        finally:
            release.set()
            upstream.shutdown()
            thread.join(timeout=2)
            if run_id is not None:
                asyncio.run(cleanup())
