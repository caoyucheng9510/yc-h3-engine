"""Loopback daemon lifecycle, bound to one EngineConfig and data directory."""
from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from yc_h3.config import EngineConfig, load_config
from yc_h3.fsutil import atomic_write_json, file_lock
from yc_h3.providers.base import ProviderError

from . import config
from .platform_guard import ColabPlatformError, require_colab_platform

DAEMON_LOG_MAX_BYTES = 5 * 1024 * 1024


def _endpoint(engine, host, port):
    host = config.validate_loopback_host(host or engine.get("YC_H3_HOST", config.HOST))
    port = int(port if port is not None else engine.get("YC_H3_PORT", str(config.PORT)))
    if not 1 <= port <= 65535:
        raise ValueError("守护进程端口须介于 1 和 65535 之间")
    return host, port


def _identity(engine):
    return {"data_dir_hash": engine.data_dir_hash, "config_identity": engine.config_identity}


def _lock_path(engine):
    return engine.data_dir / ".daemon.lock"


def _owner_path(engine):
    return engine.data_dir / ".daemon-owner.json"


def _read_owner(engine):
    try:
        owner = json.loads(_owner_path(engine).read_bytes())
    except (OSError, ValueError):
        return None
    if (isinstance(owner, dict) and type(owner.get("pid")) is int and owner["pid"] > 1
            and isinstance(owner.get("host"), str) and type(owner.get("port")) is int):
        return owner
    return None


def _held_owner(engine):
    path = _lock_path(engine)
    if not path.is_file():
        return None
    try:
        with file_lock(path, timeout=0):
            return None
    except TimeoutError:
        return _read_owner(engine) or {}


