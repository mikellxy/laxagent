"""Validate an initialization ZIP completely before publishing new workspace files."""

import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, LargeZipFile, ZipFile
import zlib


MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10000


class PackageError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _safe_name(name):
    if ("\x00" in name or "\\" in name or PureWindowsPath(name).drive
            or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))):
        raise PackageError("Package contains an unsafe path")


def _directory(path, created):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise PackageError("Workspace path conflicts with an existing file or link", 409)
    if not path.exists():
        _directory(path.parent, created)
        path.mkdir()
        created.append(path)


def initialize_workspace(source, workspace: Path) -> list[str]:
    source.seek(0, os.SEEK_END)
    if source.tell() > MAX_PACKAGE_BYTES:
        raise PackageError("Package exceeds 64 MiB", 413)
    source.seek(0)
    created_dirs = []
    published = []
    try:
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise PackageError("Package exceeds entry or expanded size limit", 413)
            names = {}
            for entry in entries:
                _safe_name(entry.orig_filename)
                name = entry.filename.rstrip("/")
                if name in names:
                    raise PackageError("Package contains duplicate paths")
                names[name] = entry
                mode = stat.S_IFMT(entry.external_attr >> 16) if entry.create_system == 3 else 0
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise PackageError("Package links and special files are not supported")
                if mode and entry.is_dir() != (mode == stat.S_IFDIR):
                    raise PackageError("Package entry type conflicts with its path")
                if entry.flag_bits & 1 or (entry.is_dir() and entry.file_size):
                    raise PackageError("Encrypted or malformed package entry")
            for name in names:
                for parent in Path(name).parents:
                    entry = names.get(parent.as_posix())
                    if entry is not None and not entry.is_dir():
                        raise PackageError("Package parent path is a file")
            # Stage on the destination filesystem, enabling exclusive hard-link publication.
            _directory(workspace.parent, created_dirs)
            with TemporaryDirectory(prefix=".eval-init-", dir=workspace.parent) as temporary:
                staging = Path(temporary)
                for entry in entries:
                    target = staging / entry.filename
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        archive.read(entry)  # CRC validation also for directory entries.
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as reader, target.open("xb") as writer:
                            shutil.copyfileobj(reader, writer, length=1024 * 1024)
                        mode = entry.external_attr >> 16 & 0o777 if entry.create_system == 3 else 0
                        target.chmod(mode or 0o644)
                # Check all destinations before publishing any files. Existing directories merge.
                _directory(workspace, created_dirs)
                for entry in entries:
                    target = workspace / entry.filename
                    parts = Path(entry.filename).parts
                    for count in range(1, len(parts) + (1 if entry.is_dir() else 0)):
                        _directory(workspace.joinpath(*parts[:count]), created_dirs)
                    if not entry.is_dir() and (target.exists() or target.is_symlink()):
                        raise PackageError("Package would overwrite an existing workspace file", 409)
                for entry in entries:
                    if not entry.is_dir():
                        target = workspace / entry.filename
                        os.link(staging / entry.filename, target)
                        published.append(target)
            return sorted(entry.filename for entry in entries if not entry.is_dir())
    except BaseException as exc:
        for path in reversed(published):
            path.unlink()
        for path in reversed(created_dirs):
            path.rmdir()
        if isinstance(exc, (BadZipFile, LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error)):
            raise PackageError("Invalid or unsupported ZIP package") from exc
        if isinstance(exc, FileExistsError):
            raise PackageError("Workspace file already exists", 409) from exc
        raise
