"""User data paths; calculating a path never creates files or directories."""

from pathlib import Path, PurePosixPath


def get_user_data_dir(user_id: str) -> Path:
    return Path.home() / "laxagent" / user_id


def get_session_config_dir(user_id: str, session_id: int) -> Path:
    return get_user_data_dir(user_id) / "session" / str(session_id)


def get_project_workdir(user_id: str, project_id: int) -> Path:
    return get_user_data_dir(user_id) / "project" / str(project_id)


def get_user_sandbox_mounts(user_id: str) -> tuple[tuple[Path, str], ...]:
    """Only projects and skills are exposed; other user data stays on the host."""
    return ((get_user_data_dir(user_id) / "project", "/user/project"),
            (get_skills_dir(user_id), "/user/skills"))


def get_sandbox_project_workdir(project_id: str) -> PurePosixPath:
    """Current project path shared by sandbox cwd and file preview registration."""
    return PurePosixPath("/user/project") / project_id


def get_skills_dir(user_id: str) -> Path:
    return get_user_data_dir(user_id) / "skills"


def get_appendices_dir(user_id: str) -> Path:
    return get_user_data_dir(user_id) / "appendix"


def get_run_diagnostics_dir(user_id: str, run_id: int) -> Path:
    return get_user_data_dir(user_id) / "diagnostics" / "runs" / str(run_id)
