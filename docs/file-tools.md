# 文件与技能的沙箱路径

模型使用统一的沙箱路径：当前项目为 `/user/project/{project_id}`，技能为 `/user/skills/{name}`。沙箱只挂载当前用户的全部项目和技能；附件、session 配置及诊断数据不挂载。服务端数据库、文件预览和 Eval 采集仍使用宿主 layout 路径。

`read_file`、`write_file`、`edit_file` 接受绝对路径，在 bwrap 中执行 Go 二进制，不接受 `skill_name`，不依赖工作目录，不回退宿主执行。bash 使用相同挂载，默认目录为当前项目，文件工具与 bash 可以相互读取产生的文件。每次调用的 `/tmp` 和 shell 状态不保留。

`load_skill` 在宿主读取 `SKILL.md` 并枚举资源，返回入口完整内容和排序后的沙箱绝对路径清单。技能 YAML 的名称和描述仍用于系统提示词目录；关闭自动使用的技能仍可按名称加载。脚本通过 bash 执行，读取当前文件，不保存已加载状态、内容摘要或只读执行快照。

技能文件按普通文件维护。写入和编辑不校验元数据，发现或加载时校验；可以删除入口文件，删除后技能不可发现或加载。多次文件操作不是事务，取消不撤销已完成的修改。当前项目成功写入或编辑的路径登记为成果候选；技能、其他项目、删除和 bash 产物不自动登记，但助手可引用已知项目文件。

## 构建与部署

- `bash scripts/build_file_tools.sh` 构建三个 Linux 二进制。需要 Go 1.22 或更新版本；可以用 `GOARCH` 指定架构。
- Lima 和 ECS 部署在目标机器构建，不同步本机 `tools/bin`。Docker 在镜像构建阶段编译，构建缓存不占用服务账号的 home。
- 部署探针 `uv run --no-sync python scripts/probe_file_tools.py` 验证实际读写、编辑、删除及 bash 技能脚本路径互通。已有 bash 探针继续检查运行环境。
- 既有用户技能副本不会被内置技能更新覆盖。旧技能说明和历史 checkpoint 不自动迁移；验证使用新用户和新会话，不清理已有用户数据。

## 验证

Linux/Lima 中先构建二进制，再执行 `uv run pytest -m ""`，包含全部 Python 测试和真实 bwrap 测试。测试使用专用 `laxagent_test` 数据库；`LAXAGENT_TEST_DB_HOST` 指定数据库主机，`NATS_URL` 指向独立测试 NATS，避免与活动服务消费者互相干扰。

Go 测试在 `src/laxagent/tools/go` 执行 `go test ./...`。前端执行 `npm --prefix frontend test` 与 `npm --prefix frontend run test:browser`。

真实模型冒烟在 Lima 使用被 Git 忽略的 `config/config.json`。`scripts/smoke_skills.py` 验证发现、创建、读写编辑、脚本执行和成果预览；`scripts/smoke_resume.py`、`scripts/smoke_cancel.py` 验证中断恢复、跨实例取消及副作用保留。`scripts/smoke_chat.py` 对已启动的服务验证初始化、项目复用和绝对路径读取。
