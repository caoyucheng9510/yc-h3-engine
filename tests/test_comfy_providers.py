"""Offline tests for the shared managed generation and delivery paths."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from yc_h3.config import credential_fingerprint
from yc_h3.fsutil import sha256_bytes
from yc_h3.perf_history import load_observations
from yc_h3.providers import video_common
from yc_h3.providers.base import ApprovalRequiredError, ArtifactContractError, GenerationRequest, ProviderConflictError
from yc_h3.providers.chenyu_comfy import ChenyuComfyProvider
from yc_h3.providers.colab_comfy import ColabComfyProvider
from yc_h3.tasks_store import TaskStore, delivery_confirmed
from tests.test_runninghub_provider import TEXT_PROMPT, make_config, render_clip


class Daemon:
    def __init__(self, config, data=b"", *, chenyu=False):
        self.config = config
        self.data = data
        self.chenyu = chenyu
        self.origin = "https://instance.example.test" if chenyu else "http://127.0.0.1:7798"
        self.port = 7798
        self.session = "yh3-offline"
        self.vm = "fake-vm"
        self.instance = "remote-process"
        self.rate = 4.0
        self.hardware = None
        self.perf = {}
        self.cold_start = False
        self.jobs = {}
        self.calls = []
        self.submit_calls = []
        self.drop = False
        self.hide_index = False

    def health(self):
        self.calls.append("health")
        return {"app": "yc-h3-engine", "ok": True, "data_dir_hash": self.config.data_dir_hash,
                "config_identity": self.config.config_identity}

    def state(self):
        self.calls.append("state")
        remote = {"instance_id": self.instance}
        if self.chenyu:
            remote["platform_instance_id"] = self.vm
        return {"tunnel": {"session": self.session}, "remote": remote,
                "settings": {"managed_sessions": {self.session: {"host": self.vm}}}}

    def preflight(self, payload):
        self.calls.append("preflight")
        return 200, {"status": "pass", "instance_id": self.instance, "preflight_digest": "4" * 64,
                     "managed_instance_id": self.vm if self.chenyu else None,
                     "hardware": {"gpu_name": self.hardware}, "perf": self.perf,
                     "submission_preflight": "private-handle" if payload.get("for_submission") else None,
                     "billing_context": {"session_name": self.session, "vm_host": self.vm,
                                         "billing_rate": self.rate, "endpoint_origin": self.origin}}

    def submit_job(self, payload):
        self.submit_calls.append(copy.deepcopy(payload))
        request_id = payload["client_request_id"]
        previous = next((job_id for job_id, row in self.jobs.items() if row["client_request_id"] == request_id), None)
        if previous:
            return 200, {"job_id": previous}
        job_id = f"offjob-{len(self.jobs) + 1}"
        self.jobs[job_id] = {
            "id": job_id, "client_request_id": request_id, "status": "done", "params": copy.deepcopy(payload["params"]),
            "created": 4, "started": 10, "finished": 20,
            "outputs": [{"path": f"/jobs/{job_id}/clip.mp4", "sha256": sha256_bytes(self.data)}],
        }
        if self.drop:
            self.drop = False
            raise ConnectionResetError("lost response after accepting")
        return 200, {"job_id": job_id}

    def list_jobs(self):
        return [] if self.hide_index else list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs[job_id]

    def download_file(self, remote_path, path):
        path.write_bytes(self.data)

    def archive_perf(self, job_id):
        return {"archived": True, "cold_start": self.cold_start}


def request_at(tmp_path, **options):
    return GenerationRequest("video", prompt=TEXT_PROMPT, output_path=tmp_path / "out.mp4", **{
        "params": {"width": 320, "height": 192, "seconds": 0.5}, **options,
    })


def test_pass_requires_approval_and_checks_strict_decode(tmp_path, monkeypatch, render_clip):
    config = make_config(tmp_path, monkeypatch)
    daemon = Daemon(config, render_clip(320, 192, 22))
    provider = ColabComfyProvider(client=daemon, config=config)
    request = request_at(tmp_path)
    approval = provider.preflight(request)
    assert approval.status == "pass", approval.signals
    assert not config.data_dir.exists()
    with pytest.raises(ApprovalRequiredError):
        provider.generate(request)
    assert not daemon.submit_calls
    request.approve_digest = approval.digest
    daemon.rate = 5.0
    with pytest.raises(ApprovalRequiredError):
        provider.generate(request)
    daemon.rate = 4.0
    commands = []
    strict = video_common.default_ffmpeg

    def record_command(args):
        commands.append(args)
        strict(args)

    monkeypatch.setattr(video_common, "default_ffmpeg", record_command)
    result = provider.generate(request)
    assert result.artifact_path == str(request.output_path)
    assert commands[0][:4] == ["-v", "error", "-xerror", "-i"]
    assert commands[0][5:] == ["-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]
    assert result.perf_archived is True
    assert "private-handle" not in TaskStore(config=config).task_path(result.task_key).read_text()


@pytest.mark.parametrize("provider_class", [ColabComfyProvider, ChenyuComfyProvider])
def test_comfy_quotes_prioritize_exact_then_normalize_warm_same_gpu(
    tmp_path, monkeypatch, render_clip, provider_class,
):
    config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="chenyu-fake-key", COLAB_CNY_PER_CU="0.5")
    chenyu = provider_class is ChenyuComfyProvider
    daemon = Daemon(config, render_clip(320, 192, 22), chenyu=chenyu)
    daemon.hardware = "NVIDIA L4"
    daemon.cold_start = True
    provider = provider_class(client=daemon, config=config)
    store = TaskStore(config=config)
    request = request_at(tmp_path)
    approval = provider.preflight(request, store=store)
    assert approval.facts["estimate_source"] == "uncalibrated"
    assert not config.data_dir.exists()
    cold = provider.generate(replace(request, approve_digest=approval.digest), store=store)

    longer = replace(request, output_path=tmp_path / "longer.mp4", params={**request.params, "seconds": 1.0})
    assert provider.preflight(longer, store=store).facts["estimate_source"] == "uncalibrated"
    daemon.cold_start = False
    warm = replace(request, output_path=tmp_path / "warm.mp4")
    approval = provider.preflight(warm, store=store)
    result = provider.generate(replace(warm, approve_digest=approval.digest), store=store)
    assert result.state == "delivered"
    assert provider.fetch(result.task_key, store=store).recovered
    rows = load_observations(config, provider=provider.provider_id)
    assert [row["idempotency_key"] for row in rows] == [
        f"{provider.provider_id}:{cold.remote_id}", f"{provider.provider_id}:{result.remote_id}",
    ]
    assert rows[0]["cold_start"] is True and rows[1]["cold_start"] is False
    assert rows[1]["hardware"] == "NVIDIA L4" and rows[1]["output_seconds"] == 0.5
    assert rows[1]["width"] == 320 and rows[1]["height"] == 192
    assert rows[1]["run_seconds"] == 10 and rows[1]["wall_seconds"] == 16
    assert rows[1]["unit"] == ("CNY" if chenyu else "CU") and rows[1]["estimated_billing"] is True

    daemon.hardware = " nvidia l4 "
    facts = provider.preflight(longer, store=store).facts
    assert facts["estimate_source"] == "local_history_normalized" and facts["estimate_samples"] == 1
    assert facts["estimate_scope"] == "warm_run"
    assert facts["estimate_seconds"] == 20.0 and facts["estimated_wall_seconds"] == 32.0
    assert facts["estimated_quantity"] == 0.022222
    assert facts["estimated_cny"] == (0.022222 if chenyu else 0.011111)

    daemon.perf = {"comparable_samples": 2, "warm_run": {"median_total_s": 45},
                   "cold_start": {"median_total_s": 90}}
    exact = provider.preflight(longer, store=store).facts
    assert exact["estimate_source"] == "local_history_exact" and exact["estimate_samples"] == 2
    assert exact["estimate_scope"] == "warm_run"
    assert exact["estimate_seconds"] == exact["estimated_wall_seconds"] == 45.0
    assert exact["estimated_quantity"] == 0.05
    assert exact["estimated_cny"] == (0.05 if chenyu else 0.025)

    daemon.perf = {"comparable_samples": 1, "cold_start": {"median_total_s": 90}}
    exact_cold = provider.preflight(longer, store=store).facts
    assert exact_cold["estimate_source"] == "local_history_exact" and exact_cold["estimate_scope"] == "cold_start"
    assert exact_cold["estimate_seconds"] == exact_cold["estimated_wall_seconds"] == 90.0

    daemon.perf = {}
    for card in ("NVIDIA A100", None):
        daemon.hardware = card
        unmatched = provider.preflight(longer, store=store).facts
        assert unmatched["estimate_source"] == "uncalibrated" and unmatched["estimate_samples"] == 0
        assert all(unmatched[key] is None for key in (
            "estimate_seconds", "estimated_wall_seconds", "estimate_scope", "estimated_quantity", "estimated_cny",
        ))


def test_decode_failure_keeps_final_path_and_fetch_can_retry(tmp_path, monkeypatch, render_clip):
    config = make_config(tmp_path, monkeypatch)
    daemon = Daemon(config, render_clip(320, 192, 22))
    provider = ColabComfyProvider(client=daemon, config=config)
    store = TaskStore(config=config)
    request = request_at(tmp_path)
    plan = provider.preflight(request, store=store)
    assert plan.status == "pass", plan.signals
    request.approve_digest = plan.digest
    old_decode = video_common.default_ffmpeg

    def fail_decode(args):
        assert "-xerror" in args
        raise ArtifactContractError("simulated decoder rejects corrupt frames")

    monkeypatch.setattr(video_common, "default_ffmpeg", fail_decode)
    with pytest.raises(ArtifactContractError, match="双流严格解码"):
        provider.generate(request, store=store)
    assert not request.output_path.exists()
    assert load_observations(config) == []
    record = store.get(plan.facts["task_key"])
    assert record["state"] == "submitted" and record["reserved_output_path"] is None
    monkeypatch.setattr(video_common, "default_ffmpeg", old_decode)
    result = provider.fetch(record["task_key"], store=store, output_path=tmp_path / "retrieved.mp4")
    assert result.state == "delivered"
    assert Path(result.artifact_path).is_file()
    assert len(load_observations(config)) == 1


def test_ambiguous_force_new_replays_original_request_id_only_on_same_instance(tmp_path, monkeypatch, render_clip):
    config = make_config(tmp_path, monkeypatch)
    daemon = Daemon(config, render_clip(320, 192, 22))
    daemon.drop = daemon.hide_index = True
    provider = ColabComfyProvider(client=daemon, config=config)
    store = TaskStore(config=config)
    request = request_at(tmp_path, force_new=True)
    plan = provider.preflight(request, store=store)
    request.approve_digest = plan.digest
    with pytest.raises(ProviderConflictError, match="提交结果不明"):
        provider.generate(request, store=store)
    task_key = plan.facts["task_key"]
    assert store.get(task_key)["state"] == "ambiguous"
    with pytest.raises(ProviderConflictError, match="审批已消费"):
        provider.generate(request, store=store)
    daemon.instance = "different-remote-process"
    with pytest.raises(ProviderConflictError, match="GPU 会话"):
        provider.resume(task_key, store=store)
    assert len(daemon.submit_calls) == 1
    daemon.instance = "remote-process"
    result = provider.resume(task_key, store=store)
    assert result.artifact_path == str(request.output_path)
    assert len(daemon.jobs) == 1 and len(daemon.submit_calls) == 2
    assert "submission_preflight" not in daemon.submit_calls[1]
    assert {key: val for key, val in daemon.submit_calls[0].items() if key != "submission_preflight"} == daemon.submit_calls[1]


def test_chenyu_fetches_historical_job_and_preserves_multiple_proofs(tmp_path, monkeypatch, render_clip):
    config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="chenyu-fake-key")
    daemon = Daemon(config, render_clip(320, 192, 22), chenyu=True)
    provider = ChenyuComfyProvider(client=daemon, config=config)
    store = TaskStore(config=config)
    request = request_at(tmp_path, metadata={"tag": "clip"})
    plan = provider.preflight(request, store=store)
    assert plan.status == "pass", plan.signals
    request.approve_digest = plan.digest
    first = provider.generate(request, store=store)
    Path(first.artifact_path).unlink()
    second_request = replace(request, force_new=True)
    second_request.approve_digest = provider.preflight(second_request, store=store).digest
    second = provider.generate(second_request, store=store)
    recovered = provider.fetch(second.task_key, store=store, output_path=tmp_path / "historical.mp4",
                               remote_job_id=first.remote_id)
    assert recovered.remote_id == first.remote_id
    assert recovered.artifact_path == str(tmp_path / "historical.mp4")
    current = store.get(second.task_key)
    assert current["remote_job_id"] == second.remote_id
    assert len(load_observations(config)) == 2
    proofs = store.list_job_history(provider="chenyu-comfy", instance_uuid=daemon.vm)
    assert len(proofs) == 2 and all(delivery_confirmed(row) for row in proofs)
    lines = (config.data_dir / "ledger" / "usage.jsonl").read_text().splitlines()
    for entry in map(json.loads, lines):
        assert entry["provider"] == "chenyu-comfy" and entry["wallet_owner"] == "chenyu"
        assert entry["account_fp"] == credential_fingerprint(config.get("CHENYU_API_KEY"))
    assert len(lines) == 2


def test_colab_daemon_config_identity_conflict_stops_before_request(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    wrong = replace(config, config_identity="other-config")
    daemon = Daemon(wrong)
    provider = ColabComfyProvider(client=daemon, config=config)
    result = provider.preflight(request_at(tmp_path))
    assert result.status == "block"
    assert result.signals[0]["code"] == "daemon_identity_conflict"
    assert daemon.calls == ["health"]


def test_uploaded_reference_produces_same_remote_approval_as_preview(tmp_path, monkeypatch, render_clip):
    from PIL import Image
    from yc_h3.remote import preflight as remote_preflight
    from yc_h3.remote.workflows.minimax_h3 import registry as remote_registry
    from tests.test_runninghub_provider import IMAGE_PROMPT

    config = make_config(tmp_path, monkeypatch)
    source = tmp_path / "local.png"
    Image.new("RGB", (32, 32)).save(source)
    remote_inputs = tmp_path / "remote-uploads"
    remote_inputs.mkdir()
    monkeypatch.setattr(remote_preflight.comfy_assets, "weight_ready", lambda *_: True)

    class RemoteDaemon(Daemon):
        def review(self, payload):
            return remote_preflight.evaluate_preflight(
                instance_id=self.instance, profile=remote_registry.resolve_hardware_profile(96, 96, "A100-SXM4-80GB"),
                model={"id": "minimax-h3"}, workflow=remote_registry.get_workflow("jinyu-hybrid"),
                task=payload["task"], params=payload["params"], input_facts=payload.get("input_facts"),
                input_root=remote_inputs, read_assets=dict, check_runtime=False,
                billing_context={"provider_id": "colab-comfy", "session_name": self.session, "vm_host": self.vm,
                                 "billing_rate": self.rate, "endpoint_origin": self.origin},
            )[0]

        def preflight(self, payload):
            result = self.review(payload)
            return (409 if result["status"] == "block" else 200), result

        def upload(self, filename, data):
            path = remote_inputs / filename
            path.write_bytes(data)
            return {"remote_path": str(path)}

        def submit_job(self, payload):
            assert self.review(payload)["preflight_digest"] == payload["approve_digest"]
            return super().submit_job(payload)

    client = RemoteDaemon(config, render_clip(320, 192, 22))
    provider = ColabComfyProvider(client=client, config=config)
    request = replace(request_at(tmp_path), prompt=IMAGE_PROMPT, inputs={"images": [source]})
    plan = provider.preflight(request)
    assert plan.status in ("pass", "warn"), plan.signals
    assert plan.input_hashes == {"images.0": sha256_bytes(source.read_bytes())}
    result = provider.generate(replace(request, approve_digest=plan.digest))
    assert result.state == "delivered"


def test_comfy_normalized_fallback_requires_run_seconds(tmp_path, monkeypatch):
    from yc_h3.perf_history import record_observation

    config = make_config(tmp_path, monkeypatch)
    daemon = Daemon(config)
    daemon.hardware = "A100-SXM4-80GB"
    record_observation(
        config, idempotency_key="colab-comfy:wall-only-1", provider="colab-comfy",
        workflow="jinyu-hybrid", hardware="A100-SXM4-80GB", width=320, height=192,
        output_seconds=1.0, has_ref_video=False, run_seconds=None, wall_seconds=15.0,
        billed_quantity=None, unit="CU", estimated_billing=True, cold_start=False,
    )
    provider = ColabComfyProvider(client=daemon, config=config)
    plan = provider.preflight(request_at(tmp_path))
    assert plan.status == "pass", plan.signals
    assert plan.facts["estimate_source"] == "uncalibrated"
    assert plan.facts["estimate_samples"] == 0
    assert plan.facts["estimate_seconds"] is None
    assert plan.facts["estimated_quantity"] is None


def test_comfy_resumed_delivery_backfills_observation_after_transient_write_error(tmp_path, monkeypatch, render_clip):
    from yc_h3 import perf_history

    config = make_config(tmp_path, monkeypatch)
    daemon = Daemon(config, render_clip(320, 192, 22))
    daemon.hardware = "A100-SXM4-80GB"
    daemon.cold_start = True
    provider = ColabComfyProvider(client=daemon, config=config)
    store = TaskStore(config=config)
    request = request_at(tmp_path)
    plan = provider.preflight(request, store=store)
    request.approve_digest = plan.digest

    real_record = perf_history.record_observation
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temporary disk busy")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(perf_history, "record_observation", fail_once)
    first = provider.generate(request, store=store)
    assert first.state == "delivered"
    assert load_observations(config) == []

    resumed = provider.fetch(first.task_key, store=store)
    assert resumed.artifact_path == first.artifact_path
    rows = load_observations(config)
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == f"colab-comfy:{first.remote_id}"
    assert rows[0]["hardware"] == "A100-SXM4-80GB"
    assert rows[0]["cold_start"] is True


def test_comfy_preflight_prompt_bindings_r2v_and_i2v(tmp_path, monkeypatch, render_clip):
    from PIL import Image
    from yc_h3.providers.comfy_common import prompt_bindings

    secret = "store-secret-token"
    config = make_config(tmp_path, monkeypatch)
    store_config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY=secret)
    store = TaskStore(config=store_config)
    daemon = Daemon(config, render_clip(640, 384, 22))
    daemon.upload = lambda filename, data: {"remote_path": f"/inputs/{filename}"}
    provider = ColabComfyProvider(client=daemon, config=config)

    secret_dir = tmp_path / secret
    secret_dir.mkdir()
    img = secret_dir / "hero.png"
    Image.new("RGB", (64, 36)).save(img)
    vid_sound = tmp_path / "with_audio.mp4"
    vid_silent = tmp_path / "silent.mp4"
    aud = tmp_path / "voice.wav"
    for p in (vid_sound, vid_silent, aud):
        p.write_bytes(b"media")

    def fake_probe(path):
        name = Path(path).name
        if name == "with_audio.mp4":
            return {"streams": [
                {"codec_type": "video", "index": 0, "width": 640, "height": 360, "duration": "2.0"},
                {"codec_type": "audio", "index": 1, "duration": "2.0"},
            ]}
        if name == "silent.mp4":
            return {"streams": [
                {"codec_type": "video", "index": 0, "width": 640, "height": 360, "duration": "2.0"},
            ]}
        return {"streams": [{"codec_type": "audio", "index": 0, "duration": "2.0"}]}

    r2v_prompt = """subject_definitions: <Subject 1> in <Picture 1>, <Video 1>, <Video 2>, <Audio 1>, <Audio 2>.
