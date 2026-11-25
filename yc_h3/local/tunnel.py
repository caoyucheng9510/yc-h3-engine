"""Single SSH ControlMaster tunnel, loopback enforcement, and /api/health identity verification.

Adapted from WorldForge (MIT License).
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional

from . import config


def port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe_remote_health_detailed(
    port: int, timeout: float = 1.5, host: str = "127.0.0.1"
) -> tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Query ``GET /api/health`` on a loopback host/port.

    Returns ``(state, data, detail)`` where ``state`` is:
    - ``"ok"``: HTTP 200 valid JSON dict with ``ok=True``
    - ``"foreign_response"``: port responded over HTTP, but not a valid ``ok=True`` health JSON
    - ``"unreachable"``: connection refused / timed out / no server listening
    """
    config.validate_loopback_host(host)
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/api/health")
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            return "foreign_response", None, f"端口返回 HTTP {r.status}"
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return "foreign_response", None, "端口返回非 JSON 响应"
        if isinstance(data, dict) and data.get("ok"):
            return "ok", data, None
        return "foreign_response", data if isinstance(data, dict) else None, "端口响应缺少 ok=True"
    except (OSError, http.client.HTTPException) as exc:
        return "unreachable", None, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class RemoteUnreachable(Exception):
    pass


class Tunnel:
    HEALTH_EVERY = 5

    def __init__(self, colab):
        self.colab = colab
        self.lock = threading.RLock()
        self.proc = None
        self.desired = False
        self.session: Optional[str] = None
        self.local_host = "127.0.0.1"
        self.local_port = config.TUNNEL_PORT
        self.status = "down"  # down | connecting | up | error
        self.since = None
        self.error = None
        self.started_at = None
        self.fail_count = 0
        self.next_retry = 0
        self.remote = {
            "status": "unreachable",
            "app": None,
            "remote_root": None,
            "version": None,
            "gpu": None,
            "instance_id": None,
            "checked": None,
        }
        self.deploying = False
        self.errf = None
        self._wake = threading.Event()

    def _session_is_owned(self, session: Optional[str]) -> bool:
        if not config.is_managed_session_name(session):
            return False
        managed = self.colab.settings.get("managed_sessions")
        return bool(managed.get(session, {}).get("host"))

    def _matches(self, health: Optional[Dict[str, Any]], session: Optional[str]) -> bool:
        return bool(
            self._session_is_owned(session)
            and isinstance(health, dict)
            and health.get("ok")
            and health.get("app") == config.APP_NAME
            and health.get("remote_root") == config.REMOTE_ROOT
            and health.get("session") == session
        )

    # ---------- Public API ----------
    def start_threads(self):
        threading.Thread(target=self._monitor, name="tunnel-monitor", daemon=True).start()
        threading.Thread(target=self._health_loop, name="remote-health", daemon=True).start()

    def start(self, session: str):
        """Start (or switch to) SSH tunnel for a verified managed yh3-* session."""
        session = config.require_managed_session_name(session)
        if not self._session_is_owned(session):
            raise ValueError(f"拒绝为未托管会话建立隧道：{session}")
        with self.lock:
            if self.desired and self.session == session and self._alive():
                return
            self._kill()
            self._set_remote()
            self.desired = True
            self.session = session
            self.fail_count = 0
            self.next_retry = 0
            self.error = None
            self._spawn()
        self._wake.set()

    def stop(self):
        with self.lock:
            self.desired = False
            self._kill()
            self._set_status("down")
            self.error = None
            self._set_remote()

    def snapshot(self):
        with self.lock:
            t = {
                "status": self.status,
                "local_port": self.local_port,
                "session": self.session,
                "since": self.since,
            }
            if self.error:
                t["error"] = self.error
            r = dict(self.remote)
            if self.deploying and r["status"] != "ok":
                r["status"] = "deploying"
            return t, r

    def wait_up(self, timeout: float = 30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.status == "up":
                    return True
                if self.status == "error" and not self.desired:
                    return False
                if self.proc is None and not self.desired:
                    return False
            time.sleep(0.3)
        return False

    # ---------- Process management ----------
    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _set_status(self, st: str):
        if st != self.status:
            self.status = st
            self.since = time.time()

    def _kill(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(3)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _spawn(self):
        port = next((candidate for candidate in range(config.TUNNEL_PORT, config.TUNNEL_PORT + 20) if port_free(candidate)), None)
        if port is None:
            self.error = "没有可用的本地隧道端口"
            self.next_retry = time.time() + 10
            self._set_status("error")
            return
        self.local_host = "127.0.0.1"
        self.local_port = port
        ctl = self.colab.ctl_path(self.session)
        try:
            self.colab.prepare_control_directory(self.session)
            if os.path.lexists(ctl):
                os.unlink(ctl)
            args = self.colab.master_cmd(self.session, port, config.REMOTE_PORT)
            self.errf = tempfile.TemporaryFile()
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self.errf,
                start_new_session=True,
            )
        except OSError as e:
            self.proc = None
            self.error = "无法启动 ssh: %s" % e
            self._set_status("error")
            return
        self.started_at = time.time()
        self._set_status("connecting")

    def _monitor(self):
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            restart_session = None
            with self.lock:
                if not self.desired:
                    continue
                if self._alive():
                    if self.status != "up" and os.path.exists(self.colab.ctl_path(self.session)):
                        self._set_status("up")
                        self.error = None
                        self.fail_count = 0
                    continue
                if self.proc is not None:
                    err = b""
                    try:
                        self.errf.seek(0)
                        err = self.errf.read()[-2000:]
                        self.errf.close()
                    except Exception:  # noqa: BLE001
                        pass
                    msg = err.decode("utf-8", "replace").strip()[-300:]
                    self.error = "隧道断开" + ("：" + msg if msg else "")
                    self.proc = None
                    self.fail_count += 1
                    self.next_retry = time.time() + min(30, 2 ** min(self.fail_count, 5))
                    self._set_status("connecting" if self.fail_count < 8 else "error")
                    self._set_remote()
                if time.time() >= self.next_retry:
                    restart_session = self.session
            if restart_session:
                exists = self.colab.session_exists_fresh(restart_session)
                with self.lock:
                    if not self.desired or self.session != restart_session or self._alive():
                        continue
                    if exists is False:
                        self.desired = False
                        self.error = "会话 %s 已不存在，隧道已停止" % restart_session
                        self._set_status("error")
                        continue
                    if exists is None:
                        self.next_retry = time.time() + 10
                        continue
                    self._spawn()

    # ---------- Health checks ----------
    def _set_remote(self, health=None):
        health = health or {}
        with self.lock:
            self.remote.update(
                status="ok" if health else "unreachable",
                app=health.get("app"),
                remote_root=health.get("remote_root"),
                version=health.get("version"),
                gpu=health.get("gpu"),
                instance_id=health.get("instance_id"),
                checked=time.time(),
            )

    def inspect_health(self, timeout: float = 4) -> tuple[str, Optional[Dict[str, Any]], Optional[str]]:
        """Return ``(state, health_or_None, detail)`` where state is ``ok``, ``unreachable``, or ``foreign_app``."""
        with self.lock:
            if not self._alive():
                return "unreachable", None, "tunnel down"
            host, port, session = self.local_host, self.local_port, self.session
        probe_state, health, detail = probe_remote_health_detailed(port, timeout=timeout, host=host)
        if probe_state == "unreachable":
            return "unreachable", None, detail
        if probe_state == "foreign_response":
            return "foreign_app", health, detail or "远端端口存在非 yc-h3-engine 服务"
        with self.lock:
            if (port, session) != (self.local_port, self.session):
                return "unreachable", None, "tunnel target changed"
            if self._matches(health, session):
                return "ok", health, None
        return (
            "foreign_app",
            health,
            f"远端端口已有异源应用响应（app={health.get('app')!r}, "
            f"remote_root={health.get('remote_root')!r}, session={health.get('session')!r}）",
        )

    def check_health(self, timeout: float = 4):
        state, health, _detail = self.inspect_health(timeout=timeout)
        return health if state == "ok" else None

    def _health_loop(self):
        while True:
            self._poll_health()
            time.sleep(self.HEALTH_EVERY)

    def _poll_health(self):
        with self.lock:
            session, desired = self.session, self.desired
        health = self.check_health()
        with self.lock:
            if (session, desired) != (self.session, self.desired):
                return
            if health:
                self._set_remote(health)
                self._set_status("up")
            else:
                self._set_remote()

    # ---------- Remote HTTP ----------
    def connection(self, timeout: float = 60):
        with self.lock:
            if not self._alive():
                raise RemoteUnreachable("tunnel down")
            host = config.validate_loopback_host(getattr(self, "local_host", "127.0.0.1"))
            port = self.local_port
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 60):
        try:
            conn = self.connection(timeout)
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            data = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            conn.close()
            return r.status, hdrs, data
        except (OSError, http.client.HTTPException) as e:
            raise RemoteUnreachable(str(e))

    def get_json(self, path: str, timeout: float = 20):
        st, _, data = self.request("GET", path, timeout=timeout)
        if st != 200:
            raise RemoteUnreachable("HTTP %d: %s" % (st, data[:200].decode("utf-8", "replace")))
        return json.loads(data.decode("utf-8"))

    def upload_bytes(self, filename: str, data: bytes, timeout: float = 300):
        q = urllib.parse.urlencode({"filename": filename})
        st, _, body = self.request(
            "POST",
            "/api/upload?" + q,
            body=data,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))},
            timeout=timeout,
        )
        try:
            obj = json.loads(body.decode("utf-8"))
        except ValueError:
            obj = {"detail": body[:300].decode("utf-8", "replace")}
        if st != 200:
            raise RuntimeError("远端上传失败 HTTP %d: %s" % (st, obj.get("detail")))
        return obj
