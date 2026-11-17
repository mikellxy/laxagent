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

from laxagent.config import load_config
from laxagent.layout import get_project_workdir, get_skills_dir
from laxagent.storage.database import Database
from laxagent.storage.models import Run
from smoke_chat import chat_round
from smoke_resume import free_port, ready


async def smoke(args):
    config = load_config(args.config)
    async with Database.open(config.database) as db:
        async with db.transaction() as tx:
            assert await tx.scalar(select(Run.id).where(Run.status.in_(["waiting", "running"])).limit(1)) is None, \
                "Wait for existing active runs before starting this smoke test"
    ports = []
    while len(ports) < 3:
        port = free_port()
        if port not in ports:
            ports.append(port)
    user_id, marker = str(uuid4()), "skill-smoke-" + uuid4().hex
    with tempfile.TemporaryDirectory(prefix="laxagent-skills-smoke-") as temporary:
        root = Path(temporary)
        raw = json.loads(args.config.read_text())
        raw["api_server"]["port"], raw["llm_router"]["port"] = ports[:2]
        raw.setdefault("eval_server", {})["port"] = ports[2]
        if config.frontend:
            raw["frontend"]["dist_dir"] = config.frontend.dist_dir
        private_config = root / "config.json"
        private_config.write_text(json.dumps(raw))
        private_config.chmod(0o600)
        shutil.copytree(config.builtin_skills_dir, root / "builtin_skills")
        with (root / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "laxagent.main", "--config", str(private_config)],
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
                               and e["content"].get("result", {}).get("name") == "laxagent" for e in first_events)
                    for name in ("laxagent", "skill-creator"):
                        assert (get_skills_dir(user_id) / name / "SKILL.md").is_file()
                    script = "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('BEFORE')\nprint('BEFORE')\n"
                    query = (
                        "这是技能工具验收。请先加载 skill-creator，创建 smoke-demo 技能（description 写冒烟测试用途），"
                        "配套 scripts/main.py 的初始完整内容如下：\n" + script +
                        "\n创建后用 read_file 读取脚本，再用 edit_file 将两处 BEFORE 分别改为 " + marker +
                        "（每处提供足够上下文确保匹配唯一）。用 write_file 创建 /user/skills/smoke-demo/obsolete.txt，再用 delete 操作删除它。"
                        "最后 load_skill smoke-demo，并用 bash 调用 python3 执行 /user/skills/smoke-demo/scripts/main.py，"
                        "输出参数使用当前 workspace 的绝对路径加 /skill-result.txt。文件维护使用文件工具，bash 命令只包含 python3 执行脚本这一条命令，不要拼接 ls 或其他命令。确认成功后用独立段落的 workspace 链接展示结果文件。"
                    )
                    second_id, events = await chat_round(api, session_id, config.llm.default_model, query)
                    tools = [e["content"].get("tool_name") for e in events if e["type"] == "tool_end"]
                    assert {"load_skill", "write_file", "read_file", "edit_file", "bash"} <= set(tools)
                    assert (workspace / "skill-result.txt").read_text() == marker
                    skill = get_skills_dir(user_id) / "smoke-demo"
                    assert not (skill / "obsolete.txt").exists()
                    assert marker in (skill / "scripts/main.py").read_text()
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "load_skill"
                               and e["content"].get("result", {}).get("name") == "skill-creator" for e in events)
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "bash"
                               and e["content"].get("result", {}).get("exit_code") == 0 for e in events)
                    preview = await api.get(f"/api/v1/sessions/{session_id}/workspace-files/preview",
                                            params={"path": "skill-result.txt"})
                    assert preview.status_code == 200 and preview.json()["text"] == marker
                    assert any(e["type"] == "tool_end" and e["content"].get("tool_name") == "load_skill"
                               and "/user/skills/smoke-demo/SKILL.md" in e["content"].get("result", {}).get("files", [])
                               for e in events)
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
