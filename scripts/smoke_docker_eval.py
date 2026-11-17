"""Lima: real-model Docker eval CRUD in an owned container, without SWE images.

Requires a migrated real configuration and Docker access. Keeps fresh database
records and a report; removes only its own container/server and temporary config.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx2
from sqlalchemy import select

from laxagent.config import load_config
from laxagent.layout import get_project_workdir, get_skills_dir, get_session_config_dir
from laxagent.sandbox.docker import docker_command
from laxagent.storage.database import Database
from laxagent.storage.models import Run
from smoke_chat import chat_round
from smoke_resume import free_port, ready


async def smoke(args):
    config = load_config(args.config)
    async with Database.open(config.database) as db:
        async with db.transaction() as tx:
            assert await tx.scalar(select(Run.id).where(Run.status.in_(["waiting","running","paused"])).limit(1)) is None, \
                "Wait for existing active runs before starting this smoke test"
    ports = []
    while len(ports) < 3:
        port = free_port()
        if port not in ports:
            ports.append(port)
    artifacts = Path(tempfile.mkdtemp(prefix="laxagent-docker-eval-smoke-"))
    print(json.dumps({"artifacts":str(artifacts)}),flush=True)
    container_name = "laxagent-eval-crud-" + uuid4().hex
    marker = "crud-" + uuid4().hex
    container = docker_command("run","-d","--name",container_name,"--user","0","--entrypoint","/bin/bash",
                               args.image,"-c","mkdir -p /testbed; exec sleep infinity",timeout=120).decode().strip()
    try:
        with tempfile.TemporaryDirectory(prefix="laxagent-eval-config-") as temporary:
            raw = json.loads(args.config.read_text())
            raw["api_server"]["port"],raw["llm_router"]["port"] = ports[:2]
            raw.setdefault("eval_server",{})["port"] = ports[2]
            raw.pop("frontend",None)
            private_config = Path(temporary)/"config.json"
            private_config.write_text(json.dumps(raw))
            private_config.chmod(0o600)
            # Deliberately do not copy builtin_skills: eval must not need them.
            with (artifacts/"server.log").open("w") as log:
                process = subprocess.Popen([sys.executable,"-m","laxagent.main","--config",str(private_config),"--eval_in_docker"],
                                           stdout=log,stderr=log)
                try:
                    async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}",trust_env=False,timeout=400) as api, \
                               httpx2.AsyncClient(base_url=f"http://127.0.0.1:{ports[2]}",trust_env=False,timeout=60) as evaluation:
                        await ready(process,api)
                        created = await evaluation.post("/eval/create_session",json={"session_name":"Docker CRUD smoke",
                                                         "container_id":container,"container_workspace":"/testbed"})
                        created.raise_for_status()
                        identity = created.json()
                        login = await api.post("/api/v1/login",json={"user_id":identity["user_id"]})
                        login.raise_for_status()
                        api.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                        assert (await api.get("/api/v1/skills")).status_code == 404
                        assert (await evaluation.post("/eval/collect",json={})).status_code == 404
                        query = (
                            "请完成一个文件 CRUD 验收任务，必须实际使用以下四个工具，不要使用联网搜索。"
                            "1. 用 write_file 在当前 workspace 创建 result.txt，内容严格为 BEFORE 加一个换行。"
                            "2. 用 read_file 读取它确认内容。"
                            f"3. 用 edit_file 将 BEFORE 替换为 {marker}，保留换行，再用 read_file 确认。"
                            "4. 用 write_file 创建 obsolete.txt，内容为 temporary，再用 write_file 的 delete 操作删除它。"
                            "5. 最后用 bash 执行 pwd、cat result.txt 和 test ! -e obsolete.txt 验证。"
                            "所有文件工具使用当前 workspace 的绝对路径。保留 result.txt，简短报告结果。"
                        )
                        run_id,events = await chat_round(api,identity["session_id"],config.llm.default_model,query)
                        calls = [event["content"]["tool_name"] for event in events if event["type"]=="tool_end"]
                        assert {"read_file","write_file","edit_file","bash"} <= set(calls)
                        assert set(calls) <= {"read_file","write_file","edit_file","bash"}
                        output = docker_command("exec",container,"cat","/testbed/result.txt").decode()
                        assert output == marker+"\n"
                        docker_command("exec",container,"test","!","-e","/testbed/obsolete.txt")
                        assert not get_project_workdir(identity["user_id"],identity["project_id"]).exists()
                        assert not get_skills_dir(identity["user_id"]).exists()
                        assert not get_session_config_dir(identity["user_id"],identity["session_id"]).exists()
                        report = {"status":"PASS",**identity,"run_id":run_id,"container_id":container,
                                  "workspace":"/testbed","tool_calls":calls,"result":output}
                        (artifacts/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
                        print(json.dumps(report,ensure_ascii=False),flush=True)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            await asyncio.to_thread(process.wait,timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            await asyncio.to_thread(process.wait,timeout=5)
    finally:
        docker_command("rm","-f",container)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path("config/config.json"))
    parser.add_argument("--image",default="python:3.11-slim")
    args=parser.parse_args()
    try:
        asyncio.run(smoke(args))
    except Exception as exc:
        parser.exit(1,f"Docker eval smoke failed ({type(exc).__name__}); inspect the retained report/server log.\n")
