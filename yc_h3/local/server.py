"""Pure JSON local HTTP server bound strictly to loopback with narrowed controlled routes.

Adapted from WorldForge (MIT License).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .app import ApprovalRequiredError
from .tasks import TaskError
from .tunnel import RemoteUnreachable

CHUNK = 256 * 1024


class HTTPError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "yc-h3-engine"
    disable_nagle_algorithm = True
    app = None
    daemon_version = None
    remote_version = None

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            code_i = 0
        path = self.path.split("?", 1)[0]
        if self.command not in ("GET", "HEAD") or code_i >= 400:
            line = "%s %s %s %s\n" % (time.strftime("%H:%M:%S"), self.command, path, code)
            sys.stderr.write(self.app.engine_config.redact(line))

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        chunks, left = [], n
        while left > 0:
            c = self.rfile.read(min(left, 1 << 20))
            if not c:
                break
            chunks.append(c)
            left -= len(c)
        return b"".join(chunks)

    def _json_body(self):
        raw = self._body
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise HTTPError(400, "请求体必须是 JSON 对象")
        return parsed

    def send_json(self, obj, status: int = 200):
        data = json.dumps(self.app.engine_config.redact(obj), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error_json(self, status: int, detail, **extra):
        if isinstance(detail, dict):
            payload = dict(detail)
            payload.update(extra)
        else:
            payload = {"detail": detail, **extra}
        self.send_json(payload, status)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        try:
            self._body = self._read_body()
        except (OSError, ValueError):
            self.close_connection = True
            return
        u = urllib.parse.urlsplit(self.path)
        path, self.query = urllib.parse.unquote(u.path), u.query
        self.qs = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        try:
            if path.startswith("/api/local/"):
                route = path[len("/api/local/") :].strip("/")
                return self._local(route)
            raise HTTPError(404, "not found")
        except ApprovalRequiredError as e:
            self.send_error_json(e.http_status, e.payload, exit_code=e.exit_code)
        except HTTPError as e:
            self.send_error_json(e.status, e.detail)
        except (TaskError, ValueError) as e:
            self.send_error_json(400, str(e))
        except LookupError as e:
            self.send_error_json(404, str(e).strip("'\"") or "not found")
        except RemoteUnreachable:
            self.send_error_json(502, "remote unreachable")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            import traceback

            sys.stderr.write(self.app.engine_config.redact(traceback.format_exc()))
            try:
                self.send_error_json(500, "%s: %s" % (type(e).__name__, e))
            except OSError:
                self.close_connection = True

    def _stream_remote_file(self, remote_path: str):
        if not remote_path or not remote_path.startswith("/"):
            raise HTTPError(400, "缺少有效的远端文件路径 path")
        q = urllib.parse.urlencode({"path": remote_path})
        try:
            conn = self.app.tunnel.connection(timeout=300)
            conn.request("GET", "/api/files?" + q)
            resp = conn.getresponse()
        except (RemoteUnreachable, OSError, http.client.HTTPException):
            raise RemoteUnreachable()
        try:
            if resp.status != 200:
                detail = resp.read()[:300].decode("utf-8", "replace")
                raise HTTPError(resp.status, detail or "远端文件下载失败")
            clen = resp.getheader("Content-Length")
            self.send_response(200)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/octet-stream")
            disp = resp.getheader("Content-Disposition")
            if disp:
                self.send_header("Content-Disposition", disp)
            if clen is not None:
                self.send_header("Content-Length", clen)
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                buf = resp.read(CHUNK)
                if not buf:
                    break
                self.wfile.write(buf)
        finally:
            conn.close()

    def _local(self, route: str):
        app, m = self.app, self.command
        parts = route.split("/") if route else []

        # Health & state
        if m == "GET" and route == "health":
            return self.send_json(
                {
                    "ok": True,
                    "app": config.APP_NAME,
                    "role": "local-daemon",
                    "version": self.daemon_version,
                    "remote_version": self.remote_version,
                    "pid": os.getpid(),
                    "data_dir_hash": app.engine_config.data_dir_hash,
                    "config_identity": app.engine_config.config_identity,
                }
            )
        if m == "GET" and route in ("state", "status"):
            return self.send_json(app.state())

        # Custom assets (loopback-only + origin check)
        if parts and parts[0] == "custom-assets":
            origin = self.headers.get("Origin")
            origin_host = urllib.parse.urlsplit(origin).hostname if origin else None
            if not ipaddress.ip_address(self.client_address[0]).is_loopback or (
                origin and origin_host not in ("localhost", "127.0.0.1", "::1")
            ):
                raise HTTPError(403, "自定义资产登记仅可在本机访问")
            if m == "GET" and len(parts) == 1:
                return self.send_json({"assets": app.custom_assets()})
            if m == "POST" and len(parts) == 1:
                if self.headers.get_content_type() != "application/json":
                    raise HTTPError(415, "自定义资产必须以 application/json 提交")
                return self.send_json(app.edit_custom_assets(body=self._json_body()))
            if m == "POST" and len(parts) == 2 and parts[1] == "sync":
                b = self._json_body()
                return self.send_json(app.sync_custom_assets(b.get("session")))
            if m == "DELETE" and len(parts) in (1, 2):
                asset_id = parts[1] if len(parts) == 2 else self.qs.get("id")
                if asset_id is None:
                    asset_id = self._json_body().get("id")
                if not isinstance(asset_id, str):
                    raise ValueError("缺少资产 id")
                return self.send_json(app.edit_custom_assets(asset_id=asset_id))

        # Gated GPU session lifecycle
        if m == "POST" and route == "gpu/plan":
            b = self._json_body()
            plan = app.gpu_plan(
                session=b.get("session") or b.get("name"),
                gpu=b.get("gpu"),
                setup=bool(b.get("setup", False)),
                setup_params=b.get("params") or b.get("setup_params"),
                max_runtime_minutes=b.get("max_runtime_minutes"),
                idle_minutes=b.get("idle_minutes"),
                high_mem=b.get("high_mem"),
            )
            return self.send_json(plan, status=409 if plan["status"] == "block" else 200)
        if m == "POST" and route == "gpu/reconcile":
            b = self._json_body()
            result = app.reconcile_session(b.get("session") or b.get("name"))
            return self.send_json(result, status=409 if result["status"] == "block" else 200)
        if m == "POST" and route == "gpu/up":
            b = self._json_body()
            t = app.gpu_up(
                session=b.get("session") or b.get("name"),
                gpu=b.get("gpu"),
                setup=bool(b.get("setup", False)),
                setup_params=b.get("params") or b.get("setup_params"),
                max_runtime_minutes=b.get("max_runtime_minutes"),
                idle_minutes=b.get("idle_minutes"),
                high_mem=b.get("high_mem"),
                approve_digest=b.get("approve_digest"),
                connect=bool(b.get("connect", True)),
            )
            return self.send_json({"task_id": t.id})
        if m == "POST" and route == "gpu/stop":
            b = self._json_body()
            t = app.session_stop(b.get("session") or b.get("name"))
            return self.send_json({"task_id": t.id})

        # Strictly read-only preflight, catalogs & perf
        if m == "POST" and route == "preflight":
            status, data = app.preflight(self._json_body())
            return self.send_json(data, status=status)
        if m == "GET" and route == "workflows":
            return self.send_json(app.list_remote_workflows())
        if m == "GET" and route == "perf":
            return self.send_json(app.preflight_history(self.qs.get("workflow")))

        # Controlled job submission, query, cancel, perf archive
        if m == "POST" and route == "jobs":
            status, data = app.submit_job(self._json_body())
            return self.send_json(data, status=status)
        if m == "GET" and route == "jobs":
            limit = int(self.qs.get("limit") or 50)
            return self.send_json(app.list_remote_jobs(limit=limit))
        if m == "GET" and len(parts) == 2 and parts[0] == "jobs":
            return self.send_json(app.get_remote_job(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            return self.send_json(app.cancel_remote_job(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "archive_perf":
            return self.send_json(app.archive_job_perf(parts[1]))

        # Controlled file download & explicit asset upload
        if m in ("GET", "HEAD") and route == "files":
            return self._stream_remote_file(self.qs.get("path") or "")
        if m == "POST" and route == "upload":
            fname = self.qs.get("filename") or "upload.png"
            if not self._body:
                raise HTTPError(400, "请求体为空")
            try:
                return self.send_json(app.upload(fname, self._body))
            except RemoteUnreachable:
                raise
            except RuntimeError as e:
                raise HTTPError(502, str(e))

        # Local async tasks
        if len(parts) >= 2 and parts[0] == "tasks":
            t = app.tasks.get(parts[1])
            if not t:
                raise HTTPError(404, "task not found")
            if m == "GET" and len(parts) == 2:
                return self.send_json(t.to_dict())
            if m == "GET" and len(parts) == 3 and parts[2] == "log":
                try:
                    off = int(self.qs.get("offset") or 0)
                except ValueError:
                    raise HTTPError(400, "offset 必须是整数")
                return self.send_json(t.read_log(off))

        # Daemon shutdown with active Colab guard
        if m == "POST" and route == "daemon/stop":
            b = self._json_body()
            res = app.prepare_daemon_stop(
                stop_colab=bool(b.get("stop_colab", False)),
                detach_colab=bool(b.get("detach_colab", False)),
            )
            status = 200 if res.get("ok") else 409
            self.send_json(res, status=status)
            if res["ok"]:
                self.server.shutdown()
            return

        raise HTTPError(404 if m in ("GET", "POST", "DELETE") else 405, "not found: /api/local/" + route)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class ServerV6(Server):
    address_family = socket.AF_INET6


def make_server(app, host: str = config.HOST, port: int = config.PORT):
    bind_host = config.validate_loopback_host(host)
    daemon_version = config.daemon_version()
    remote_version = config.remote_version()
    handler_cls = type(
        "BoundHandler",
        (Handler,),
        {
            "app": app,
            "daemon_version": daemon_version,
            "remote_version": remote_version,
        },
    )
    is_v6 = False
    try:
        is_v6 = ipaddress.ip_address(bind_host).version == 6
    except ValueError:
        is_v6 = False
    server_cls = ServerV6 if is_v6 else Server
    return server_cls((bind_host, port), handler_cls)
