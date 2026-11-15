# laxagent

---

```bash
git clone https://github.com/mikellxy/laxagent.git && cd laxagent
# `deploy/compose/config.json` 配置 `llm`
docker-compose -f deploy/compose/compose.yaml up -d --build
```

访问 `http://yourip:8000` 开始使用 laxagent！

---

建议将 `deploy/compose/config.json` 中的 `$.auth.jwt_secret` 和 `$.database.password` 改为自己的值。

启动后如想再次修改 PG 密码，请删除 PG 容器及其数据卷，再重新运行 Compose 生效（会清空数据库）。

## 模型调用诊断日志

主 ReAct 循环每次调用模型时，自动写入服务账号 home 下的用户数据目录：

```text
~/laxagent/<user_id>/diagnostics/runs/<run_id>/model-<msg_seq>-r<resume_time>-<uuid>.json
```

`msg_seq` 是本次助手消息序号；UUID 确保恢复或重复调用不会覆盖旧证据。Lima 部署时，这个路径位于虚拟机内。日志不在 Agent workspace 中，也不通过 SSE 或 Eval collect 导出。

日志包含模型、run/session/消息标识、耗时、各类流式事件数量与 delta 字符数，以及 `terminal_event` 中完整的终态事件（含整个 response 对象）。终态事件在 OpenAI SDK 解码后、laxagent 校验前记录，保留 SDK 提供的未知字段和显式 null；它不是原始 HTTP/SSE 字节转储。未截断，也不会逐 token 落盘。`missing_assistant_message` 等解析错误仍保留终态响应和错误码。

`parsed` 记录正文长度与工具调用数量/ID；消息保存成功后，`routing` 记录按现有规则继续 `tool_node` 或以 `no_tool_calls` 结束至 `END` 的决定，不代表任务验收通过。凭据字段及已配置服务密钥会脱敏，文件权限为 0600；诊断写入失败只告警，不中止任务。日志可能包含模型正文、推理和工具参数，仅供本地诊断，需按需清理。
