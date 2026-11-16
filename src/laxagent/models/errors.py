"""Safe model retry classification; public failures still use the run catalog."""
import httpx2
from openai import APIConnectionError, APIError, APIStatusError

from laxagent.run_errors import ModelError


def retryable_provider_error(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    return error.get("code") in (
        "server_error", "rate_limit_exceeded", "upstream_stream_error",
    )


def retryable_request_error(error: Exception) -> bool:
    if isinstance(error, APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        # The internal router classifies before sanitizing the upstream payload.
        if body.get("type") == "router_error":
            return body.get("retryable") is True
        if body.get("code") in ("insufficient_quota", "billing_hard_limit_reached"):
            return False
        return error.status_code in (408, 429, 500, 502, 503, 504)
    if isinstance(error, (APIConnectionError, httpx2.TimeoutException,
                          httpx2.NetworkError, httpx2.RemoteProtocolError)):
        return True
    if isinstance(error, APIError):
        return retryable_provider_error(error.body)
    return False


def retryable_model_error(error: Exception) -> bool:
    return isinstance(error, ModelError) and error.retryable
