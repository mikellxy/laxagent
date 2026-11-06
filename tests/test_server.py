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

import httpx2
from openai import DefaultHttpx2Client, OpenAI


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
            [sys.executable, "-m", "laxcode.main", "--config", str(config_path)],
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
                response = client.post(url, json={"query": "hello"})
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
                response = wait_for_api(process, f"http://127.0.0.1:{api_port}/api/v1/sessions/test/chat", log_path)
                assert response.json() == {"session_id": "test", "query": "hello"}
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
