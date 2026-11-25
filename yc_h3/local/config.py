"""Paths, isolation constants, managed session naming, and persisted local settings.

Adapted from WorldForge (MIT License) with strict yc-h3-engine namespace isolation.
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from yc_h3.config import DEFAULT_HOST, DEFAULT_PORT, EngineConfig, load_config
from yc_h3.fsutil import atomic_write_json, file_lock, find_bin
from yc_h3.providers.video_common import ffmpeg_bin, ffprobe_bin
from yc_h3.remote_bundle import REMOTE_DIR, remote_source_files, remote_source_version

LOCAL_DIR = Path(__file__).resolve().parent

APP_NAME = "yc-h3-engine"
HOST = DEFAULT_HOST
PORT = DEFAULT_PORT
REMOTE_PORT = 7860
TUNNEL_PORT = 17898
REMOTE_ROOT = "/content/yc_h3_engine"
REMOTE_APP_DIR = REMOTE_ROOT + "/app"
REMOTE_RUN_DIR = REMOTE_ROOT + "/run"
REMOTE_LOG = REMOTE_ROOT + "/server.log"

SESSION_PREFIX = "yh3-"
_MANAGED_SESSION_RE = re.compile(r"^yh3-[A-Za-z0-9_-]{1,36}$")
_RAW_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")

GPU_CHOICES = ("T4", "L4", "G4", "H100", "A100")
DEFAULT_GPU_FALLBACK = ("G4", "H100", "A100")


def custom_assets_path(engine_config: EngineConfig) -> Path:
    value = engine_config.get("YC_H3_CUSTOM_ASSETS")
    return engine_config.cwd / Path(value).expanduser() if value else engine_config.data_dir / "custom_assets.json"


def perf_dir(engine_config: EngineConfig) -> Path:
    value = engine_config.get("YC_H3_PERF_DIR")
    return engine_config.cwd / Path(value).expanduser() if value else engine_config.data_dir / "perf"


def is_loopback_host(host: Optional[str]) -> bool:
    cleaned = (host or "").strip()
    if cleaned in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_loopback_host(host: Optional[str]) -> str:
    cleaned = (host or "").strip()
    if is_loopback_host(cleaned):
        return cleaned
    raise ValueError(
        f"拒绝非 loopback 地址：{cleaned!r}。"
        "本地调度与隧道仅允许 loopback 地址（127.0.0.1 / ::1 / localhost）。"
    )


def colab_bin(engine_config: EngineConfig) -> str:
    value = engine_config.get("COLAB_CLI")
    if value:
        return str(engine_config.cwd / Path(value).expanduser())
    return find_bin("colab", "~/.local/bin/colab") or os.path.expanduser("~/.local/bin/colab")


def remote_version() -> Optional[str]:
    try:
        return remote_source_version(REMOTE_DIR)
    except OSError:
        return None


def daemon_version() -> Optional[str]:
    """Deterministic SHA-256 over local daemon and remote runtime sources."""
    try:
        digest = hashlib.sha256()
        for scope, directory in (("local", LOCAL_DIR), ("remote", REMOTE_DIR)):
            for relative in remote_source_files(directory):
                digest.update(f"{scope}/{relative.as_posix()}".encode("utf-8") + b"\0")
                digest.update(hashlib.sha256((directory / relative).read_bytes()).digest())
        return digest.hexdigest()
    except OSError:
        return None


def is_managed_session_name(name: Optional[str]) -> bool:
    return isinstance(name, str) and bool(_MANAGED_SESSION_RE.fullmatch(name.strip()))


def normalize_session_name(value: Optional[str]) -> str:
    """Normalize a session name to carry the ``yh3-`` prefix."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("session_name 不能为空")
    if raw.startswith(SESSION_PREFIX):
        if not _MANAGED_SESSION_RE.fullmatch(raw):
            raise ValueError("session_name 只能包含字母、数字、- 和 _，且总长不超过 40")
        return raw
    if not _RAW_SESSION_RE.fullmatch(raw):
        raise ValueError("session_name 只能包含字母、数字、- 和 _")
    candidate = SESSION_PREFIX + raw
    if not _MANAGED_SESSION_RE.fullmatch(candidate):
        raise ValueError("session_name 加上 yh3- 前缀后超过长度限制（40）")
    return candidate


def require_managed_session_name(value: Optional[str]) -> str:
    """Strictly require an existing ``yh3-`` prefix (rejects unmanaged/foreign names)."""
    raw = str(value or "").strip()
    if not is_managed_session_name(raw):
        raise ValueError(f"拒绝管理非 yh3- 前缀的 Colab 会话：{raw!r}")
    return raw


def gpu_candidates(value) -> tuple[str, ...]:
    if isinstance(value, str):
        if value.strip().upper() == "AUTO":
            return DEFAULT_GPU_FALLBACK
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("gpu 必须是卡型、AUTO 或卡型列表")
    cards = tuple(str(card).strip().upper() for card in value)
    if any(card not in GPU_CHOICES for card in cards):
        raise ValueError("gpu 必须是 " + "/".join(GPU_CHOICES))
    return tuple(dict.fromkeys(cards))


