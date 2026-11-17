"""Execution dependencies bound once per run, outside model-supplied arguments."""
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol

from laxagent.sandbox.bubblewrap import run_bash
from laxagent.sandbox.file_tools import run_file_tool


class ExecutionBackend(Protocol):
    def run_file_tool(self, tool_name: str, request: dict, *, stop_event: Event | None = None) -> dict: ...
    def run_bash(self, command: str, timeout: int, *, stop_event: Event | None = None) -> dict: ...


@dataclass(frozen=True)
class BubblewrapBackend:
    mounts: tuple[tuple[Path, str], ...]
    workspace: str

    def run_file_tool(self, tool_name, request, *, stop_event=None):
        return run_file_tool(tool_name, self.mounts, request, stop_event=stop_event)

    def run_bash(self, command, timeout, *, stop_event=None):
        return run_bash(self.mounts, self.workspace, command, timeout, stop_event=stop_event)
