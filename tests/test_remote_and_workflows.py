import asyncio
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from yc_h3.remote_bundle import REMOTE_DIR, pack_remote_tar, remote_source_version


def remote_test_env(root, **variables):
    env = {name: os.environ[name] for name in ("PATH", "SystemRoot") if name in os.environ}
    return {**env, "PYTHONUTF8": "1", "YC_H3_REMOTE_ROOT": str(root), **variables}


def test_workflow_spec_imports_without_posix_or_remote_runtime():
    script = """
import sys

class BlockRuntime:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'fcntl', 'yc_h3.remote.setup_env'}:
            raise ImportError(fullname)

sys.meta_path.insert(0, BlockRuntime())
from yc_h3.remote.workflows.minimax_h3 import spec
assert spec.DEFAULT_WORKFLOW == 'jinyu-hybrid'
assert spec.workflow_id('h3-jinyu-combat') == 'jinyu-combat'
assert spec.workflow_id('chenyu-jinyu-hybrid') == 'jinyu-hybrid'
assert spec.output_frames({'seconds': 5}) == 124
assert spec.snap_down_17k5(120) == 107
params = spec.normalize_request('r2v', {'images': ['frame.png'], 'seconds': 2.5, 'aspect': '9:16'})
assert (params['width'], params['height']) == (928, 1664)
assert 'yc_h3.remote.setup_env' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_registry_only_exposes_supported_workflows_and_builds_graphs(monkeypatch):
    from yc_h3.remote.workflows.minimax_h3 import registry

    assert registry.DEFAULT_WORKFLOW == "jinyu-hybrid"
    assert set(registry.WORKFLOWS) == {"jinyu-hybrid", "jinyu-combat"}
    profile = registry.resolve_hardware_profile(96, 96, "offline GPU")
    for name, workflow in registry.WORKFLOWS.items():
        params = workflow.normalize("r2v", {"workflow": name, "images": ["example.png"]}, profile)
        assert params["text_encoder"] == "heretic"
        assert "hybrid_b25_49" in workflow.assets(profile, params)
        assert workflow.plugins(profile, {"use_selflift": True}) == ("ComfyUI-KJNodes", "comfyui-SelfLift")
        assert workflow.plugins(profile, {"use_selflift": False}) == ("ComfyUI-KJNodes",)
        with monkeypatch.context() as patch:
            patch.delitem(workflow.manifest, "plugins")
            assert workflow.plugins(profile, {}) == ("ComfyUI-KJNodes", "comfyui-SelfLift")
        params.update(prompt="Offline graph", seed=123)
        graph, output = workflow.build(params, profile, "job_example")
        assert graph[output]["class_type"] == "SaveVideo"
    for obsolete in ("official-fl2va", "h3-fight", "h3-swap"):
        with pytest.raises(ValueError, match="未知工作流"):
            registry.workflow_id(obsolete)


def test_jinyu_builder_preserves_sparse_video_audio_slot_alignment():
    from yc_h3.remote.workflows.minimax_h3 import registry

    profile = registry.resolve_hardware_profile(96, 96, "offline GPU")
    workflow = registry.get_workflow("jinyu-hybrid")
    params = workflow.normalize(
        "r2v",
        {
            "workflow": "jinyu-hybrid",
            "prompt": "Offline sparse audio graph",
            "seed": 7,
            "images": ["hero.png"],
            "videos": [{"path": "silent.mp4", "use_audio": False}, {"path": "spoken.mp4", "use_audio": True}],
            "audios": ["voice.wav"],
        },
        profile,
    )
    graph, _ = workflow.build(params, profile, "job_sparse")
    ref_node = next(node for node in graph.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    inputs = ref_node["inputs"]
    assert "ref_images.ref_image_0" in inputs
    assert "ref_videos.ref_video_0" in inputs and "ref_videos.ref_video_1" in inputs
    assert "ref_video_audios.ref_video_audio_0" not in inputs
    assert "ref_video_audios.ref_video_audio_1" in inputs
    assert "ref_audios.ref_audio_0" in inputs


@pytest.mark.parametrize("invalid", ["missing", "mismatched", "unsupported"])
def test_registry_rejects_invalid_plugin_manifests(tmp_path, invalid):
    from yc_h3.remote.workflows.minimax_h3 import registry

    workflow = registry.get_workflow("jinyu-hybrid")
    directory = tmp_path / workflow.family / workflow.directory.name
    directory.mkdir(parents=True)
    (directory / "builder.py").touch()
    manifest = json.loads((workflow.directory / "manifest.json").read_text(encoding="utf-8"))
    if invalid == "missing":
        del manifest["plugins"]
    elif invalid == "mismatched":
        manifest["plugins"].remove("comfyui-SelfLift")
    else:
        manifest["plugins"].append("UnknownPlugin")
        manifest["plugin_nodes"]["UnknownPlugin"] = "FixtureNode"
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    message = "不受支持的插件" if invalid == "unsupported" else "plugins 与 plugin_nodes"
    with pytest.raises(ValueError, match=message):
        registry.load_workflows(tmp_path)


@pytest.mark.parametrize("workflow_id", ["jinyu-hybrid", "jinyu-combat"])
@pytest.mark.parametrize("use_selflift", [True, False])
def test_real_mode_preflight_checks_required_plugins(tmp_path, monkeypatch, workflow_id, use_selflift):
    from yc_h3.remote import preflight
    from yc_h3.remote.workflows.minimax_h3 import registry

    checked_plugins = []

    def check_plugin(plugin, profile_id):
        checked_plugins.append(plugin)
        return plugin != "comfyui-SelfLift"

    monkeypatch.setattr(preflight.setup_env, "comfy_runtime_installed", lambda *_: True)
    monkeypatch.setattr(preflight.setup_env, "custom_node_installed", check_plugin)
    monkeypatch.setattr(preflight.comfy_assets, "weight_ready", lambda *_: True)
    monkeypatch.setattr(preflight, "_free_space", lambda _: 1024**3)
    facts, _ = preflight.evaluate_preflight(
        instance_id="offline-instance", profile=registry.resolve_hardware_profile(96, 96, "offline GPU"),
        model={"id": "minimax-h3"}, workflow=registry.get_workflow(workflow_id), task="t2v",
        params={"workflow": workflow_id, "prompt": "Offline preflight", "use_selflift": use_selflift},
        input_root=tmp_path, read_assets=dict, simulated=False, check_runtime=True,
    )
    assert facts["simulated_gpu"] is False
    missing = [signal for signal in facts["signals"] if signal["code"] == "runtime_or_plugins_not_ready"]
    if use_selflift:
        assert checked_plugins == ["ComfyUI-KJNodes", "comfyui-SelfLift"]
        assert facts["status"] == "block" and missing[0]["missing_plugins"] == ["comfyui-SelfLift"]
    else:
        assert checked_plugins == ["ComfyUI-KJNodes"]
        assert facts["status"] in ("pass", "warn") and not missing


def test_image_inspection_uses_exif_orientation(tmp_path):
    from yc_h3.remote.workflows.image import inspect_image

    path = tmp_path / "frame.jpg"
    image = Image.new("RGB", (64, 96))
    image.getexif()[274] = 6
    image.save(path, exif=image.getexif())
    assert inspect_image(path) == {"width": 96, "height": 64}


def test_crlf_checkout_matches_standalone_deployed_health_and_preflight(tmp_path):
    from yc_h3.remote import setup_env

    windows_tree = tmp_path / "checkout"
    for path in REMOTE_DIR.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        target = windows_tree / path.relative_to(REMOTE_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    deployed = tmp_path / "deployed"
    with tarfile.open(fileobj=io.BytesIO(pack_remote_tar(windows_tree)), mode="r:gz") as archive:
        assert archive.getmember("bootstrap_remote.sh").mode == 0o755
        archive.extractall(deployed, filter="data")
    expected = remote_source_version(windows_tree)
    assert setup_env.remote_source_version(windows_tree) == expected == remote_source_version(deployed)
    script = """
