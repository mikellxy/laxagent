"""Per-call diagnostics, outside the agent workspace and never on the SSE bus."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from laxagent.layout import get_run_diagnostics_dir


logger = logging.getLogger(__name__)
_SECRET_FIELDS = {"api_key", "authorization", "password", "jwt_secret", "access_token", "refresh_token"}


def _redact(value, secrets):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in _SECRET_FIELDS else _redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
                value = value.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        return value
    return value


def _save(path: Path, data: dict) -> bool:
    temporary = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(encoded)
        temporary.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        # Diagnostic failure must not change model execution or expose payloads.
        logger.warning("Model diagnostic write failed: path=%s", path)
        return False


class ModelDiagnostics:
    def __init__(self, *, user_id: str, session_id: int, run_id: int, msg_seq: int,
                 resume_time: int, model: str, secrets: tuple[str, ...] = ()):
        self.path = get_run_diagnostics_dir(user_id, run_id) / (
            f"model-{msg_seq:04d}-r{resume_time}-{uuid4().hex}.json")
        self.secrets = tuple(sorted(set(secrets), key=len, reverse=True))
        self.started = time.monotonic()
        self.data = {
            "schema_version": 1, "run_id": run_id, "session_id": session_id,
            "msg_seq": msg_seq, "resume_time": resume_time, "model": model,
            "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
            "response_source": "SDK-decoded event before agent validation; unknown fields retained",
            "truncated": False, "event_counts": {}, "delta_characters": {},
            "stream_tool_calls": [], "terminal_event": None, "parsed": None,
            "routing": None, "error": None,
        }
        self.save()

    def save(self):
        self.data["elapsed_seconds"] = round(time.monotonic() - self.started, 6)
        return _save(self.path, _redact(self.data, self.secrets))

    def observe(self, event: dict):
        kind = event.get("type", "unknown")
        counts = self.data["event_counts"]
        counts[kind] = counts.get(kind, 0) + 1
        delta = event.get("delta")
        if isinstance(delta, str):
            lengths = self.data["delta_characters"]
            lengths[kind] = lengths.get(kind, 0) + len(delta)
        item = event.get("item")
        if kind == "response.output_item.added" and isinstance(item, dict) and item.get("type") == "function_call":
            self.data["stream_tool_calls"].append({key: item.get(key) for key in ("id", "call_id", "name")})
        response = event.get("response")
        if isinstance(response, dict) and response.get("id"):
            self.data["response_id"] = response["id"]
        if kind in {"response.completed", "response.incomplete", "response.failed", "error"}:
            self.data["terminal_event"] = event
            self.save()  # Persist before completed_content/completed_tool_calls can raise.

    def parsed(self, text: str, calls: list[dict]):
        self.data["parsed"] = {"text_characters": len(text), "tool_call_count": len(calls),
                               "tool_call_ids": [call["call_id"] for call in calls]}

    def finish(self, exc: BaseException | None):
        if self.data["parsed"] is not None:
            self.data["status"] = "parsed"
        else:
            self.data["status"] = "interrupted" if exc and not isinstance(exc, Exception) else "error"
            self.data["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        emit = logger.warning if self.data["error"] else logger.info
        emit("Model diagnostic: run_id=%s msg_seq=%s status=%s error_code=%s path=%s",
             self.data["run_id"], self.data["msg_seq"], self.data["status"],
             (self.data["error"] or {}).get("code"), self.path)


def record_routing(path: str | None, calls: list[dict]):
    if path is None:  # Checkpoints created before diagnostics were introduced.
        return
    try:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        data["routing"] = {"next_node": "tool_node" if calls else "END",
                           "reason": "tool_calls" if calls else "no_tool_calls"}
        _save(target, data)
    except (OSError, ValueError, TypeError):
        logger.warning("Model diagnostic routing write failed: path=%s", path)
