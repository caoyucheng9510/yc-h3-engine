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


def test_registry_only_exposes_supported_workflows_and_builds_graphs():
    from yc_h3.remote.workflows.minimax_h3 import registry

    assert registry.DEFAULT_WORKFLOW == "jinyu-hybrid"
    assert set(registry.WORKFLOWS) == {"jinyu-hybrid", "jinyu-combat"}
    profile = registry.resolve_hardware_profile(96, 96, "offline GPU")
    for name, workflow in registry.WORKFLOWS.items():
        params = workflow.normalize("r2v", {"workflow": name, "images": ["example.png"]}, profile)
        assert params["text_encoder"] == "heretic"
        assert "hybrid_b25_49" in workflow.assets(profile, params)
        params.update(prompt="Offline graph", seed=123)
        graph, output = workflow.build(params, profile, "job_example")
        assert graph[output]["class_type"] == "SaveVideo"
    for obsolete in ("official-fl2va", "h3-fight", "h3-swap"):
        with pytest.raises(ValueError, match="未知工作流"):
            registry.workflow_id(obsolete)


def test_image_inspection_uses_exif_orientation(tmp_path):
    from yc_h3.remote.workflows.image import inspect_image

    path = tmp_path / "frame.jpg"
    image = Image.new("RGB", (64, 96))
    image.getexif()[274] = 6
    image.save(path, exif=image.getexif())
    assert inspect_image(path) == {"width": 96, "height": 64}


def test_crlf_checkout_matches_standalone_deployed_health(tmp_path):
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
    env = {**os.environ, "YC_H3_REMOTE_ROOT": str(tmp_path / "runtime")}
    env.pop("PYTHONPATH", None)
    script = """
import asyncio
import httpx
import forge_server

async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=forge_server.app),
                                 base_url='http://test') as client:
        response = await client.get('/api/health')
        response.raise_for_status()
        print(response.text)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=deployed, env=env, check=True, capture_output=True, encoding="utf-8", errors="replace",
    )
    health = json.loads(result.stdout)
    assert (health["app"], health["version"]) == ("yc-h3-engine", expected)


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
