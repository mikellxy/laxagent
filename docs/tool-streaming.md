# 工具调用生成进度

工具卡片在模型开始生成调用时展示；文件写入等实际执行仍等待完整响应校验、checkpoint 和消息保存完成。增量参数只用于显示，不作为执行输入，也不写入历史消息表。

## SSE 契约

沿用 `/api/v1/runs/{run_id}/events`。所有事件包含 `run_id`、`resume_time`；调用 ID 在 `content.tool_call_id`。客户端以三者组成的键关联同一卡片，不以工具名区分调用。

| 事件 | content | msg_seq |
| --- | --- | --- |
| `tool_call_start` | `tool_call_id`, `tool_name` | 当前 assistant 消息序号 |
| `tool_call_delta` | `tool_call_id`, `arguments_delta` | 当前 assistant 消息序号 |
| `tool_start` | `tool_call_id`, `tool_name`, 完整 `arguments` | 正式 tool 消息序号 |
| `tool_end` | 沿用现有结果结构 | 正式 tool 消息序号 |

四类事件的 role 均为 `tool`。生成阶段的 msg_seq 不代表工具消息已经存在，同一模型响应可以有多个调用。上游使用 item_id 标识参数增量，模型适配层负责映射为 tool_call_id。只提供完整调用的上游仍可直接进入已有的 tool_start/tool_end 流程。

参数片段按顺序拼接为字符串；未完成时不要求合法 JSON。tool_start 的完整参数具有最终权威性，覆盖预览。SSE `id` 沿用 JetStream 消息序号，客户端在重连回放时据此去重参数增量。

## 展示与中断

- 生成卡片与执行卡片共享身份及展开状态，两个同名工具保持各自独立。
- 前端查询确认 run 为 failed/cancelled 后，将尚未执行的临时卡片标记“已中断”；网络断开本身不代表任务中断。
- 已开始执行却未确认结果的调用不能标为“未执行”，也不意味着副作用回滚。
- 未完成调用不进入消息历史；“已中断”卡片仅保留在当前页面，刷新后不恢复。resume_time 变化后清理上一执行次数的临时卡片。

数据库无变更。
