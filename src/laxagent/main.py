import argparse
import asyncio
import signal
from contextlib import nullcontext
from pathlib import Path

import uvicorn

from laxagent.api.app import create_app
from laxagent.config import AppConfig, load_config
from laxagent.llm_router.app import create_router_app
from laxagent.eval_api.app import create_eval_app

app = create_app()


class ManagedServer(uvicorn.Server):
    def capture_signals(self):
        # One supervisor shuts down eval, then API/runtime, then the model router.
        return nullcontext()

    async def serve(self, sockets=None):
        try:
            await super().serve(sockets)
        except SystemExit:
            raise RuntimeError("HTTP listener failed to start") from None


async def run_servers(config: AppConfig, *, eval_in_docker: bool = False) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    api_app = create_app(config, eval_in_docker=eval_in_docker)
    servers = [
        ManagedServer(uvicorn.Config(
            create_router_app(config.llm), host="127.0.0.1", port=config.llm_router.port,
            timeout_graceful_shutdown=10,
        )),
        ManagedServer(uvicorn.Config(
            api_app, host="127.0.0.1", port=config.api_server.port,
            timeout_graceful_shutdown=10,
        )),
        ManagedServer(uvicorn.Config(
            create_eval_app(api_app), host="127.0.0.1", port=config.eval_server.port,
            timeout_graceful_shutdown=10,
        )),
    ]
    tasks = []
    stop_task = asyncio.create_task(stop.wait())
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        for server in servers:
            task = asyncio.create_task(server.serve())
            tasks.append(task)
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP listener stopped during startup")
                if stop.is_set():
                    return
                await asyncio.sleep(0.01)
        done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            for task in done:
                await task
            raise RuntimeError("HTTP listener stopped unexpectedly")
    finally:
        for server, task in reversed(list(zip(servers, tasks))):
            server.should_exit = True
            await asyncio.gather(task, return_exceptions=True)
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the API, LLM router and private eval server")
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    parser.add_argument("--eval_in_docker", action="store_true", help="Run tools in project-bound Docker containers")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        asyncio.run(run_servers(config, eval_in_docker=args.eval_in_docker))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