def default_settings(data_dir: Path) -> Dict[str, Any]:
    return {
        "session_name": "yh3-default",
        "gpu": "AUTO",
        "idle_minutes": 120,
        "max_runtime_minutes": 360,
        "output_dir": str(data_dir / "outputs"),
        "managed_sessions": {},
        "pending_stop_billing": {},
    }


class Settings:
    def __init__(self, path: Optional[str | os.PathLike[str]] = None, *,
                 engine_config: EngineConfig | None = None):
        self.engine_config = engine_config or load_config(data_dir=Path(path).parent if path else None)
        self.path = Path(path).expanduser() if path is not None else self.engine_config.data_dir / "settings.json"
        self.lock = threading.RLock()
        self.data = default_settings(self.engine_config.data_dir)
        self.reload()

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @contextlib.contextmanager
    def _file_lock(self, timeout: float = 15.0) -> Iterator[None]:
        with self.lock:
            with file_lock(self._lock_path, timeout=timeout, timeout_message="获取 Colab 设置文件锁超时"):
                yield

    def _load_locked(self, defaults: Optional[Dict[str, Any]] = None) -> bool:
        """Read and validate settings.json under ``_file_lock``.

        Returns ``False`` ONLY when ``settings.json`` genuinely does not exist
        (``FileNotFoundError``). Any read permission/IO failure or JSON/schema
        corruption raises immediately and NEVER overwrites the file with defaults.
        """
        base_defaults = defaults if defaults is not None else default_settings(self.engine_config.data_dir)
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        try:
            loaded = json.loads(raw_text)
        except ValueError as exc:
            raise ValueError(f"配置文件 JSON 损坏，拒绝覆盖默认值：{self.path} ({exc})") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件顶层必须是 JSON 对象，拒绝覆盖默认值：{self.path}")
        merged = dict(base_defaults)
        for k in base_defaults:
            if k in loaded:
                merged[k] = loaded[k]
        self.data = self._validate(merged)
        return True

    def reload(self) -> Dict[str, Any]:
        with self.lock:
            existed = self._load_locked()
            if not existed:
                self.data = self._validate(default_settings(self.engine_config.data_dir))
            return json.loads(json.dumps(self.data))

    def get(self, key: str) -> Any:
        with self.lock:
            val = self.data[key]
            if isinstance(val, dict):
                return json.loads(json.dumps(val))
            return val

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    @property
    def output_dir(self) -> Path:
        return Path(self.get("output_dir"))

    @staticmethod
    def _validate(d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d)
        out["session_name"] = normalize_session_name(out.get("session_name") or "yh3-default")
        gpu = out.get("gpu") or "AUTO"
        out["gpu"] = "AUTO" if isinstance(gpu, str) and gpu.strip().upper() == "AUTO" else ",".join(gpu_candidates(gpu))

        try:
            idle = int(float(out.get("idle_minutes", 120)))
        except (TypeError, ValueError):
            raise ValueError("idle_minutes 必须是数字")
        out["idle_minutes"] = max(0, min(idle, 24 * 60))

        try:
            max_rt = int(float(out.get("max_runtime_minutes", 360)))
        except (TypeError, ValueError):
            raise ValueError("max_runtime_minutes 必须是正整数")
        if max_rt < 1 or max_rt > 24 * 60:
            raise ValueError("max_runtime_minutes 须介于 1 和 1440 之间")
        out["max_runtime_minutes"] = max_rt

        od = str(out.get("output_dir") or "").strip()
        if not od:
            raise ValueError("output_dir 不能为空")
        out["output_dir"] = str(Path(os.path.expanduser(od)).resolve())

        managed = out.get("managed_sessions")
        if not isinstance(managed, dict):
            managed = {}
        out["managed_sessions"] = {
            name: rec for name, rec in managed.items()
            if is_managed_session_name(name) and isinstance(rec, dict)
        }
        settlements = out.get("pending_stop_billing")
        if not isinstance(settlements, dict):
            raise ValueError("Colab 停机台账待结算记录不完整")
        return out

    def _write_atomic_unlocked(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, payload, indent=2)

    def update(self, patch: Dict[str, Any], *, strict: bool = True) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("body 必须是 JSON 对象")
        defaults = default_settings(self.engine_config.data_dir)
        with self._file_lock():
            self._load_locked(defaults)
            merged = json.loads(json.dumps(self.data))
            for k, v in patch.items():
                if k in defaults:
                    merged[k] = v
            merged = self._validate(merged)
            try:
                self._write_atomic_unlocked(merged)
            except OSError:
                if strict:
                    raise
                return self.snapshot()
            self.data = merged
            return self.snapshot()

    def mutate(self, fn: Callable[[Dict[str, Any]], None], *, strict: bool = True) -> Dict[str, Any]:
        """Atomically read-modify-write settings under exclusive flock; never mutates memory if disk write fails."""
        defaults = default_settings(self.engine_config.data_dir)
        with self._file_lock():
            self._load_locked(defaults)
            working = json.loads(json.dumps(self.data))
            fn(working)
            validated = self._validate(working)
            try:
                self._write_atomic_unlocked(validated)
            except OSError:
                if strict:
                    raise
                return self.snapshot()
            self.data = validated
            return self.snapshot()
