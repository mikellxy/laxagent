"""Skill metadata, file discovery and incremental builtin initialization."""

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from threading import Lock, RLock
from weakref import WeakValueDictionary

import yaml

from laxagent import layout
from laxagent.tools.errors import ToolError
from laxagent.tools.workspace import atomic_replace, open_parent

logger = logging.getLogger(__name__)
MAX_FILES = 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_SKILL_BYTES = 64 * 1024
_locks = WeakValueDictionary()
_locks_guard = Lock()


class SkillError(ToolError):
    pass


def validate_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise SkillError("INVALID_SKILL_NAME")
    return name


class _MetadataLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SkillError("INVALID_SKILL_METADATA")
        result[key] = loader.construct_object(value_node)
    return result


_MetadataLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_metadata(name: str, content: bytes) -> dict:
    validate_name(name)
    if len(content) > MAX_SKILL_BYTES:
        raise SkillError("SKILL_TOO_LARGE")
    try:
        text = content.decode("utf-8")
        lines = text.splitlines()
        if not lines or lines[0] != "---" or "\x00" in text:
            raise ValueError
        end = lines.index("---", 1)
        meta = yaml.load("\n".join(lines[1:end]), Loader=_MetadataLoader)
        if not isinstance(meta, dict) or set(meta) - {"name", "description", "allow_auto_use"}:
            raise ValueError
        if meta.get("name") != name:
            raise ValueError
        description = meta.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError
        auto = meta.get("allow_auto_use", True)
        if type(auto) is not bool:
            raise ValueError
        return {"name": name, "description": description, "allow_auto_use": auto}
    except (ValueError, UnicodeError, yaml.YAMLError, RecursionError):
        raise SkillError("INVALID_SKILL_METADATA") from None


@contextmanager
def skill_lock(user_id: str, name: str):
    key = (user_id, name)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = RLock()
            _locks[key] = lock
    with lock:
        yield


@contextmanager
def skills_root(path: Path, *, create=False):
    directory = os.open(Path.home(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(Path.home()).parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


@contextmanager
def user_skills_root(user_id: str, *, create=False):
    with skills_root(layout.get_skills_dir(user_id), create=create) as root:
        yield root


@contextmanager
def skill_directory_at(path: Path, name: str, *, create=False):
    validate_name(name)
    with skill_lock(str(path), name), skills_root(path, create=create) as root:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            yield directory
        finally:
            os.close(directory)


@contextmanager
def skill_directory(user_id: str, name: str, *, create=False):
    with skill_directory_at(layout.get_skills_dir(user_id), name, create=create) as directory:
        yield directory


def _walk(directory: int, prefix="", *, depth=0):
    if depth > 32:
        raise SkillError("SKILL_TOO_LARGE")
    for name in sorted(os.listdir(directory)):
        path = prefix + name
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                yield from _walk(child, path + "/", depth=depth + 1)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            yield directory, name, path
        else:
            raise SkillError("INVALID_SKILL_FILE")


def file_tree(directory: int) -> list[str]:
    files = []
    for _, _, path in _walk(directory):
        files.append(path)
        if len(files) > MAX_FILES:
            raise SkillError("SKILL_TOO_LARGE")
    return sorted(files)


def read_skill_entry(directory: int) -> bytes:
    fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SkillError("INVALID_SKILL_FILE")
        return file.read(MAX_SKILL_BYTES + 1)


@dataclass(frozen=True)
class BuiltinSkill:
    metadata: dict
    contents: dict[str, bytes]


def _read_builtin_skill(directory: int, name: str) -> BuiltinSkill:
    contents, total = {}, 0
    for parent, filename, relative in _walk(directory):
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SkillError("INVALID_SKILL_FILE")
            data = file.read(MAX_BYTES - total + 1)
        contents[relative] = data
        total += len(data)
        if total > MAX_BYTES or len(contents) > MAX_FILES:
            raise SkillError("SKILL_TOO_LARGE")
    if "SKILL.md" not in contents:
        raise SkillError("INVALID_SKILL_METADATA")
    metadata = parse_metadata(name, contents["SKILL.md"])
    return BuiltinSkill(metadata, contents)


def builtin_snapshots(source: Path | None) -> list[BuiltinSkill]:
    """Read builtin files for startup validation and initial copying, not execution."""
    if source is None or not source.exists():
        return []
    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        result = []
        for name in sorted(os.listdir(root)):
            validate_name(name)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                result.append(_read_builtin_skill(child, name))
            finally:
                os.close(child)
        return result
    finally:
        os.close(root)


def initialize_skills(user_id: str, source: Path | None) -> None:
    builtins = builtin_snapshots(source)
    with user_skills_root(user_id, create=True) as root:
        for skill in builtins:
            name = skill.metadata["name"]
            with skill_lock(str(layout.get_skills_dir(user_id)), name):
                try:
                    os.stat(name, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    continue
                temporary = ".laxagent-seed-" + secrets.token_hex(16)
                os.mkdir(temporary, mode=0o700, dir_fd=root)
                directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                try:
                    for path, content in skill.contents.items():
                        relative = Path(path)
                        with open_parent(directory, relative, create=True) as parent:
                            atomic_replace(parent, relative.name, content)
                    os.rename(temporary, name, src_dir_fd=root, dst_dir_fd=root)
                finally:
                    os.close(directory)
                    # rmtree uses descriptor-relative, symlink-resistant traversal.
                    try:
                        shutil.rmtree(temporary, dir_fd=root)
                    except FileNotFoundError:
                        pass


def skill_catalog(user_id: str) -> list[dict]:
    return skill_catalog_at(layout.get_skills_dir(user_id))


def skill_catalog_at(path: Path) -> list[dict]:
    with skills_root(path) as root:
        names = sorted(os.listdir(root))
    result = []
    for name in names:
        if name.startswith("."):
            continue
        try:
            with skill_directory_at(path, name) as directory:
                metadata = parse_metadata(name, read_skill_entry(directory))
                file_tree(directory)
            if metadata["allow_auto_use"]:
                result.append({key: metadata[key] for key in ("name", "description")})
        except (ToolError, OSError, UnicodeError):
            logger.warning("Ignoring invalid skill %r", name)
    return result


def user_skill_list(user_id: str) -> list[dict]:
    """Read entry metadata for explicit selection, without loading skill resources."""
    try:
        with user_skills_root(user_id) as root:
            names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    result = []
    for name in names:
        if name.startswith("."):
            continue
        try:
            with skill_directory(user_id, name) as directory:
                metadata = parse_metadata(name, read_skill_entry(directory))
            result.append({"name": metadata["name"], "desc": metadata["description"]})
        except (ToolError, OSError, UnicodeError):
            logger.warning("Ignoring invalid skill entry %r", name)
    return result
