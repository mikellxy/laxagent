"""Compose execution policy once; tools consume resolved paths and a backend."""
from dataclasses import dataclass
from pathlib import PurePosixPath

from laxagent import layout
from laxagent.context.prompts import system_prompt
from laxagent.context.eval_prompt import system_prompt as eval_prompt
from laxagent.sandbox.backend import BubblewrapBackend
from laxagent.sandbox.docker import DockerBackend, prepare_container
from laxagent.skills import builtin_snapshots, initialize_skills, skill_catalog_at
from laxagent.storage.database import DatabaseError


@dataclass(frozen=True)
class StandardProfile:
    tool_names = None
    host_file_routes = True
    attachments = True

    def startup(self, builtin_skills_dir):
        builtin_snapshots(builtin_skills_dir)

    def login(self, user_id, builtin_skills_dir):
        initialize_skills(user_id, builtin_skills_dir)

    def prepare_project(self, container_id, workspace):
        if container_id is not None or workspace is not None:
            raise DatabaseError("Container binding requires Docker eval mode", 422)
        return {"container_id": None, "container_workspace": None}

    def environment(self, project):
        workspace = str(layout.get_sandbox_project_workdir(str(project.id)))
        return dict(workspace=workspace,
                    backend=BubblewrapBackend(layout.get_user_sandbox_mounts(project.user_id), workspace),
                    skill_dir=layout.get_skills_dir(project.user_id), sandbox_skill_dir=PurePosixPath("/user/skills"))

    def prepare(self, context, state):
        from laxagent.agent.nodes.preparation import initialize_workspace
        initialize_workspace(state, context.builtin_skills_dir)
        return system_prompt(context.workspace, state.get("user_configs"), skill_catalog_at(context.skill_dir))


@dataclass(frozen=True)
class DockerEvalProfile:
    tool_names = frozenset({"read_file", "write_file", "edit_file", "bash", "web_search"})
    host_file_routes = False
    attachments = False

    def startup(self, builtin_skills_dir):
        pass

    def login(self, user_id, builtin_skills_dir):
        pass

    def prepare_project(self, container_id, workspace):
        if container_id is None or workspace is None:
            raise DatabaseError("Docker eval requires container_id and container_workspace", 422)
        return {"container_id": prepare_container(container_id, workspace), "container_workspace": workspace}

    def environment(self, project):
        return dict(workspace=project.container_workspace,
                    backend=DockerBackend(project.container_id, project.container_workspace),
                    skill_dir=None, sandbox_skill_dir=None)

    def prepare(self, context, state):
        return eval_prompt(context.workspace)
