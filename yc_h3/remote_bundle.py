"""Portable source discovery, digest and deployment archive for the remote runtime."""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path

REMOTE_DIR = Path(__file__).parent / "remote"
_TEXT_SUFFIXES = {".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini"}


def remote_source_files(remote_dir: str | os.PathLike[str] | None = None) -> Iterator[Path]:
    root = Path(remote_dir or REMOTE_DIR).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"远端代码目录不存在：{root}")

    def fail(exc: OSError) -> None:
        raise exc

    for directory, dirs, names in os.walk(root, onerror=fail):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__" and not name.startswith("."))
        for name in [*dirs, *names]:
            if name.startswith(".") or name.endswith((".pyc", ".pyo", ".tmp")):
                continue
            if (Path(directory) / name).is_symlink():
                raise ValueError("远端部署源不能包含符号链接")
        for name in sorted(names):
            if not name.startswith(".") and not name.endswith((".pyc", ".pyo", ".tmp")):
                path = Path(directory) / name
                if not path.is_file():
                    raise ValueError("远端部署源只能包含普通文件")
                yield path.relative_to(root)


def normalize_remote_bytes(path: str | Path, data: bytes) -> bytes:
    if Path(path).suffix.lower() in _TEXT_SUFFIXES:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def _source_entries(remote_dir: str | os.PathLike[str] | None) -> Iterator[tuple[Path, bytes]]:
    root = Path(remote_dir or REMOTE_DIR).expanduser().resolve()
    for relative in remote_source_files(root):
        yield relative, normalize_remote_bytes(relative, (root / relative).read_bytes())


def remote_source_version(remote_dir: str | os.PathLike[str] | None = None) -> str:
    digest = hashlib.sha256()
    for relative, data in _source_entries(remote_dir):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


source_version = remote_source_version


def pack_remote_tar(remote_dir: str | os.PathLike[str] | None = None) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for relative, data in _source_entries(remote_dir):
            info = tarfile.TarInfo(relative.as_posix())
            info.size = len(data)
            info.mode = 0o755 if relative.suffix.lower() == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return archive.getvalue()
