"""Run failure catalog; resume policy is deliberately not implied by a failure code.

Only safe, registered messages cross the persistence/SSE boundary. Infrastructure
exceptions are classified in runtime.runner, keeping this module dependency-free.
"""
from typing import NotRequired, TypedDict


class RunError(Exception):
    def __init__(self, message: str = "Agent execution failed", *, incomplete=False):
        super().__init__(message)
        self.code: str | None = None
        self.incomplete = incomplete


class ModelError(RunError):
    pass


class FailureDefinition(TypedDict):
    exception: type[RunError]
    message: str | None
    desc: str
    incomplete: NotRequired[bool]


RUN_FAILURES: dict[str, FailureDefinition] = {
    "model_request_failed": {
        "exception": ModelError, "message": "Model request failed",
        "desc": "模型服务请求失败，请稍后重试。",
    },
    "model_stream_failed": {
        "exception": ModelError, "message": "Model stream failed",
        "desc": "模型生成回答时出现错误，请重试。",
    },
    "model_stream_ended": {
        "exception": ModelError, "message": "Model stream ended without response.completed",
        "desc": "模型回答意外中断，请重试。",
    },
    "model_incomplete_max_output_tokens": {
        "exception": ModelError, "message": "Model response incomplete: max_output_tokens",
        "desc": "回答达到长度上限，未能完整生成。", "incomplete": True,
    },
    "model_incomplete_content_filter": {
        "exception": ModelError, "message": "Model response incomplete: content_filter",
        "desc": "回答受到内容限制，未能完整生成。", "incomplete": True,
    },
    "model_incomplete_unspecified": {
        "exception": ModelError, "message": "Model response incomplete: unspecified",
        "desc": "模型未能完整生成回答，请重试。", "incomplete": True,
    },
    "invalid_model_stream": {
        "exception": ModelError, "message": "Invalid model stream",
        "desc": "模型返回的数据格式异常，请重试。",
    },
    "invalid_completed_response": {
        "exception": ModelError, "message": "Invalid completed response",
        "desc": "模型返回的完成状态异常，请重试。",
    },
    "unsupported_model_output": {
        "exception": ModelError, "message": "Unsupported model output",
        "desc": "模型返回了暂不支持的结果类型。",
    },
    "unsupported_assistant_content": {
        "exception": ModelError, "message": "Unsupported assistant content",
        "desc": "模型返回了暂不支持的回答格式。",
    },
    "missing_assistant_message": {
        "exception": ModelError, "message": "Completed response contains no assistant message",
        "desc": "模型未返回有效回答，请重试。",
    },
    "invalid_tool_call": {
        "exception": ModelError, "message": "Invalid tool call",
        "desc": "模型生成的工具调用格式异常，请重试。",
    },
    "missing_completed_model_output": {
        "exception": ModelError, "message": "Expected completed model event with output",
        "desc": "模型回答处理异常，请重试。",
    },
    "workspace_initialization_failed": {
        "exception": ModelError, "message": "Workspace initialization failed",
        "desc": "无法准备任务工作目录，请稍后重试。",
    },
    "graph_step_limit_reached": {
        "exception": ModelError, "message": "Agent graph step limit reached",
        "desc": "任务执行步骤已达到上限。",
    },
    "run_submission_failed": {
        "exception": RunError, "message": "Run submission failed",
        "desc": "任务未能启动，请稍后重试。",
    },
    "execution_cancelled": {
        "exception": RunError, "message": "Execution cancelled",
        "desc": "服务中断，任务未能完成。",
    },
    "service_execution_interrupted": {
        "exception": RunError, "message": "Service execution interrupted",
        "desc": "服务中断，上次任务未能完成。",
    },
    "run_storage_failed": {
        "exception": RunError, "message": "Run data operation failed",
        "desc": "任务数据处理失败，请稍后重试。",
    },
    "run_event_failed": {
        "exception": RunError, "message": "Event storage unavailable",
        "desc": "任务执行遇到问题，请稍后重试。",
    },
    "agent_execution_failed": {
        "exception": ModelError, "message": None,
        "desc": "任务执行失败，请稍后重试。",
    },
}


def make_run_error(code: str) -> RunError:
    """Create only catalog-defined exceptions; callers cannot override their payload."""
    definition = RUN_FAILURES[code]
    exception = definition["exception"]
    message = definition["message"]
    error = exception() if message is None else exception(message)
    if not str(error).strip():
        raise ValueError("Run failure messages must not be empty")
    error.code = code
    error.incomplete = definition.get("incomplete", False)
    return error


def registered_run_error(error: RunError) -> RunError:
    """Rebuild from the catalog so ad-hoc messages/metadata never reach users."""
    code = error.code if isinstance(error, RunError) else None
    return make_run_error(code if code in RUN_FAILURES else "agent_execution_failed")
