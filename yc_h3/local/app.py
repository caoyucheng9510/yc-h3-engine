"""Local orchestration: session isolation, single-session/hard-timeout enforcement,
tiered cost-approval gates, read-only preflight, cold deployment, and perf archival.

Adapted from WorldForge (MIT License).
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import statistics
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from yc_h3.config import EngineConfig, credential_fingerprint, load_config
from yc_h3.fsutil import atomic_write_bytes, atomic_writer, sha256_bytes, sha256_file
from yc_h3.remote import comfy_assets, preflight as remote_preflight, setup_env
from yc_h3.remote.workflows.minimax_h3.registry import load_workflows
from yc_h3.remote.preflight import WORKLOAD_FIELDS
from yc_h3.tasks_store import TaskStore, comfy_job_delivery_state
from yc_h3.ledger import record_colab_stop

from . import config
from .colab import Colab, summarize_cli_error
from .platform_guard import require_colab_platform
from .tasks import TaskError, TaskRegistry
from .tunnel import RemoteUnreachable, Tunnel


def _normalize_setup_params(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


def _compute_remote_setup_digest(
    *,
    instance_id: str,
    session: Optional[str],
    remote_root: str,
    params: Dict[str, Any],
) -> str:
    payload = {
        "kind": "remote_setup",
        "instance_id": instance_id,
        "session": session,
        "remote_root": remote_root,
        "params": params,
    }
    return sha256_bytes(comfy_assets.canonical_json(payload))


def build_kill_remote_script(remote_root: str = config.REMOTE_ROOT) -> str:
    """Build a PID/PGID-scoped remote cleanup script that verifies strict root-boundary ownership.

    Reads only ``<remote_root>/run/server.pid`` and ``<remote_root>/run/worker_pgids/*``,
    verifies ``/proc/<pid>/cmdline`` or ``/proc/<pid>/cwd`` against exact ``remote_root`` path
    boundaries (never loose substrings), and re-verifies identity before escalating to SIGKILL.
    """
    clean_root = remote_root.rstrip("/") or "/"
    q_root = shlex.quote(clean_root)
    return f"""set -eu
ROOT={q_root}
RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/server.pid"
PGID_DIR="$RUN_DIR/worker_pgids"

token_matches_root() {{
    tok="$1"
    case "$tok" in
        "$ROOT"|"$ROOT"/*|*"=$ROOT"|*"=$ROOT"/*) return 0;;
        *) return 1;;
    esac
}}

belongs_to_root() {{
    pid="$1"
    case "$pid" in ''|*[!0-9]*) return 1;; esac
    [ "$pid" -gt 1 ] || return 1
    if [ -L "/proc/$pid/cwd" ]; then
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        case "$cwd" in
            "$ROOT"|"$ROOT"/*) return 0;;
        esac
    fi
    if [ -r "/proc/$pid/cmdline" ]; then
        if tr '\\0' '\\n' < "/proc/$pid/cmdline" 2>/dev/null | {{
            while IFS= read -r arg || [ -n "$arg" ]; do
                if token_matches_root "$arg"; then
                    exit 0
                fi
            done
            exit 1
        }}; then
            return 0
        fi
        return 1
    fi
    args_line="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    for tok in $args_line; do
        if token_matches_root "$tok"; then
            return 0
        fi
    done
    return 1
}}

collect_groups() {{
    if [ -d "$PGID_DIR" ]; then
        for f in "$PGID_DIR"/*.pgid "$PGID_DIR"/*; do
            [ -f "$f" ] || continue
            cat "$f"
            printf '\\n'
        done
    fi
}}

server_pid=""
if [ -f "$PID_FILE" ]; then
    cand="$(tr -dc '0-9' < "$PID_FILE" || true)"
    if [ -n "$cand" ] && belongs_to_root "$cand"; then
        server_pid="$cand"
    fi
fi

raw_groups="$(collect_groups | sort -u || true)"
valid_groups=""
for g in $raw_groups; do
    if belongs_to_root "$g"; then
        valid_groups="$valid_groups $g"
    fi
done

if [ -n "$server_pid" ]; then
    kill -TERM "$server_pid" 2>/dev/null || true
fi
for g in $valid_groups; do
    kill -TERM -- "-$g" 2>/dev/null || true
done

for _i in 1 2 3 4 5; do
    alive=""
    if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null && belongs_to_root "$server_pid"; then
        alive=1
    fi
    for g in $valid_groups; do
        if kill -0 -- "-$g" 2>/dev/null && belongs_to_root "$g"; then
            alive=1
        fi
    done
    [ -n "$alive" ] || break
    sleep 1
done

if [ -n "$server_pid" ] && belongs_to_root "$server_pid"; then
    kill -KILL "$server_pid" 2>/dev/null || true
fi
for g in $valid_groups; do
    if belongs_to_root "$g"; then
        kill -KILL -- "-$g" 2>/dev/null || true
    fi
done

rm -f -- "$PID_FILE"
if [ -d "$PGID_DIR" ]; then
    for f in "$PGID_DIR"/*; do
        [ -f "$f" ] && rm -f -- "$f"
    done
fi
"""


def build_start_remote_command(
    session: str,
    *,
    remote_root: str = config.REMOTE_ROOT,
    remote_app_dir: Optional[str] = None,
    remote_log: Optional[str] = None,
    remote_port: int = config.REMOTE_PORT,
) -> str:
    app_dir = remote_app_dir or f"{remote_root}/app"
    log_path = remote_log or f"{remote_root}/server.log"
    return (
        f"mkdir -p {shlex.quote(remote_root)} && "
        f"YC_H3_REMOTE_ROOT={shlex.quote(remote_root)} "
        f"YC_H3_SESSION={shlex.quote(session)} "
        f"bash {shlex.quote(app_dir + '/bootstrap_remote.sh')} "
        f"--daemon --host 127.0.0.1 --port {int(remote_port)} "
        f"--root {shlex.quote(remote_root)} --log {shlex.quote(log_path)}"
    )


class ApprovalRequiredError(TaskError):
    """Raised when a gated operation requires explicit approval digest or is blocked."""

    def __init__(self, payload: Dict[str, Any], *, exit_code: int = 2, http_status: int = 409):
        msg = payload.get("reason") or payload.get("detail") or "需要审批摘要或已被门禁阻断"
        super().__init__(msg)
        self.payload = payload
        self.exit_code = exit_code
        self.http_status = http_status


def _perf_peak(record):
    gpu = record.get("gpu_stats") or {}
    return (gpu.get("mem_used_mb") or {}).get("max")


def _perf_median(records):
    values = [value for record in records if (value := (record.get("phases_s") or {}).get("total")) is not None]
    return statistics.median(values) if values else None


def summarize_perf_history(root: Path, workflow: Optional[str] = None) -> List[Dict[str, Any]]:
    """Summarize completed local archives by comparable workload."""
    groups: Dict[bytes, List[Dict[str, Any]]] = {}
    for path in root.glob("*/perf.json"):
        try:
            run_rec = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(run_rec, dict) or run_rec.get("status") != "done":
            continue
        key = run_rec.get("workload_key")
        if (
            not isinstance(key, dict)
            or key.keys() != set(WORKLOAD_FIELDS)
            or (workflow and workflow != key["workflow_id"])
        ):
            continue
        groups.setdefault(comfy_assets.canonical_json(key), []).append(run_rec)
    rows = []
    for encoded, samples in groups.items():
        partial = [r for r in samples if (r.get("markers") or {}).get("comfy_partial_load") is True]
        row = {
            "workload_key": json.loads(encoded),
            "comparable_samples": len(samples),
            "partial_load_samples": len(partial),
            "partial_load_median_total_s": _perf_median(partial),
        }
        for label, cold in (("cold_start", True), ("warm_run", False)):
            runs = [r for r in samples if r.get("cold_start") is cold]
            peaks = [value for r in runs if (value := _perf_peak(r)) is not None]
            row[label] = {
                "samples": len(runs),
                "median_total_s": _perf_median(runs),
                "peak_vram_mb": max(peaks, default=None),
                "median_peak_vram_mb": statistics.median(peaks) if peaks else None,
            }
        rows.append(row)
    return rows


def _valid_amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _session_id(name, host, allocated_at):
    if not name or not host:
        return None
    return sha256_bytes(comfy_assets.canonical_json([name, host, allocated_at]))


def _safe_filename(name: str, fallback: str = "upload.png") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", os.path.basename(str(name or "").strip()))
    return cleaned or fallback


def _uses_high_mem(gpu: str, high_mem: Optional[bool]) -> bool:
    """Resolve effective ``--high-mem`` per card (G4/H100/L4 auto-filtered; A100 defaults True)."""
    if gpu in ("G4", "H100", "L4"):
        return False
    if high_mem is not None:
        return bool(high_mem)
    return gpu == "A100"


