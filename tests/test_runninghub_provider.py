"""Offline RunningHub catalog, fee approval and task recovery."""
from __future__ import annotations

import io
import json
import subprocess
import time
import urllib.parse
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace

import pytest

from yc_h3.config import load_config
from yc_h3.ledger import summarize_usage
from yc_h3.perf_history import load_observations
from yc_h3.providers.base import ApprovalRequiredError, GenerationRequest, ProviderConflictError
from yc_h3.providers.runninghub import RunningHubProvider
from yc_h3.providers.runninghub_workflows.catalog import load_and_verify_graph, load_registry
from yc_h3.providers.video_common import ffmpeg_bin
from yc_h3.tasks_store import TaskStore

TEXT_PROMPT = """integrated_multimodal_description: [Shot 1] A tree sways.
overall_soundscape: Wind in the leaves.
non_diegetic_music: N/A
"""
IMAGE_PROMPT = """subject_definitions: <Subject 1> is pictured in <Picture 1>.
summary: A person looks toward a window.
retention_analysis: Keep the hairstyle and coat.
detailed_description: [Shot 1] The person turns.
overall_soundscape: Footsteps and room tone.
non_diegetic_music: N/A
"""
MEDIA_URL = "https://media.example.test/output.mp4"


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status
        self.headers = Message()
        self.headers["Content-Length"] = str(self.getbuffer().nbytes)

    def getcode(self):
        return self.status


class Transport:
    def __init__(self, **responses):
        self.responses = {route: list(sequence) for route, sequence in responses.items()}
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        route = urllib.parse.urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        value = self.responses[route].pop(0)
        if isinstance(value, BaseException):
            raise value
        return Response(value(request) if callable(value) else value)

    def calls(self, route):
        return [request for request in self.requests if urllib.parse.urlsplit(request.full_url).path.endswith("/" + route)]


