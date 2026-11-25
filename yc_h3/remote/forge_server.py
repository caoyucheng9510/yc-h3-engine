"""yc-h3-engine pure ComfyUI remote orchestrator — runs on a managed Colab or Chenyu VM.

Adapted from WorldForge (MIT License).
Key invariants:
- Loopback by default; explicit non-loopback binding requires a strong Bearer token.
- Pure ComfyUI engine only (no Lyra, Diffusers, Qwen-Image, Breeze-TTS, or personal Drive OAuth).
- Single-source-of-truth atomic idempotency via ``jobs/<job_id>/job.json`` (no separate request_index.json).
- Persistence failure (OSError) aborts before enqueueing and returns HTTP 500.
- Restart recovery rebuilds client_request_id deduplication and classifies interrupted vs done jobs.
- Writes server PID to ``<remote_root>/run/server.pid`` and worker PGIDs to ``<remote_root>/run/worker_pgids/<pid>.pgid``.
"""
from __future__ import annotations

import asyncio
import atexit
import hmac
import importlib.metadata
import ipaddress
import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

if __package__:
    from . import _util, comfy_assets, job_perf, preflight, presets, setup_env
    from .comfy_worker import _process_vram
    from .workflows import registry as workflows
else:
    import _util
    import comfy_assets
    import job_perf
    import preflight
    import presets
    import setup_env
    from comfy_worker import _process_vram
    from workflows import registry as workflows

APP_NAME = "yc-h3-engine"
VERSION = setup_env.remote_source_version(Path(__file__).resolve().parent)
INSTANCE_ID = uuid.uuid4().hex
SESSION = os.environ.get("YC_H3_SESSION") or None


def validate_loopback_host(host: str) -> str:
    """Reject any non-loopback bind address (Decision 1 = A)."""
    cleaned = (host or "").strip()
    if cleaned in ("127.0.0.1", "localhost", "::1"):
        return cleaned
    try:
        if ipaddress.ip_address(cleaned).is_loopback:
            return cleaned
    except ValueError:
        pass
    raise ValueError(
        f"拒绝绑定非 loopback 地址：{cleaned!r}。"
        "算力端仅允许监听本机 loopback（127.0.0.1 / ::1 / localhost）。"
    )


def _visible_token(token: str) -> bool:
    return bool(token) and token.isascii() and all(33 <= ord(char) <= 126 for char in token)


def resolve_auth_token() -> str:
    token = os.environ.get("YC_H3_REMOTE_AUTH_TOKEN") or ""
    path = os.environ.get("YC_H3_REMOTE_AUTH_TOKEN_FILE")
    if not token and path:
        try:
            with open(path, "rb") as stream:
                line = stream.readline()
                token = (line[:-1].removesuffix(b"\r") if line.endswith(b"\n") else line).decode("ascii")
        except (OSError, UnicodeError):
            raise ValueError("无法读取有效的远端认证令牌文件") from None
    if (token or path) and not _visible_token(token):
        raise ValueError("远端认证令牌必须是无空白的可见 ASCII")
    return token


_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"'}]+")
_SECRET_ENV_NAME = re.compile(r"(?:^|_)(?:API_?KEY|TOKEN|PASSWORD|SECRET)(?:$|_)")
_ENV_SECRETS = tuple(
    value.strip() for name, value in os.environ.items()
    if _SECRET_ENV_NAME.search(name) and len(value.strip()) >= 6
    and not name.endswith(("_FILE", "_PATH", "_URL", "_DIR", "_ENABLED"))
)


def _redact(value):
    try:
        token = resolve_auth_token()
    except ValueError:
        token = ""
    secrets = sorted(set(_ENV_SECRETS) | ({token} if token else set()), key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, secrets))) if secrets else None

    def clean(item):
        if isinstance(item, str):
            item = _BEARER.sub("[REDACTED]", item)
            if pattern:
                return "[REDACTED]".join(pattern.sub("[REDACTED]", part) for part in item.split("[REDACTED]"))
            return item
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        return item

    return clean(value)


def validate_bind_host(host: str, *, allow_non_loopback: bool = False, auth_token: str = "") -> str:
    if not allow_non_loopback:
        return validate_loopback_host(host)
    if not _visible_token(auth_token) or len(auth_token) < 32:
        raise ValueError("开放远端监听须提供至少 32 字符的可见 ASCII 认证令牌")
    cleaned = (host or "").strip()
    if cleaned != "localhost":
        try:
            ipaddress.ip_address(cleaned)
        except ValueError:
            raise ValueError("远端监听地址必须是合法 IP 或 localhost") from None
    return cleaned


ROOT = _util.remote_root()
os.environ.setdefault("YC_H3_REMOTE_ROOT", ROOT)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS = f"{ROOT}/inputs"
PLATFORM_INSTANCE_ID = os.environ.get("YC_H3_PLATFORM_INSTANCE_ID") or ""
if PLATFORM_INSTANCE_ID and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", PLATFORM_INSTANCE_ID):
    raise ValueError("远端平台实例标识无效")
_namespace = f"/{PLATFORM_INSTANCE_ID}" if PLATFORM_INSTANCE_ID else ""
OUTPUTS = f"{ROOT}/outputs{_namespace}"
JOBS_DIR = f"{ROOT}/jobs{_namespace}"
RUN_DIR = Path(ROOT) / "run"
SERVER_PID_FILE = RUN_DIR / "server.pid"
WORKER_PGIDS = RUN_DIR / "worker_pgids"
SYS_PY = sys.executable or ("/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3")

for _d in (INPUTS, OUTPUTS, JOBS_DIR, RUN_DIR, WORKER_PGIDS):
    os.makedirs(_d, exist_ok=True)

H3_PY = str(setup_env.H3_PY)
COMFY_WORKER = f"{APP_DIR}/comfy_worker.py"
h3 = workflows.h3
MARK = b"@@FORGE@@ "

# ---------------------------------------------------------------------------
# Pure ComfyUI Model Registry
# ---------------------------------------------------------------------------
DEFAULT_MODELS: List[Dict[str, Any]] = presets.DEFAULT_MODELS
BUILTIN_MODELS: List[Dict[str, Any]] = DEFAULT_MODELS

_reg_lock = threading.RLock()
_models: Dict[str, Dict[str, Any]] = {}


def resolve_model_id(mid: Optional[str], fallback_task: Optional[str] = None) -> str:
    if not mid and fallback_task in ("t2v", "i2v", "r2v"):
        return "minimax-h3"
    return (mid or "").strip()


def _custom_assets() -> Dict[str, Any]:
    path = Path(ROOT) / "private" / "custom_assets.json"
    return comfy_assets.read_custom_assets(path)


def _load_registry() -> None:
    with _reg_lock:
        _models.clear()
        for m in DEFAULT_MODELS:
            entry = dict(m, status="available")
            entry.setdefault("size_gb", None)
            _models[m["id"]] = entry
    threading.Thread(target=_refresh_all_status, daemon=True).start()


