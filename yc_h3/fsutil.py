"""Shared filesystem, locking and credential redaction helpers."""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator


def fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if os.name != "nt":
            raise


@contextmanager
def atomic_writer(path: Path, *, temp: Path | None = None, mode: int = 0o600) -> Iterator[BinaryIO]:
    """Flush a private sibling staging file, then replace and sync its destination."""
    if temp is None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
    else:
        if temp.parent.resolve() != path.parent.resolve() or temp.resolve() == path.resolve():
            raise ValueError("暂存文件须与目标文件同目录且使用不同路径")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name != "nt" and mode != 0o600:
                os.chmod(temp, mode)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    with atomic_writer(path) as stream:
        stream.write(data)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any, *, newline: bool = False, **options: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, **options)
    atomic_write_text(path, text + ("\n" if newline else ""))


@contextmanager
def file_lock(
    path: Path | str, *, timeout: float = 30.0, timeout_message: str = "获取文件锁超时",
) -> Iterator[None]:
    """Acquire a process lock on a persistent file, creating it only for writers."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("锁超时须为有限非负数")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise ValueError("锁文件不能是符号链接")

    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            def lock_byte(acquire: bool) -> None:
                # msvcrt locks relative to the current file position and supports locking past EOF.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)

            busy_codes = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
        else:
            import fcntl

            def lock_byte(acquire: bool) -> None:
                operation = fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN
                fcntl.flock(fd, operation)

            busy_codes = {errno.EACCES, errno.EAGAIN}

        deadline = time.monotonic() + timeout
        while True:
            try:
                lock_byte(True)
                break
            except OSError as exc:
                if exc.errno not in busy_codes:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(timeout_message) from None
                time.sleep(min(0.02, remaining))
        try:
            yield
        finally:
            lock_byte(False)
    finally:
        os.close(fd)


class AtomicPublishError(OSError):
    code = "atomic_publish_unsupported"


def publish_no_clobber(
    staged: Path, wanted: Path, *, before_publish: Callable[[Path], None] | None = None,
) -> Path:
    """Publish a complete sibling file using an atomic, non-replacing operation."""
    if staged.parent.resolve() != wanted.parent.resolve() or staged == wanted:
        raise ValueError("成片暂存文件须与目标文件同目录且使用不同路径")
    if staged.is_symlink():
        raise ValueError("成片暂存文件不能是符号链接")
    with staged.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())

    install = os.rename if os.name == "nt" else os.link
    version = 1
    while True:
        candidate = wanted if version == 1 else wanted.with_name(f"{wanted.stem}_v{version}{wanted.suffix}")
        if not os.path.lexists(candidate):
            if before_publish is not None:
                before_publish(candidate)
            try:
                install(staged, candidate)
            except FileExistsError:
                pass
            except OSError as exc:
                if os.name != "nt" and exc.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.ENOSYS}:
                    raise AtomicPublishError(exc.errno, "目标卷不支持原子发布，请使用 yc-h3 jobs fetch <task_key> --out <支持目录>") from None
                raise
            else:
                fsync_dir(candidate.parent)
                if os.name != "nt":
                    staged.unlink()
                return candidate
        version += 1


def ensure_real_path(
    path: Path | str, *, must_exist: bool = False, expect_dir: bool | None = None, label: str = "",
) -> Path:
    """Resolve a local path and check its requested type."""
    raw = Path(path).expanduser()
    checked = raw.parent.resolve() / raw.name if expect_dir is False else raw.resolve()
    desc = label or str(checked)
    if must_exist and not checked.exists():
        raise ValueError(f"缺少{desc}：{checked}")
    if checked.exists() and expect_dir is not None:
        valid = checked.is_dir() if expect_dir else checked.is_file()
        if not valid:
            raise ValueError(f"{desc} 必须是{'目录' if expect_dir else '普通文件'}：{checked}")
    return checked


def ensure_real_subpath(base: Path | str, target: Path | str) -> Path:
    root = Path(base).expanduser().resolve()
    checked = Path(target).expanduser().resolve()
    if not checked.is_relative_to(root):
        raise ValueError(f"路径不在受管目录内：{target}")
    return checked


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def find_bin(name: str, *fallbacks: str, path_first: bool = True) -> str | None:
    if path_first and (found := shutil.which(name)):
        return found
    for candidate in fallbacks:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None if path_first else shutil.which(name)


_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"'}]+")
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:api_?key|auth(?:orization)?(?:_token|_key)?|token|secret|password|passwd|cookie|credential)(?:$|_)",
    re.IGNORECASE,
)
_NON_SECRET_SUFFIXES = (
    "_fp", "_fingerprint", "_sha256", "_hash", "_digest", "_file", "_path", "_dir",
    "_sock", "_socket", "_mode", "_type", "_enabled", "_disabled", "_url", "_uri",
    "_host", "_port", "_timeout", "_count",
)


def is_secret_key(key: str) -> bool:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", key).replace("-", "_").lower()
    if normalized.endswith(_NON_SECRET_SUFFIXES):
        return False
    return bool(_SECRET_KEY.search(normalized))


def redact_secrets(value: str, *secrets: str | None, keep_bearer: bool = False) -> str:
    value = _BEARER.sub("Bearer [REDACTED]" if keep_bearer else "[REDACTED]", value)
    known = {variant for secret in secrets if secret for variant in (secret, secret.strip()) if variant}
    if known:
        pattern = "|".join(map(re.escape, sorted(known, key=lambda item: (-len(item), item))))
        parts = value.split("[REDACTED]")
        value = "[REDACTED]".join(re.sub(pattern, "[REDACTED]", part) for part in parts)
    return value


def redact_data(value: Any, *secrets: str) -> Any:
    """Redact structured payloads, including values under credential-named fields."""
    if isinstance(value, str):
        return redact_secrets(value, *secrets)
    if isinstance(value, dict):
        return {
            redact_secrets(key, *secrets) if isinstance(key, str) else key:
            "[REDACTED]" if isinstance(key, str) and is_secret_key(key) else redact_data(item, *secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_data(item, *secrets) for item in value]
    return value
