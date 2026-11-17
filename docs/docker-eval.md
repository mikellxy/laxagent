# Docker eval 模式

使用 `--eval_in_docker` 启动独立评测进程，将项目绑定到已经准备好的 Docker 容器。Agent 与正式模式共用 Graph、文件工具、消息和恢复机制；提示词、工具集合和执行后端在启动时装配。

```bash
uv run alembic -x config=config/config.json upgrade head
uv run python -m laxagent.main --config config/config.json --eval_in_docker
```

服务账号需要能够访问目标 Docker daemon。服务不负责拉取镜像、启动或删除 case 容器，也不挂载宿主项目、skills、附件到容器。正式服务不带此参数，仍使用 bubblewrap。

## 接口

先启动一个持续运行的容器，再通过已认证的 `POST /api/v1/projects` 创建项目：

```json
{
  "name": "case-name",
  "container_id": "container-name-or-id",
  "container_workspace": "/testbed"
}
```

也可以调用仅监听 loopback 的 `POST /eval/create_session`，一次创建用户、项目和会话：

```json
{
  "session_name": "case-name",
  "container_id": "container-name-or-id",
  "container_workspace": "/testbed"
}
```

Docker eval 下两个容器字段必填，workspace 必须是容器中存在的绝对目录。创建时解析为完整容器 ID、检查目标架构、复制并试运行工具，成功后才创建数据库记录。失败可重试；已复制的工具保留，仓库内容不改动。正式模式不接受非空容器绑定。响应沿用既有项目或会话结构。

使用返回的用户 ID 登录，再通过既有 session/chat、run 状态、SSE 和消息接口提交任务。eval 登录不初始化 skills，也不为游客自动创建未绑定容器的默认项目。chat 不接受附件引用。

Docker eval 提供 read_file、write_file、edit_file、bash，以及配置启用时的 web_search。模型声明与执行分发使用同一集合。不提供 skills、附件、记忆、用户问答或候选文件展示工具。

Docker eval 不注册 skills、附件和宿主 workspace 预览路由，也不提供 `/eval/init_session`、`/eval/collect`。通过外部评测脚本从容器采集 patch 和文件；消息记录仍可从业务接口读取。首次评测建议每个 case 使用独立容器、项目和会话，不并发修改同一仓库。

## 数据库与恢复

迁移 `0012` 为 projects 增加可空 Text 字段 `container_id`、`container_workspace`，约束两项同时为空或同时有值。原有项目保持空值，不迁移磁盘数据。

项目绑定本期不可通过 API 修改。同一 session 始终使用同一种运行模式，由使用方式保证；运行时不额外保存模式/绑定快照或做恢复比对。每次执行（包括恢复）直接读取项目绑定来构建后端。容器停止后可启动原容器再重试；服务不会按名称寻找替代容器或回退到宿主。结果未知的工具调用沿用不自动重放规则。

## 执行与路径

每次 run 独立注入执行环境中的 workspace 和执行后端，避免修改跨任务共享 context。正式后端持有挂载源，Docker 后端持有容器 ID。两种后端均实现：

```python
run_file_tool(tool_name, request, *, stop_event)
run_bash(command, timeout, *, stop_event)
```

工具直接传递模型给出的绝对路径。write/edit 候选登记仅相对 context.workspace 计算。正式模式技能的宿主读取路径与沙箱返回路径分别注入，eval 不进入技能初始化、发现或加载流程。附件数据库路径和诊断日志仍由宿主存储管理；无实际用途的 session_dir 不注入。

可信内部调用遵循自身接口约定，不在调用方重复校验或兜底；外部请求/模型参数在入口校验，文件、容器等可变外部资源的错误仍正常处理。

三个 Go 文件工具复制到 `/read_file`、`/write_file`、`/edit_file`，通过 stdin 传 JSON，工作目录为 `/`。额外的 `/sandbox_exec` 是内部 Go 命令监督器，负责超时、取消、输出截断和子进程回收，不注册为模型工具。

bash 在容器 workspace 启动新 shell，继承容器环境（包括其配置的 BASH_ENV），不覆盖为宿主 PATH，也不自动为所有镜像指定某个 Conda 环境。case 准备阶段应配置合适的环境。文件与 /tmp 跨调用保留，shell 变量/cwd 不保留。监督器作为 Linux subreaper，清理进程组及被收养的后台子进程；取消通过容器内控制请求完成，不能仅杀宿主 Docker CLI。Docker daemon 失联等基础设施故障不能保证即时清理，监督器自身超时仍会终止该调用。

## 二进制构建

原有构建命令现在同时生成三个文件工具和内部监督器：

```bash
bash scripts/build_file_tools.sh
```

默认使用服务器本机架构。如果 case 镜像架构不同，提前构建对应产物：

```bash
GOARCH=amd64 LAXAGENT_TOOLS_OUTPUT="$PWD/src/laxagent/tools/bin/amd64" bash scripts/build_file_tools.sh
GOARCH=arm64 LAXAGENT_TOOLS_OUTPUT="$PWD/src/laxagent/tools/bin/arm64" bash scripts/build_file_tools.sh
```

创建项目优先使用目标架构子目录，否则使用默认目录，并检查 Linux ELF 架构。无需在 case 容器中安装 Go 或 Python 来执行文件工具。

## 验证

相关模式/API 测试：

```bash
uv run pytest tests/test_docker_eval.py
```

真实 Docker 后端测试（需要 Docker 和已构建二进制）：

```bash
LAXAGENT_DOCKER_TEST_IMAGE=python:3.11-slim uv run pytest -m sandbox_linux tests/test_docker_linux.py
```

真实模型 CRUD 冒烟按项目约定在 Lima 中进行：

```bash
bash scripts/deploy_lima.sh
# 以下在 Lima 的项目目录执行，使用真实 config/config.json：
uv run alembic -x config=config/config.json upgrade head
uv run python scripts/smoke_docker_eval.py --config config/config.json --image python:3.11-slim
```

脚本创建专用容器和临时端口的 eval 服务，不读取 SWE 官方镜像。Agent 用文件工具创建、读取、修改和删除文件，用 bash 验证最终结果。脚本核验实际容器文件、工具调用及宿主无项目/skills/session 目录，保留数据库证据与 `/tmp/laxagent-docker-eval-smoke-*` 报告，然后清理自己的服务和容器。

SWE-bench 镜像准备、patch 提取、预测文件生成和官方评分留给外部评测流程，本次不运行官方 case。
