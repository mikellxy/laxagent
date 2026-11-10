"""Lima: real config/LLM, builtin discovery and skill file/script tool round-trip.

Starts an owned server on temporary ports. Retains fresh user/session artifacts
as evidence, never prints configuration or credentials. Requires no active runs.
"""

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx2
from sqlalchemy import select

from laxcode.config import load_config
from laxcode.layout import get_project_workdir, get_skills_dir
from laxcode.storage.database import Database
from laxcode.storage.models import Run
from smoke_chat import chat_round
from smoke_resume import free_port, ready


async def smoke(args):
    config = load_config(args.config)
    async with Database.open(config.database) as db:
        async with db.transaction() as tx:
            assert await tx.scalar(select(Run.id).where(Run.status.in_(["waiting", "running"])).limit(1)) is None, \
                "Wait for existing active runs before starting this smoke test"
    ports = [free_port(), free_port()]
    while ports[0] == ports[1]:
        ports[1] = free_port()
    user_id, marker = str(uuid4()), "skill-smoke-" + uuid4().hex
    with tempfile.TemporaryDirectory(prefix="laxcode-skills-smoke-") as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        raw["api_server"]["port"], raw["llm_router"]["port"] = ports
        if config.frontend:
            raw["frontend"]["dist_dir"] = config.frontend.dist_dir
        private_config = root / "config.json"
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        shutil.copytree(config.builtin_skills_dir, root / "builtin_skills")
        with (root / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "laxcode.main", "--config", str(private_config)],
                                       stdout=log, stderr=log)
            try:
                async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}", trust_env=False, timeout=400) as api:
                    await ready(process, api)
                    login = await api.post("/api/v1/login", json={"user_id": user_id, "from_page": "guest_login"})
                    login.raise_for_status()
                    api.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                    session_id = login.json()["initial_session_id"]
                    detail = (await api.get(f"/api/v1/sessions/{session_id}")).json()
                    workspace = get_project_workdir(user_id, detail["project_id"])
                    first_id, first_events = await chat_round(api, session_id, config.llm.default_model,
                        "你是谁，可以帮我做什么？请先查阅与你身份相关的技能，简短介绍，不涉及内部实现细节。")
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "load_skill"
                               and e["content"].get("result", {}).get("name") == "laxcode" for e in first_events)
                    for name in ("laxcode", "skill-creator"):
                        assert (get_skills_dir(user_id) / name / "SKILL.md").is_file()
                    script = "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('BEFORE')\nprint('BEFORE')\n"
                    query = (
                        "这是技能工具验收。请先加载 skill-creator，创建 smoke-demo 技能（description 写冒烟测试用途），"
                        "配套 scripts/main.py 的初始完整内容如下：\n" + script +
                        "\n创建后用 read_file 读取脚本，再用 edit_file 将两处 BEFORE 分别改为 " + marker +
                        "（每处提供足够上下文确保匹配唯一）。用 write_file 创建 obsolete.txt，再用 delete 操作删除它。"
                        "最后 load_skill smoke-demo，并用 run_skill_scripts 的 python3 执行 scripts/main.py，"
                        "args=[\"{workspace}/skill-result.txt\"]。确认成功后简短回复。不要用 bash 替代这些工具。"
                    )
                    second_id, events = await chat_round(api, session_id, config.llm.default_model, query)
                    tools = [e["content"].get("tool_name") for e in events if e["type"] == "tool_end"]
                    assert {"load_skill", "write_file", "read_file", "edit_file", "run_skill_scripts"} <= set(tools)
                    assert (workspace / "skill-result.txt").read_text() == marker
                    skill = get_skills_dir(user_id) / "smoke-demo"
                    assert not (skill / "obsolete.txt").exists()
                    assert marker in (skill / "scripts/main.py").read_text()
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "load_skill"
                               and e["content"].get("result", {}).get("name") == "skill-creator" for e in events)
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "run_skill_scripts"
                               and e["content"].get("result", {}).get("exit_code") == 0 for e in events)
                    print(json.dumps({"status": "PASS", "user_id": user_id, "session_id": session_id,
                                      "run_ids": [first_id, second_id], "tool_calls": tools}), flush=True)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args))
    except Exception as exc:
        parser.exit(1, f"Skill smoke failed ({type(exc).__name__}); inspect the run above.\n")