import asyncio
import httpx
import forge_server

profile = forge_server.h3.resolve_hardware_profile(96, 96, 'offline GPU')
forge_server.h3.probe_hardware = lambda: profile
forge_server._available_vram = lambda profile: profile.vram_gb
forge_server.preflight.setup_env.comfy_runtime_installed = lambda *_: True
forge_server.preflight.setup_env.custom_node_installed = lambda plugin, _: plugin != 'comfyui-SelfLift'
forge_server.preflight.comfy_assets.weight_ready = lambda *_: True
forge_server.preflight._free_space = lambda _: 1024**3
forge_server._load_registry()

async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=forge_server.app),
                                 base_url='http://test') as client:
        response = await client.get('/api/health')
        response.raise_for_status()
        print(response.text)
        body = {'task': 't2v', 'params': {'prompt': 'Offline preflight'}}
        blocked = await client.post('/api/preflight', json=body)
        assert blocked.status_code == 409, blocked.text
        facts = blocked.json()['detail']
        assert facts['simulated_gpu'] is False
        missing = next(signal for signal in facts['signals'] if signal['code'] == 'runtime_or_plugins_not_ready')
        assert missing['missing_plugins'] == ['comfyui-SelfLift']
        body['params']['use_selflift'] = False
        ready = await client.post('/api/preflight', json=body)
        assert ready.status_code == 200, ready.text
        assert ready.json()['status'] in ('pass', 'warn')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=deployed, env=remote_test_env(tmp_path / "runtime"),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    health = json.loads(result.stdout)
    assert (health["app"], health["version"]) == ("yc-h3-engine", expected)


def test_comfy_worker_uses_server_instance_namespace_for_output_and_cancellation(tmp_path):
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from yc_h3.remote import comfy_worker as worker, forge_server as server

