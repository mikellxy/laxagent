# 内部评测接口

原启动命令同时启动三个监听器：

```sh
uv run python -m laxagent.main --config config/config.json
```

业务 API 默认 127.0.0.1:8000，LLM Router 默认 127.0.0.1:8001，独立 Eval API 默认 127.0.0.1:8002。eval_server.port 可修改端口，省略时为 8002，三个端口不能重复。

Eval 提供 create_session、init_session 和 collect，不校验 JWT，只在内部 loopback 监听器注册。业务 API、前端和 LLM Router 不提供这些路由；现有反向代理和 Compose 继续只发布业务端口。Eval 复用业务应用的数据库、AgentContext 和 TaskManager，不重复创建运行时。停止时依次关闭 Eval、业务 API、LLM Router。

## 创建会话

`POST /eval/create_session`

```json
{"session_name": "ask_pic"}
```

只接受 session_name，去除首尾空白后不能为空。每次生成新的 UUID 用户，并用 session_name 同时命名项目和会话；三个记录在同一事务中创建。不初始化工作区，Agent 执行时按现有逻辑准备目录及 skills。

HTTP 201：

```json
{"user_id": "<generated UUID>", "project_id": 100, "session_id": 101}
```

## 初始化工作区（可选）

`POST /eval/init_session`，multipart/form-data：普通字段 `session_id`（正整数），文件字段 `package`（ZIP）。无需 JWT。创建会话之后、第一轮 chat 之前调用。

```sh
curl -F session_id=101 -F package=@bug.zip http://127.0.0.1:8002/eval/init_session
```

HTTP 200：`{"session_id": 101, "files": ["src/app.py", "tests/test_app.py"]}`。files 为导入的普通文件相对路径，排序输出。ZIP 根目录直接对应 workspace，保留普通文件权限和空目录，不复制额外顶层包目录。支持向已有目录补充文件，不覆盖同名文件；重复上传同一份非空包返回 409。

workspace 由同项目会话共享；导入要求整个项目尚未产生任何 run（包括终态 run），否则 409。导入期间锁定项目及其会话，阻止同项目新会话创建和 chat 提交；不存在或已删除会话返回 404。

ZIP 压缩包最多 64 MiB，解压最多 256 MiB、10000 项，超限返回 413。非法 ZIP、CRC 错误、重复或越界路径、符号链接、特殊文件、加密条目返回 400。目标文件或链接冲突返回 409，文件系统故障返回 503。文件先完整解压校验，再发布；发布中普通错误回滚本次新增文件和目录，已有文件保留。进程强杀或主机故障不保证文件系统事务回滚。

## 登录与发起对话

`/eval/chat` 已删除。评测创建会话后，使用业务 API（默认 8000）：

1. `POST /api/v1/login`，body 为 `{"user_id": "<create_session 返回的 UUID>"}`，取得 `access_token`。不传 `from_page`，不触发游客默认项目/会话初始化。
2. `POST /api/v1/sessions/{session_id}/chat`，请求头 `Authorization: Bearer <access_token>`。

body 沿用业务 ChatRequest：

```json
{"user_query": "请说明这张图片的内容。", "model": "provider:model", "appendix": []}
```

模型必须存在于服务配置。有附件时使用已上传的 `[{"id": 301, "name": "poster.png"}]`；附件上传沿用 `POST /api/v1/appendices`，也使用 Bearer JWT。本步不提供新的附件接口。

HTTP 202 返回 `run_id`、`session_id`、`status: waiting` 和 `resume_time: 0`，只表示已受理。后续等待、查询 run 继续使用业务接口和同一个 JWT。

评测配置的 before_chat 钩子可在每轮请求之前调用登录，再将响应中的 access_token 写入运行时 values。create_session/init_session/collect 保留在 8002，业务 chat 仍执行 JWT、会话归属、模型和附件校验。

## 收集工作区与轨迹

`POST /eval/collect`

```json
{"session_id": 101, "run_id": 201}
```

run 必须属于该 session 且为最新 run。waiting/running/paused 返回 409；success/failed/cancelled 均可采集，不将“拿到 ZIP”等同于“Agent 成功”。会话或运行不存在、已删除、归属不匹配返回 404；传旧 run 返回 409。

HTTP 200，Content-Type 为 application/zip，文件名为 eval-{session_id}-{run_id}.zip：

```text
workspace/        # 当前项目工作区，未初始化时为空
trajectory.json   # 完整 messages 表记录列表，按 seq 升序
```

trajectory.json 保留消息的 id、session_id、seq、role、content、appendix、reasoning、tool_call_id、tool_name、tool_call。它不经过前端消息过滤，不导出 session_messages，也不包含 SSE 进度事件。messages 表没有 run_id，本版导出整个会话，不切分单个 run。

ZIP 在临时文件中完成后才发送，下载结束或连接关闭后关闭临时文件。读取失败返回 503，不返回部分快照。空目录和文件权限保留，符号链接保存链接文本而不读取目标，特殊文件不支持。

采集期间持有目标 session 锁，防止同 session 的下一轮提交、恢复或删除打断快照。工作区是项目级共享资源，不承诺阻止其他会话或进程修改；评测使用 create_session 新建的独立项目。采集失败不改变 run 状态。

## 本地冒烟

先按 AGENTS.md 使用 scripts/deploy_lima.sh 部署到 Lima，再在服务账号下运行：

```sh
uv run python scripts/smoke_eval.py --config config/config.json
```

脚本创建新会话、通过业务登录取得 JWT、调用业务 chat 提交真实模型请求、在 409 时等待后重试 collect，并检查 ZIP、消息和最终运行状态。会保留本次产生的数据库记录和目录便于检查，不改动已有会话。
