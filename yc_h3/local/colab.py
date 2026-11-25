"""google-colab-cli wrapper, usage/session parsing, SSH ControlMaster helpers, and remote bundle packer.

Adapted from WorldForge (MIT License); personal Drive OAuth token sync removed.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from yc_h3.remote_bundle import pack_remote_tar

from . import config
from .platform_guard import require_colab_platform

LINE_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s+(?P<host>\S+)\s*\|(?P<rest>.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s: Optional[str]) -> str:
    return ANSI_RE.sub("", s or "")


def parse_session_lines(text: str) -> List[dict]:
    """Parse ``[name] host | Hardware: G4 | Shape: ... | Status: IDLE`` lines."""
    out = []
    for raw in strip_ansi(text).splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        info = {"name": m.group("name"), "host": m.group("host"), "hardware": None, "status": None}
        for part in m.group("rest").split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                k = k.strip().lower()
                v = v.strip()
                if k in ("hardware", "shape", "variant", "status"):
                    info[k] = v
        out.append(info)
    return out


def run(args, timeout: float = 60, input_bytes: Optional[bytes] = None) -> Tuple[int, str]:
    require_colab_platform()
    text_options = {"encoding": "utf-8", "errors": "replace"} if input_bytes is None else {}
    try:
        p = subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            stdin=None if input_bytes is not None else subprocess.DEVNULL,
            **text_options,
        )
        output = p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else p.stdout
        return p.returncode, strip_ansi(output)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else e.stdout or ""
        return 124, strip_ansi(out) + "\n[超时 %ss]" % timeout
    except OSError as e:
        return 127, str(e)


class Colab:
    REFRESH = 30
    USAGE_RETRIES = 1
    USAGE_RETRY_DELAY_S = 0.5
    USAGE_TOTAL_TIMEOUT_S = 40.0
    USAGE_FIRST_TIMEOUT_S = 19.0

    def __init__(self, settings):
        require_colab_platform()
        self.settings = settings
        self.lock = threading.Lock()
        self.state = {
            "cli_ok": None,
            "balance": None,
            "rate": None,
            "sessions": [],
            "updated": None,
            "error": None,
        }
        self._kick = threading.Event()
        self._first = threading.Event()
        self._listeners = []

    def start(self):
        threading.Thread(target=self._loop, name="colab-refresh", daemon=True).start()

    def on_refresh(self, fn):
        self._listeners.append(fn)

    def kick(self):
        self._kick.set()

    def wait_first(self, timeout=None):
        return self._first.wait(timeout)

    def snapshot(self):
        with self.lock:
            d = dict(self.state)
            d["sessions"] = [dict(s) for s in self.state["sessions"]]
            return d

    def cached_session(self, name):
        with self.lock:
            for s in self.state["sessions"]:
                if s["name"] == name:
                    return dict(s)
        return None

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.state["error"] = self.settings.engine_config.redact("刷新失败: %s" % e)
            self._first.set()
            for fn in list(self._listeners):
                try:
                    fn(self.snapshot())
                except Exception:  # noqa: BLE001
                    pass
            self._kick.wait(self.REFRESH)
            self._kick.clear()

    def refresh(self):
        colab = config.colab_bin(self.settings.engine_config)
        if not os.path.exists(colab):
            with self.lock:
                self.state.update(
                    cli_ok=False,
                    error="未找到 colab CLI（google-colab-cli）",
                    updated=time.time(),
                )
            return
        usage, out = self.usage()
        balance, rate, cli_ok, error = (usage[k] for k in ("balance", "rate", "cli_ok", "error"))

        sessions = None
        rc2, out2 = run([config.colab_bin(self.settings.engine_config), "sessions"], timeout=40)
        if rc2 == 0:
            sessions = parse_session_lines(out2)
        elif cli_ok:
            error = self.settings.engine_config.redact("colab sessions 失败: " + out2.strip()[-400:])
        if sessions is None:
            sessions = parse_session_lines(out)
        for s in sessions:
            if s["name"] == "?":
                continue
            try:
                st = self.status(s["name"])
            except RuntimeError:
                st = None
            if st and st.get("status"):
                s["status"] = st["status"]
        with self.lock:
            self.state.update(
                cli_ok=cli_ok,
                balance=balance,
                rate=rate,
                sessions=sessions if (rc2 == 0 or sessions) else self.state["sessions"],
                updated=time.time(),
                error=error,
            )

    def _usage_once(self, *, timeout: float = 40.0):
        rc, out = run([config.colab_bin(self.settings.engine_config), "usage"], timeout=max(0.1, float(timeout)))
        values = {}
        for key in ("balance", "rate"):
            match = re.search(rf"{key}:\s*([\d.,]+)", out, re.I)
            values[key] = float(match.group(1).replace(",", "")) if match else None
        values["cli_ok"] = rc == 0 and values["balance"] is not None
        if not values["cli_ok"]:
            values["error"] = "colab usage 失败: " + out.strip()[-400:]
        elif values["rate"] is None:
            values["error"] = "colab usage 缺少 rate 字段"
        else:
            values["error"] = None
        return rc, values, out

    def usage(self):
        total_budget = max(0.2, float(self.USAGE_TOTAL_TIMEOUT_S))
        deadline = time.monotonic() + total_budget
        first_timeout = min(float(self.USAGE_FIRST_TIMEOUT_S), total_budget)
        rc, values, out = self._usage_once(timeout=first_timeout)
        if (values["cli_ok"] and values["rate"] is not None) or rc == 127:
            return values, out
        best_values, best_out = values, out
        for _ in range(self.USAGE_RETRIES):
            remaining = deadline - time.monotonic()
            delay = min(max(0.0, float(self.USAGE_RETRY_DELAY_S)), max(0.0, remaining))
            if remaining - delay <= 0.1:
                break
            if delay > 0:
                time.sleep(delay)
            retry_timeout = deadline - time.monotonic()
            if retry_timeout <= 0.1:
                break
            rc, values, out = self._usage_once(timeout=retry_timeout)
            if values["cli_ok"] and values["rate"] is not None:
                return values, out
            if values["cli_ok"] or not best_values["cli_ok"]:
                best_values, best_out = values, out
            if rc == 127:
                break
        return best_values, best_out

    def status(self, name):
        rc, out = run([config.colab_bin(self.settings.engine_config), "status", "-s", name], timeout=40)
        for s in parse_session_lines(out):
            if s["name"] == name:
                return s
        if "not found" in out.lower():
            return None
        if rc != 0:
            raise RuntimeError(out.strip()[-400:] or "colab status 失败")
        return None

    def session_exists_fresh(self, name):
        try:
            return any(s["name"] == name for s in self.sessions())
        except RuntimeError:
            return None

    def sessions(self):
        rc, out = run([config.colab_bin(self.settings.engine_config), "sessions"], timeout=40)
        sessions = parse_session_lines(out)
        low = out.lower()
        auth_failed = any(
            message in low
            for message in (
                "login required",
                "not logged in",
                "authenticat",
                "please log in",
                "no valid default credentials",
            )
        )
        if rc or auth_failed or (not sessions and "no active sessions" not in low):
            raise RuntimeError(out.strip()[-400:] or "无法确认 colab sessions")
        return sessions

    def stream(self, args, task, timeout=900):
        cmd = [config.colab_bin(self.settings.engine_config)] + list(args)
        task.log("$ colab " + " ".join(shlex.quote(a) for a in args))
        require_colab_platform()
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        buf = []
        try:
            for raw in p.stdout:
                line = self.settings.engine_config.redact(strip_ansi(raw).rstrip())
                if line:
                    buf.append(line)
                    task.log("  " + line)
            p.wait()
        finally:
            timer.cancel()
            p.stdout.close()
        return p.returncode, "\n".join(buf)

    # ---------- SSH ControlMaster ----------
    def ctl_path(self, session: str) -> str:
        require_colab_platform()
        session = config.require_managed_session_name(session)
        uid = os.getuid()
        directory = Path(tempfile.gettempdir()) / f"yh3-ssh-{uid}"
        identity = f"{self.settings.engine_config.data_dir_hash}:{session}"
        leaf = "ctl-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20] + ".sock"
        if len(os.fsencode(directory / leaf)) >= 104:
            directory = Path("/tmp") / f"yh3-ssh-{uid}"
        return str(directory / leaf)

    def prepare_control_directory(self, session: str) -> None:
        directory = Path(self.ctl_path(session)).parent
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("SSH 控制套接字目录权限无效")


    @staticmethod
    def host(session: str) -> str:
        return "root@colab-" + session

    def ssh_opts(self, session: str) -> List[str]:
        proxy = "%s ssh --proxy-mode -s %s" % (shlex.quote(config.colab_bin(self.settings.engine_config)), shlex.quote(session))
        return [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=40",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ProxyCommand=" + proxy,
        ]

    def master_cmd(self, session: str, local_port: int, remote_port: int) -> List[str]:
        return (
            ["ssh", "-M", "-S", self.ctl_path(session), "-o", "ControlPersist=no"]
            + self.ssh_opts(session)
            + ["-N", "-L", "127.0.0.1:%d:127.0.0.1:%d" % (local_port, remote_port), self.host(session)]
        )

    def mux_cmd(self, session: str, remote_cmd: str) -> List[str]:
        return [
            "ssh",
            "-S", self.ctl_path(session),
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",
            "-o", "LogLevel=ERROR",
            self.host(session),
            remote_cmd,
        ]

    def ssh_run(self, session: str, remote_cmd: str, timeout: int = 120, input_bytes: Optional[bytes] = None):
        if not os.path.exists(self.ctl_path(session)):
            return 255, "SSH 主连接未建立（请先连接）"
        return run(self.mux_cmd(session, remote_cmd), timeout=timeout, input_bytes=input_bytes)

    @staticmethod
    def _validate_remote_target_dir(remote_dir: Optional[str]) -> str:
        """Validate remote app target directory before staging replacement."""
        raw = config.REMOTE_APP_DIR if remote_dir is None else str(remote_dir)
        cleaned = raw.strip()
        if not cleaned:
            raise ValueError("remote_dir 不能为空")
        if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
            raise ValueError(f"remote_dir 包含非法控制字符：{remote_dir!r}")
        if not cleaned.startswith("/"):
            raise ValueError(f"remote_dir 必须为绝对路径：{remote_dir!r}")
        raw_parts = [part for part in cleaned.split("/") if part]
        if ".." in raw_parts or "." in raw_parts:
            raise ValueError(f"remote_dir 不允许包含相对路径段：{remote_dir!r}")
        norm = posixpath.normpath(cleaned)
        parts = [part for part in norm.split("/") if part]
        if norm == "/" or len(parts) < 2 or posixpath.basename(norm) in ("", ".", ".."):
            raise ValueError(f"remote_dir 不能为根目录或顶层目录：{remote_dir!r}")
        if norm != config.REMOTE_APP_DIR:
            raise ValueError(f"部署目标必须是引擎代码目录 {config.REMOTE_APP_DIR}")
        return norm

    @classmethod
    def _build_upload_remote_cmd(cls, target_dir: str) -> str:
        q = shlex.quote(target_dir)
        return (
            "set -eu; "
            f"target={q}; "
            'parent=$(dirname -- "$target"); '
            'base=$(basename -- "$target"); '
            '[ -n "$target" ] && [ "$target" != "/" ] && [ -n "$base" ] && [ "$base" != "." ] && [ "$base" != ".." ]; '
            'mkdir -p -- "$parent"; '
            'staging="${target}.staging.$$"; '
            'backup="${target}.old.$$"; '
            'rm -rf -- "$staging" "$backup"; '
            'mkdir -p -- "$staging"; '
            'if ! tar -xzf - -C "$staging" --no-same-owner; then '
            'rm -rf -- "$staging"; exit 1; '
            "fi; "
            'if [ -e "$target" ] || [ -L "$target" ]; then '
            'mv -- "$target" "$backup"; '
            "fi; "
            'if ! mv -- "$staging" "$target"; then '
            'if [ -e "$backup" ] || [ -L "$backup" ]; then mv -- "$backup" "$target" || true; fi; '
            'rm -rf -- "$staging"; exit 1; '
            "fi; "
            'rm -rf -- "$backup"; '
            "echo __UPLOAD_OK__"
        )

    def upload_remote_bundle(
        self,
        session: str,
        local_dir: Optional[str | os.PathLike[str]] = None,
        remote_dir: Optional[str] = None,
        timeout: int = 300,
    ):
        """Pack ``yc_h3/remote/`` via ``pack_remote_tar`` and replace ``remote_dir`` via sibling staging."""
        target_dir = self._validate_remote_target_dir(remote_dir)
        payload = pack_remote_tar(local_dir)
        cmd = self._build_upload_remote_cmd(target_dir)
        rc, out = self.ssh_run(session, cmd, timeout=timeout, input_bytes=payload)
        if rc == 0 and "__UPLOAD_OK__" not in out:
            rc = 1
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            files = [m.name for m in tar.getmembers() if m.isfile()]
        return rc, out.replace("__UPLOAD_OK__", "").strip(), files