root = Path(server.ROOT)
assert worker.OUTPUTS == Path(server.OUTPUTS) == root / 'outputs' / 'fixture-vm'
assert worker.JOBS_DIR == Path(server.JOBS_DIR) == root / 'jobs' / 'fixture-vm'
job_id = server.new_job('minimax-h3', 't2v', {}, jid='job_fixture')['id']
other_job = root / 'jobs' / job_id / 'job.json'
other_job.parent.mkdir()
other_job.write_text('{"status": "cancelled"}', encoding='utf-8')
worker.check_cancelled(job_id)
server.update_job(job_id, cancel_requested=True)
with pytest.raises(worker.Cancelled):
    worker.check_cancelled(job_id)
server.update_job(job_id, cancel_requested=False, status='cancelled')
with pytest.raises(worker.Cancelled):
    worker.check_cancelled(job_id)

job_id = server.new_job('minimax-h3', 't2v', {}, jid='job_output_fixture')['id']
events, submissions = [], []
worker.emit = lambda *args, **event: events.append(event)
worker._ensure_workflow_weights = lambda *_: None
worker._process_vram = lambda _: 0
worker.submit_prompt = lambda *args: submissions.append(args)
worker.publish_output = lambda workflow, history, save_id, directory, *args: {'directory': str(directory)}
sys.modules['websocket'] = SimpleNamespace(create_connection=lambda *args, **kwargs: None)
workflow = worker.workflows.get_workflow('jinyu-hybrid')
profile = worker.registry.resolve_hardware_profile(96, 96, 'offline GPU')
params = workflow.normalize('t2v', {'prompt': 'Offline inference', 'seed': 123}, profile)
graph, _ = workflow.build(params, profile, job_id)
comfy = SimpleNamespace(start=lambda *_: None, port=0, api=None, proc=SimpleNamespace(pid=1), baseline=0,
                        nodes={node['class_type']: None for node in graph.values()},
                        wait_history=lambda *args, **kwargs: {})
command = {'job_id': job_id, 'task': 't2v', 'model': {'engine': 'minimax_h3'}, 'params': params}
worker.run_job(comfy, dict(command, out_dir=str(root / 'outputs' / job_id)), profile)
assert events[-1]['event'] == 'error' and events[-1]['msg'] == 'ComfyUI 输出目录无效', events
assert not submissions
out = Path(server.OUTPUTS) / job_id
out.mkdir()
worker.run_job(comfy, dict(command, out_dir=str(out)), profile)
assert events[-1]['event'] == 'done' and events[-1]['outputs'] == [{'directory': str(out.resolve())}], events
assert len(submissions) == 1 and (out / 'workflow.executed.json').is_file()
"""
    subprocess.run([sys.executable, "-c", script], check=True,
                   env=remote_test_env(tmp_path, YC_H3_PLATFORM_INSTANCE_ID="fixture-vm"))


def test_remote_inventory_fails_closed_and_cleanup_preserves_deliverables(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("YC_H3_REMOTE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("YC_H3_SESSION", "yh3-test")
    monkeypatch.setenv("YC_H3_REMOTE_AUTH_TOKEN", "synthetic-token-" + "x" * 32)
    monkeypatch.delenv("YC_H3_PLATFORM_INSTANCE_ID", raising=False)
    server = importlib.import_module("yc_h3.remote.forge_server")
    root = Path(server.ROOT)
    job = server.new_job("minimax-h3", "r2v", {}, jid="job_unclaimed")
    server.update_job(job["id"], status="done")
    output = root / "outputs" / job["id"] / "result.mp4"
    output.parent.mkdir()
    output.write_bytes(b"unclaimed footage")
    source = root / "inputs" / "source.png"
    source.write_bytes(b"user input")
    cache_root = root / "cache"
    for name in ("pip", "uv", "huggingface"):
        path = cache_root / name / "cached"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"rebuildable")
    shared = tmp_path / "other-app-cache"
    shared.mkdir()
    (shared / "keep").write_bytes(b"outside engine")
    monkeypatch.setenv("YC_H3_CLEANUP_CACHE_DIR", str(shared))

    async def exercise_routes():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test",
            headers={"Authorization": "Bearer " + os.environ["YC_H3_REMOTE_AUTH_TOKEN"]},
        ) as client:
            endpoint = "/api/maintenance/job-inventory"
            inventory = (await client.get(endpoint)).json()
            assert inventory["complete"] and inventory["session"] == "yh3-test"
            assert inventory["jobs"][0]["id"] == job["id"]
            assert inventory["jobs"][0]["has_outputs"]
            resp = await client.post("/api/maintenance/cleanup-for-image")
            assert resp.status_code == 200
            assert len(resp.json()["cleaned"]) == 3
            assert output.read_bytes() == b"unclaimed footage"
            assert source.read_bytes() == b"user input"
            assert (shared / "keep").is_file()
            path = root / "jobs" / job["id"] / "job.json"
            assert path.is_file()
            token = os.environ["YC_H3_REMOTE_AUTH_TOKEN"]
            server.update_job(job["id"], stage=f"Bearer {token}")
            server.job_log(job["id"], f"diagnostic: {token}")
            assert token not in path.read_text(encoding="utf-8")
            assert token not in path.with_name("log.txt").read_text(encoding="utf-8")
            path.write_text("{", encoding="utf-8")
            assert (await client.get(endpoint)).status_code == 409

    asyncio.run(exercise_routes())
