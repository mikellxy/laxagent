"""User data paths; calculating a path never creates files or directories."""

from pathlib import Path


def get_user_data_dir(user_id: str) -> Path:
    return Path.home() / "laxcode" / user_id


def get_session_config_dir(user_id: str, session_id: int) -> Path:
    return get_user_data_dir(user_id) / "session" / str(session_id)


def get_project_workdir(user_id: str, project_id: int) -> Path:
    return get_user_data_dir(user_id) / "project" / str(project_id)


def get_skills_dir(user_id: str) -> Path:
    return get_user_data_dir(user_id) / "skills"


def get_appendices_dir(user_id: str) -> Path:
    return get_user_data_dir(user_id) / "appendix"
