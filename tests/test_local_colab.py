import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from yc_h3.config import credential_fingerprint, load_config
from yc_h3.local import colab, daemon, platform_guard
from yc_h3.local.app import App
from yc_h3.local.config import Settings
from yc_h3.local.tunnel import Tunnel
from yc_h3.providers.base import ProviderError


colab_runtime = pytest.mark.skipif(sys.platform == "win32", reason="Colab 运行态需 macOS、Linux 或 WSL2")


def test_native_windows_blocks_colab_and_wsl2_is_allowed(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(platform_guard.ColabPlatformError) as error:
        colab.Colab(Settings(tmp_path / "missing" / "settings.json"))
    assert error.value.exit_code == 3
    assert daemon.main(["start", "--data-dir", str(tmp_path / "missing")]) == 3
    assert "WSL2" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()
    monkeypatch.setattr(sys, "platform", "linux")
    platform_guard.require_colab_platform()
    assert platform_guard.colab_supported()


@colab_runtime
def test_read_only_settings_and_short_control_path(tmp_path, monkeypatch):
    long_data = tmp_path / ("data-" + "x" * 140)
    engine = load_config(data_dir=long_data, cwd=tmp_path, environ={})
    settings = Settings(engine_config=engine)
    settings.reload()
    assert settings.get("session_name") == "yh3-default"
    assert not long_data.exists()
    monkeypatch.setattr(colab.tempfile, "gettempdir", lambda: str(tmp_path / ("temp-" + "t" * 140)))
    runner = colab.Colab(settings)
    control = runner.ctl_path("yh3-example")
    assert len(os.fsencode(control)) < 104
    assert Path(control).parent.name == f"yh3-ssh-{os.getuid()}"
    assert Path(control).name.startswith("ctl-")
    assert control == runner.ctl_path("yh3-example")
    other = colab.Colab(Settings(engine_config=load_config(data_dir=tmp_path / "other", cwd=tmp_path, environ={})))
    assert control != other.ctl_path("yh3-example")
    master = runner.master_cmd("yh3-example", 19500, 7860)
    mux = runner.mux_cmd("yh3-example", "true")
    assert master[master.index("-S") + 1] == mux[mux.index("-S") + 1] == control
    assert "127.0.0.1:19500:127.0.0.1:7860" in master
    assert not long_data.exists()


@colab_runtime
def test_colab_subprocess_preserves_bytes_and_decodes_text(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"\xff" if kwargs.get("input") else "中文")

    monkeypatch.setattr(colab.subprocess, "run", run)
    assert colab.run(["ssh"], input_bytes=b"\x1f\x8b")[1] == "�"
    assert calls[0]["input"] == b"\x1f\x8b"
    assert "encoding" not in calls[0]
    assert colab.run(["colab", "sessions"])[1] == "中文"
    assert calls[1]["encoding"] == "utf-8" and calls[1]["errors"] == "replace"


@colab_runtime
def test_background_daemon_verifies_config_identity_across_processes(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("COLAB_ACCOUNT=fixture-account\n", encoding="utf-8")
    env_file = tmp_path / "explicit.env"
    env_file.write_text(f"COLAB_CLI={tmp_path / 'missing-colab'}\n", encoding="utf-8")
    global_env = home / ".yc-h3-engine" / ".env"
    global_env.parent.mkdir(parents=True)
    global_env.write_text("YC_H3_PERF_DIR=perfs\n", encoding="utf-8")
    engine = load_config(data_dir=tmp_path / "chosen", cwd=work, env_file=env_file)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    initial_status = daemon.daemon_status(port=port, engine_config=engine)
    assert not initial_status["running"]
    assert not engine.data_dir.exists()

    launch = daemon.start_daemon_background(port=port, engine_config=engine, no_auto_tunnel=True)
    try:
        health = daemon.daemon_status(port=port, engine_config=engine)
        assert health["data_dir_hash"] == engine.data_dir_hash
        assert health["config_identity"] == engine.config_identity
        assert health["pid"] == launch["pid"]
        assert daemon.start_daemon_background(port=port, engine_config=engine)["already_running"]
        other = load_config(data_dir=tmp_path / "other", cwd=work, env_file=env_file)
        with pytest.raises(ProviderError) as error:
            daemon.start_daemon_background(port=port, engine_config=other)
        assert error.value.exit_code == 3
        assert str(launch["pid"]) in str(error.value) and str(port) in str(error.value)
        assert not other.data_dir.exists()
        env_file.write_text(f"COLAB_CLI={tmp_path / 'another-missing-colab'}\n", encoding="utf-8")
        updated = load_config(data_dir=engine.data_dir, cwd=work, env_file=env_file)
        with pytest.raises(ProviderError, match="配置冲突"):
            daemon.start_daemon_background(port=port, engine_config=updated)
    finally:
        req = urllib.request.Request(
            launch["origin"] + "/api/local/daemon/stop", data=b'{"stop_colab":true}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert json.load(response)["ok"]
        daemon.wait_for_daemon_exit("127.0.0.1", port, engine_config=engine)
        assert not daemon.daemon_status(port=port, engine_config=engine)["running"]


class FakeColab:
    def __init__(self, settings):
        self.settings = settings
        self.rows = [{"name": "yh3-offline", "host": "fixture-vm", "hardware": "G4"}]
        self.stop_calls = []

    def sessions(self):
        return list(self.rows)

    def usage(self):
        return {"cli_ok": True, "balance": 18, "rate": 2}, ""

    def stream(self, args, task, timeout):
        self.stop_calls.append(tuple(args))
        self.rows.clear()
        return 0, ""

    def kick(self):
        pass


@pytest.mark.parametrize("mode", ["manual", "idle", "deadline", "daemon", "restarted-placeholder"])
@colab_runtime
def test_every_colab_stop_path_records_session_and_balance(tmp_path, mode):
    engine = load_config(data_dir=tmp_path / "engine", cwd=tmp_path, environ={"COLAB_ACCOUNT": "fixture-profile"})
    settings = Settings(engine_config=engine)
    fake = FakeColab(settings)
    app = App(settings=settings, colab=fake, tunnel=Tunnel(fake))
    now = time.time()
    name = "yh3-offline"
    settings.update({
        "session_name": name, "idle_minutes": 1,
        "managed_sessions": {
            name: {
                "host": None if mode == "restarted-placeholder" else "fixture-vm",
                "allocated_at": now - 180,
                "deadline_at": now - 1 if mode in ("deadline", "restarted-placeholder") else now + 60,
                "max_runtime_minutes": 4, "gpu": "G4", "balance_before": 20, "cu_rate": 2,
                "account_fp": credential_fingerprint("fixture-profile"),
            },
        },
    })
    if mode == "manual":
        task = app.session_stop(name)
    elif mode == "daemon":
        assert app.prepare_daemon_stop()["exit_code"] == 3
        assert app.prepare_daemon_stop(stop_colab=True)["stopped_sessions"] == [name]
        task = None
    else:
        app.last_activity = now - 120
        app._remote_is_busy = lambda _: False
        app._watchdog_check(now=now)
        task = app.auto_stop_task
        assert task is not None
    if task:
        for _ in range(200):
            if task.finished:
                break
            time.sleep(0.01)
        assert task.status == "done", task.error

    app._watchdog_check(now=now)
    assert not settings.get("managed_sessions")
    assert not settings.get("pending_stop_billing")
    assert fake.stop_calls == [("stop", "-s", name)]
    usage = [json.loads(line) for line in (engine.data_dir / "ledger/usage.jsonl").read_text().splitlines()]
    assert len(usage) == 1 and usage[0]["quantity"] == 2
    assert usage[0]["provider"] == "colab-comfy"
    assert usage[0]["account_fp"] == credential_fingerprint("fixture-profile")
    balances = (engine.data_dir / "ledger/balances.tsv").read_text().splitlines()
    assert len(balances) == 2 and "\t18" in balances[1]


@colab_runtime
def test_colab_retries_balance_settlement_after_stop_without_duplicate_usage(tmp_path, monkeypatch):
    engine = load_config(data_dir=tmp_path / "engine", cwd=tmp_path, environ={})
    settings = Settings(engine_config=engine)
    fake = FakeColab(settings)
    app = App(settings=settings, colab=fake, tunnel=Tunnel(fake))
    name = "yh3-offline"
    settings.update({"managed_sessions": {
        name: {"host": "fixture-vm", "gpu": "G4", "allocated_at": time.time() - 180, "cu_rate": 2},
    }})
    monkeypatch.setattr(fake, "usage", lambda: ({"cli_ok": False}, ""))
    task = app.session_stop(name)
    for _ in range(200):
        if task.finished:
            break
        time.sleep(0.01)
    assert task.status == "done", task.error
    assert task.result["ledger_recorded"] and not task.result["ledger_balance_recorded"]
    assert settings.get("pending_stop_billing")
    assert not settings.get("managed_sessions")
    monkeypatch.setattr(fake, "usage", lambda: ({"cli_ok": True, "balance": 18, "rate": 99}, ""))
    App(settings=Settings(engine_config=engine), colab=fake, tunnel=Tunnel(fake))._watchdog_check()
    assert not settings.reload()["pending_stop_billing"]
    assert len((engine.data_dir / "ledger/usage.jsonl").read_text().splitlines()) == 1
    assert len((engine.data_dir / "ledger/balances.tsv").read_text().splitlines()) == 2
