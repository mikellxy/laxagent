"""Run in Lima as the service account; retain fresh eval records for inspection."""

import argparse
import asyncio
import io
import json
from pathlib import Path
import time
from zipfile import ZipFile

import httpx2

from laxagent.config import load_config
from laxagent.storage.database import Database
from laxagent.storage.repositories import Repository


async def smoke(args):
    config = load_config(args.config)
    base = args.base_url or f"http://127.0.0.1:{config.eval_server.port}"
    public_base = args.public_base_url or f"http://127.0.0.1:{config.api_server.port}"
    async with (httpx2.AsyncClient(base_url=base, trust_env=False, timeout=30) as api,
                httpx2.AsyncClient(base_url=public_base, trust_env=False, timeout=30) as public):
        created = await api.post("/eval/create_session", json={"session_name": "eval-smoke"})
        created.raise_for_status()
        assert created.status_code == 201
        ids = created.json()
        assert (await api.post("/eval/chat", json={})).status_code == 404
        login = await public.post("/api/v1/login", json={"user_id": ids["user_id"]})
        login.raise_for_status()
        public.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        submitted = await public.post(f"/api/v1/sessions/{ids['session_id']}/chat", json={
            "model": config.llm.default_model,
            "user_query": "请只回复：评测链路已连通。不要调用工具。",
        })
        submitted.raise_for_status()
        assert submitted.status_code == 202 and submitted.json()["status"] == "waiting"
        run_id = submitted.json()["run_id"]
        deadline = time.monotonic() + args.timeout
        while True:
            collected = await api.post("/eval/collect", json={"session_id": ids["session_id"], "run_id": run_id})
            if collected.status_code != 409:
                collected.raise_for_status()
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Evaluation run {run_id} did not finish before the deadline")
            await asyncio.sleep(1)
        assert collected.headers["content-type"] == "application/zip"
        with ZipFile(io.BytesIO(collected.content)) as archive:
            assert archive.testzip() is None
            assert "workspace/" in archive.namelist()
            messages = json.loads(archive.read("trajectory.json"))
            assert [row["seq"] for row in messages] == sorted(row["seq"] for row in messages)
            assert messages[-1]["role"] == "assistant"
            assert "评测链路已连通" in messages[-1]["content"]
        async with Database.open(config.database) as db:
            run = await Repository(db).get_run(run_id, ids["user_id"])
            assert run.status == "success", f"Run ended with status {run.status}"
        schema = (await public.get("/openapi.json")).json()
        assert not any(path.startswith("/eval") for path in schema["paths"])
        print(json.dumps({**ids, "run_id": run_id, "status": run.status,
                          "message_count": len(messages), "zip_bytes": len(collected.content)},
                         ensure_ascii=False))
        print("PASS: private create_session, authenticated public chat, completed ZIP collection and public route isolation.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    parser.add_argument("--base-url")
    parser.add_argument("--public-base-url")
    parser.add_argument("--timeout", type=float, default=180)
    asyncio.run(smoke(parser.parse_args()))


if __name__ == "__main__":
    main()