summary: Summary.
retention_analysis: Keep.
detailed_description: [Shot 1] Action.
overall_soundscape: Ambient.
non_diegetic_music: N/A
"""
    # Sparse video audio order: first video silent, second video with audio -> <Audio 1> comes from <Video 2>.
    with monkeypatch.context() as patch:
        patch.setattr("yc_h3.providers.video_common.default_ffprobe", fake_probe)
        sparse_plan = provider.preflight(GenerationRequest(
            "video", prompt=r2v_prompt, output_path=tmp_path / "sparse.mp4",
            inputs={"images": [img], "videos": [vid_silent, vid_sound], "audios": [aud]},
            params={"seconds": 5},
        ), store=store)
    assert sparse_plan.status == "pass", sparse_plan.signals
    assert sparse_plan.facts["aspect"] == "16:9"
    assert sparse_plan.facts["width"] == 1664 and sparse_plan.facts["height"] == 928
    bindings = sparse_plan.facts["prompt_bindings"]
    assert list(bindings.keys()) == ["<Picture 1>", "<Video 1>", "<Video 2>", "<Audio 1>", "<Audio 2>"]
    assert secret not in bindings["<Picture 1>"]["path"]
    assert bindings["<Video 1>"]["use_audio"] is False
    assert bindings["<Video 2>"]["use_audio"] is True
    assert bindings["<Audio 1>"]["source"] == "video_audio" and bindings["<Audio 1>"]["from"] == "<Video 2>"
    assert bindings["<Audio 2>"]["source"] == "audio" and bindings["<Audio 2>"]["file"] == "voice.wav"

    # i2v accepts first_frame_path alias and omits prompt_bindings from persisted checkpoint quote.
    i2v_prompt = """The video starts at 0.00 seconds with <Picture 1>.
