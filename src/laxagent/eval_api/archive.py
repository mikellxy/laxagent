"""Build evidence ZIPs without following workspace symlinks or reading special files."""

import json
import os
from contextlib import ExitStack
from pathlib import Path
import shutil
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from laxagent.layout import get_project_workdir
from laxagent.tools.workspace import open_parent


def _entry(path: str, mode: int) -> ZipInfo:
    info = ZipInfo(path)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = ZIP_DEFLATED
    return info


def _add_directory(archive: ZipFile, directory: int, prefix: str) -> None:
    for name in sorted(os.listdir(directory)):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        path = f"{prefix}/{name}"
        if stat.S_ISLNK(info.st_mode):
            # Preserve the link itself, never copy data from its destination.
            archive.writestr(_entry(path, info.st_mode), os.fsencode(os.readlink(name, dir_fd=directory)))
        elif stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                archive.writestr(_entry(path + "/", info.st_mode), b"")
                _add_directory(archive, child, path)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                current = os.fstat(source.fileno())
                if not stat.S_ISREG(current.st_mode):
                    raise OSError("Workspace file changed during collection")
                with archive.open(_entry(path, current.st_mode), "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        else:
            raise OSError("Workspace contains an unsupported special file")


def write_archive(file, user_id: str, project_id: int, messages: list[dict]) -> None:
    workspace = get_project_workdir(user_id, project_id).relative_to(Path.home())
    with ZipFile(file, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("trajectory.json", json.dumps(messages, ensure_ascii=False, indent=2))
        archive.writestr("workspace/", b"")
        # Only a missing workspace is an empty snapshot. Missing files encountered
        # after traversal starts are errors rather than silently incomplete exports.
        with ExitStack() as stack:
            try:
                parent = stack.enter_context(open_parent(str(Path.home()), workspace))
                directory = os.open(workspace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
            except FileNotFoundError:
                directory = None
            if directory is not None:
                try:
                    _add_directory(archive, directory, "workspace")
                finally:
                    os.close(directory)
    file.seek(0)