def _refresh_status(mid: str) -> None:
    m = _models.get(mid)
    if not m or m.get("status") in ("downloading", "loading"):
        return
    w = WORKERS.get(COMFY_SLOT)
    params = {"workflow": w.active_workflow} if w and w.active_model_id == mid and w.active_workflow else {}
    status = comfy_workflow_status(h3.probe_hardware(), workflows.for_model(m, params), mid)
    m.update(
        status="loaded" if status["vram_loaded"] else ("downloaded" if status["weights_downloaded"] else "available"),
        storage_tier="gpu" if status["vram_loaded"] else ("nvme" if status["weights_downloaded"] else "cloud"),
        size_gb=round(status["size_bytes"] / 1e9, 2),
    )


def _refresh_all_status() -> None:
    for mid in list(_models):
        try:
            _refresh_status(mid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------
def write_server_pid(pid_file: Optional[Path] = None) -> Path:
    target = Path(pid_file) if pid_file is not None else SERVER_PID_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    _util.atomic_write_text(target, f"{os.getpid()}\n")
    return target


def remove_server_pid(pid_file: Optional[Path] = None) -> None:
    target = Path(pid_file) if pid_file is not None else SERVER_PID_FILE
    try:
        if target.is_file() and target.read_text(encoding="utf-8").strip() == str(os.getpid()):
            target.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Jobs — Single-source-of-truth persistence & deduplication via job.json
# ---------------------------------------------------------------------------
_jobs: Dict[str, Dict[str, Any]] = {}
_request_index: Dict[str, str] = {}
_jobs_lock = threading.RLock()
_active_io_jobs: set[str] = set()
_gpu_queue: deque = deque()
_gpu_cv = threading.Condition()
_gpu_owner = None


def _now() -> float:
    return time.time()


def _persist(job: Dict[str, Any]) -> None:
    """Atomically persist job.json with fsync. Never swallows OSError."""
    job_dir = Path(JOBS_DIR) / job["id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "job.json"
    payload = {k: v for k, v in job.items() if k != "_perf"}
    raw = json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    _util.atomic_write_text(path, raw)




def normalize_setup_params(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = params if isinstance(params, dict) else {}
    comp = str(raw.get("component") or "comfy").strip()
    if comp not in ("comfy", "minimax"):
        raise ValueError(f"setup 仅支持 comfy/minimax，收到：{comp}")
    profile = str(raw.get("profile") or setup_env.COMFY_MAIN).strip()
    setup_env.comfy_runtime(profile)
    revision = str(raw.get("revision") or setup_env.COMFY_REVISION).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("ComfyUI revision 必须为完整 Git SHA")
    if revision != setup_env.COMFY_REVISION:
        raise ValueError("comfy-main 必须使用固定的 ComfyUI revision")
    raw_plugins = raw.get("plugins") or []
    if isinstance(raw_plugins, str):
        raw_plugins = [p.strip() for p in raw_plugins.split(",") if p.strip()]
    if not isinstance(raw_plugins, (list, tuple)) or any(not isinstance(p, str) for p in raw_plugins):
        raise ValueError("plugins 必须是字符串列表")
    plugins = sorted({p.strip() for p in raw_plugins if p.strip()})
    for p in plugins:
        if p not in setup_env.CUSTOM_NODES:
            raise ValueError(f"未授权的 ComfyUI 插件：{p}")
    return {
        "component": comp,
        "profile": profile,
        "revision": revision,
        "plugins": plugins,
    }


def compute_setup_approval_digest(
    *,
    instance_id: str,
    session: Optional[str],
    remote_root: str,
    params: Dict[str, Any],
) -> str:
    """Deterministic approval digest binding target instance, session, root, and setup spec."""
    payload = {
        "kind": "remote_setup",
        "instance_id": instance_id,
        "session": session,
        "remote_root": remote_root,
        "params": params,
    }
    return _util.sha256_bytes(comfy_assets.canonical_json(payload))


def new_job(
    model_id: Optional[str],
    task: str,
    params: Dict[str, Any],
    *,
    jid: Optional[str] = None,
    workload_key=None,
    client_request_id: Optional[str] = None,
    request_digest: Optional[str] = None,
) -> Dict[str, Any]:
    jid = jid or (time.strftime("job_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    job: Dict[str, Any] = {
        "id": jid,
        "model_id": model_id,
        "task": task,
        "params": params,
        "status": "queued",
        "enqueue_state": "queued",
        "recovery_state": None,
        "client_request_id": client_request_id,
        "request_digest": request_digest,
        "instance_id": INSTANCE_ID,
        "cancel_requested": False,
        "progress": 0.0,
        "stage": "排队中",
        "created": _now(),
        "started": None,
        "finished": None,
        "outputs": [],
        "error": None,
        "eta_s": None,
    }
    if workload_key is not None:
        job["workload_key"] = workload_key
    with _jobs_lock:
        _jobs[jid] = job
        if client_request_id:
            _request_index[client_request_id] = jid
        try:
            _persist(job)
        except Exception:
            _jobs.pop(jid, None)
            if client_request_id and _request_index.get(client_request_id) == jid:
                _request_index.pop(client_request_id, None)
            raise
    return job


def job_log(jid: str, text: str) -> None:
    if not jid:
        return
    text = _redact(text)
    try:
        job_dir = Path(JOBS_DIR) / jid
        job_dir.mkdir(parents=True, exist_ok=True)
        with (job_dir / "log.txt").open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def update_job(jid: str, **kw) -> None:
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job or job["status"] == "cancelled":
            return
        job.update(kw)
        if "progress" in kw and job.get("started") and job["status"] == "running":
            p = float(job["progress"] or 0)
            el = _now() - job["started"]
            job["eta_s"] = int(el / p * (1 - p)) if p > 0.03 else None
        _persist(job)


def _recover_job_record(job: Dict[str, Any]) -> bool:
    """Classify persisted job on startup; returns True if record was mutated."""
    mutated = False
    status = job.get("status")
    enqueue_state = job.get("enqueue_state")
    if status == "done":
        return mutated
    if enqueue_state == "enqueue_failed" and status == "queued":
        job["status"] = "error"
        job["recovery_state"] = "interrupted_before_enqueue"
        job["error"] = "服务重启，任务在入队前中断"
        job["stage"] = "入队前中断"
        mutated = True
    elif status in ("queued", "running"):
        job["status"] = "error"
        job["recovery_state"] = "interrupted_unknown"
        job["error"] = "服务重启，任务执行结果不明（不自动重跑）"
        job["stage"] = "重启中断（结果不明）"
        mutated = True
    return mutated


def _load_old_jobs() -> None:
    """Rebuild _jobs and client_request_id -> job_id strictly from jobs/*/job.json."""
    jobs_root = Path(JOBS_DIR)
    if not jobs_root.is_dir():
        return
    with _jobs_lock:
        _request_index.clear()
        for entry in sorted(jobs_root.iterdir()):
            if not entry.is_dir():
                continue
            p = entry / "job.json"
            if not p.is_file():
                continue
            try:
                job = json.loads(p.read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                continue
            jid = job["id"]
            mutated = _recover_job_record(job)
            if mutated:
                try:
                    _persist(job)
                except OSError:
                    pass
            _jobs[jid] = job
            cid = job.get("client_request_id")
            if isinstance(cid, str) and cid:
                _request_index[cid] = jid


# ---------------------------------------------------------------------------
# Workers: long-lived pure ComfyUI worker process
# ---------------------------------------------------------------------------
def _pgid_path(pid: int) -> Path:
    return WORKER_PGIDS / f"{pid}.pgid"


def _register_group(proc) -> None:
    WORKER_PGIDS.mkdir(parents=True, exist_ok=True)
    _pgid_path(proc.pid).write_text(f"{proc.pid}\n", encoding="utf-8")


def _group_active(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    if sys.platform == "linux":
        result = subprocess.run(["ps", "-eo", "pgid=,stat="], capture_output=True, encoding="utf-8", errors="replace", timeout=2)
        if result.returncode == 0:
            return any(
                group == str(pgid) and not state.startswith("Z")
                for line in result.stdout.splitlines()
                if len(line.split()) == 2
                for group, state in [line.split()]
            )
    return True


def _terminate_group(proc) -> None:
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()
            if not _group_active(proc.pid):
                proc.wait(timeout=1)
                _pgid_path(proc.pid).unlink(missing_ok=True)
                return
            time.sleep(0.1)
    raise RuntimeError(f"进程组 {proc.pid} 未能回收")


def _check_job_cancelled(jid: Optional[str]) -> None:
    if not jid:
        return
    with _jobs_lock:
        job = _jobs.get(jid, {})
        if job.get("status") == "cancelled" or job.get("cancel_requested"):
            raise RuntimeError("任务已取消")


class Worker:
    def __init__(self, model: Dict[str, Any]):
        self.model = model
        self.comfy = True
        self.active_model_id: Optional[str] = None
        self.active_workflow: Optional[str] = None
        self.active_profile: Optional[str] = None
        self.worker_python = H3_PY
        self.reader = None
        self.comfy_pid = None
        self.comfy_baseline = None
        self.mid = model["id"]
        self.proc: Optional[subprocess.Popen] = None
        self.state = "dead"
        self.events: "deque[Dict[str, Any]]" = deque()
        self.ev_cv = threading.Condition()
        self.current_job: Optional[str] = None
        self.vram_mb = 0
        self.lock = threading.RLock()
        self.log_tail: deque = deque(maxlen=400)

    def _cmdline(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        env["YC_H3_REMOTE_ROOT"] = ROOT
        env.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
        return [self.worker_python, "-u", COMFY_WORKER], APP_DIR, env

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        cmd, cwd, env = self._cmdline()
        self.state = "loading"
        with self.ev_cv:
            self.events.clear()
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
        _register_group(self.proc)
        self.reader = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
        self.reader.start()

    def _reader(self, proc) -> None:
        buf = b""
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                line, buf = buf[: m.start()], buf[m.end() :]
                self._handle_line(line)
        if buf:
            self._handle_line(buf)
        proc.stdout.close()
        self.state = "dead"
        with self.ev_cv:
            self.events.append({"event": "exit", "code": proc.poll()})
            self.ev_cv.notify_all()

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        k = line.find(MARK)
        if k > 0:
            self._handle_line(line[:k])
            line = line[k:]
        if line.startswith(MARK):
            try:
                ev = json.loads(line[len(MARK) :].decode("utf-8", "replace"))
            except Exception:
                return
            et = ev.get("event")
            if et == "log":
                self._log(ev.get("msg", ""))
                return
            if et != "ready" and ev.get("job_id") != self.current_job:
                return
            if et in ("phase", "observations", "comfy_process"):
                if et == "comfy_process":
                    self.comfy_pid = ev.get("comfy_pid")
                with _jobs_lock:
                    job = _jobs.get(self.current_job)
                    if job is not None:
                        perf = job.setdefault("_perf", {})
                        if et == "phase":
                            phases = perf.setdefault("phases", {})
                            name = ev["name"]
                            if ev.get("finished") and name in phases:
                                phases[name]["end"] = ev["at"]
                            else:
                                phases[name] = {"start": ev["at"]}
                        elif et == "observations":
                            perf["observations"] = ev["observations"]
                        elif "cold_start" in ev:
                            perf["cold_start"] = ev["cold_start"]
                return
            if et == "progress":
                if self.current_job:
                    kw = {"progress": float(ev.get("p", 0))}
                    if ev.get("stage"):
                        kw["stage"] = ev["stage"]
                    update_job(self.current_job, **kw)
                return
            if et in ("done", "prepared"):
                self.active_workflow = ev.get("workflow")
                self.active_profile = ev.get("profile")
                self.vram_mb = ev.get("vram_mb") or 0
                self.comfy_pid = ev.get("comfy_pid")
                self.comfy_baseline = ev.get("baseline_mb")
                self.state = "on_gpu" if self.vram_mb > (self.comfy_baseline or 0) + 256 else "prepared"
            if et == "ready":
                self.state = "prepared"
                self.vram_mb = ev.get("vram_mb", self.vram_mb)
            elif et == "offloaded":
                self.state = "offloaded"
                self.comfy_pid = ev.get("comfy_pid")
                self.vram_mb = ev.get("vram_mb", 0)
            with self.ev_cv:
                self.events.append(ev)
                self.ev_cv.notify_all()
        else:
            self._log(line.decode("utf-8", "replace"))

    def _log(self, text: str) -> None:
        self.log_tail.append(_redact(text))
        if self.current_job:
            job_log(self.current_job, text)

    def send(self, obj: Dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        self.proc.stdin.flush()

    def wait_event(self, kinds, timeout: float = 3600 * 6) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        job_id = self.current_job
        while True:
            job = _jobs.get(job_id, {})
            cancelled = job.get("status") == "cancelled" or job.get("cancel_requested")
            if cancelled or time.monotonic() >= deadline:
                self.kill()
                return {"event": "error", "msg": "任务已取消" if cancelled else "worker 超时"}
            with self.ev_cv:
                while self.events:
                    ev = self.events.popleft()
                    et = ev.get("event")
                    if et not in ("ready", "exit") and ev.get("job_id") != job_id:
                        continue
                    if et in kinds or et in ("error", "exit"):
                        break
                else:
                    self.ev_cv.wait(timeout=min(max(0, deadline - time.monotonic()), 0.5))
                    continue
            if ev.get("recycle") or et == "exit":
                self.kill()
            return ev

    def kill(self) -> None:
        with self.lock:
            if self.proc and (self.alive() or _pgid_path(self.proc.pid).exists()):
                _terminate_group(self.proc)
                if self.proc.stdin:
                    self.proc.stdin.close()
            if self.reader is not None:
                self.reader.join(timeout=3)
            self.state = "dead"
            self.vram_mb = 0
            self.comfy_pid = None
            self.active_model_id = self.active_workflow = self.active_profile = None


WORKERS: Dict[str, Worker] = {}
COMFY_SLOT = "__comfyui__"


def _comfy_model(model: Dict[str, Any]) -> Dict[str, Any]:
    return {key: model[key] for key in ("engine", "workflow") if key in model}


def comfy_workflow_status(profile, workflow, mid=None) -> Dict[str, Any]:
    assets = workflow.assets(profile, {})
    w = WORKERS.get(COMFY_SLOT)
    matches = bool(w and w.alive() and w.active_workflow == workflow.id and (mid is None or w.active_model_id == mid))
    observed = _process_vram(w.comfy_pid) if matches and w.comfy_pid and w.state == "on_gpu" else None
    return {
        "id": workflow.id,
        "model_id": workflow.model_id,
        "tasks": workflow.manifest["tasks"],
        "verification": workflow.manifest.get("verification", {}),
        "profile_id": workflow.profile_id,
        "comfyui_revision": workflow.revision,
        "output_kind": workflow.output_kind,
        "comfy_installed": setup_env.comfy_runtime_installed(workflow.profile_id, workflow.revision),
        "weights_downloaded": all(comfy_assets.weight_ready(item, setup_env.COMFY_MODELS_DIR) for item in assets.values()),
        "size_bytes": sum(item.get("size_bytes") or 0 for item in assets.values()),
        "comfy_running": matches and w.comfy_pid is not None,
        "vram_loaded": observed is not None and observed > (w.comfy_baseline or 0) + 256,
    }


def _ensure_comfy_runtime(jid: Optional[str], workflow, profile=None, params=None) -> None:
    """Verify ComfyUI runtime and required plugins are installed; NEVER silently install during gen/download."""
    _check_job_cancelled(jid)
    if not setup_env.comfy_runtime_installed(workflow.profile_id, workflow.revision):
        raise RuntimeError(
            f"runtime_or_plugins_not_ready: ComfyUI 运行时 {workflow.profile_id} ({workflow.revision}) 未安装，"
            "请先通过经审批的 setup 流程安装"
        )
    if profile is not None:
        required_plugins = workflow.plugins(profile, params or {})
        missing_plugins = [p for p in required_plugins if not setup_env.custom_node_installed(p, workflow.profile_id)]
        if missing_plugins:
            raise RuntimeError(
                "runtime_or_plugins_not_ready: 缺少 ComfyUI 插件 "
                + ", ".join(missing_plugins)
                + "，请先通过经审批的 setup 流程安装"
            )
    _check_job_cancelled(jid)


def _download_model_sync(mid: str, jid: Optional[str] = None) -> None:
    m = _models[mid]
    m["status"] = "downloading"
    if jid:
        update_job(jid, stage=f"下载 ComfyUI 模型权重 {mid}…")
    try:
        params = _jobs[jid]["params"] if jid else {}
        workflow = workflows.for_model(m, params)
        _ensure_comfy_runtime(jid, workflow)
        command = {"job_id": jid, "task": "download", "params": params, "model": _comfy_model(m)}
        python = str(setup_env.comfy_runtime(workflow.profile_id).python)
        rc = _stream_cmd(jid, [python, "-u", COMFY_WORKER, "--ensure", json.dumps(command)], timeout=3 * 3600, job_bound=True)
        if rc:
            raise RuntimeError(f"ComfyUI 下载失败（exit {rc}）")
    finally:
        m["status"] = "available"
    _refresh_status(mid)


def _bind_worker(mid: str, jid: Optional[str]) -> Worker:
    worker = WORKERS.get(COMFY_SLOT)
    if worker is None:
        worker = WORKERS[COMFY_SLOT] = Worker(_models[mid])
    worker.current_job = jid
    return worker


def _ensure_on_gpu(mid: str, jid: Optional[str]) -> Worker:
    mid = resolve_model_id(mid)
    if mid not in _models:
        raise RuntimeError(f"未知模型 {mid}")
    model = _models[mid]
    with _jobs_lock:
        _check_job_cancelled(jid)
        w = _bind_worker(mid, jid)
        if jid:
            perf = _jobs[jid].setdefault("_perf", {})
            perf["worker"] = w
    params = _jobs.get(jid, {}).get("params", {}) if jid else {}
    workflow = workflows.for_model(model, params)
    if jid:
        with _jobs_lock:
            _jobs[jid].setdefault("_perf", {})["profile"] = workflow.profile_id
    _ensure_comfy_runtime(jid, workflow, profile=h3.probe_hardware(), params=params)
    _check_job_cancelled(jid)

    if not w.alive():
        w.kill()
        model["status"] = "loading"
        if jid:
            update_job(jid, stage="启动 ComfyUI Worker")
        w.worker_python = str(setup_env.comfy_runtime(workflow.profile_id).python)
        with _jobs_lock:
            _check_job_cancelled(jid)
            w.start()
        ev = w.wait_event({"ready"}, timeout=30)
        if ev.get("event") != "ready":
            model["status"] = "error"
            raise RuntimeError(f"ComfyUI Worker 启动失败: {ev.get('msg') or ev}\n" + "\n".join(list(w.log_tail)[-30:]))
        model["status"] = "available"
    w.active_model_id = mid
    return w


def _claim_gpu_job():
    global _gpu_owner
    with _gpu_cv:
        while True:
            while _gpu_owner is not None or not _gpu_queue:
                _gpu_cv.wait()
            jid = _gpu_queue.popleft()
            with _jobs_lock:
                job = _jobs.get(jid)
                if not job or job["status"] != "queued" or job.get("cancel_requested"):
                    continue
                mid = resolve_model_id(job["model_id"], fallback_task=job["task"])
                update_job(jid, model_id=mid, status="running", started=_now(), stage="准备中", progress=0.0)
                _gpu_owner = ("job", jid)
                if mid in _models:
                    worker = _bind_worker(mid, jid)
                    job["_perf"] = {"worker": worker}
                return job


def _gpu_process_pid(jid: str):
    with _jobs_lock:
        worker = _jobs.get(jid, {}).get("_perf", {}).get("worker")
    if worker and worker.alive():
        return worker.comfy_pid
    return None


def _release_gpu(owner) -> None:
    global _gpu_owner
    with _gpu_cv:
        if _gpu_owner == owner:
            _gpu_owner = None
            _gpu_cv.notify_all()


def _publish_gpu_terminal(job, result, sampler=None) -> None:
    finished = _now()
    if job.get("cancel_requested"):
        result = {"status": "cancelled", "stage": "已取消"}
    perf_status = "cancelled_in_queue" if result["status"] == "cancelled" and not job["started"] else result["status"]
    perf_available = False
    try:
        job_perf.write_perf(Path(JOBS_DIR) / job["id"], job, perf_status, finished, sampler)
        perf_available = True
    except (OSError, ValueError) as error:
        job_log(job["id"], f"性能归档失败：{error}")
    update_job(
        job["id"],
        **result,
        finished=finished,
        eta_s=0,
        perf_available=perf_available,
        cancel_requested=result["status"] == "cancelled",
    )
    job.pop("_perf", None)


def _finish_gpu_job(job, result, sampler) -> None:
    jid = job["id"]
    cleaned = False
    while True:
        with _gpu_cv, _jobs_lock:
            teardown = job.get("cancel_requested") or result["status"] == "error"
        if teardown and not cleaned:
            for w in list(WORKERS.values()):
                if w.current_job == jid:
                    w.kill()
            cleaned = True
        sampler.stop()
        with _gpu_cv, _jobs_lock:
            if job.get("cancel_requested") and not cleaned:
                continue
            _publish_gpu_terminal(job, result, sampler)
            for w in list(WORKERS.values()):
                if w.current_job == jid:
                    w.current_job = None
            return


def _run_gpu_job(job) -> Dict[str, Any]:
    jid, mid = job["id"], job["model_id"]
    if mid not in _models:
        raise RuntimeError(f"未知模型 {mid}")
    if job["task"] == "load":
        w = _ensure_on_gpu(mid, jid)
        with _jobs_lock:
            _check_job_cancelled(jid)
            w.current_job = jid
            w.send(
                {
                    "cmd": "load",
                    "task": "load",
                    "job_id": jid,
                    "params": job["params"],
                    "model": _comfy_model(_models[mid]),
                }
            )
        event = w.wait_event({"prepared"}, timeout=4 * 3600)
        if event.get("event") != "prepared":
            raise RuntimeError(event.get("msg") or "ComfyUI 加载中断")
        _refresh_status(mid)
        return {"status": "done", "progress": 1.0, "stage": "模型预热完成，显存状态已更新"}

    out_dir = f"{OUTPUTS}/{jid}"
    os.makedirs(out_dir, exist_ok=True)
    params = dict(job.get("params") or {})
    m_info = _models[mid]
    w = _ensure_on_gpu(mid, jid)
    with _jobs_lock:
        _check_job_cancelled(jid)
        w.current_job = jid
        update_job(jid, stage="生成中")
        w.send(
            {
                "cmd": "run",
                "job_id": jid,
                "task": job["task"],
                "params": params,
                "out_dir": out_dir,
                "model": _comfy_model(m_info),
            }
        )
    ev = w.wait_event({"done"}, timeout=5 * 3600)
    _check_job_cancelled(jid)
    if ev.get("event") == "done":
        outputs = list(ev.get("outputs", []))
        for o in outputs:
            if isinstance(o, dict):
                fn = o.get("file") or o.get("name") or (os.path.basename(o["path"]) if o.get("path") else "")
                if fn:
                    o.setdefault("file", fn)
                    o.setdefault("name", fn)
                    o.setdefault("path", f"{out_dir}/{fn}")
        return {"status": "done", "progress": 1.0, "stage": "完成", "outputs": outputs}
    msg = ev.get("msg") or f"worker 退出 (code={ev.get('code')})"
    job_log(jid, "ERROR: " + msg)
    if ev.get("event") == "exit":
        _models[mid]["status"] = "downloaded"
    return {"status": "error", "error": msg, "stage": "失败"}


def _gpu_loop() -> None:
    while True:
        job = _claim_gpu_job()
        jid = job["id"]
        sampler = job_perf.Sampler(Path(JOBS_DIR) / jid, lambda: _gpu_process_pid(jid))
        try:
            sampler.start()
            try:
                result = _run_gpu_job(job)
            except Exception as error:
                job_log(jid, traceback.format_exc())
                result = {"status": "error", "error": str(error), "stage": "失败"}
            while True:
                try:
                    _finish_gpu_job(job, result, sampler)
                    break
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    job_log(jid, traceback.format_exc())
                    update_job(jid, stage="等待 GPU 回收和性能归档")
                    time.sleep(2)
        finally:
            _release_gpu(("job", jid))


def _run_io_job(jid: str) -> None:
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job or job["status"] != "queued":
            return
        update_job(jid, status="running", started=_now(), stage="进行中")
        _active_io_jobs.add(jid)
    try:
        if job["task"] == "download":
            mid = resolve_model_id(job["model_id"])
            job["model_id"] = mid
            _download_model_sync(mid, jid)
        elif job["task"] == "setup":
            norm = job["params"]
            if os.environ.get("YC_H3_CHENYU_TORCH") == "1":
                try:
                    torch_version = importlib.metadata.version("torch")
                except importlib.metadata.PackageNotFoundError:
                    torch_version = "0.0"
                version = re.match(r"^(\d+)\.(\d+)", torch_version)
                if not version or tuple(map(int, version.groups())) < (2, 8):
                    rc = _stream_cmd(jid, [
                        SYS_PY, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "torch", "torchvision",
                        "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu128",
                    ], timeout=2700)
                    if rc:
                        raise RuntimeError(f"晨羽 PyTorch 安装失败 (exit {rc})")
            cmd = [
                SYS_PY,
                "-u",
                f"{APP_DIR}/setup_env.py",
                "--component",
                norm["component"],
                "--profile",
                norm["profile"],
                "--revision",
                norm["revision"],
            ]
            if norm.get("plugins"):
                cmd += ["--plugins", ",".join(norm["plugins"])]
            rc = _stream_cmd(jid, cmd, timeout=2700)
            if rc != 0:
                raise RuntimeError(f"环境安装失败 (exit {rc})")
        update_job(jid, status="done", progress=1.0, stage="完成", finished=_now(), eta_s=0)
    except Exception as e:
        if job.get("model_id") in _models:
            _refresh_status(job["model_id"])
        update_job(jid, status="error", error=str(e), stage="失败", finished=_now())
        job_log(jid, traceback.format_exc())
    finally:
        with _jobs_lock:
            _active_io_jobs.discard(jid)


_io_procs: Dict[str, subprocess.Popen] = {}
_io_cancels: Dict[str, Any] = {}


def _stream_cmd(jid: Optional[str], cmd: List[str], timeout=6 * 3600, job_bound=False) -> int:
    if jid:
        job_log(jid, "$ " + " ".join(cmd))
    reaped = threading.Lock()
    timed_out = threading.Event()
    stopped = False

    def stop():
        nonlocal stopped
        with reaped:
            if not stopped:
                _terminate_group(p)
                stopped = True

    def expire():
        timed_out.set()
        stop()

    with _jobs_lock:
        _check_job_cancelled(jid)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True)
        _register_group(p)
        if jid:
            _io_procs[jid] = p
            _io_cancels[jid] = stop

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        rc = _read_io(jid, p, job_bound)
        return 124 if timed_out.is_set() else rc
    finally:
        timer.cancel()
        stop()
        p.stdout.close()
        if jid:
            with _jobs_lock:
                _io_procs.pop(jid, None)
                _io_cancels.pop(jid, None)


def _read_io(jid: Optional[str], p, job_bound: bool) -> int:
    buf = b""
    fd = p.stdout.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        while True:
            m = re.search(rb"[\r\n]", buf)
            if not m:
                break
            line, buf = buf[: m.start()], buf[m.end() :]
            s = line.decode("utf-8", "replace")
            if not s.strip():
                continue
            k = s.find("@@FORGE@@ ")
            if k > 0:
                if jid:
                    job_log(jid, s[:k])
                s = s[k:]
            if s.startswith("@@FORGE@@ "):
                try:
                    ev = json.loads(s[10:])
                    if job_bound and ev.get("job_id") != jid:
                        continue
                    if ev.get("event") == "progress" and jid:
                        update_job(jid, progress=float(ev.get("p", 0)), stage=ev.get("stage") or _jobs[jid]["stage"])
                    elif ev.get("event") == "log" and jid:
                        job_log(jid, ev.get("msg", ""))
                except Exception:
                    pass
                continue
            if jid:
                job_log(jid, s)
                pm = re.search(r"(\d{1,3})%\|", s)
                if pm and _jobs[jid]["task"] == "download":
                    update_job(jid, progress=min(0.99, int(pm.group(1)) / 100))
    return p.wait()


def submit(job: Dict[str, Any]) -> None:
    if job["task"] in ("download", "setup"):
        threading.Thread(target=_run_io_job, args=(job["id"],), daemon=True).start()
    else:
        with _gpu_cv, _jobs_lock:
            if job["status"] == "queued":
                _gpu_queue.append(job["id"])
                _gpu_cv.notify_all()


# ---------------------------------------------------------------------------
# Hardware telemetry
# ---------------------------------------------------------------------------


def _parse_smi_num(val: str):
    v = (val or "").strip()
    if not v or "N/A" in v or "Not Supported" in v:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
app = FastAPI(title="yc-h3-engine Remote", version=VERSION)


async def require_bearer(request: Request, call_next):
    try:
        token = resolve_auth_token()
    except ValueError:
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    if token:
        headers = request.headers.getlist("Authorization")
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("latin-1"), b"Bearer " + token.encode("ascii")):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


if hasattr(app, "middleware"):
    app.middleware("http")(require_bearer)
elif os.environ.get("YC_H3_REMOTE_AUTH_TOKEN") or os.environ.get("YC_H3_REMOTE_AUTH_TOKEN_FILE"):
    raise RuntimeError("当前 HTTP 服务无法启用认证中间件")


@app.exception_handler(Exception)
async def _err(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": _redact(f"{exc.__class__.__name__}: {exc}")})


def _directory_file_bytes(directory: Path) -> int:
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return 0
    except OSError:
        return 0
    total = 0
    for root, _, files in os.walk(directory, followlinks=False):
        for name in files:
            try:
                info = (Path(root) / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": APP_NAME,
        "remote_root": ROOT,
        "session": SESSION,
        "instance_id": INSTANCE_ID,
        "platform_instance_id": PLATFORM_INSTANCE_ID or None,
        "version": VERSION,
        "workflows": sorted(workflows.WORKFLOWS.keys()),
        "gpu": h3.probe_hardware().gpu_name,
        "models_dir_bytes": _directory_file_bytes(setup_env.COMFY_MODELS_DIR),
        "custom_assets_sha256": _util.sha256_bytes(comfy_assets.canonical_json(_custom_assets())),
        "capabilities": ["comfy_workflows", "preflight", "perf"],
    }


@app.post("/api/custom-assets")
async def api_custom_assets(req: Request):
    if not resolve_auth_token():
        raise HTTPException(404, "接口未启用")
    try:
        assets = comfy_assets.normalize_custom_assets(await _request_object(req))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    path = Path(ROOT) / "private" / "custom_assets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = comfy_assets.canonical_json(assets)
    _util.atomic_write_bytes(path, raw)
    return {"sha256": _util.sha256_bytes(raw)}


def _clean_image_files() -> tuple[int, list[str]]:
    # Only cache trees explicitly owned by this engine. Keep every job, input and output,
    # including files whose reference status is unknown.
    root = Path(ROOT)
    cache = root / "cache"
    if cache.resolve() != cache.absolute():
        return 0, []
    models = setup_env.COMFY_MODELS_DIR.resolve()
    cleaned, freed_bytes = [], 0
    for name in ("pip", "uv", "huggingface"):
        directory = cache / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        if models.is_relative_to(directory) or directory.is_relative_to(models):
            continue
        size = _directory_file_bytes(directory)
        shutil.rmtree(directory)
        freed_bytes += size
        cleaned.append(str(directory))
    return freed_bytes, cleaned


@app.get("/api/maintenance/job-inventory")
def api_job_inventory():
    """Return the complete on-disk job inventory or fail closed if it cannot be verified."""
    if not resolve_auth_token():
        raise HTTPException(404, "接口未启用")
    try:
        with _jobs_lock:
            jobs_root = Path(JOBS_DIR)
            if jobs_root.resolve() != jobs_root.absolute():
                raise ValueError
            inventory = {}
            for entry in jobs_root.iterdir():
                job_file = entry / "job.json"
                if (entry.is_symlink() or not entry.is_dir() or job_file.is_symlink()
                        or not job_file.is_file()):
                    raise ValueError
                job = json.loads(job_file.read_bytes())
                if (not isinstance(job, dict) or job.get("id") != entry.name
                        or job.get("status") not in {"submitting", "queued", "running", "done", "error", "cancelled"}):
                    raise ValueError
                if entry.name in _jobs and _jobs[entry.name]["status"] != job["status"]:
                    raise ValueError
                inventory[entry.name] = {
                    key: job.get(key) for key in ("id", "status", "task", "client_request_id", "recovery_state")
                }
                output_dir = Path(OUTPUTS) / entry.name
                if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
                    raise ValueError
                inventory[entry.name]["has_outputs"] = bool(job.get("outputs")) or (
                    output_dir.is_dir() and any(output_dir.iterdir())
                )
            outputs_root = Path(OUTPUTS)
            if (inventory.keys() != _jobs.keys() or outputs_root.resolve() != outputs_root.absolute()
                    or any(path.name not in inventory for path in outputs_root.iterdir())):
                raise ValueError
    except (OSError, ValueError, KeyError):
        raise HTTPException(409, {"code": "job_inventory_unverified", "detail": "无法核实远端作业清单"}) from None
    return {
        "complete": True, "session": SESSION, "instance_id": INSTANCE_ID,
        "platform_instance_id": PLATFORM_INSTANCE_ID or None, "jobs": list(inventory.values()),
    }


@app.post("/api/maintenance/cleanup-for-image")
def api_cleanup_for_image():
    if not resolve_auth_token():
        raise HTTPException(404, "接口未启用")
    with _jobs_lock:
        if _active_io_jobs or any(job["status"] in ("queued", "running") for job in _jobs.values()):
            raise HTTPException(409, {"code": "active_jobs_present", "detail": "仍有作业在途，无法清理镜像"})
        freed_bytes, cleaned = _clean_image_files()

    return {"ok": True, "freed_bytes": freed_bytes, "models_dir_bytes": _directory_file_bytes(setup_env.COMFY_MODELS_DIR),
            "cleaned": cleaned}


@app.get("/api/workflows")
def api_workflows():
    profile = h3.probe_hardware()
    return {
        "hardware": asdict(profile),
        "workflows": [comfy_workflow_status(profile, item) for item in workflows.WORKFLOWS.values()],
    }


async def _request_object(req):
    try:
        body = await req.json()
    except ValueError as error:
        raise HTTPException(400, "请求体须为有效 JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(400, "body 必须是 JSON 对象")
    return body


JOB_TASKS = ("t2v", "i2v", "r2v", "setup", "download", "load")


def _job_request(body):
    task, raw_mid = body.get("task"), body.get("model_id")
    raw_mid = "" if raw_mid is None else raw_mid
    params = body.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise ValueError("params 必须是 JSON 对象")
    if any(k.startswith("_") for k in params):
        raise ValueError("params 的内部字段由服务端生成")
    if not isinstance(raw_mid, str) or task not in JOB_TASKS:
        raise ValueError(f"无效 model_id 或未知任务类型 {task}")
    if task == "setup":
        comp = params.get("component", "comfy")
        if comp not in ("comfy", "minimax"):
            raise ValueError(f"setup 仅支持 comfy/minimax，收到：{comp}")
        return {"id": raw_mid}, None, task, dict(params)
    params = dict(params)
    if params.get("workflow") is not None:
        expected = workflows.get_workflow(params["workflow"]).model_id
        mid = resolve_model_id(raw_mid) if raw_mid else expected
        if mid != expected:
            raise ValueError(f"{params['workflow']} 需要模型 {expected}")
    else:
        mid = resolve_model_id(raw_mid, fallback_task=None if raw_mid else task)
    if mid not in _models:
        raise ValueError(f"未知模型 {raw_mid}")
    model = _models[mid]
    workflow = workflows.for_model(model, params)
    prompt = params.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ValueError("prompt 必须是字符串")
    if task in ("t2v", "i2v", "r2v") and not (
        prompt or ""
    ).strip():
        raise ValueError("请输入提示词描述")
    return model, workflow, task, params


def _simulated_profile(value):
    return preflight.resolve_simulated_gpu(value)


def _available_vram(profile):
    raw = job_perf._smi("--query-gpu=memory.free", "--format=csv,noheader,nounits").splitlines()
    free = _parse_smi_num(raw[0]) if raw else None
    if free is None:
        return None
    workers = list(WORKERS.values())
    pids = {w.comfy_pid for w in workers if w.alive() and w.comfy_pid}
    reclaimable = sum(_process_vram(pid) or 0 for pid in pids if pid)
    return min(profile.vram_gb, (free + reclaimable) / 1024)


def _default_billing_context(supplied: Any) -> Dict[str, Any]:
    ctx = dict(supplied) if isinstance(supplied, dict) else {}
    ctx.setdefault("provider_id", "colab-comfy")
    ctx.setdefault("session_name", SESSION)
    ctx.setdefault("endpoint_origin", "http://127.0.0.1")
    ctx.setdefault("vm_host", None)
    if ctx.get("billing_rate") is None:
        ctx["billing_rate"] = "unknown"
    return preflight.normalize_billing_context(ctx)


def _evaluate_request(body, *, preview):
    model, workflow, task, params = _job_request(body)
    if task == "setup":
        norm = normalize_setup_params(params)
        approve_digest = compute_setup_approval_digest(
            instance_id=INSTANCE_ID,
            session=SESSION,
            remote_root=ROOT,
            params=norm,
        )
        facts = {
            "status": "warn",
            "requires_approval": True,
            "task": "setup",
            "instance_id": INSTANCE_ID,
            "session": SESSION,
            "remote_root": ROOT,
            "params": norm,
            "approve_digest": approve_digest,
            "preflight_digest": approve_digest,
        }
        return facts, model["id"] or None, task, norm
    simulation = body.get("simulated_gpu") if preview else None
    if simulation is None:
        profile = h3.probe_hardware()
        free = _available_vram(profile) if profile.vram_gb else None
    else:
        profile, free = _simulated_profile(simulation)
    billing_context = _default_billing_context(body.get("billing_context"))
    facts, normalized = preflight.evaluate_preflight(
        instance_id=INSTANCE_ID,
        profile=profile,
        model=model,
        workflow=workflow,
        task=task,
        params=params,
        input_root=INPUTS,
        read_assets=_custom_assets,
        vram_free_gb=free,
        input_facts=body.get("input_facts") if preview else None,
        local_history=body.get("local_history"),
        simulated=simulation is not None,
        billing_context=billing_context,
    )
    facts["status"] = preflight.normalize_preflight_status(facts.get("status"))
    return facts, model["id"], task, normalized


def _validate_idempotency_fields(body: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    cid = body.get("client_request_id")
    if cid is not None:
        if not isinstance(cid, str) or not cid.strip() or len(cid.strip()) > 128:
            raise ValueError("client_request_id 必须为长度 1-128 的非空字符串")
        cid = cid.strip()
    rd = body.get("request_digest")
    if rd is not None:
        if not isinstance(rd, str) or not rd.strip() or len(rd.strip()) > 128:
            raise ValueError("request_digest 必须为非空字符串")
        rd = rd.strip()
    return cid, rd


def _check_dedup_match(
    existing: Dict[str, Any],
    *,
    client_request_id: str,
    request_digest: str,
) -> Dict[str, Any]:
    if request_digest != existing.get("request_digest"):
        raise HTTPException(
            409,
            {
                "detail": "client_request_id 冲突：request_digest 与已有作业不一致",
                "code": "client_request_id_conflict",
                "job_id": existing["id"],
                "client_request_id": client_request_id,
            },
        )
    return {
        "job_id": existing["id"],
        "model_id": existing["model_id"],
        "deduplicated": True,
        "status": existing["status"],
    }


def _validate_session_deadline(body: Dict[str, Any]) -> Optional[float]:
    raw = body.get("session_deadline_at")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("session_deadline_at 必须是有限数字时间戳")
    import math

    val = float(raw)
    if not math.isfinite(val):
        raise ValueError("session_deadline_at 必须是有限数字时间戳")
    return val


async def _queue_request(body):
    try:
        client_request_id, supplied_digest = _validate_idempotency_fields(body)
        session_deadline_at = _validate_session_deadline(body)
        request_digest = supplied_digest or _util.sha256_bytes(comfy_assets.canonical_json(
            [body.get("model_id"), body.get("task"), body.get("params") or {}]
        ))
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error

    # Fast-path idempotency / conflict check using the exact same canonical fingerprint as first submission.
    if client_request_id:
        with _jobs_lock:
            existing_jid = _request_index.get(client_request_id)
            if existing_jid and existing_jid in _jobs:
                return _check_dedup_match(
                    _jobs[existing_jid],
                    client_request_id=client_request_id,
                    request_digest=request_digest,
                )

    try:
        facts, mid, task, params = await asyncio.to_thread(_evaluate_request, body, preview=False)
        ack = body.get("ack_warnings", False)
        digest = body.get("approve_digest") if body.get("approve_digest") is not None else body.get("preflight_digest")
        if type(ack) is not bool or (
            digest is not None and (not isinstance(digest, str) or not comfy_assets.SHA256.fullmatch(digest))
        ):
            raise ValueError("无效 ack_warnings 或 preflight_digest/approve_digest")
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error

    if task == "setup":
        expected_setup_digest = facts["approve_digest"]
        if not digest or digest != expected_setup_digest:
            err_detail = {
                k: v for k, v in facts.items() if k not in ("approve_digest", "preflight_digest")
            }
            err_detail.update(
                detail="远端 setup 缺少或过期的审批摘要 approve_digest，请先经审批后再提交",
                code="setup_approval_required",
                approval_status="missing" if not digest else "stale",
            )
            raise HTTPException(403, err_detail)
    elif facts:
        status = preflight.normalize_preflight_status(facts.get("status"))
        facts["status"] = status
        digest_matched = digest is not None and digest == facts["preflight_digest"]
        approved = (ack or body.get("approve_digest") is not None) and digest_matched
        if status == "block":
            raise HTTPException(409, facts)
        if digest is not None and not digest_matched:
            raise HTTPException(409, dict(facts, status="warn", approval_status="stale"))
        if ack and not approved:
            raise HTTPException(409, dict(facts, status="warn", approval_status="stale"))
        if status == "warn" and not approved:
            raise HTTPException(409, facts)

    jid = time.strftime("job_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]

    if session_deadline_at is not None and _now() >= session_deadline_at:
        raise HTTPException(
            409,
            {
                "status": "block",
                "code": "session_deadline_expired",
                "detail": f"受管会话在入队前超过硬时限 deadline_at={session_deadline_at}，远端拒绝入队",
            },
        )

    # Re-check under _jobs_lock and perform SINGLE atomic persist (enqueue_state="queued") BEFORE submit().
    with _jobs_lock:
        if client_request_id and client_request_id in _request_index:
            existing_jid = _request_index[client_request_id]
            return _check_dedup_match(
                _jobs[existing_jid],
                client_request_id=client_request_id,
                request_digest=request_digest,
            )
        if task not in ("setup", "download") and params.get("seed", -1) == -1:
            params["seed"] = random.randint(1, 2**31 - 1)
        try:
            job = new_job(
                mid,
                task,
                params,
                jid=jid,
                workload_key=facts.get("workload_key") if isinstance(facts, dict) else None,
                client_request_id=client_request_id,
                request_digest=request_digest,
            )
        except OSError as error:
            shutil.rmtree(Path(JOBS_DIR) / jid, ignore_errors=True)
            raise HTTPException(500, f"持久化 job.json 失败，已拒绝入队：{error}") from error

    try:
        submit(job)
    except Exception as error:
        with _jobs_lock:
            job["status"] = "error"
            job["enqueue_state"] = "enqueue_failed"
            job["error"] = f"投递后台队列失败：{error}"
            try:
                _persist(job)
            except OSError:
                pass
        raise HTTPException(500, f"作业投递队列失败：{error}") from error

    return {"job_id": job["id"], "model_id": mid, "deduplicated": False}


@app.post("/api/preflight")
async def api_preflight(req: Request):
    body = await _request_object(req)
    try:
        facts, _, task, _ = await asyncio.to_thread(_evaluate_request, body, preview=True)
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(400, str(error)) from error
    if task == "setup":
        return facts
    if facts["status"] == "block":
        raise HTTPException(409, facts)
    return facts


@app.post("/api/jobs")
async def api_new_job(req: Request):
    return await _queue_request(await _request_object(req))


@app.get("/api/jobs")
def api_jobs(limit: int = 50):
    with _jobs_lock:
        return [_public_job(j) for j in sorted(_jobs.values(), key=lambda j: j.get("created", 0), reverse=True)[:limit]]


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return _redact({key: value for key, value in job.items() if key != "_perf"})


def _find_job(jid: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", jid):
        raise HTTPException(404, "任务不存在")
    with _jobs_lock:
        if jid in _jobs:
            return _public_job(_jobs[jid])
        try:
            job = json.loads((Path(JOBS_DIR) / jid / "job.json").read_bytes())
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        _recover_job_record(job)
        return _public_job(job)


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    return _find_job(jid)


def _perf_file(jid: str, filename: str, media_type: str):
    _find_job(jid)
    path = Path(JOBS_DIR) / jid / filename
    if not path.is_file():
        raise HTTPException(404, "任务没有性能记录")
    return FileResponse(path, media_type=media_type)


@app.get("/api/jobs/{jid}/perf")
def api_job_perf(jid: str):
    return _perf_file(jid, "perf.json", "application/json")


@app.get("/api/jobs/{jid}/telemetry")
def api_job_telemetry(jid: str):
    return _perf_file(jid, "telemetry.jsonl", "application/x-ndjson")


@app.get("/api/jobs/{jid}/log")
def api_job_log(jid: str, offset: int = 0):
    j = _find_job(jid)
    p = f"{JOBS_DIR}/{jid}/log.txt"
    text, new_off = "", offset
    if os.path.exists(p):
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read(512 * 1024)
            new_off = offset + len(data)
            text = data.decode("utf-8", "replace")
    return {"text": text, "offset": new_off, "done": j["status"] in ("done", "error", "cancelled")}


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    with _gpu_cv, _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            raise HTTPException(404, "任务不存在")
        if job["status"] not in ("queued", "running"):
            return {"ok": True}
        owner = _gpu_owner == ("job", jid)
        if owner:
            update_job(jid, cancel_requested=True)
        else:
            if job["task"] in ("setup", "download"):
                update_job(jid, status="cancelled", cancel_requested=True, stage="已取消", finished=_now())
            else:
                _publish_gpu_terminal(job, {"status": "cancelled", "stage": "已取消"})
                if jid in _gpu_queue:
                    _gpu_queue.remove(jid)
                _gpu_cv.notify_all()
        stop_io = _io_cancels.get(jid)
        workers = [w for w in list(WORKERS.values()) if w.current_job == jid]
    if stop_io:
        stop_io()
    for worker in workers:
        with worker.lock:
            if worker.current_job == jid:
                worker.kill()
    return {"ok": True}


@app.post("/api/upload")
async def api_upload(req: Request, filename: str = "upload.png"):
    name = re.sub(r"[^\w.\-]+", "_", os.path.basename(filename)) or "upload.png"
    stem, ext = os.path.splitext(name)
    path = f"{INPUTS}/{stem}_{uuid.uuid4().hex[:6]}{ext or '.png'}"
    data = await req.body()
    if not data:
        raise HTTPException(400, "空文件")
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path}


@app.get("/api/files")
def api_files(path: str):
    real = os.path.realpath(path)
    if Path(OUTPUTS).resolve() not in Path(real).parents or not os.path.isfile(real):
        raise HTTPException(404, "文件不存在")
    return FileResponse(real, filename=os.path.basename(real))


def _cleanup_groups():
    for worker in list(WORKERS.values()):
        if worker.proc is not None:
            worker.kill()
    for proc in list(_io_procs.values()):
        _terminate_group(proc)
    remove_server_pid()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(description="yc-h3-engine Pure ComfyUI Remote Server")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，非 loopback 须启用认证）")
    ap.add_argument("--port", type=int, default=7860, help="监听端口（默认 7860）")
    args = ap.parse_args(argv)

    try:
        bind_host = validate_bind_host(
            args.host,
            allow_non_loopback=os.environ.get("YC_H3_ALLOW_NON_LOOPBACK") == "1",
            auth_token=resolve_auth_token(),
        )
    except ValueError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2

    write_server_pid()
    _load_old_jobs()
    _load_registry()
    threading.Thread(target=_gpu_loop, daemon=True).start()
    print(f"yc-h3-engine Remote v{VERSION} listening on http://{bind_host}:{args.port} (ROOT={ROOT})", flush=True)
    atexit.register(_cleanup_groups)
    uvicorn.run(app, host=bind_host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
