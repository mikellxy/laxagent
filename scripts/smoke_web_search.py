"""Lima acceptance using real search credentials and model, on temporary server ports."""

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
from laxcode.web_search import WebSearchService
from smoke_questions import events
from smoke_resume import free_port, ready, report


async def smoke(args):
    config = load_config(args.config)
    assert config.web_search is not None, "web_search configuration is required"
    # Verify image support separately: the end-to-end model run below exercises web search.
    async with WebSearchService.open(config.web_search) as service:
        images = await service.search({"Query": "故宫博物院", "SearchType": "image", "Count": 1})
        assert images["results"] and images["results"][0]["image_url"]
        report(progress="image search passed", result_count=len(images["results"]))
    with tempfile.TemporaryDirectory(prefix="laxcode-search-smoke-") as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        api_port, router_port = free_port(), free_port()
        while api_port == router_port:
            router_port = free_port()
        raw["api_server"]["port"], raw["llm_router"]["port"] = api_port, router_port
        if config.frontend:
            raw["frontend"]["dist_dir"] = config.frontend.dist_dir
        private_config = root / "config.json"
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        log_path = root / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "laxcode.main", "--config", str(private_config)],
                                       stdout=log, stderr=log)
            try:
                async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", trust_env=False, timeout=240) as api:
                    await ready(process, api)
                    login = await api.post("/api/v1/login", json={"user_id": str(uuid4()), "from_page": "guest_login"})
                    login.raise_for_status()
                    api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
                    session_id = login.json()["initial_session_id"]
                    query = ("这是 web_search 工具验收。请只调用一次 web_search，query 为“LangGraph 官方文档 checkpoint”，"
                             "search_type=web，count=2，time_range=AnyTime，authoritative_only=false，query_rewrite=false。"
                             "不要调用其他工具。根据返回结果简述 checkpoint 用途，并用 Markdown 链接引用一个返回的来源。"
                             "搜索成功后在末尾写 WEB_SEARCH_OK；失败则如实说明，不得声称成功。")
                    response = await api.post(f"/api/v1/sessions/{session_id}/chat",
                                              json={"user_query": query, "model": config.llm.default_model})
                    assert response.status_code == 202, response.status_code
                    run_id = response.json()["run_id"]
                    report(progress="submitted", run_id=run_id, session_id=session_id)
                    stream = await events(api, run_id, 0)
                    run = (await api.get(f"/api/v1/runs/{run_id}")).json()
                    assert run["status"] == "success", run.get("error_code")
                    tool_events = [item for item in stream if item["type"] == "tool_end"
                                   and item["content"]["tool_name"] == "web_search"]
                    assert len(tool_events) == 1, "Expected exactly one search call"
                    tool_result = tool_events[0]["content"]
                    assert tool_result["error_code"] is None, tool_result["error_code"]
                    results = tool_result["result"]["results"]
                    assert results and any(item["summary"] for item in results), "Search returned no useful summaries"
                    history = (await api.get(f"/api/v1/sessions/{session_id}/messages")).json()["items"]
                    assert any(item.get("tool_name") == "web_search" for item in history)
                    answer = "\n".join(item.get("content", "") for item in history if item["role"] == "assistant")
                    assert "WEB_SEARCH_OK" in answer
                    assert any(item["url"] and item["url"] in answer for item in results), "Missing source citation"
                    key = config.web_search.api_key.get_secret_value()
                    assert key not in json.dumps([stream, history]) and key not in log_path.read_text()
                    report(progress="passed", run_id=run_id, session_id=session_id, result_count=len(results),
                           checks=["real-key", "image-search", "model-tool-call", "web-summaries", "SSE", "history", "citation", "no-key-exposure"])
            finally:
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    asyncio.run(smoke(parser.parse_args()))
