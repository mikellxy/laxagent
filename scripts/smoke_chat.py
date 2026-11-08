"""Run against an already-started server; creates fresh resources and retains them for inspection.

uv run python scripts/smoke_chat.py --config config/config.json
"""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx2
from sqlalchemy import select

from laxcode.config import load_config
from laxcode.storage.database import Database
from laxcode.storage.models import Message, Session, SessionMessage
from laxcode.storage.repositories import Repository


async def smoke(args):
    config = load_config(args.config)
    model = args.model or config.llm.default_model
    base = args.base_url or f"http://127.0.0.1:{config.api_server.port}"
    async with Database.open(config.database) as db:
        repository = Repository(db)
        user_id = str(uuid4())
        await repository.create_user(user_id)
        project = await repository.create_project(user_id, "chat-smoke")
        session = await repository.create_session(user_id, project.id, "chat-smoke")
        print(json.dumps({"user_id": user_id, "project_id": project.id, "session_id": session.id}), flush=True)
        async with httpx2.AsyncClient(base_url=base, trust_env=False, timeout=400) as api:
            login = await api.post("/api/v1/login", json={"user_id": user_id})
            login.raise_for_status()
            api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
            response = await api.post(f"/api/v1/sessions/{session.id}/chat", json={
                "user_query": args.query, "model": model,
            })
            response.raise_for_status()
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            print(json.dumps({"run_id": run_id, "status": response.json()["status"]}), flush=True)
            ended = False
            kinds = []
            async with asyncio.timeout(400):
                async with api.stream("GET", f"/api/v1/runs/{run_id}/events") as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            assert event["run_id"] == run_id
                            kinds.append(event["type"])
                            if event["type"] == "sse_end":
                                ended = True
                                break
            status = await api.get(f"/api/v1/runs/{run_id}")
            status.raise_for_status()
            summary = status.json()
            print(json.dumps({"run_id": run_id, "status": summary["status"], "error": summary["error"],
                              "event_count": len(kinds), "tool_count": kinds.count("tool_end")}), flush=True)
            assert ended and summary["status"] == "success", "Run did not complete successfully"
        async with db.transaction() as transaction:
            tables = []
            for table in (Message, SessionMessage):
                tables.append((await transaction.execute(select(
                    table.seq, table.role, table.content, table.tool_call_id, table.tool_name, table.tool_call,
                ).where(table.session_id == session.id).order_by(table.seq))).all())
            current = await transaction.get(Session, session.id)
            assert tables[0] == tables[1] and len(tables[0]) >= 2
            assert [row.seq for row in tables[0]] == list(range(1, current.next_msg_seq))
            assert tables[0][0].role == "user" and tables[0][0].content == args.query
            assert sum(row.role == "user" for row in tables[0]) == 1
            assert tables[0][-1].role == "assistant"
        print("PASS: chat, run status, SSE and both message tables verified.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--query", default="请调用 read_file 读取 hello.txt 的第 1 行，从第 1 字节开始，共读 1 行，然后回复文件内容。")
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args))
    except Exception as exc:
        # Avoid printing HTTP objects, configuration or SDK exception payloads.
        parser.exit(1, f"Smoke test failed ({type(exc).__name__}); inspect the run above.\n")


if __name__ == "__main__":
    main()
