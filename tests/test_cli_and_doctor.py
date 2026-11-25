"""Offline CLI, request routing and native-Windows import boundary."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yc_h3 import cli
from yc_h3.config import read_env_file
from yc_h3.providers import registry
from yc_h3.providers.base import ArtifactResult, GenerationRequest, ProbeResult, ProviderError
from yc_h3.tasks_store import TaskStore
from tests.test_runninghub_provider import TEXT_PROMPT, make_config


def test_init_create_preserve_and_update(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    credential = "offline-first-secret"
    supplied = {"RUNNINGHUB_API_KEY": credential, "CHENYU_API_KEY": "  ", "CUSTOM_KEY": "retained"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(supplied)))
    command = ["init", "--local", "--from-stdin-json", "--json"]
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "created"
    path = tmp_path / ".env"
    before = path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    updated = "offline-second-secret"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"RUNNINGHUB_API_KEY": updated})))
    assert cli.main(command) == 0
    kept = json.loads(capsys.readouterr().out)
    assert kept["status"] == "kept"
    assert kept["configured_keys"] == ["RUNNINGHUB_API_KEY"]
    assert before == path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"RUNNINGHUB_API_KEY": updated})))
    assert cli.main([*command, "--update"]) == 0
    output = capsys.readouterr().out
    assert credential not in output and updated not in output
    data = read_env_file(path)
    assert data["RUNNINGHUB_API_KEY"] == updated and data["CUSTOM_KEY"] == "retained"


def test_doctor_and_workflows_are_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    make_config(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text('RUNNINGHUB_API_KEY="offline-no-workflow"\n')
    monkeypatch.delenv("RUNNINGHUB_WORKFLOW_ID", raising=False)
    monkeypatch.delenv("RUNNINGHUB_WORKFLOW_ID_HYBRID", raising=False)
    monkeypatch.delenv("RUNNINGHUB_WORKFLOW_ID_COMBAT", raising=False)
    monkeypatch.setattr("yc_h3.providers.video_common.ffmpeg_bin", lambda: "/mock/ffmpeg")
    monkeypatch.setattr("yc_h3.providers.video_common.ffprobe_bin", lambda: "/mock/ffprobe")
    monkeypatch.setattr("yc_h3.providers.comfy_client.LocalDaemonClient.health", lambda self: (_ for _ in ()).throw(ConnectionRefusedError()))
    prefix = ["--data-dir", str(tmp_path / "absent")]
    assert cli.main([*prefix, "doctor", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["env_files"] == [str(tmp_path / ".env")]
    assert {row["reason"] for row in result["runninghub_workflows"]} == {"missing_workflow_id"}
    assert cli.main([*prefix, "doctor", "--provider", "runninghub", "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert "2124779546246475777" in result["providers"][0]["details"]["detail"]
    assert cli.main([*prefix, "workflows", "--json"]) == 0
    workflows = json.loads(capsys.readouterr().out)
    assert len(workflows) == 8
    runninghub = [row for row in workflows if row["provider_id"] == "runninghub"]
    assert all(row["workflow_id"] is None and row["slots"]["videos"] == 1 for row in runninghub)
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(("last_available", "inputs", "params", "expected", "expected_skips"), [
    ("colab-comfy", {}, {"aspect": "16:9"}, "colab-comfy", {}),
    ("chenyu-comfy", {"images": ["one.png"]}, {}, "chenyu-comfy", {}),
    ("runninghub", {"videos": ["ref.mp4"]}, {}, "runninghub", {}),
    ("chenyu", {}, {}, "chenyu", {}),
    ("chenyu", {"images": ["one.png"]}, {"seconds": 5}, "chenyu", {}),
    ("chenyu", {"videos": ["ref.mp4"]}, {}, None, {"chenyu": "unsupported_videos"}),
    ("chenyu", {}, {"seconds": 5.5}, None, {"chenyu": "unsupported_fractional_seconds"}),
    ("chenyu", {}, {"seed": 7}, None, {"chenyu": "unsupported_seed"}),
    ("chenyu", {}, {"steps": 12}, None, {"chenyu": "unsupported_steps"}),
    ("chenyu-comfy", {}, {"trim_to_requested_seconds": True}, "chenyu-comfy", {}),
    ("runninghub", {}, {"trim_to_requested_seconds": True}, None, {
        "runninghub": "unsupported_trim_to_requested_seconds", "chenyu": "unsupported_trim_to_requested_seconds",
    }),
    ("colab-comfy", {}, {"aspect": "21:9"}, "chenyu", {
        "colab-comfy": "unsupported_aspect", "chenyu-comfy": "unsupported_aspect", "runninghub": "unsupported_aspect",
    }),
])
def test_capability_aware_provider_matrix(tmp_path, monkeypatch, last_available, inputs, params, expected, expected_skips):
    config = make_config(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.sys, "platform", "linux")
    checked = []

    class MockProvider:
        def __init__(self, name):
            self.provider_id = name

        def probe(self, **kwargs):
            checked.append(self.provider_id)
            available = registry.PROVIDER_ORDER.index(self.provider_id) >= registry.PROVIDER_ORDER.index(last_available)
            return ProbeResult(self.provider_id, available, "" if available else "offline")

    monkeypatch.setattr(registry, "get_provider", lambda name, cfg: MockProvider(name))
    request = GenerationRequest("video", inputs=inputs, params=params, workflow="chenyu-jinyu-hybrid")
    route = registry.resolve_provider(request, config)
    assert (route.provider.provider_id if route.provider else None) == expected
    assert request.workflow == "jinyu-hybrid"
    skipped = {row["provider_id"]: row["reason"] for row in route.skipped}
    assert expected_skips.items() <= skipped.items()
    assert not expected_skips.keys() & set(checked)
    with pytest.raises(ValueError, match="--workflow jinyu-hybrid"):
        registry.normalize_workflow("h3-jinyu-hybrid", "chenyu")


@pytest.mark.skipif(sys.platform == "win32", reason="Colab 运行态需 macOS、Linux 或 WSL2")
@pytest.mark.parametrize(("action", "stream_result", "exit_code", "error_code", "message"), [
    ("up", (0, ""), 0, None, None),
    ("up", (1, "service unavailable"), 1, "local_task_failed", "colab new 失败"),
    ("up", (124, "synthetic timeout"), 3, "needs_reconcile", "新端点"),
    ("stop", (0, ""), 0, None, None),
    ("stop", (1, "synthetic stop failure"), 1, "local_task_failed", "colab sessions 仍有会话"),
])
def test_colab_gpu_cli_waits_for_background_result(tmp_path, monkeypatch, capsys, action, stream_result,
                                                   exit_code, error_code, message):
    from tests.test_local_colab import FakeColab
    from yc_h3.local import app as local_app
    from yc_h3.local.config import Settings
    from yc_h3.local.server import make_server
    from yc_h3.local.tasks import Task
    from yc_h3.local.tunnel import Tunnel

    config = make_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(cli, "_COLAB_TASK_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cli, "_COLAB_TASK_TIMEOUTS", {action: 5})
    # Skip the daemon's stop retry delay while keeping every session check.
    monkeypatch.setattr(local_app, "time", SimpleNamespace(time=time.time, sleep=lambda _: None))
    settings = Settings(engine_config=config)
    fake_colab = FakeColab(settings)
    app = local_app.App(settings=settings, colab=fake_colab, tunnel=Tunnel(fake_colab))
    session = fake_colab.rows[0].copy()
    name = session["name"]
    arguments = ["gpu", action, "--backend", "colab", "--session", name, "--json"]
    if action == "up":
        fake_colab.rows.clear()
        plan = app.gpu_plan(session=name, gpu="G4")
        arguments.extend(["--gpu", "G4", "--no-connect", "--approve-digest", plan["digest"]])
    else:
        settings.update({"managed_sessions": {name: {
            "host": session["host"], "gpu": "G4", "allocated_at": time.time() - 60,
            "balance_before": 20, "cu_rate": 2,
        }}})

    first_poll = threading.Event()
    snapshots = []
    original_snapshot = Task.to_dict

    def observe_snapshot(task):
        snapshot = original_snapshot(task)
        snapshots.append(snapshot)
        first_poll.set()
        return snapshot

    stream_calls = []

    def stream(args, task, timeout):
        stream_calls.append(args[:3])
        assert first_poll.wait(5)
        if stream_result[0] == 0:
            fake_colab.rows[:] = [session] if action == "up" else []
        return stream_result

    monkeypatch.setattr(Task, "to_dict", observe_snapshot)
    monkeypatch.setattr(fake_colab, "stream", stream)
    with make_server(app, host="127.0.0.1", port=0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            assert cli.main([*arguments, "--host", "127.0.0.1", "--port", str(server.server_port)]) == exit_code
        finally:
            first_poll.set()
            server.shutdown()
            thread.join(timeout=5)

    captured = capsys.readouterr()
    assert stream_calls == [["new" if action == "up" else "stop", "-s", name]]
    assert snapshots[0]["status"] == "running"
    terminal = snapshots[-1]
    if exit_code == 0:
        result = json.loads(captured.out)
        assert result == {**terminal, "task_id": terminal["id"]}
        assert result["status"] == "done"
        assert result["result"]["session"] == (session if action == "up" else name)
    else:
        assert captured.out == ""
        error = json.loads(captured.err.splitlines()[-1])
        assert error["task_id"] == terminal["id"] and error["status"] == terminal["status"] == "error"
        assert error["exit_code"] == exit_code and error["error"] == error_code and message in error["message"]
        if action == "stop":
            assert fake_colab.sessions() == [session]
            assert name in settings.get("managed_sessions")
            assert not (config.data_dir / "ledger" / "usage.jsonl").exists()


@pytest.mark.parametrize(("action", "setup", "timeout_key"), [
    ("stop", False, "stop"), ("up", False, "up"), ("up", True, "setup"),
])
def test_colab_gpu_cli_times_out(tmp_path, monkeypatch, capsys, action, setup, timeout_key):
    config = make_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    timeouts = {"stop": 3, "up": 18, "setup": 36}
    monkeypatch.setattr(cli, "_COLAB_TASK_TIMEOUTS", timeouts)
    monkeypatch.setattr(cli, "_COLAB_TASK_POLL_INTERVAL", 2)
    now = 0

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    polls = []

    def request(method, path, *, timeout, payload=None):
        if method == "POST":
            assert path == f"/api/local/gpu/{action}"
            return 200, {"task_id": "t_pending"}
        assert (method, path) == ("GET", "/api/local/tasks/t_pending")
        polls.append(timeout)
        return 200, {"id": "t_pending", "status": "running"}

    client = SimpleNamespace(
        health=lambda: {"ok": True, "app": "yc-h3-engine", "data_dir_hash": config.data_dir_hash,
                        "config_identity": config.config_identity},
        _request_json=request,
    )
    monkeypatch.setattr(cli, "_colab_client", lambda args, config: client)
    arguments = ["gpu", action, "--backend", "colab", "--json"]
    assert cli.main([*arguments, "--setup"] if setup else arguments) == 1
    assert now == timeouts[timeout_key] and polls[0] == min(30, now)
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "local_task_timeout" and error["task_id"] == "t_pending"
    assert "yc-h3 gpu status" in error["message"] and "yc-h3 gpu reconcile" in error["message"]


@pytest.mark.parametrize(("message", "error_code", "exit_code"), [
    ("approval_required: 需要审批", "approval_required", 2),
    ("approval_stale: 审批已过期", "approval_stale", 2),
    ("远端 setup 提交失败 HTTP 403: 缺少或过期的审批摘要", "approval_required", 2),
    ("请先释放受管会话 yh3-first，再分配 yh3-second", "active_managed_session_exists", 3),
    ("Colab 当前账号配置与占位不符，请使用分配时的配置核对", "local_task_conflict", 3),
    ("unmanaged_session: 会话未由本机托管", "unmanaged_session", 3),
    ("session_host_changed: 会话端点已变化", "session_host_changed", 3),
    ("session_deadline_or_cancelled: 会话已取消", "session_deadline_or_cancelled", 3),
    ("foreign_app: 远端服务身份不符", "foreign_app", 3),
])
def test_colab_gpu_task_gate_error_codes(message, error_code, exit_code):
    client = SimpleNamespace(_request_json=lambda *args, **kwargs: (200, {"status": "error", "error": message}))
    with pytest.raises(ProviderError) as error:
        cli._wait_for_colab_gpu_task(client, "t_failed", timeout=5)
    assert (error.value.code, error.value.exit_code, str(error.value)) == (error_code, exit_code, message)


def test_jobs_list_only_safe_summary_and_passes_fetch_options(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    store = TaskStore(config=config)
    _, row = store.begin_external_submit(
        "task-1", provider="runninghub", endpoint_origin="https://example.test", request_digest="9" * 64,
        credential_fingerprint="4" * 64, logical_target=str(tmp_path / "old.mp4"),
        extra={"workflow": "jinyu-combat", "tag": "shot", "prompt": "unpublished prompt",
               "submit_payload": {"inputs": "internal replay payload"}},
    )
    store.mark_external_submitted("task-1", "remote-1", expected_submission_id=row["submission_id"])
    assert cli.main(["jobs", "list", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]["task_key"] == "task-1" and result[0]["remote_task_id"] == "remote-1"
    assert not {"prompt", "submit_payload", "request_digest", "credential_fingerprint", "endpoint_origin"} & result[0].keys()
    assert "internal replay payload" not in json.dumps(result)

    class FakeProvider:
        def fetch(self, task_key, *, store, output_path, remote_job_id):
            assert task_key == "task-1" and output_path == tmp_path / "new.mp4"
            assert remote_job_id == "remote-1" and store.get(task_key)["state"] == "submitted"
            return ArtifactResult("runninghub", task_key, "delivered", str(output_path), remote_id=remote_job_id)

    monkeypatch.setattr(registry, "get_provider", lambda provider, engine: FakeProvider())
    assert cli.main(["jobs", "fetch", "task-1", "--out", str(tmp_path / "new.mp4"),
                     "--remote-job-id", "remote-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["artifact_path"] == str(tmp_path / "new.mp4")


def test_cli_selects_runninghub_and_reports_approval(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, monkeypatch, RUNNINGHUB_API_KEY="fake-runninghub-api-key",
                         RUNNINGHUB_WORKFLOW_ID_HYBRID="3579024681")
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(registry.sys, "platform", "win32")
    from yc_h3.chenyu_gpu import ChenyuGpuManager

    class InactiveClient:
        def state(self):
            return {}

    monkeypatch.setattr(ChenyuGpuManager, "_client", lambda self: InactiveClient())
    arguments = ["--prompt", TEXT_PROMPT, "--out", str(tmp_path / "clip.mp4"), "--json"]
    assert cli.main(["preflight", *arguments]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["selected_provider"] == "runninghub"
    assert result["routing_skipped"][0]["reason"] == registry.WINDOWS_COLAB_REASON
    assert result["approve_digest"] == result["digest"] and len(result["digest"]) == 64
    assert cli.main(["gen", "--provider", "runninghub", *arguments]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "approval_missing" and error["preflight"]["approve_digest"] == result["digest"]
    assert not config.data_dir.exists()
    assert cli.main(["gen", "--provider", "runninghub", "--seconds", "not-number", *arguments]) == 1


def test_native_windows_imports_and_three_preflights(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = r'''
import contextlib, importlib.abc, io, json, ssl, subprocess, sys
from pathlib import Path
# The host subprocess implementation already selected its native backend.
# Only imports requested by the engine are subject to the Windows boundary.
sys.modules.pop("fcntl", None)
root = Path(sys.argv[1])
prompt = sys.argv[2]
sys.platform = "win32"
class RestrictImports(importlib.abc.MetaPathFinder):
    allow_colab_guard = False
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fcntl" or (fullname.startswith("yc_h3.local") and not self.allow_colab_guard):
            raise AssertionError("POSIX dependency attempted: " + fullname)
checker = RestrictImports()
sys.meta_path.insert(0, checker)
from yc_h3 import cli
from yc_h3.providers import registry, runninghub, chenyu, chenyu_comfy
from yc_h3 import config as config_module
from yc_h3.chenyu_gpu import ChenyuGpuManager
config_module.global_env_path = lambda: root / "nonexistent-global.env"
config = config_module.load_config(cwd=root, data_dir=root / "state", environ={
    "RUNNINGHUB_API_KEY": "fake-runninghub", "RUNNINGHUB_WORKFLOW_ID": "12340000", "CHENYU_API_KEY": "fake-chenyu",
})
cli.load_config = lambda **kwargs: config
cli.video_common.ffmpeg_bin = cli.video_common.ffprobe_bin = lambda: "mock-tool"
class Client:
    origin = "https://managed.example.test"
    def state(self):
        return {"tunnel": {"session": "yh3-win"}, "remote": {"instance_id": "live-id", "platform_instance_id": "vm"},
                "settings": {"managed_sessions": {"yh3-win": {"host": "vm"}}}}
    def preflight(self, body):
        return 200, {"status": "pass", "instance_id": "live-id", "managed_instance_id": "vm",
                     "preflight_digest": "1" * 64, "billing_context": {
                         "session_name": "yh3-win", "vm_host": "vm", "billing_rate": 3,
                     }}
ChenyuGpuManager._client = lambda self: Client()
def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([*args, "--json"])
    return code, json.loads(out.getvalue() or err.getvalue())
code, doctor = run("doctor")
assert code == 0 and doctor["colab"]["reason"] == "colab_requires_posix_or_wsl2", (code, doctor)
code, workflows = run("workflows")
assert code == 0 and len(workflows) == 8, (code, workflows)
for provider, expected in (("runninghub", 2), ("chenyu", 2), ("chenyu-comfy", 0)):
    code, result = run("preflight", "--provider", provider, "--prompt", prompt, "--out", str(root / "video.mp4"))
    assert code == expected and result["selected_provider"] == provider, (code, result)
assert not (root / "state").exists()
assert "fcntl" not in sys.modules and "yc_h3.local" not in sys.modules
checker.allow_colab_guard = True
for command in (("daemon", "status"), ("gpu", "status", "--backend", "colab"),
                ("preflight", "--provider", "colab-comfy", "--prompt", prompt, "--out", str(root / "video.mp4"))):
    code, result = run(*command)
    assert code == 3 and "WSL2" in result["message"], (code, result)
assert "yc_h3.local.daemon" not in sys.modules and "fcntl" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), TEXT_PROMPT], cwd=root,
                            capture_output=True, encoding="utf-8", timeout=25)
    assert result.returncode == 0, result.stderr