integrated_multimodal_description: [Shot 1] The camera pushes in.
overall_soundscape: Soft wind.
non_diegetic_music: N/A
"""
    i2v_req = GenerationRequest(
        "video", prompt=i2v_prompt, output_path=tmp_path / "i2v.mp4",
        inputs={"first_frame_path": img},
        params={"width": 640, "height": 384, "seconds": 0.5},
    )
    i2v_plan = provider.preflight(i2v_req, store=store)
    assert i2v_plan.status == "pass", i2v_plan.signals
    assert i2v_plan.facts["aspect"] is None
    assert i2v_plan.facts["prompt_bindings"]["<Picture 1>"]["source"] == "first_frame"
    assert i2v_plan.facts["prompt_bindings"]["<Picture 1>"]["file"] == "hero.png"
    assert secret not in i2v_plan.facts["prompt_bindings"]["<Picture 1>"]["path"]

    delivered = provider.generate(replace(i2v_req, approve_digest=i2v_plan.digest), store=store)
    assert delivered.state == "delivered"
    saved = store.get(delivered.task_key)
    assert "prompt_bindings" not in saved["quote"]

    # Helper accepts dict, Path/str, and tuple forms for videos without mis-unpacking dicts.
    direct = prompt_bindings(videos=[{"path": vid_sound, "use_audio": False}, vid_sound, (vid_silent, False)])
    assert direct["<Video 1>"]["file"] == "with_audio.mp4" and direct["<Video 1>"]["use_audio"] is False
    assert direct["<Video 2>"]["file"] == "with_audio.mp4" and direct["<Video 2>"]["use_audio"] is True
    assert direct["<Video 3>"]["file"] == "silent.mp4" and direct["<Video 3>"]["use_audio"] is False
    assert direct["<Audio 1>"]["from"] == "<Video 2>" and "<Audio 2>" not in direct