@pytest.fixture
def render_clip(tmp_path):
    def render(width, height, frames):
        binary = ffmpeg_bin()
        if not binary:
            pytest.skip("本机 FFmpeg 未安装")
        path = tmp_path / f"clip-{width}-{height}-{frames}.mp4"
        subprocess.run(
            [binary, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=blue:s={width}x{height}:r=24",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{frames / 24:.9f}",
             "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(path)],
            capture_output=True, check=True, timeout=30,
        )
        return path.read_bytes()
    return render


def make_config(tmp_path, monkeypatch, **env):
    monkeypatch.setattr("yc_h3.config.global_env_path", lambda: tmp_path / "absent-global.env")
    return load_config(cwd=tmp_path, data_dir=tmp_path / "state", environ=env)


def runninghub_config(tmp_path, monkeypatch, **options):
    return make_config(tmp_path, monkeypatch, **{
        "RUNNINGHUB_API_KEY": "runninghub-fake-credential",
        "RUNNINGHUB_WORKFLOW_ID": "2468013579",
        "RUNNINGHUB_CNY_PER_POINT": "0.11",
        **options,
    })


def test_public_graphs_and_missing_workflow_id(tmp_path, monkeypatch):
    registry = load_registry()
    assert registry["default"] == "jinyu-hybrid"
    assert set(registry["workflows"]) == {"jinyu-hybrid", "jinyu-combat"}
    for spec in registry["workflows"].values():
        assert spec["workflow_id"] is None
        assert load_and_verify_graph(spec)

    config = runninghub_config(tmp_path, monkeypatch, RUNNINGHUB_WORKFLOW_ID="")
    provider = RunningHubProvider(config=config, auth_opener=Transport())
    request = GenerationRequest("video", prompt=TEXT_PROMPT, output_path=tmp_path / "out.mp4")
    result = provider.preflight(request)
    assert result.status == "block"
    assert result.signals[0]["code"] == "missing_workflow_id"
    assert "https://www.runninghub.cn/post/2124779546246475777" in result.signals[0]["detail"]
    assert not config.data_dir.exists()


def test_force_new_is_consumed_on_ambiguous_create(tmp_path, monkeypatch):
    config = runninghub_config(tmp_path, monkeypatch)
    transport = Transport(create=[ConnectionResetError("response lost")])
    provider = RunningHubProvider(config=config, auth_opener=transport)
    store = TaskStore(config=config)
    request = GenerationRequest("video", prompt=TEXT_PROMPT, output_path=tmp_path / "out.mp4", force_new=True)
    approval = provider.preflight(request, store=store)
    assert approval.status == "warn"
    assert approval.facts["cny_per_point"] == 0.11
    assert approval.facts["replaced_submission_id"] is None
    assert not config.data_dir.exists()
    request.approve_digest = approval.digest
    with pytest.raises(ProviderConflictError, match="提交结果不明"):
        provider.generate(request, store=store)
    task_key = approval.facts["task_key"]
    assert store.get(task_key)["state"] == "ambiguous"
    assert provider.preflight(request, store=store).facts["replaced_submission_id"] == store.get(task_key)["submission_id"]
    with pytest.raises(ProviderConflictError, match="审批已消费"):
        provider.generate(request, store=store)
    assert len(transport.calls("create")) == 1


def test_digest_binds_destination_account_rate_state_and_input_bytes(tmp_path, monkeypatch):
    config = runninghub_config(tmp_path, monkeypatch)
    opener = Transport()
    provider = RunningHubProvider(config=config, auth_opener=opener)
    material = tmp_path / "image.png"
    material.write_bytes(b"original reference")
    request = GenerationRequest("video", prompt=IMAGE_PROMPT, output_path=tmp_path / "first.mp4",
                                inputs={"images": [material]}, job_key="same-task")
    plan = provider.preflight(request)
    assert plan.status == "warn", plan.signals
    request.approve_digest = plan.digest
    variants = [
        (provider, replace(request, output_path=tmp_path / "moved.mp4"), None),
        (provider, request, TaskStore(data_dir=tmp_path / "different-state")),
        (RunningHubProvider(config=runninghub_config(tmp_path, monkeypatch, RUNNINGHUB_API_KEY="another-fake"),
                            auth_opener=opener), request, None),
        (RunningHubProvider(config=runninghub_config(tmp_path, monkeypatch, RUNNINGHUB_CNY_PER_POINT="0.12"),
                            auth_opener=opener), request, None),
    ]
    for selected, variant, store in variants:
        with pytest.raises(ApprovalRequiredError):
            selected.generate(variant, store=store)
    material.write_bytes(b"edited reference")
    with pytest.raises(ApprovalRequiredError):
        provider.generate(request)
    assert not opener.requests
    assert not config.data_dir.exists()


def test_portrait_delivery_calibrates_runninghub_quotes_and_preserves_checkpoint_reuse(tmp_path, monkeypatch, render_clip):
    config = runninghub_config(tmp_path, monkeypatch)

    def output(_):
        clock.time = lambda: 1080.0
        return {"code": 0, "data": [{"fileType": "mp4", "fileUrl": MEDIA_URL,
                                     "taskCostTime": "50", "chargedPoint": "4.2"}]}

    clock = SimpleNamespace(time=lambda: 1000.0, monotonic=time.monotonic)
    monkeypatch.setattr("yc_h3.providers.runninghub.time", clock)
    transport = Transport(create=[{"code": 0, "data": {"taskId": "portrait-offline"}}], outputs=[output])
    provider = RunningHubProvider(config=config, auth_opener=transport,
                                  download_opener=Transport(**{"output.mp4": [render_clip(928, 1664, 22)]}))
    baseline = GenerationRequest("video", prompt=TEXT_PROMPT, output_path=tmp_path / "wide.mp4")
    quote = provider.preflight(baseline)
    assert quote.facts["estimate_source"] == "initial_baseline"
    assert quote.facts["estimate_samples"] == 0
    assert quote.facts["estimated_run_seconds"] == 185
    assert quote.facts["estimated_reference_points"] == round(185 * 0.23, 1)
    assert quote.facts["estimated_wall_seconds"] is None

    request = replace(baseline, output_path=tmp_path / "portrait.mp4",
                      params={"aspect": "9:16", "seconds": 0.5, "steps": 8})
    approval = provider.preflight(request)
    assert approval.status == "warn", approval.signals
    assert approval.facts["estimate_source"] == "uncalibrated"
    assert not config.data_dir.exists()
    result = provider.generate(replace(request, approve_digest=approval.digest))
    assert result.state == "delivered"
    rows = load_observations(config, provider="runninghub")
    assert len(rows) == 1
    assert rows[0]["width"] == 928 and rows[0]["height"] == 1664
    assert rows[0]["wall_seconds"] == 80 and rows[0]["estimated_billing"] is False

    next_request = replace(request, output_path=tmp_path / "longer.mp4", params={**request.params, "seconds": 1.5})
    facts = provider.preflight(next_request).facts
    assert facts["estimate_source"] == "local_history" and facts["estimate_samples"] == 1
    assert facts["estimated_run_seconds"] == 150.0 and facts["estimated_wall_seconds"] == 240.0
    assert facts["estimate_calibrated_for_point"] is True
    assert facts["estimated_reference_points"] == 12.6 and facts["estimated_cny"] == 1.386
    assert "本地 1 条" in facts["estimate_note"] and "chargedPoint" in facts["estimate_note"]

    refreshed = provider.preflight(request)
    assert refreshed.digest != approval.digest
    assert provider.generate(replace(request, approve_digest=refreshed.digest)).recovered
    assert len(transport.calls("create")) == 1
    assert load_observations(config) == rows


def test_fetch_claimed_job_and_recover_to_another_destination(tmp_path, monkeypatch, render_clip):
    config = runninghub_config(tmp_path, monkeypatch)
    transport = Transport(create=[ConnectionResetError()], outputs=[{"code": 0, "data": [
        {"fileType": "mp4", "fileUrl": MEDIA_URL, "chargedPoint": "1.25"},
    ]}])
    provider = RunningHubProvider(config=config, auth_opener=transport,
                                  download_opener=Transport(**{"output.mp4": [render_clip(320, 192, 22)]}))
    store = TaskStore(config=config)
    old, new = (tmp_path / name for name in ("old.mp4", "new.mp4"))
    new.write_bytes(b"existing user artifact")
    request = GenerationRequest("video", prompt=TEXT_PROMPT, output_path=old, params=dict(seconds=0.5, width=320, height=192),
                                metadata={"tag": "test-shot"})
    plan = provider.preflight(request, store=store)
    with pytest.raises(ProviderConflictError):
        provider.generate(replace(request, approve_digest=plan.digest), store=store)
    def unavailable(*args, **kwargs):
        raise PermissionError("simulated observation write failure")

    with monkeypatch.context() as patch:
        patch.setattr("yc_h3.perf_history.record_observation", unavailable)
        result = provider.fetch(plan.facts["task_key"], store=store, remote_job_id="run-offline", output_path=new)
    assert load_observations(config) == []
    assert result.artifact_path == str(tmp_path / "new_v2.mp4")
    assert new.read_bytes() == b"existing user artifact"
    assert not old.exists()
    again = provider.resume(result.task_key, store=store, output_path=old)
    assert again.artifact_path == str(old)
    observation, = load_observations(config)
    assert observation["wall_seconds"] > 0
    summary = summarize_usage(config=config, project="test-shot", account_fp=plan.target_identity["credential_fingerprint"])
    assert summary["jobs"][0]["quantity"] == 1.25
    assert summary["jobs"][0]["count"] == 1
