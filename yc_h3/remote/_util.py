"""Filesystem and digest helpers shipped with the standalone remote server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    """Return lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, check_cancel=lambda: None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
            check_cancel()
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def atomic_write_bytes(path, data, *, temp=None):
    """Use a private sibling by default or the caller's existing staging path."""
    if temp is None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        stream = os.fdopen(fd, "wb")
    else:
        stream = temp.open("wb")
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_text(path, text, *, temp=None):
    atomic_write_bytes(path, text.encode("utf-8"), temp=temp)


def remote_root() -> str:
    if configured := (os.environ.get("YC_H3_REMOTE_ROOT") or "").strip():
        return str(Path(configured).expanduser().absolute())
    if Path("/content").is_dir() and os.access("/content", os.W_OK):
        return "/content/yc_h3_engine"
    if Path.home() == Path("/root"):
        return "/root/yc_h3_engine"
    return str(Path.home() / ".yc-h3-engine" / "remote")


def remote_job_paths(root: str | Path) -> tuple[str, Path, Path]:
    instance_id = os.environ.get("YC_H3_PLATFORM_INSTANCE_ID") or ""
    if instance_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance_id):
        raise ValueError("远端平台实例标识无效")
    root = Path(root)
    return instance_id, root / "outputs" / instance_id, root / "jobs" / instance_id


# The deployable archive is standalone. Keep its source digest aligned with remote_bundle.py.
_TEXT_SUFFIXES = {".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini"}


def remote_source_files(remote_dir: str | os.PathLike[str]) -> Iterator[Path]:
    root = Path(remote_dir).expanduser().resolve()
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
    root = Path(remote_dir).expanduser().resolve()
    for relative in remote_source_files(root):
        yield relative, normalize_remote_bytes(relative, (root / relative).read_bytes())


def remote_source_version(remote_dir: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    for relative, data in _source_entries(remote_dir):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()
