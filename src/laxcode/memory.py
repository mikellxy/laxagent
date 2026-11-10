"""Shared vocabulary for persistent, user-scoped memory."""

from typing import Literal


MemoryCategory = Literal["profile", "preference", "instruction", "knowledge", "project", "workflow"]

MEMORY_CATEGORY_LABELS: dict[MemoryCategory, str] = {
    "profile": "用户背景", "preference": "长期偏好", "instruction": "行为要求",
    "knowledge": "业务知识", "project": "项目背景", "workflow": "工作方法",
}


def memory_enabled(user_configs: dict[str, str] | None) -> bool:
    return (user_configs or {}).get("enable_memory") == "1"

MEMORY_CATEGORY_DESCRIPTIONS: dict[MemoryCategory, str] = {
    "profile": "用户明确提供的稳定背景、角色和长期环境，不保存推测的人物画像。",
    "preference": "用户明确表达的长期偏好，保留适用条件和例外。",
    "instruction": "用户明确提出、希望跨会话沿用的行为要求。",
    "knowledge": "用户业务中的术语、口径和领域事实，不保存随时可查的通用知识。",
    "project": "特定项目的长期目标、约束、决策和背景，摘要须注明项目范围。",
    "workflow": "已验证、可复用的操作方法或排障经验，保留适用条件。",
}


def memory_category_description() -> str:
    return "\n".join(f"- {category}: {description}" for category, description in MEMORY_CATEGORY_DESCRIPTIONS.items())


class MemoryUnavailableError(Exception):
    """Missing, replaced and foreign-owned IDs deliberately share one error."""
