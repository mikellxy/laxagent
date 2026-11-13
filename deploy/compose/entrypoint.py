"""Container-only migration, loopback forwarding and process supervision."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen


CONFIG = "/app/config/config.json"
UV = ["uv", "run", "--no-sync"]
stopping = False


def request_stop(signum, frame):
    global stopping
    stopping = True


def start(args):
    return subprocess.Popen(args, start_new_session=True)


def stop(process, timeout):
    if process is None:
        return
    # Signal the group as socat forks one child per connection.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_step(args):
    process = start(args)
    try:
        while process.poll() is None and not stopping:
            time.sleep(0.1)
        return 0 if stopping else process.returncode
    finally:
        stop(process, 10)


def main():
    if sys.argv[1:] == ["--healthcheck"]:
        try:
            with urlopen("http://127.0.0.1:8080/openapi.json", timeout=3) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)

    # Use the application's existing validator without importing it into system Python.
    result = run_step(UV + ["python", "-c",
        "from pathlib import Path; from laxagent.config import load_config; "
        "load_config(Path('/app/config/config.json'))"])
    if result or stopping:
        return result
    config = json.loads(Path(CONFIG).read_text())
    api_port = config["api_server"]["port"]
    if 8080 in (api_port, config["llm_router"]["port"]):
        print("Port 8080 is reserved for the container HTTP forwarder", file=sys.stderr)
        return 1
    result = run_step(UV + ["python", "src/laxagent/sandbox/bubblewrap.py", "--probe"])
    if result or stopping:
        return result
    result = run_step(UV + ["alembic", "-x", f"config={CONFIG}", "upgrade", "head"])
    if result or stopping:
        return result

    app = relay = None
    try:
        app = start(UV + ["python", "-m", "laxagent.main", "--config", CONFIG])
        relay = start(["socat", "TCP-LISTEN:8080,bind=0.0.0.0,reuseaddr,fork",
                       f"TCP:127.0.0.1:{api_port}"])
        while not stopping:
            for name, process in (("laxagent", app), ("HTTP forwarder", relay)):
                if process.poll() is not None:
                    print(f"{name} exited unexpectedly", file=sys.stderr)
                    return 1
            time.sleep(0.2)
        return 0
    finally:
        # Keep SSE connections open while the app cancels tasks and flushes events.
        stop(app, config.get("cancel_timeout_seconds", 30) + 20)
        stop(relay, 5)


if __name__ == "__main__":
    sys.exit(main())