class App:
    JOBS_POLL = 5

    def __init__(
        self,
        *,
        settings: Optional[config.Settings] = None,
        colab: Optional[Colab] = None,
        tunnel: Optional[Tunnel] = None,
        task_store: Optional[TaskStore] = None,
        engine_config: EngineConfig | None = None,
        pending_deliveries_fn: Optional[Callable[[str, Optional[str]], bool]] = None,
        job_delivered_fn: Optional[Callable[[str], bool]] = None,
    ):
        require_colab_platform()
        self.engine_config = engine_config or (settings.engine_config if settings else load_config())
        if settings is not None and settings.engine_config.config_identity != self.engine_config.config_identity:
            raise ValueError("Colab 设置与守护进程配置身份不一致")
        self.settings = settings or config.Settings(engine_config=self.engine_config)
        self.colab = colab or Colab(self.settings)
        self.tunnel = tunnel or Tunnel(self.colab)
        self.tasks = TaskRegistry(redact=self.engine_config.redact)
        self.task_store = task_store or TaskStore(config=self.engine_config)
        self._pending_deliveries_fn = pending_deliveries_fn
        self._job_delivered_fn = job_delivered_fn
        self.lock = threading.Lock()
        self._session_cancels: Dict[str, threading.Event] = {}
        self._session_cancel_reasons: Dict[str, str] = {}
        self._asset_lock = threading.RLock()
        self._billing_lock = threading.Lock()
        self._submission_preflight = None
        self._perf_locks: Dict[str, threading.Lock] = {}
        self._tracked_jobs: Dict[Any, Any] = {}
        self._archived_jobs = set()
        self._perf_unavailable = set()
        self.last_activity = time.time()
        self.notices: List[Dict[str, Any]] = []
        self.remote_jobs: List[Dict[str, Any]] = []
        self.remote_jobs_at = None
        self.auto_stop_task = None
        self._autotunnel_done = False
        self.auto_tunnel = True
        self._draining = False

    def _cancel_event(self, name: str) -> threading.Event:
        with self.lock:
            ev = self._session_cancels.get(name)
            if ev is None:
                ev = threading.Event()
                self._session_cancels[name] = ev
            return ev

    def _mark_session_cancelled(self, name: str, reason: str = "会话正在停止或已超时") -> None:
        with self.lock:
            ev = self._session_cancels.get(name)
            if ev is None:
                ev = threading.Event()
                self._session_cancels[name] = ev
            self._session_cancel_reasons[name] = reason
            ev.set()

    def _clear_session_cancel(self, name: str) -> None:
        with self.lock:
            ev = self._session_cancels.get(name)
            if ev is not None:
                ev.clear()
            self._session_cancel_reasons.pop(name, None)

    def _session_expired_or_cancelled_reason(self, name: Optional[str], *, now: Optional[float] = None) -> Optional[str]:
        if not name:
            return None
        with self.lock:
            ev = self._session_cancels.get(name)
            if ev is not None and ev.is_set():
                return self._session_cancel_reasons.get(name) or f"会话 {name} 已触发停止/取消"
        now_ts = time.time() if now is None else float(now)
        managed = self.settings.get("managed_sessions").get(name)
        if managed and managed.get("deadline_at") is not None and now_ts >= float(managed["deadline_at"]):
            return f"会话 {name} 已超过硬时限 deadline_at={managed['deadline_at']}，拒绝继续部署或入队"
        return None

    def _check_not_expired_or_cancelled(self, name: Optional[str]) -> None:
        reason = self._session_expired_or_cancelled_reason(name)
        if reason:
            raise TaskError(f"session_deadline_or_cancelled: {reason}")

    def _is_job_delivered(self, jid: str) -> bool:
        if self._job_delivered_fn is not None:
            return bool(self._job_delivered_fn(jid))
        if self.task_store is not None:
            return comfy_job_delivery_state(jid, task_store=self.task_store) == "delivered"
        return False

    # ------------------------------------------------------------ Lifecycle
    def start(self):
        self.colab.on_refresh(self._on_colab_refresh)
        self.colab.start()
        self.tunnel.start_threads()
        threading.Thread(target=self._jobs_loop, name="jobs-poll", daemon=True).start()
        threading.Thread(target=self._watchdog_loop, name="session-watchdog", daemon=True).start()

    def shutdown(self):
        try:
            self.tunnel.stop()
        except Exception:  # noqa: BLE001
            pass

    def prepare_daemon_stop(self, *, stop_colab=False, detach_colab=False):
        if stop_colab and detach_colab:
            raise ValueError("--stop-colab 与 --detach-colab 不可同时指定")
        with self.lock:
            if self._draining:
                return {"ok": False, "exit_code": 3, "message": "守护进程正在执行停机操作"}
            self._draining = True
        result = None
        try:
            result = self._prepare_daemon_stop(stop_colab=stop_colab, detach_colab=detach_colab)
            return result
        finally:
            if not result or not result["ok"]:
                with self.lock:
                    self._draining = False

    def _prepare_daemon_stop(self, *, stop_colab, detach_colab):
        managed = self.settings.get("managed_sessions")
        if detach_colab:
            return {
                "ok": True, "exit_code": 0, "detached_sessions": list(managed),
                "message": "守护进程退出，看门狗停止；保留的会话请手动核对并释放" if managed else "守护进程退出",
            }
        if self.tasks.running("session_start"):
            return {"ok": False, "exit_code": 3, "message": "正在申请受管会话，请等待分配完成后再关停"}
        stopped = []
        if managed:
            try:
                fresh = self.colab.sessions()
                self._claim_live_placeholders(fresh)
                active = [row["name"] for row in self._active_sessions(fresh)]
            except (TaskError, RuntimeError) as exc:
                return {"ok": False, "exit_code": 3, "message": f"无法确认受管会话，请运行 colab sessions 人工核对。{exc}"}
            if active and not stop_colab:
                return {
                    "ok": False, "exit_code": 3, "code": "active_managed_colab_session", "active_sessions": active,
                    "message": f"受管会话 {', '.join(active)} 仍在运行，请先 gpu stop 或指定 --stop-colab / --detach-colab",
                }
            for name in self.settings.get("managed_sessions"):
                task = self.session_stop(name, stop_reason="daemon_stop")
                while task.status == "running":
                    time.sleep(0.05)
                if task.status != "done":
                    return {"ok": False, "exit_code": 1, "message": task.error}
                stopped.append(name)
        self._settle_stop_billing()
        return {"ok": True, "exit_code": 0, "stopped_sessions": stopped, "message": "守护进程退出"}

    def _on_colab_refresh(self, snap):
        if self._autotunnel_done or not snap.get("cli_ok") or not self.auto_tunnel:
            return
        self._autotunnel_done = True
        name = self.settings.get("session_name")
        managed = self.settings.get("managed_sessions")
        if name not in managed or not config.is_managed_session_name(name):
            return
        record = managed[name]
        matched = next(
            (s for s in snap.get("sessions", []) if s["name"] == name and s["host"] == record.get("host")),
            None,
        )
        if matched and not self.tunnel.desired:
            self.notice("info", "检测到受管会话 %s，已自动建立隧道" % name)
            self.tunnel.start(name)

    def touch(self):
        self.last_activity = time.time()

    def notice(self, level: str, text: str):
        with self.lock:
            self.notices.append({"time": time.time(), "level": level, "text": self.engine_config.redact(text)})
            del self.notices[:-20]

    def has_pending_deliveries(self, session_name: str, vm_host: Optional[str] = None) -> bool:
        if self._pending_deliveries_fn is not None:
            return bool(self._pending_deliveries_fn(session_name, vm_host))
        if self.task_store is not None:
            return self.task_store.has_pending_comfy_deliveries(session_name=session_name, vm_host=vm_host)
        return False

    # ------------------------------------------------------------ State
    def idle_info(self) -> Dict[str, Any]:
        now = time.time()
        idle = max(0, now - self.last_activity)
        mins = self.settings.get("idle_minutes")
        name = self.settings.get("session_name")
        managed = self.settings.get("managed_sessions").get(name)
        exists = self.colab.cached_session(name) is not None and managed is not None
        will = max(0, int(mins * 60 - idle)) if (mins and exists) else None
        hard_left = None
        if managed and managed.get("deadline_at"):
            hard_left = max(0, int(float(managed["deadline_at"]) - now))
        return {
            "seconds": int(idle),
            "will_stop_in": will,
            "hard_deadline_in": hard_left,
            "enabled": bool(mins),
        }

    def state(self) -> Dict[str, Any]:
        tunnel, remote = self.tunnel.snapshot()
        with self.lock:
            notices = list(self.notices)
        return {
            "app": config.APP_NAME,
            "colab": self.colab.snapshot(),
            "tunnel": tunnel,
            "remote": remote,
            "settings": {key: value for key, value in self.settings.snapshot().items() if key != "pending_stop_billing"},
            "idle": self.idle_info(),
            "notices": notices,
            "local": {
                "remote_code_version": config.remote_version(),
                "tasks_running": len(self.tasks.running()),
                "ffmpeg": bool(config.ffmpeg_bin()),
            },
        }

    # ------------------------------------------------------------ Ownership & single-session guard
    def _existing_task(self, ttype: str, name: str):
        return next((task for task in self.tasks.running(ttype) if getattr(task, "session_name", None) == name), None)

    def _owned_session(self, name, sessions):
        """Match a managed record to a current endpoint before using the CLI by name."""
        config.require_managed_session_name(name)
        record = self.settings.get("managed_sessions").get(name)
        live = next((row for row in sessions if row["name"] == name), None)
        if record is None:
            if live:
                raise TaskError(f"unmanaged_session: {name} 未由本机托管")
            return None, None
        host = record.get("host")
        if not host:
            raise TaskError(f"needs_reconcile: {name} 的分配结果不明，请运行 gpu reconcile --session {name} 核对 colab sessions")
        matches = [row for row in sessions if row["name"] == name or row["host"] == host]
        if matches and (len(matches) != 1 or live is None or live["host"] != host):
            raise TaskError(f"session_host_mismatch: {name} 的端点已变化，请运行 colab sessions 人工核对")
        return live, record

    def _active_sessions(self, sessions):
        active = []
        for name in self.settings.get("managed_sessions"):
            live, record = self._owned_session(name, sessions)
            if live:
                active.append({"name": name, "host": live["host"], "gpu": record.get("gpu")})
        return active

    # ------------------------------------------------------------ Tiered Cost Gate: gpu plan / gpu up
    def gpu_plan(
        self,
        *,
        session: Optional[str] = None,
        gpu=None,
        setup: bool = False,
        setup_params: Optional[Dict[str, Any]] = None,
        max_runtime_minutes: Optional[int] = None,
        idle_minutes: Optional[int] = None,
        high_mem: Optional[bool] = None,
        instance_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Strictly read-only Colab allocation/setup plan with strong digest binding."""
        if high_mem is not None and not isinstance(high_mem, bool):
            raise ValueError("high_mem 必须是布尔值")
        if not isinstance(setup, bool):
            raise ValueError("setup 必须是布尔值")

        raw_name = session or self.settings.get("session_name")
        name = config.normalize_session_name(raw_name)
        cards = list(config.gpu_candidates(gpu if gpu is not None else self.settings.get("gpu")))

        explicit_max_rt = max_runtime_minutes is not None
        if explicit_max_rt:
            req_max_rt = int(max_runtime_minutes)
            if req_max_rt < 1 or req_max_rt > 24 * 60:
                raise ValueError("max_runtime_minutes 须介于 1 和 1440 之间")
        else:
            req_max_rt = int(self.settings.get("max_runtime_minutes"))

        idle_m = int(self.settings.get("idle_minutes") if idle_minutes is None else idle_minutes)
        if idle_m < 0 or idle_m > 24 * 60:
            raise ValueError("idle_minutes 须介于 0 和 1440 之间")

        norm_setup = _normalize_setup_params(setup_params) if setup else None

        managed = self.settings.get("managed_sessions")
        usage_info, _ = self.colab.usage()
        fresh, active_managed = [], []
        blocker = None
        try:
            fresh = self.colab.sessions()
            active_managed = self._active_sessions(fresh)
        except RuntimeError as exc:
            blocker = "sessions_unreachable", f"无法确认当前会话，请运行 colab sessions 人工核对：{exc}"
        except TaskError as exc:
            code, _, reason = str(exc).partition(": ")
            blocker = code, reason

        now = time.time()
        action = "allocate"
        status = "warn"
        requires_approval = True
        code = "colab_allocation_requires_approval"
        existing_host: Optional[str] = None
        target_instance_id: Optional[str] = instance_id
        current_deadline_at: Optional[float] = None
        effective_deadline_at: Optional[float] = None
        effective_max_rt = req_max_rt
        update_deadline = False

        live = next((s for s in fresh if s["name"] == name), None)
        rec = managed.get(name)
        for active in active_managed:
            if active["name"] != name:
                blocker = "active_managed_session_exists", f"请先释放受管会话 {active['name']}，再分配 {name}"
                break
        if blocker:
            action = "blocked"
            status = "block"
            requires_approval = False
            code, reason = blocker
        elif live is not None and rec is None:
            action = "blocked"
            status = "block"
            requires_approval = False
            code = "unmanaged_session_collision"
            reason = f"账号上已存在同名会话 {name}（host={live['host']}）但不在本机归属账本中，拒绝接管或分配"
        elif live is not None and rec is not None:
            existing_host = live.get("host")
            existing_allocated_at = float(rec.get("allocated_at") or now)
            existing_max_rt = int(rec.get("max_runtime_minutes") or self.settings.get("max_runtime_minutes"))
            current_deadline_at = (
                float(rec["deadline_at"])
                if rec.get("deadline_at") is not None
                else existing_allocated_at + existing_max_rt * 60
            )
            if explicit_max_rt and req_max_rt != existing_max_rt:
                effective_max_rt = req_max_rt
                effective_deadline_at = existing_allocated_at + req_max_rt * 60
                update_deadline = True
            else:
                effective_max_rt = existing_max_rt
                effective_deadline_at = current_deadline_at
                update_deadline = False

            # Capture live instance_id via fresh inspect_health() when tunnel targets this exact session.
            if target_instance_id is None:
                t_snap, r_snap = self.tunnel.snapshot()
                if t_snap.get("session") == name:
                    probe_state, live_h, _probe_detail = self.tunnel.inspect_health()
                    if (
                        probe_state == "ok"
                        and isinstance(live_h, dict)
                        and live_h.get("session") == name
                        and live_h.get("instance_id")
                    ):
                        target_instance_id = live_h.get("instance_id")
                    elif probe_state != "foreign_app" and r_snap.get("status") == "ok" and r_snap.get("instance_id"):
                        target_instance_id = r_snap.get("instance_id")

            if setup:
                action = "setup"
                status = "warn"
                requires_approval = True
                code = "colab_setup_requires_approval"
                rem_min = max(0, int((effective_deadline_at - now) / 60))
                reason = (
                    f"将在已托管会话 {name}（host={existing_host}）上执行 --setup 安装"
                    f"（当前硬时限 deadline_at={int(effective_deadline_at)}，约剩余 {rem_min} 分钟），需审批摘要确认"
                )
            elif update_deadline:
                action = "reconnect"
                status = "warn"
                requires_approval = True
                code = "colab_deadline_change_requires_approval"
                reason = (
                    f"变更已托管会话 {name} 硬时限（{existing_max_rt} -> {effective_max_rt} 分钟，"
                    f"deadline_at={int(effective_deadline_at)}），需审批摘要确认"
                )
            else:
                action = "reconnect"
                status = "pass"
                requires_approval = False
                code = "reconnect_owned_session"
                reason = f"重连已批准运行中的受管会话 {name}（host={existing_host}）"
        elif setup:
            action = "allocate_and_setup"
            status = "warn"
            requires_approval = True
            code = "colab_allocation_and_setup_requires_approval"
            reason = f"将新分配会话 {name} 并执行 --setup 安装（候选卡型：{', '.join(cards)}，硬时限 {effective_max_rt} 分钟）"
        else:
            reason = f"将新分配受管 Colab 会话 {name}（候选卡型：{', '.join(cards)}，硬时限 {effective_max_rt} 分钟）"

        remaining_seconds = (
            max(0, int(effective_deadline_at - now)) if effective_deadline_at is not None else effective_max_rt * 60
        )

        balance = usage_info.get("balance")
        pricing = {
            "rate": usage_info.get("rate"),
            "cli_ok": usage_info.get("cli_ok"),
        }
        high_mem_gpus = [card for card in cards if _uses_high_mem(card, high_mem)]
        digest_payload = {
            "session_name": name,
            "data_dir_hash": self.engine_config.data_dir_hash,
            "config_identity": self.engine_config.config_identity,
            "candidates": cards,
            "setup": setup,
            "setup_params": norm_setup,
            "high_mem": high_mem,
            "high_mem_gpus": high_mem_gpus,
            "max_runtime_minutes": effective_max_rt,
            "deadline_at": effective_deadline_at,
            "update_deadline": update_deadline,
            "idle_minutes": idle_m,
            "action": action,
            "existing_host": existing_host,
            "instance_id": target_instance_id,
            "pricing": pricing,
        }
        digest = sha256_bytes(comfy_assets.canonical_json(digest_payload))

        return {
            "status": remote_preflight.normalize_preflight_status(status),
            "code": code,
            "reason": reason,
            "requires_approval": requires_approval,
            "action": action,
            "digest": digest,
            "session_name": name,
            "candidates": cards,
            "setup": setup,
            "setup_params": norm_setup,
            "high_mem": high_mem,
            "high_mem_gpus": high_mem_gpus,
            "max_runtime_minutes": effective_max_rt,
            "current_deadline_at": current_deadline_at,
            "deadline_at": effective_deadline_at,
            "remaining_seconds": remaining_seconds,
            "deadline_changed": update_deadline,
            "idle_minutes": idle_m,
            "existing_host": existing_host,
            "instance_id": target_instance_id,
            "balance": balance,
            "pricing": {**pricing, "balance": balance},
            "active_managed_sessions": active_managed,
        }

    def gpu_up(
        self,
        *,
        session: Optional[str] = None,
        gpu=None,
        setup: bool = False,
        setup_params: Optional[Dict[str, Any]] = None,
        max_runtime_minutes: Optional[int] = None,
        idle_minutes: Optional[int] = None,
        high_mem: Optional[bool] = None,
        approve_digest: Optional[str] = None,
        connect: bool = True,
    ):
        """Gated entry point to allocate/connect/setup a managed Colab session."""
        plan = self.gpu_plan(
            session=session,
            gpu=gpu,
            setup=setup,
            setup_params=setup_params,
            max_runtime_minutes=max_runtime_minutes,
            idle_minutes=idle_minutes,
            high_mem=high_mem,
        )
        if plan["status"] == "block":
            raise ApprovalRequiredError(plan, exit_code=3, http_status=409)
        if plan["requires_approval"] and approve_digest != plan["digest"]:
            detail = dict(
                plan,
                approval_status="missing" if not approve_digest else "stale",
                detail="缺少或过期的 --approve-digest，请先运行 gpu plan 确认费用与规格",
            )
            raise ApprovalRequiredError(detail, exit_code=2, http_status=409)

        name = plan["session_name"]
        cards = tuple(plan["candidates"])
        max_rt = plan["max_runtime_minutes"]
        idle_m = plan["idle_minutes"]
        settings_patch: Dict[str, Any] = {"idle_minutes": idle_m}
        if max_runtime_minutes is not None or plan["action"] in ("allocate", "allocate_and_setup"):
            settings_patch["max_runtime_minutes"] = max_rt
        self.settings.update(settings_patch)

        approval_context = {
            "approved_action": plan["action"],
            "session_name": name,
            "expected_host": plan["existing_host"],
            "instance_id": plan["instance_id"],
            "candidates": list(cards),
            "setup": bool(plan["setup"]),
            "setup_params": plan["setup_params"],
            "high_mem": plan["high_mem"],
            "max_runtime_minutes": max_rt,
            "deadline_at": plan["deadline_at"],
            "update_deadline": bool(plan["deadline_changed"]),
            "digest": plan["digest"],
            "balance_before": plan["pricing"]["balance"] if plan["pricing"]["cli_ok"] else None,
            "cu_rate": plan["pricing"]["rate"],
        }

        if plan["action"] == "reconnect" and not plan["deadline_changed"]:
            return self.connect(
                name=name,
                gpu=cards,
                high_mem=high_mem,
                connect=connect,
                expected_host=plan["existing_host"],
            )
        return self.session_start(
            approval=approval_context,
            connect=connect,
        )

    # ------------------------------------------------------------ Sessions
    def session_start(
        self,
        approval: Optional[Dict[str, Any]] = None,
        *,
        connect: bool = False,
    ):
        """Execute a gpu_up()-verified approval context. Rejects unverified direct calls."""
        if not isinstance(approval, dict):
            raise ApprovalRequiredError(
                {
                    "status": "block",
                    "code": "approval_context_required",
                    "detail": "session_start 必须通过 gpu_up() 提供经校验的完整审批上下文",
                },
                exit_code=2,
                http_status=409,
            )
        approved_action = approval.get("approved_action")
        if approved_action not in ("allocate", "allocate_and_setup", "setup", "reconnect"):
            raise ValueError(f"无效 approved_action：{approved_action!r}")
        name = config.require_managed_session_name(approval.get("session_name"))
        cards = config.gpu_candidates(approval.get("candidates") or self.settings.get("gpu"))
        high_mem = approval.get("high_mem")
        if high_mem is not None and not isinstance(high_mem, bool):
            raise ValueError("high_mem 必须是布尔值")
        max_rt = int(approval.get("max_runtime_minutes") or self.settings.get("max_runtime_minutes"))
        if max_rt < 1 or max_rt > 24 * 60:
            raise ValueError("max_runtime_minutes 须介于 1 和 1440 之间")

        ctx = dict(approval, session_name=name, candidates=list(cards), max_runtime_minutes=max_rt)
        with self.lock:
            if self._draining:
                raise ApprovalRequiredError(
                    {"status": "block", "code": "daemon_stopping", "reason": "守护进程正在停机"},
                    exit_code=3,
                )
            for running in self.tasks.running("session_start"):
                if running.session_name == name:
                    return running
                raise ApprovalRequiredError(
                    {"status": "block", "code": "active_managed_session_exists",
                     "reason": f"正在启动受管会话 {running.session_name}，请等待分配完成"},
                    exit_code=3,
                )
            self.touch()
            task = self.tasks.create(
                "session_start",
                "启动会话 %s (%s)" % (name, ", ".join(cards)),
                self._session_start,
                ctx,
                connect,
                session_name=name,
            )
            return task

    def _allocate_session(self, task, approval, attempt):
        name = approval["session_name"]
        sessions = self.colab.sessions()
        active = self._active_sessions(sessions)
        if active:
            raise TaskError(f"请先释放受管会话 {active[0]['name']}，再分配 {name}")
        if any(row["name"] == name for row in sessions):
            raise TaskError(f"会话 {name} 已存在，请重新执行 gpu plan")
        existing_hosts = {row["host"] for row in sessions}
        started = time.time()
        cards = approval["candidates"]
        record = {
            "session_name": name, "host": None, "gpu": cards[0],
            "allocated_at": started, "balance_before": approval["balance_before"] if not sessions else None,
            "max_runtime_minutes": approval["max_runtime_minutes"],
            "deadline_at": started + approval["max_runtime_minutes"] * 60,
            "cu_rate": approval.get("cu_rate"),
            "known_hosts": sorted(existing_hosts),
            "account_fp": self._account_fingerprint(),
        }
        self._clear_session_cancel(name)
        self.settings.mutate(lambda data: data["managed_sessions"].update({name: record}))
        task.set_progress(0.1)
        for gpu in cards:
            self._check_not_expired_or_cancelled(name)
            if gpu != record["gpu"]:
                self.settings.mutate(lambda data: data["managed_sessions"][name].update(gpu=gpu))
                record["gpu"] = gpu
            task.log(f"申请 {gpu} GPU 运行时 …")
            args = ["new", "-s", name, "--gpu", gpu]
            if _uses_high_mem(gpu, approval.get("high_mem")):
                args.append("--high-mem")
            candidate = {"gpu": gpu, "requested_at": time.time(), "outcome": "timeout_needs_reconcile",
                         "code": "needs_reconcile"}
            attempt["candidates"].append(candidate)
            stream_error = None
            try:
                rc, out = self.colab.stream(args, task, timeout=420)
            except Exception as exc:  # noqa: BLE001
                # A failed invocation may have reached Colab even without an exit code.
                stream_error = exc
                rc, out = None, ""
            try:
                after = self.colab.sessions()
            except RuntimeError as exc:
                candidate["finished_at"] = time.time()
                attempt["outcome"] = "timeout_needs_reconcile"
                raise TaskError(
                    f"needs_reconcile: {name} 分配结果不明，请运行 gpu reconcile --session {name} 核对 colab sessions：{exc}"
                ) from exc
            candidate["finished_at"] = time.time()
            added = [row for row in after if row["host"] and row["host"] not in existing_hosts]
            matches = [row for row in added if row["name"] == name]
            if rc == 0 and len(matches) == 1:
                live = matches[0]
                confirmed = candidate["finished_at"]
                actual_gpu = str(live.get("hardware") or "").upper()
                candidate.update(outcome="ok", code=None)
                attempt.update(
                    candidate_started_at=candidate["requested_at"], allocated_at=confirmed,
                    gpu_allocated=actual_gpu if actual_gpu in config.GPU_CHOICES else gpu,
                    outcome="boot_failed",
                )
                try:
                    self._check_not_expired_or_cancelled(name)
                    record = self._bind_session(name, live, confirmed_at=confirmed)
                except OSError as exc:
                    raise TaskError(
                        f"needs_reconcile: {name} 端点保存失败，请运行 gpu reconcile --session {name} 核对 colab sessions：{exc}"
                    ) from exc
                return live, record

            refused = "allocation refused (precondition failed)" in out.lower() or re.search(r"\b412\b", out)
            unavailable = "backend rejected accelerator" in out.lower()
            service_unavailable = re.search(r"service unavailable|\b503\b", out, re.I)
            denied = re.search(
                r"\b40[13]\b|unauthenticated|unauthorized|authenticat|not logged in|login required|"
                r"no valid default credentials|invalid_grant|log in|permission denied", out, re.I,
            )
            transport_error = re.search(
                r"ssl(eof)?error|tls handshake|unexpected eof|remotedisconnected|"
                r"connection(reset|aborted|refused)?error|connection (reset|aborted|closed|refused)|"
                r"timed? out|timeout", out, re.I,
            )
            if (
                added or any(row["name"] == name for row in after)
                or not rc or rc < 0 or rc == 124 or transport_error
                or not (refused or unavailable or denied or service_unavailable)
            ):
                attempt["outcome"] = "timeout_needs_reconcile"
                raise TaskError(
                    f"needs_reconcile: 无法确认 {name} 的新端点，请运行 gpu reconcile --session {name} 核对 colab sessions"
                ) from stream_error
            if denied or (refused and sessions and not (unavailable or service_unavailable)):
                candidate.update(outcome="rejected", code="access_denied" if denied else "precondition_failed")
            elif service_unavailable:
                candidate.update(outcome="service_unavailable", code="http_503")
            else:
                candidate.update(outcome="out_of_stock", code="accelerator_unavailable")
            if gpu == cards[-1] or denied or (refused and sessions and not (unavailable or service_unavailable)):
                outcomes = {row["outcome"] for row in attempt["candidates"]}
                if "rejected" not in outcomes:
                    attempt["outcome"] = "service_unavailable" if "service_unavailable" in outcomes else "out_of_stock"
                self.settings.mutate(lambda data: data["managed_sessions"].pop(name, None))
                detail = summarize_cli_error(out)
                if len(attempt["candidates"]) > 1:
                    tried = "、".join(f"{row['gpu']}[{row['outcome']}]" for row in attempt["candidates"])
                    raise TaskError(f"colab new 失败 (已依次尝试 {tried})：{detail}")
                raise TaskError(f"colab new 失败 ({gpu})：{detail}")
            if service_unavailable:
                task.log(f"Colab 服务暂不可用，{gpu} 未分配，尝试下一候选卡型")
            else:
                task.log(f"Colab 拒绝分配 {gpu}，尝试下一候选卡型")

    def _bind_session(self, name: str, live: Dict[str, Any], *, confirmed_at=None) -> Dict[str, Any]:
        if confirmed_at is None:
            confirmed_at = time.time()
        actual_gpu = str(live.get("hardware") or "").upper()

        def bind(data):
            record = data["managed_sessions"].get(name)
            if record is None or record.get("host") not in (None, live["host"]):
                raise TaskError(f"Colab 会话 {name} 的占位已变化，请核对会话归属")
            record.update(host=live["host"], state="active")
            record.pop("known_hosts", None)
            record.setdefault("confirmed_allocated_at", confirmed_at)
            if actual_gpu in config.GPU_CHOICES:
                record["gpu"] = actual_gpu
            data["session_name"] = name

        return self.settings.mutate(bind)["managed_sessions"][name]

    def _claim_live_placeholders(self, sessions):
        for name, record in self.settings.get("managed_sessions").items():
            if record.get("host") or record.get("account_fp", self._account_fingerprint()) != self._account_fingerprint():
                continue
            matches = [row for row in sessions if row["name"] == name]
            if len(matches) != 1:
                continue
            row = matches[0]
            host = row.get("host")
            if (not host or host in record.get("known_hosts", ())
                    or sum(item.get("host") == host for item in sessions) != 1):
                continue
            hardware = str(row.get("hardware") or "").upper()
            if hardware in config.GPU_CHOICES and hardware != record.get("gpu"):
                continue
            self._bind_session(name, row)

    def reconcile_session(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Compare local ownership with a live Colab listing before changing it."""
        # Session tasks register under this lock. Keep their registration and the
        # ownership check/write mutually exclusive, including the fresh query.
        with self.lock:
            managed = self.settings.get("managed_sessions")
            if name is not None:
                name = config.require_managed_session_name(name)
            elif len(managed) > 1:
                raise ValueError("存在多条受管会话记录，请用 --session 指定完整会话名")
            else:
                name = next(iter(managed), None)

            if any(
                task.type in ("session_start", "session_stop", "connect")
                or (name is not None and getattr(task, "session_name", None) == name)
                for task in self.tasks.running()
            ):
                raise ApprovalRequiredError(
                    {"status": "block", "code": "session_task_running", "session_name": name,
                     "reason": "有会话任务正在运行，请等待任务结束后再对账"},
                    exit_code=3,
                )

            try:
                sessions = self.colab.sessions()
            except RuntimeError as exc:
                raise TaskError(f"无法核对 colab sessions：{exc}") from exc

            record = managed.get(name)
            host = record.get("host") if record is not None else None
            matches = [row for row in sessions if row["name"] == name or (host and row.get("host") == host)]
            if record is not None and matches:
                live = matches[0]
                if (
                    len(matches) != 1 or live["name"] != name or not live.get("host")
                    or (host and live["host"] != host)
                ):
                    return {
                        "status": "block", "code": "session_host_mismatch", "session_name": name,
                        "host": host, "sessions": matches, "reason": f"无法唯一确认 {name} 的端点归属，请人工核对 Colab 会话",
                    }
                if host:
                    return {
                        "status": "ok", "action": "noop", "session_name": name,
                        "host": host, "reason": "本地记录与 Colab 会话一致",
                    }
                self._claim_live_placeholders(sessions)
                record = self.settings.get("managed_sessions")[name]
                if not record.get("host"):
                    return {"status": "block", "code": "session_host_mismatch", "session_name": name,
                            "reason": "无法唯一确认 Colab 会话的端点归属"}
                self.colab.kick()
                return {
                    "status": "ok", "action": "adopted", "session_name": name,
                    "host": record["host"], "gpu": record.get("gpu"), "reason": "已补全同名 Colab 会话的端点记录",
                }

            if not host:
                unmanaged = [
                    row for row in sessions
                    if row["name"].startswith(config.SESSION_PREFIX)
                    and (not row.get("host") or row["host"] != managed.get(row["name"], {}).get("host"))
                ]
                if unmanaged:
                    names = ", ".join(row["name"] for row in unmanaged)
                    return {
                        "status": "block",
                        "code": "unmanaged_session" if record is None else "unexpected_managed_sessions",
                        "session_name": name, "sessions": unmanaged,
                        "reason": f"发现本机未绑定端点的 Colab 会话 {names}，请人工核对归属",
                    }

            if record is None:
                return {
                    "status": "ok", "action": "noop", "session_name": name, "reason": "没有需要对账的受管 Colab 会话",
                }

            if host:
                stopped_at = time.time()
                if self.tunnel.session == name:
                    self.tunnel.stop()
                self._queue_stop_billing(name, record, stopped_at, self._fresh_billing_info(), balance_before=None)
            else:
                account = self._account_fingerprint()
                if record.get("account_fp", account) != account:
                    return {"status": "block", "code": "session_account_mismatch", "session_name": name,
                            "reason": "请切换回占位记录所属的 Colab 账号再对账"}

                def clear_placeholder(data):
                    if data["managed_sessions"].get(name) != record:
                        raise TaskError("Colab 本地会话占位已变化，请重新对账")
                    del data["managed_sessions"][name]

                self.settings.mutate(clear_placeholder)
            self._clear_job_tracking(name)
            self.colab.kick()
            if host:
                self._settle_stop_billing()
            return {
                "status": "ok", "action": "cleared", "session_name": name,
                "reason": "Colab 会话列表中无对应实例，已清理本地受管记录",
            }

    def _session_start(self, task, approval: Dict[str, Any], connect: bool):
        attempt = {"candidates": []} if approval["approved_action"] in ("allocate", "allocate_and_setup") else None
        return self._start_session(task, approval, connect, attempt)

    def _start_session(self, task, approval, connect, attempt):
        name = approval["session_name"]
        approved_action = approval["approved_action"]
        if approved_action in ("setup", "reconnect"):
            st, record = self._owned_session(name, self.colab.sessions())
            if st is None:
                raise TaskError(f"session_vanished: {name} 已不存在，请重新执行 gpu plan")
            if st["host"] != approval["expected_host"]:
                raise TaskError(f"session_host_changed: {name} 的端点已变化，请重新执行 gpu plan")
            self._clear_session_cancel(name)
            self._check_not_expired_or_cancelled(name)
            if approval["update_deadline"]:
                record.update(max_runtime_minutes=approval["max_runtime_minutes"], deadline_at=approval["deadline_at"])
            self.settings.mutate(lambda data: data["managed_sessions"].update({name: record}))
        else:
            st, record = self._allocate_session(task, approval, attempt)

        self.colab.kick()
        self.touch()
        task.log("✓ 会话已就绪：%s（硬时限 %d 分钟）" % (st["host"], record["max_runtime_minutes"]))
        task.set_progress(0.5 if connect else 1.0)
        self._check_not_expired_or_cancelled(name)
        health_info = None
        if connect:
            health_info = self._connect(task, name, base=0.5, allocation=attempt)
        self._check_not_expired_or_cancelled(name)
        if approval["setup"]:
            if attempt is not None and health_info:
                attempt.setdefault("bootstrap_started_at", time.time())
            connected_instance_id = health_info.get("instance_id") if isinstance(health_info, dict) else None
            self._trigger_remote_setup(
                task,
                name,
                setup_params=approval["setup_params"],
                health=health_info,
                expected_instance_id=approval["instance_id"],
                connected_instance_id=connected_instance_id,
                approved_action=approved_action,
                cold_started=bool(health_info and health_info.get("cold_started")),
            )
        if attempt is not None:
            attempt["outcome"] = "ok"
            if health_info:
                attempt["ready_at"] = time.time()
        return {"session": st, "managed": record}

    def _trigger_remote_setup(
        self,
        task,
        session_name: str,
        *,
        setup_params: Optional[Dict[str, Any]] = None,
        health: Optional[Dict[str, Any]] = None,
        expected_instance_id: Optional[str] = None,
        connected_instance_id: Optional[str] = None,
        approved_action: Optional[str] = None,
        cold_started: bool = False,
    ):
        norm_params = _normalize_setup_params(setup_params)
        probe_state, live_health, probe_detail = self.tunnel.inspect_health()
        if probe_state != "ok" or not isinstance(live_health, dict):
            raise TaskError(f"远端健康校验失败，拒绝提交 setup：{probe_detail or probe_state}")
        if (
            live_health.get("app") != config.APP_NAME
            or live_health.get("remote_root") != config.REMOTE_ROOT
            or live_health.get("session") != session_name
            or not live_health.get("instance_id")
        ):
            raise TaskError(
                f"setup_target_mismatch: 当前隧道远端身份（app={live_health.get('app')!r}, "
                f"remote_root={live_health.get('remote_root')!r}, session={live_health.get('session')!r}）与目标 {session_name!r} 不一致"
            )
        live_instance_id = live_health["instance_id"]
        fixed_connected_id = (
            connected_instance_id
            if connected_instance_id is not None
            else (health.get("instance_id") if isinstance(health, dict) else None)
        )
        if expected_instance_id is not None:
            if live_instance_id != expected_instance_id:
                raise TaskError(
                    f"instance_id_changed: 远端实例已变更（审批 {expected_instance_id} vs 实时 {live_instance_id}），"
                    "拒绝用新实例重签名提交 setup，请重新执行 gpu plan 审批"
                )
        else:
            cold_start_allowed = approved_action == "allocate_and_setup" or (
                approved_action == "setup" and cold_started is True
            )
            if not cold_start_allowed:
                raise TaskError(
                    f"instance_id_changed: 审批未绑定远端实例 ID（action={approved_action!r}, cold_started={cold_started}，"
                    f"当前实时实例 {live_instance_id}），请重新执行 gpu plan 审批"
                )
            if not isinstance(fixed_connected_id, str) or not fixed_connected_id.strip():
                raise TaskError(
                    f"instance_id_changed: 连接阶段未固定首个远端实例 ID（当前实时实例 {live_instance_id}），"
                    "拒绝重签名提交 setup，请重新执行 gpu plan 审批"
                )
            if live_instance_id != fixed_connected_id:
                raise TaskError(
                    f"instance_id_changed: 首次冷启动后远端实例已变更（连接确认 {fixed_connected_id} vs 实时 {live_instance_id}），"
                    "拒绝用新实例重签名提交 setup，请重新执行 gpu plan 审批"
                )
        remote_digest = _compute_remote_setup_digest(
            instance_id=live_instance_id,
            session=live_health.get("session"),
            remote_root=live_health.get("remote_root") or config.REMOTE_ROOT,
            params=norm_params,
        )
        managed_rec = self.settings.get("managed_sessions").get(session_name)
        payload: Dict[str, Any] = {
            "task": "setup",
            "model_id": "",
            "params": norm_params,
            "approve_digest": remote_digest,
        }
        if managed_rec and managed_rec.get("deadline_at") is not None:
            payload["session_deadline_at"] = float(managed_rec["deadline_at"])
        body_bytes = comfy_assets.canonical_json(payload)
        task.log("⑤ 提交远端 ComfyUI 运行时安装（setup）…")
        # Re-check deadline & cancellation after serialization/logging, right before sending remote POST!
        self._check_not_expired_or_cancelled(session_name)
        status, _, raw = self.tunnel.request(
            "POST",
            "/api/jobs",
            body=body_bytes,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if status != 200:
            raise TaskError(f"远端 setup 提交失败 HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
        resp = json.loads(raw)
        if resp.get("job_id"):
            self.track_job(resp["job_id"])
            task.log("   已提交远端 setup 作业：%s" % resp["job_id"])

    def session_stop(self, name: Optional[str] = None, reason: Optional[str] = None, *,
                     stop_reason: str = "manual"):
        checked = config.require_managed_session_name(name or self.settings.get("session_name"))
        with self.lock:
            cur = self._existing_task("session_stop", checked)
            if cur:
                return cur
            return self.tasks.create(
                "session_stop", reason or f"释放会话 {checked}",
                self._session_stop, checked, reason, stop_reason, session_name=checked,
            )

    def _account_fingerprint(self):
        # Colab CLI owns its login. An explicit account label or credential can distinguish
        # multiple CLI profiles without storing the label or credential in state.
        account = self.engine_config.get("COLAB_ACCOUNT") or self.engine_config.get("COLAB_API_KEY")
        return credential_fingerprint(account) if account else ""

    def _queue_stop_billing(self, name, record, stopped_at, usage, *, balance_before):
        def amount(value):
            return value if _valid_amount(value) else None

        identity = _session_id(name, record["host"], record.get("allocated_at"))
        allocated = amount(record.get("allocated_at"))
        rate = amount(record.get("cu_rate"))
        if rate is None:
            rate = amount(usage.get("rate"))
        balance_after = amount(usage.get("balance")) if usage.get("cli_ok") else None
        billing = {
            "session_id": identity, "session_name": name,
            "account_fp": record.get("account_fp", self._account_fingerprint()),
            "ts": datetime.fromtimestamp(stopped_at, timezone.utc).isoformat(),
            "uptime_seconds": max(0, stopped_at - allocated) if allocated is not None else None,
            "cu_rate": rate,
            "balance_before": amount(balance_before) if balance_after is not None else None,
            "balance_after": balance_after,
        }

        def queue(data):
            if data["managed_sessions"].get(name, {}).get("host") != record["host"]:
                raise TaskError("Colab 本地会话归属已变化，无法核销")
            del data["managed_sessions"][name]
            data["pending_stop_billing"][identity] = billing

        self.settings.mutate(queue)
        return billing

    def _settle_stop_billing(self, billing=None):
        results = {}
        with self._billing_lock:
            pending = self.settings.get("pending_stop_billing")
            if billing is not None:
                session_id = billing["session_id"]
                pending = {session_id: pending[session_id]} if session_id in pending else {}
            for session_id, current in pending.items():
                if current["account_fp"] != self._account_fingerprint():
                    continue
                balance = current.get("balance_after")
                before = current.get("balance_before")
                needs_rate = current.get("cu_rate") is None and (before is None or balance is None or before < balance)
                if balance is None or needs_rate:
                    usage = self._fresh_billing_info()
                    updated_balance, updated_rate = usage.get("balance"), usage.get("rate")
                    changed = False
                    if balance is None and usage.get("cli_ok") and _valid_amount(updated_balance):
                        current["balance_after"] = updated_balance
                        current["ts"] = datetime.now(timezone.utc).isoformat()
                        changed = True
                    if needs_rate and _valid_amount(updated_rate):
                        current["cu_rate"] = updated_rate
                        changed = True
                    if changed:
                        self.settings.mutate(lambda data: data["pending_stop_billing"].update({session_id: current}))
                result = record_colab_stop(current, config=self.engine_config)
                if result["ledger_recorded"] and result["ledger_balance_recorded"]:
                    self.settings.mutate(lambda data: data["pending_stop_billing"].pop(session_id, None))
                results.update(result)
        return results

    def _session_stop(self, task, name: str, reason: Optional[str] = None, stop_reason: str = "manual"):
        self._mark_session_cancelled(name, reason or f"释放会话 {name}")
        before = self.colab.sessions()
        self._claim_live_placeholders(before)
        live, record = self._owned_session(name, before)
        if not record:
            return {"session": name, "status": "absent", **self._settle_stop_billing()}
        if record.get("account_fp", self._account_fingerprint()) != self._account_fingerprint():
            raise TaskError("Colab 当前账号配置与占位不符，请使用分配时的配置核对")
        if self.tunnel.session == name:
            self.tunnel.stop()
        task.set_progress(0.2)
        stopped_at = time.time()
        fresh = before
        if live:
            rc, out = self.colab.stream(["stop", "-s", name], task, timeout=120)
            stopped_at = time.time()
            for attempt in range(5):
                fresh = self.colab.sessions()
                if all(row.get("host") != live["host"] and row.get("name") != name for row in fresh):
                    break
                if attempt < 4:
                    time.sleep(1)
            else:
                raise TaskError(f"colab sessions 仍有会话 {name} (rc={rc})：{out[-400:]}")
        elif stop_reason == "hard_deadline" and record.get("deadline_at"):
            stopped_at = max(record.get("allocated_at", 0), record["deadline_at"])
        with self.lock:
            self._clear_job_tracking(name)
        usage = self._fresh_billing_info()
        billing = self._queue_stop_billing(
            name, record, stopped_at, usage,
            balance_before=record.get("balance_before") if live and len(before) == 1 and not fresh else None,
        )
        self.colab.kick()
        task.log(f"✓ 会话 {name} 已释放")
        return {"session": name, "billing": billing, **self._settle_stop_billing(billing)}

    def connect(
        self,
        name: Optional[str] = None,
        gpu=None,
        high_mem: Optional[bool] = None,
        connect: bool = True,
        expected_host: Optional[str] = None,
    ):
        raw = name or self.settings.get("session_name")
        checked = config.require_managed_session_name(raw)
        cards = config.gpu_candidates(gpu) if gpu is not None else ()
        if high_mem is not None and not isinstance(high_mem, bool):
            raise ValueError("high_mem 必须是布尔值")
        with self.lock:
            cur = self._existing_task("connect", checked)
            if cur:
                return cur
            self.touch()
            task = self.tasks.create(
                "connect",
                "连接 %s" % checked,
                self._connect_existing,
                checked,
                cards,
                connect,
                expected_host,
                session_name=checked,
            )
            return task

    def _connect_existing(
        self,
        task,
        name: str,
        cards,
        connect: bool,
        expected_host: Optional[str] = None,
    ):
        try:
            fresh = self.colab.sessions()
        except RuntimeError as e:
            raise TaskError(str(e)) from e
        st, _rec = self._owned_session(name, fresh)
        if not st:
            raise TaskError("会话 %s 不存在，请先启动会话" % name)
        if expected_host and st.get("host") != expected_host:
            raise TaskError(
                f"session_host_changed: 会话 {name} 远端 host 已从 {expected_host} 变为 {st.get('host')}，请重新 gpu plan"
            )
        self._clear_session_cancel(name)
        self._check_not_expired_or_cancelled(name)
        self.settings.update({"session_name": name})
        if connect:
            return self._connect(task, name)
        return {"session": st}

    def _connect(self, task, name: str, base: float = 0.0, *, allocation=None):
        def prog(p):
            task.set_progress(base + (1 - base) * p)

        local_ver = config.remote_version()
        if not (config.REMOTE_DIR / "forge_server.py").exists():
            raise TaskError("缺少远端代码：%s/forge_server.py" % config.REMOTE_DIR)
        try:
            expected_workflows = sorted(load_workflows(config.REMOTE_DIR / "workflows"))
        except (OSError, ValueError) as exc:
            raise TaskError(f"读取远端工作流清单失败：{exc}") from exc
        self._check_not_expired_or_cancelled(name)
        task.log("① 核验受管会话 %s …" % name)
        fresh = self.colab.sessions()
        st, _rec = self._owned_session(name, fresh)
        if not st:
            raise TaskError("会话 %s 不存在，请先执行 gpu up" % name)
        task.log("   %s | %s | %s" % (st["host"], st.get("hardware"), st.get("status")))
        prog(0.1)

        self.settings.mutate(lambda data: data["managed_sessions"][name].update(remote_used=True))
        self.tunnel.deploying = True
        try:
            self._check_not_expired_or_cancelled(name)
            task.log("② 建立 SSH 隧道（主连接） 127.0.0.1:%d → VM:%d …" % (self.tunnel.local_port, config.REMOTE_PORT))
            self.tunnel.start(name)
            if not self.tunnel.wait_up(45):
                raise TaskError("隧道建立失败：%s" % (self.tunnel.error or "超时"))
            self._check_not_expired_or_cancelled(name)
            task.log("   隧道已建立（本地端口 %d）" % self.tunnel.local_port)
            prog(0.3)

            def matches(health):
                return bool(
                    health
                    and health.get("ok")
                    and health.get("app") == config.APP_NAME
                    and health.get("remote_root") == config.REMOTE_ROOT
                    and health.get("version") == local_ver
                    and health.get("workflows") == expected_workflows
                    and health.get("session") == name
                )

            probe_state, health, probe_detail = self.tunnel.inspect_health()
            if probe_state == "foreign_app":
                raise TaskError(f"foreign_app: {probe_detail}，禁止上传冷部署以免覆盖异源服务")
            if probe_state == "ok" and matches(health):
                self._sync_connected_assets(name, health)
                self.tunnel._set_remote(health)
                task.log("✓ 已连接：v%s · %s" % (health.get("version"), health.get("gpu")))
                self.touch()
                return {
                    "version": health.get("version"),
                    "gpu": health.get("gpu"),
                    "instance_id": health.get("instance_id"),
                    "session": health.get("session"),
                    "remote_root": health.get("remote_root"),
                    "cold_started": False,
                }

            self._check_not_expired_or_cancelled(name)
            task.log("③ 打包并上传远端引擎 yc_h3/remote/ → %s …" % config.REMOTE_APP_DIR)
            t0 = time.time()
            if allocation is not None:
                allocation["bootstrap_started_at"] = t0
            rc, out, files = self.colab.upload_remote_bundle(name, str(config.REMOTE_DIR), config.REMOTE_APP_DIR)
            if rc != 0:
                raise TaskError("上传失败 (rc=%d)：%s" % (rc, out[-500:]))
            task.log("   已上传 %d 个文件（%.1fs）" % (len(files), time.time() - t0))
            prog(0.5)

            self._check_not_expired_or_cancelled(name)
            task.log("④ 清理旧受管进程并通过 bootstrap_remote.sh 冷部署启动 …")
            kill_script = build_kill_remote_script(config.REMOTE_ROOT)
            rc, out = self.colab.ssh_run(name, kill_script, timeout=30)
            if rc:
                raise TaskError("远端进程清理失败：%s" % out[-500:])
            self._check_not_expired_or_cancelled(name)
            start_cmd = build_start_remote_command(
                name,
                remote_root=config.REMOTE_ROOT,
                remote_app_dir=config.REMOTE_APP_DIR,
                remote_log=config.REMOTE_LOG,
                remote_port=config.REMOTE_PORT,
            )
            rc, out = self.colab.ssh_run(name, start_cmd, timeout=180)
            if rc != 0:
                raise TaskError("冷部署启动远端服务失败：%s" % out[-500:])

            self._check_not_expired_or_cancelled(name)
            self.sync_custom_assets(name)
            task.log("   已启动，等待健康检查（最多 60 秒）…")
            deadline = time.time() + 60
            health = None
            while time.time() < deadline:
                self._check_not_expired_or_cancelled(name)
                if not self.tunnel.desired:
                    raise TaskError("隧道已被关闭")
                probe_state, health, probe_detail = self.tunnel.inspect_health(timeout=3)
                if probe_state == "foreign_app":
                    raise TaskError(f"foreign_app: {probe_detail}")
                if probe_state == "ok" and matches(health):
                    break
                prog(0.55 + 0.4 * (1 - (deadline - time.time()) / 60))
                time.sleep(2)
            if not matches(health):
                rc, tail = self.colab.ssh_run(name, "tail -n 40 %s" % shlex.quote(config.REMOTE_LOG), timeout=40)
                task.log("   远端日志尾部：\n" + tail)
                raise TaskError("远端服务 60 秒内未通过身份健康检查，请查看日志")
            self._sync_connected_assets(name, health)
            self.tunnel._set_remote(health)
            task.log("✓ 已连接：v%s · %s" % (health.get("version"), health.get("gpu")))
            self.touch()
            return {
                "version": health.get("version"),
                "gpu": health.get("gpu"),
                "instance_id": health.get("instance_id"),
                "session": health.get("session"),
                "remote_root": health.get("remote_root"),
                "cold_started": True,
            }
        finally:
            self.tunnel.deploying = False

    # ------------------------------------------------------------ Custom LoRA Assets (Explicit Sync Only)
    def custom_assets(self) -> Dict[str, Any]:
        return comfy_assets.read_custom_assets(config.custom_assets_path(self.engine_config))

    def edit_custom_assets(self, body=None, asset_id: Optional[str] = None) -> Dict[str, Any]:
        with self._asset_lock:
            path = config.custom_assets_path(self.engine_config)
            table = self.custom_assets()
            if body is not None:
                if not isinstance(body, dict):
                    raise ValueError("自定义资产必须为 JSON 对象")
                asset = dict(body)
                local_file = asset.pop("local_file", None)
                asset.setdefault("source", "gdrive")
                key = asset.get("id")
                if not isinstance(key, str):
                    raise ValueError("缺少资产 id")
                entry = comfy_assets.normalize_custom_assets({key: asset})[key]
                if local_file is not None:
                    if not isinstance(local_file, str) or not local_file:
                        raise ValueError("local_file 必须是本地文件路径")
                    source = Path(local_file).expanduser()
                    if not source.is_file():
                        raise ValueError("local_file 必须是可读取的普通文件")
                    digest = sha256_file(source)
                    if entry["sha256"] and entry["sha256"] != digest:
                        raise ValueError("local_file 的 SHA-256 与登记值不一致")
                    entry["sha256"] = digest
                table[key] = entry
            else:
                if asset_id not in table:
                    raise LookupError("自定义资产不存在")
                del table[asset_id]
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, comfy_assets.canonical_json(table))
            tunnel, remote = self.tunnel.snapshot()
            session = tunnel.get("session")
            if remote.get("status") == "ok" and session:
                try:
                    self.sync_custom_assets(session)
                except (TaskError, OSError) as error:
                    self.notice("error", str(error))
                    raise
            return {"assets": table, "sha256": sha256_bytes(comfy_assets.canonical_json(table))}

    def sync_custom_assets(self, session: Optional[str] = None) -> Dict[str, Any]:
        with self._asset_lock:
            if session is None:
                tunnel, _ = self.tunnel.snapshot()
                session = tunnel.get("session")
            if not session:
                return {"synced": False, "reason": "no_session"}
            config.require_managed_session_name(session)
            fresh = self.colab.sessions()
            live, rec = self._owned_session(session, fresh)
            if live is None or rec is None:
                raise TaskError(f"unmanaged_session: 会话 {session} 不存在或不在受管账本中，拒绝 SSH 同步自定义资产")
            data = comfy_assets.canonical_json(self.custom_assets())
            directory = shlex.quote(f"{config.REMOTE_ROOT}/private")
            command = (
                f'set -eu; umask 077; dir={directory}; mkdir -p -- "$dir"; '
                """tmp=$(mktemp "$dir/.custom_assets.XXXXXXXX"); trap 'rm -f -- "$tmp"' EXIT; """
                'cat > "$tmp"; chmod 600 "$tmp"; mv -f -- "$tmp" "$dir/custom_assets.json"'
            )
            rc, output = self.colab.ssh_run(session, command, timeout=30, input_bytes=data)
            if rc:
                raise TaskError("自定义资产 SSH 同步失败（本地登记已保存）：%s" % output[-500:])
            self.touch()
            return {"synced": True, "sha256": sha256_bytes(data)}

    def _sync_connected_assets(self, session: str, health: Dict[str, Any]):
        with self._asset_lock:
            digest = sha256_bytes(comfy_assets.canonical_json(self.custom_assets()))
            if health.get("custom_assets_sha256") != digest:
                self.sync_custom_assets(session)

    # ------------------------------------------------------------ Strictly Read-Only Preflight & Gated Submission
    def _fresh_billing_info(self) -> Dict[str, Any]:
        """Query fresh Colab usage() billing facts for managed sessions instead of stale background cache."""
        try:
            usage_info, _ = self.colab.usage()
        except Exception:  # noqa: BLE001
            usage_info = None
        if not isinstance(usage_info, dict) or usage_info.get("cli_ok") is False:
            return {"balance": None, "rate": None, "cli_ok": False}
        return {
            "balance": usage_info.get("balance"),
            "rate": usage_info.get("rate"),
            "cli_ok": usage_info.get("cli_ok"),
        }

    def _build_billing_context(
        self, session: Optional[str], *, billing_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        tunnel_snap, _ = self.tunnel.snapshot()
        host = getattr(self.tunnel, "local_host", "127.0.0.1")
        port = tunnel_snap.get("local_port", getattr(self.tunnel, "local_port", config.TUNNEL_PORT))
        endpoint_origin = f"http://{host}:{port}"
        info = billing_info if billing_info is not None else self._fresh_billing_info()
        rate = info.get("rate")
        managed = self.settings.get("managed_sessions").get(session) if session else None
        vm_host = managed.get("host") if managed else None
        return remote_preflight.normalize_billing_context(
            {
                "provider_id": "colab-comfy",
                "endpoint_origin": endpoint_origin,
                "session_name": session,
                "vm_host": vm_host,
                "billing_rate": rate,
            }
        )

    def preflight_history(self, workflow: Optional[str] = None) -> List[Dict[str, Any]]:
        root = config.perf_dir(self.engine_config)
        return summarize_perf_history(root, workflow)

    def preflight(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Strictly read-only preflight: zero SSH writes, zero uploads, zero job enqueues.

        If local and remote ``custom_assets.json`` digests diverge, immediately returns
        ``(409, {"status": "block", ... code="custom_assets_out_of_sync"})`` without syncing.
        """
        if not isinstance(body, dict):
            raise ValueError("body 必须是 JSON 对象")
        body = dict(body)
        for_submission = body.pop("for_submission", False)
        with self._asset_lock:
            tunnel, _ = self.tunnel.snapshot()
            session = tunnel.get("session")
            health = self.tunnel.check_health()
            if not health:
                raise RemoteUnreachable("远端不可达")
            local_digest = sha256_bytes(comfy_assets.canonical_json(self.custom_assets()))
            remote_digest = health.get("custom_assets_sha256")
            if remote_digest is not None and remote_digest != local_digest:
                pack = {
                    "status": "block",
                    "code": "custom_assets_out_of_sync",
                    "reason": "本地与远端 custom_assets.json 不一致，请先显式执行 assets sync 或重新连接",
                    "instance_id": health.get("instance_id"),
                    "signals": [
                        {
                            "severity": "block",
                            "code": "custom_assets_out_of_sync",
                            "local_custom_assets_sha256": local_digest,
                            "remote_custom_assets_sha256": remote_digest,
                        }
                    ],
                    "warning_codes": [],
                    "custom_assets_sha256": local_digest,
                    "remote_custom_assets_sha256": remote_digest,
                }
                return 409, pack
            billing_info = self._fresh_billing_info()
            billing_context = self._build_billing_context(session, billing_info=billing_info)
            params = body.get("params")
            history = self.preflight_history(params.get("workflow") if isinstance(params, dict) else None)
            status, _, raw = self.tunnel.request(
                "POST",
                "/api/preflight",
                body=comfy_assets.canonical_json(
                    dict(body, local_history=history, billing_context=billing_context)
                ),
                headers={"Content-Type": "application/json"},
                timeout=90,
            )
            response = json.loads(raw)
            pack = response.get("detail") if status == 409 else response
            if status not in (200, 409) or not isinstance(pack, dict):
                return status, response
            pack["status"] = remote_preflight.normalize_preflight_status(pack.get("status"))
            if pack.get("instance_id") != health.get("instance_id") or pack.get("custom_assets_sha256") not in (
                None,
                local_digest,
            ):
                raise TaskError("远端会话或资产登记在预检期间发生变化，请重新预检")
            raw_remote_digest = pack.get("preflight_digest")
            # Guarantee preflight_digest ALWAYS binds billing_context even if remote omitted it.
            if raw_remote_digest and pack.get("billing_context") != billing_context:
                pack["remote_preflight_digest"] = raw_remote_digest
                pack["billing_context"] = billing_context
                pack["preflight_digest"] = sha256_bytes(
                    comfy_assets.canonical_json([raw_remote_digest, billing_context])
                )
            else:
                pack.setdefault("billing_context", billing_context)
                if raw_remote_digest:
                    pack["remote_preflight_digest"] = raw_remote_digest
        managed = self.settings.get("managed_sessions").get(session) or {}
        if health.get("version") == config.remote_version():
            pack["remote_version"] = health.get("version")
        if managed.get("host") == billing_context.get("vm_host"):
            pack["managed_instance_id"] = _session_id(session, managed.get("host"), managed.get("allocated_at"))
        pack["session"] = {
            "name": session,
            "balance": billing_info.get("balance"),
            "rate": billing_info.get("rate"),
            "idle_will_stop_in": self.idle_info()["will_stop_in"],
            "hard_deadline_in": self.idle_info()["hard_deadline_in"],
        }
        if for_submission and body.get("simulated_gpu") is None and pack["status"] in ("pass", "warn") and raw_remote_digest:
            # Only the live preflight produced by this gen may travel across its upload.
            token = uuid.uuid4().hex
            with self.lock:
                self._submission_preflight = (token, pack, local_digest)
            pack = dict(pack, submission_preflight=token)
        return (409 if pack["status"] == "block" else status), pack

    def submit_job(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Gated local job submission (Decision 3 = B).

        - Expired/cancelling managed session -> rejected (409), checked both at entry and right before POST.
        - ``block`` -> unconditionally rejected (409).
        - ``warn`` -> requires matching ``approve_digest`` / ``preflight_digest`` (409 if missing/stale).
        - ``pass`` on an approved active session -> submitted directly.
        """
        if not isinstance(body, dict):
            raise ValueError("body 必须是 JSON 对象")
        if body.get("task") == "setup":
            raise ValueError("setup 任务须经受管 GPU 启动流程提交")

        tunnel_snap, _ = self.tunnel.snapshot()
        active_session = tunnel_snap.get("session")
        approve_digest = body.get("approve_digest") or body.get("preflight_digest")
        ack = bool(body.get("ack_warnings") or body.get("approve_digest"))

        body = dict(body)
        token = body.pop("submission_preflight", None)
        if token:
            with self.lock:
                prepared = self._submission_preflight
                if prepared and token == prepared[0]:
                    self._submission_preflight = None
                else:
                    return 409, {"status": "warn", "approval_status": "stale"}
            _, facts, assets_digest = prepared
            health = self.tunnel.check_health() or {}
            if (not approve_digest or approve_digest != facts["preflight_digest"]
                    or active_session != facts["session"]["name"] or health.get("instance_id") != facts["instance_id"]):
                return 409, {"status": "warn", "approval_status": "stale"}
            if (sha256_bytes(comfy_assets.canonical_json(self.custom_assets())) != assets_digest
                    or health.get("custom_assets_sha256") not in (None, assets_digest)):
                return 409, {"status": "block", "code": "custom_assets_out_of_sync"}
        else:
            # Direct daemon callers and crash recovery always get a fresh live evaluation.
            check_body = {k: v for k, v in body.items()
                          if k not in ("ack_warnings", "preflight_digest", "approve_digest", "simulated_gpu", "for_submission")}
            pf_status, facts = self.preflight(check_body)
            if pf_status not in (200, 409) or not isinstance(facts, dict):
                return pf_status, facts

        norm_status = remote_preflight.normalize_preflight_status(facts.get("status"))
        facts["status"] = norm_status
        if norm_status == "block":
            return 409, facts

        billing_ctx = facts.get("billing_context") if isinstance(facts.get("billing_context"), dict) else {}
        if (
            active_session
            and config.is_managed_session_name(active_session)
            and billing_ctx.get("billing_rate") in (None, "", "unknown")
        ):
            return 409, dict(
                facts,
                status="warn",
                approval_status="stale",
                code="billing_rate_unknown",
                detail="无法核验受管 Colab 会话实时费率（billing_rate=unknown），拒绝向远端入队",
            )

        expected_digest = facts.get("preflight_digest")
        if norm_status == "warn":
            if not (ack and approve_digest and approve_digest == expected_digest):
                return 409, dict(
                    facts,
                    approval_status="missing" if not approve_digest else "stale",
                )
        elif approve_digest and approve_digest != expected_digest:
            return 409, dict(facts, status="warn", approval_status="stale")

        params = body.get("params")
        history = self.preflight_history(params.get("workflow") if isinstance(params, dict) else None)
        forward = dict(body)
        forward.pop("approve_digest", None)
        forward.pop("simulated_gpu", None)
        forward["local_history"] = history
        forward["billing_context"] = facts.get("billing_context") or self._build_billing_context(active_session)
        managed_rec = self.settings.get("managed_sessions").get(active_session) if active_session else None
        if managed_rec and managed_rec.get("deadline_at") is not None:
            forward["session_deadline_at"] = float(managed_rec["deadline_at"])
        remote_digest = facts.get("remote_preflight_digest") or expected_digest
        if remote_digest:
            forward["preflight_digest"] = remote_digest
        if norm_status == "warn":
            forward["ack_warnings"] = True
        payload_bytes = comfy_assets.canonical_json(forward)

        expired_reason = self._session_expired_or_cancelled_reason(active_session)
        if expired_reason:
            return 409, {"status": "block", "code": "session_deadline_expired", "detail": expired_reason}

        st, _, raw = self.tunnel.request(
            "POST",
            "/api/jobs",
            body=payload_bytes,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        data = json.loads(raw)
        if 200 <= st < 300 and isinstance(data, dict) and data.get("job_id"):
            self.track_job(data["job_id"])
            self.touch()
        return st, data

    # ------------------------------------------------------------ Controlled Remote Queries / Actions
    def list_remote_jobs(self, limit: int = 50):
        return self.tunnel.get_json(f"/api/jobs?limit={int(limit)}", timeout=20)

    def get_remote_job(self, jid: str):
        self._validate_job_id(jid)
        return self.tunnel.get_json(f"/api/jobs/{jid}", timeout=20)

    def cancel_remote_job(self, jid: str):
        self._validate_job_id(jid)
        st, _, raw = self.tunnel.request("POST", f"/api/jobs/{jid}/cancel", timeout=30)
        if st != 200:
            raise TaskError(f"取消任务失败 HTTP {st}: {raw[:200].decode('utf-8', 'replace')}")
        return json.loads(raw)

    def list_remote_workflows(self):
        return self.tunnel.get_json("/api/workflows", timeout=20)

    def upload(self, filename: str, data: bytes) -> Dict[str, Any]:
        fname = _safe_filename(filename, "upload.png")
        self.touch()
        res = self.tunnel.upload_bytes(fname, data)
        return {"remote_path": res.get("path")}

    # ------------------------------------------------------------ Performance Archival
    @staticmethod
    def _validate_job_id(jid: str):
        if not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", jid):
            raise ValueError("无效 job_id")

    def _clear_job_tracking(self, session: str):
        self._tracked_jobs.pop(session, None)
        self._perf_unavailable = {key for key in self._perf_unavailable if key[0] != session}

    def _jobs_for_session(self, session, instance_id):
        previous_id, tracked = self._tracked_jobs.get(session, (None, None))
        if tracked is None or (instance_id and previous_id and instance_id != previous_id):
            self._clear_job_tracking(session)
            tracked = set()
        self._tracked_jobs[session] = (instance_id or previous_id, tracked)
        return tracked

    def track_job(self, jid: str, session: Optional[str] = None):
        self._validate_job_id(jid)
        with self.lock:
            tunnel, remote = self.tunnel.snapshot()
            active_session = tunnel.get("session")
            if session is None:
                session = active_session
            if session == active_session and tunnel.get("status") == "down":
                return
            instance_id = remote.get("instance_id") if session == active_session else None
            self._jobs_for_session(session, instance_id).add(jid)

    def _archive_file(self, path: Path, endpoint: str, verify, confirm_job) -> bool:
        if path.is_file():
            try:
                verify(path.read_bytes())
                return True
            except ValueError:
                pass
        status, _, data = self.tunnel.request("GET", endpoint, timeout=30)
        if status == 404:
            return False
        if status != 200:
            raise TaskError(f"性能文件返回 HTTP {status}")
        confirm_job()
        verify(data)
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with atomic_writer(path, temp=temp) as stream:
            stream.write(data)
        return True

    def archive_job_perf(self, jid: str) -> Dict[str, Any]:
        self._validate_job_id(jid)
        with self.lock:
            lock = self._perf_locks.setdefault(jid, threading.Lock())
        with lock:
            with self.lock:
                tunnel, remote = self.tunnel.snapshot()
                tracked = self._jobs_for_session(tunnel.get("session"), remote.get("instance_id"))
            root = config.perf_dir(self.engine_config) / jid
            root.mkdir(parents=True, exist_ok=True)
            route = f"/api/jobs/{jid}"

            def confirm_job():
                with self.lock:
                    tracked.add(jid)

            def verify_perf(data):
                value = json.loads(data)
                if not isinstance(value, dict) or value.get("job_id") != jid or value.get("status") not in (
                    "done",
                    "error",
                    "cancelled",
                    "cancelled_in_queue",
                ):
                    raise ValueError("perf.json 与任务不匹配")

            def verify_telemetry(data):
                for line in data.splitlines():
                    row = json.loads(line)
                    if not isinstance(row, dict) or "ts" not in row:
                        raise ValueError("telemetry.jsonl 采样无效")

            perf = root / "perf.json"
            telemetry = root / "telemetry.jsonl"
            try:
                if not self._archive_file(perf, route + "/perf", verify_perf, confirm_job):
                    status, _, data = self.tunnel.request("GET", route, timeout=15)
                    if status == 404:
                        with self.lock:
                            tracked.discard(jid)
                    if status != 200:
                        raise TaskError(f"无法查询任务状态（HTTP {status}）")
                    job = json.loads(data)
                    confirm_job()
                    if job.get("status") not in ("done", "error", "cancelled"):
                        return {"status": "pending"}
                    if job.get("perf_available"):
                        raise TaskError("任务已发布，但缺少 perf.json")
                    return {"status": "missing", "reason": "该任务没有性能记录"}
                if not self._archive_file(telemetry, route + "/telemetry", verify_telemetry, confirm_job):
                    raise TaskError("缺少 telemetry.jsonl")
                record = json.loads(perf.read_bytes())
                if record.get("provider") != "colab-comfy" or not record.get("archived_at"):
                    record.update(provider="colab-comfy", archived_at=datetime.now(timezone.utc).isoformat())
                    atomic_write_bytes(perf, comfy_assets.canonical_json(record))
                result = {
                    "status": "archived",
                    "perf_json": str(perf),
                    "telemetry_jsonl": str(telemetry),
                    "cold_start": record.get("cold_start"),
                    "total_s": (record.get("phases_s") or {}).get("total"),
                    "inference_total_s": (record.get("phases_s") or {}).get("inference_total"),
                    "peak_vram_mb": _perf_peak(record),
                    "comfy_partial_load": (record.get("markers") or {}).get("comfy_partial_load"),
                }
                with self.lock:
                    self._archived_jobs.add((str(root.parent), jid))
                return result
            except (OSError, ValueError, TaskError, RemoteUnreachable) as error:
                self.notice("error", f"{jid} 性能归档失败：{error}")
                raise

    def _try_archive_perf(self, jid: str):
        try:
            return self.archive_job_perf(jid)
        except (OSError, ValueError, TaskError, RemoteUnreachable):
            return None

    # ------------------------------------------------------------ Background Jobs Poll
    def _jobs_loop(self):
        while True:
            time.sleep(self.JOBS_POLL)
            try:
                self._poll_jobs()
            except Exception:  # noqa: BLE001
                pass

    def _poll_jobs(self):
        with self.lock:
            tunnel, remote = self.tunnel.snapshot()
            if remote["status"] != "ok":
                return
            session = tunnel.get("session")
            tracked = self._jobs_for_session(session, remote.get("instance_id"))
        try:
            jobs = self.tunnel.get_json("/api/jobs?limit=200", timeout=15)
        except (RemoteUnreachable, ValueError):
            return
        if not isinstance(jobs, list):
            return
        visible = {job["id"]: job for job in jobs if isinstance(job, dict) and isinstance(job.get("id"), str)}
        perf_root = config.perf_dir(self.engine_config)
        with self.lock:
            if self._tracked_jobs.get(session, (None, None))[1] is not tracked:
                return
            self.remote_jobs, self.remote_jobs_at = jobs, time.time()
            tracked.update(visible)
            to_check = {jid for jid in tracked if (session, jid) not in self._perf_unavailable}
        if any(j.get("status") in ("queued", "running") for j in jobs if isinstance(j, dict)):
            self.touch()
        for jid in sorted(to_check):
            if (str(perf_root), jid) in self._archived_jobs and all(
                (perf_root / jid / name).is_file() for name in ("perf.json", "telemetry.jsonl")
            ):
                continue
            job = visible.get(jid)
            if job is None:
                try:
                    status, _, data = self.tunnel.request("GET", f"/api/jobs/{jid}", timeout=5)
                    if status == 404:
                        with self.lock:
                            tracked.discard(jid)
                        continue
                    if status != 200:
                        raise TaskError(f"HTTP {status}")
                    job = json.loads(data)
                except (RemoteUnreachable, ValueError, TaskError) as error:
                    self.notice("error", f"{jid} 无法补查待归档任务：{error}")
                    continue
                if not isinstance(job, dict):
                    continue
            # Never auto-archive perf for `done` jobs before local artifact delivery is confirmed.
            # Explicit backfill remains available via archive_job_perf() / perf backfill.
            if job.get("status") == "done" and not self._is_job_delivered(jid):
                continue
            if job.get("status") in ("done", "error", "cancelled"):
                result = self._try_archive_perf(jid)
                if result and result["status"] == "missing":
                    with self.lock:
                        if self._tracked_jobs.get(session, (None, None))[1] is tracked:
                            self._perf_unavailable.add((session, jid))

    # ------------------------------------------------------------ Outer Hard Deadline + Idle Watchdog
    def _watchdog_loop(self):
        while True:
            try:
                self._watchdog_check()
            except Exception as e:  # noqa: BLE001
                self.notice("error", "看门狗异常：%s" % e)
            time.sleep(5)

    def _remote_is_busy(self, name: str) -> bool:
        tunnel, remote = self.tunnel.snapshot()
        if tunnel.get("session") == name and remote.get("status") == "ok":
            try:
                jobs = self.tunnel.get_json("/api/jobs?limit=20", timeout=10)
                if isinstance(jobs, list):
                    return any(job.get("status") in ("queued", "running") for job in jobs if isinstance(job, dict))
            except (RemoteUnreachable, ValueError):
                pass
        if not self.settings.get("managed_sessions").get(name, {}).get("remote_used"):
            return False
        rc, out = self.colab.ssh_run(
            name, f"curl -fsS -m 5 'http://127.0.0.1:{config.REMOTE_PORT}/api/jobs?limit=20'", timeout=10,
        )
        if rc != 0:
            return True
        try:
            jobs = json.loads(out)
        except ValueError:
            return True
        return not isinstance(jobs, list) or any(
            not isinstance(job, dict) or job.get("status") in ("queued", "running") for job in jobs
        )

    def _watchdog_check(self, now: Optional[float] = None):
        now = time.time() if now is None else float(now)
        managed = self.settings.get("managed_sessions")
        if not managed:
            self._settle_stop_billing()
            self.touch()
            return
        fresh = self.colab.sessions()
        self._claim_live_placeholders(fresh)
        managed = self.settings.get("managed_sessions")
        for name, record in managed.items():
            if record.get("account_fp", self._account_fingerprint()) != self._account_fingerprint():
                continue

            # A deadline takes priority over deployment, idle and pending deliveries.
            deadline_at = record.get("deadline_at")
            if deadline_at is not None and now >= float(deadline_at):
                self._mark_session_cancelled(name, f"会话 {name} 已到硬时限")
                if not record.get("host"):
                    continue
                if self.auto_stop_task and self.auto_stop_task.status == "running":
                    return
                self.notice("warning", f"受管会话 {name} 已到硬时限，强制释放")
                self.auto_stop_task = self.session_stop(
                    name, reason=f"硬时限到期强制释放 {name}", stop_reason="hard_deadline",
                )
                self.touch()
                return
            if not record.get("host"):
                continue
            if not any(row["name"] == name and row["host"] == record["host"] for row in fresh):
                # Only a verified missing endpoint is eligible for stop settlement.
                live, _ = self._owned_session(name, fresh)
                if live is None:
                    self.auto_stop_task = self.session_stop(name, stop_reason="reconcile_cleared")
                continue

            # 2. Within hard deadline: protect active local tasks, in-flight remote jobs, and unclaimed deliverables.
            if any(t.type in ("connect", "session_start", "session_stop") for t in self.tasks.running()):
                self.touch()
                continue
            if self.has_pending_deliveries(name, record.get("host")):
                self.touch()
                continue
            mins = self.settings.get("idle_minutes")
            if not mins:
                continue
            if now - self.last_activity < mins * 60:
                continue
            if self.auto_stop_task and self.auto_stop_task.status == "running":
                return
            if self._remote_is_busy(name):
                self.touch()
                continue
            msg = "空闲已超过 %d 分钟，已自动释放会话 %s 以节省算力" % (mins, name)
            self.notice("warning", msg)
            self.auto_stop_task = self.session_stop(
                name, reason="空闲自动释放 %s" % name, stop_reason="idle_watchdog",
            )
            self.touch()
            return