def _probe_local_health(host, port, *, timeout=1.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/local/health", headers={"Connection": "close"})
        response = conn.getresponse()
        data = json.loads(response.read(4096)) if response.status == 200 else None
        if (isinstance(data, dict) and data.get("ok") is True
                and data.get("app") == config.APP_NAME and data.get("role") == "local-daemon"):
            return data
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    return None


def _conflict(owner, host, port):
    return ProviderError(
        f"守护进程端点或配置冲突（原 PID={owner.get('pid', '未知')}，"
        f"端口 {owner.get('port', port)}，当前请求 {host}:{port}）。"
        "请使用原配置停止旧守护进程，或为不同数据目录选择其它端口。",
        code="daemon_identity_conflict", exit_code=3,
    )


def _check_identity(engine, health, host, port):
    if any(health.get(key) != value for key, value in _identity(engine).items()):
        raise _conflict(health, host, port)


def daemon_status(*, engine_config=None, host=None, port=None):
    require_colab_platform()
    engine = engine_config or load_config()
    host, port = _endpoint(engine, host, port)
    health = _probe_local_health(host, port)
    if health:
        _check_identity(engine, health, host, port)
    return {
        **(health or {}),
        "running": bool(health), "pid": health.get("pid") if health else None,
        "host": host, "port": port, "lock_file": str(_lock_path(engine)),
    }


def make_daemon_app(*, engine_config=None, settings=None, colab=None, tunnel=None):
    require_colab_platform()
    from .app import App

    return App(engine_config=engine_config, settings=settings, colab=colab, tunnel=tunnel)


def _terminate_pid(pid, timeout=5.0):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_daemon_exit(host, port, *, engine_config=None):
    require_colab_platform()
    engine = engine_config or load_config()
    path = _lock_path(engine)
    owner = _read_owner(engine)
    if path.is_file() and owner and (owner["host"], owner["port"]) == (host, port):
        _check_identity(engine, owner, host, port)
        with file_lock(path, timeout=10, timeout_message="守护进程关停后仍持有进程锁"):
            pass


def stop_unreachable_daemon(*, expected_host, expected_port, stop_colab, detach_colab, engine_config=None):
    require_colab_platform()
    engine = engine_config or load_config()
    host, port = _endpoint(engine, expected_host, expected_port)
    health = _probe_local_health(host, port)
    if health:
        _check_identity(engine, health, host, port)
        raise _conflict(health, host, port)
    owner = _held_owner(engine)
    if owner is not None:
        _check_identity(engine, owner, host, port)
        if (owner["host"], owner["port"]) != (host, port):
            raise _conflict(owner, host, port)
    app = make_daemon_app(engine_config=engine)
    result = app.prepare_daemon_stop(stop_colab=stop_colab, detach_colab=detach_colab)
    if result["ok"] and owner and _held_owner(engine) == owner:
        _terminate_pid(owner["pid"])
    return result


def _port_accepts_connections(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _origin(host, port):
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def start_daemon_background(
    *, host=None, port=None, no_auto_tunnel=False, engine_config: EngineConfig | None = None,
    log_file: Path | None = None, timeout=15.0,
):
    require_colab_platform()
    engine = engine_config or load_config()
    host, port = _endpoint(engine, host, port)
    origin = _origin(host, port)
    log_path = Path(log_file).expanduser() if log_file is not None else engine.data_dir / "daemon.log"

    def result(health, *, reused):
        _check_identity(engine, health, host, port)
        return {
            **_identity(engine),
            "ok": True, "already_running": reused, "pid": health["pid"], "host": host, "port": port,
            "origin": origin, "log_file": str(log_path),
            "message": f"本地守护进程{'已在运行' if reused else '已在后台启动'} | {origin} | PID={health['pid']}",
        }

    health = _probe_local_health(host, port)
    if health:
        return result(health, reused=True)
    owner = _held_owner(engine)
    if owner is not None or _port_accepts_connections(host, port):
        raise _conflict(owner or {}, host, port)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > DAEMON_LOG_MAX_BYTES:
        log_path.replace(log_path.with_name(log_path.name + ".1"))
    command = [
        sys.executable, "-m", "yc_h3.local.daemon", "start",
        "--host", host, "--port", str(port), "--data-dir", str(engine.data_dir),
        "--config-cwd", str(engine.cwd),
        "--data-dir-hash", engine.data_dir_hash, "--config-identity", engine.config_identity,
    ]
    for path in engine.env_files:
        command.extend(["--loaded-env-file", str(path)])
    if no_auto_tunnel:
        command.append("--no-auto-tunnel")
    fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as log:
        process = subprocess.Popen(
            command, cwd=engine.cwd, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = _probe_local_health(host, port, timeout=0.5)
        if health:
            return result(health, reused=health.get("pid") != process.pid)
        code = process.poll()
        if code is not None:
            if code == 3:
                raise _conflict(_read_owner(engine) or {}, host, port)
            raise RuntimeError(f"后台守护进程启动失败（退出码 {code}），日志 {log_path}")
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
    raise RuntimeError(f"后台守护进程未在 {timeout:g} 秒内就绪，日志 {log_path}")


def run_daemon(*, host=None, port=None, no_auto_tunnel=False, engine_config=None):
    require_colab_platform()
    from .server import make_server

    engine = engine_config or load_config()
    host, port = _endpoint(engine, host, port)
    if health := _probe_local_health(host, port):
        raise _conflict(health, host, port)
    lock = file_lock(_lock_path(engine), timeout=0)
    try:
        lock.__enter__()
    except TimeoutError:
        raise _conflict(_read_owner(engine) or {}, host, port) from None
    try:
        app = make_daemon_app(engine_config=engine)
        app.auto_tunnel = not no_auto_tunnel
        try:
            httpd = make_server(app, host, port)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise _conflict({}, host, port) from None
            raise
        with httpd:
            try:
                atomic_write_json(_owner_path(engine), {"pid": os.getpid(), "host": host, "port": port, **_identity(engine)})
                app.start()

                def shutdown(*_):
                    threading.Thread(target=httpd.shutdown, daemon=True).start()

                signal.signal(signal.SIGINT, shutdown)
                signal.signal(signal.SIGTERM, shutdown)
                httpd.serve_forever(poll_interval=0.5)
            finally:
                app.shutdown()
                _owner_path(engine).unlink(missing_ok=True)
    finally:
        lock.__exit__(None, None, None)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="yc-h3-engine local daemon")
    start = parser.add_subparsers(dest="cmd", required=True).add_parser("start")
    start.add_argument("--host")
    start.add_argument("--port", type=int)
    start.add_argument("--data-dir", type=Path)
    start.add_argument("--env-file", type=Path)
    start.add_argument("--no-auto-tunnel", action="store_true")
    start.add_argument("-d", "--background", action="store_true")
    start.add_argument("--json", action="store_true")
    for name in ("--config-cwd", "--data-dir-hash", "--config-identity"):
        start.add_argument(name, help=argparse.SUPPRESS)
    start.add_argument("--loaded-env-file", type=Path, action="append", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    engine = None
    try:
        require_colab_platform()
        inherited = args.config_identity is not None
        highest = args.loaded_env_file[-1] if args.loaded_env_file else None
        engine = load_config(
            cwd=args.config_cwd, data_dir=args.data_dir, env_file=highest if inherited else args.env_file,
        )
        if inherited and (args.data_dir_hash != engine.data_dir_hash or args.config_identity != engine.config_identity
                          or tuple(args.loaded_env_file) != engine.env_files):
            raise ProviderError("后台守护进程读取的配置与启动端不一致", code="daemon_identity_conflict", exit_code=3)
        options = dict(host=args.host, port=args.port, no_auto_tunnel=args.no_auto_tunnel, engine_config=engine)
        if args.background:
            result = start_daemon_background(**options)
            print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
            return 0
        return run_daemon(**options)
    except (ColabPlatformError, ProviderError, RuntimeError, OSError, ValueError) as error:
        detail = engine.redact(str(error)) if engine else str(error)
        if args.json:
            print(json.dumps({"ok": False, "code": getattr(error, "code", "daemon_error"), "message": detail},
                             ensure_ascii=False))
        else:
            print(detail, file=sys.stderr)
        return getattr(error, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
