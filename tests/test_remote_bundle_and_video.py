import errno
import io
import os
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yc_h3 import fsutil, remote_bundle
from yc_h3.config import load_config
from yc_h3.providers import video_common
from yc_h3.providers.base import (
    ApprovalRequiredError, ArtifactContractError, PreflightBlockedError, PreflightResult,
    ProviderConflictError, build_approval_digest, publish_atomic_no_clobber, require_approval, verify_input_hashes,
)
from yc_h3.providers.comfy_client import DaemonHTTPError, LocalDaemonClient, ManagedComfyClient


def test_crlf_bundle_matches_extracted_content_and_executable_modes(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    (root / "bootstrap_remote.sh").write_bytes(b"#!/bin/sh\r\necho ready\r\n")
    (root / "requirements.txt").write_bytes(b"fastapi\r\n")
    nested = root / "workflows"
    nested.mkdir()
    (nested / "data.json").write_bytes(b'{\r\n  "engine": true\r\n}\r\n')
    (nested / "raw.bin").write_bytes(b"\x00\r\x01\r\n\xff")
    (nested / "ignored.pyc").write_bytes(b"ignored")
    (root / ".env").write_bytes(b"ignored too")
    members = {item.as_posix() for item in remote_bundle.remote_source_files(root)}
    assert members == {"bootstrap_remote.sh", "requirements.txt", "workflows/data.json", "workflows/raw.bin"}
    dest = tmp_path / "deployed"
    with tarfile.open(fileobj=io.BytesIO(remote_bundle.pack_remote_tar(root)), mode="r:gz") as tar:
        assert {item.name for item in tar.getmembers()} == members
        assert tar.getmember("bootstrap_remote.sh").mode == 0o755
        tar.extractall(dest, filter="data")
    assert (dest / "bootstrap_remote.sh").read_bytes() == b"#!/bin/sh\necho ready\n"
    assert (dest / "workflows" / "raw.bin").read_bytes() == b"\x00\r\x01\r\n\xff"
    assert remote_bundle.remote_source_version(root) == remote_bundle.remote_source_version(dest)
    (dest / "requirements.txt").write_bytes(b"changed\n")
    assert remote_bundle.remote_source_version(root) != remote_bundle.remote_source_version(dest)


def test_standalone_imports_dont_load_posix_or_future_batches():
    code = """
import sys
sys.modules['fcntl'] = None
import yc_h3.remote_bundle
import yc_h3.providers
import yc_h3.providers.base
import yc_h3.providers._concurrency
import yc_h3.providers.comfy_client
import yc_h3.providers.video_common
import yc_h3.ledger
assert not any(name.startswith(('yc_h3.local', 'yc_h3.remote.', 'yc_h3.providers.registry')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_approval_binds_every_identity_input_and_price_fact(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"approved bytes")
    facts = {"points": 10, "cny": 3.0, "rate": 0.3}
    inputs = {"image.1": fsutil.sha256_file(source)}
    request = dict(
        task_key="shot-1", data_dir=tmp_path / "state", provider="runninghub",
        endpoint_origin="https://provider.test", credential_fingerprint="fingerprint",
        workflow="jinyu-hybrid", params={"height": 480, "seconds": 4}, prompt="approved prompt",
        input_hashes=inputs, quote_facts=facts, force_new=True, replaced_submission_id="previous",
    )
    baseline = build_approval_digest(**request)
    assert baseline == build_approval_digest(**(request | {"params": {"seconds": 4, "height": 480}}))
    variations = {
        "task_key": "shot-2", "data_dir": tmp_path / "other", "provider": "chenyu",
        "endpoint_origin": "https://other.test", "credential_fingerprint": "other",
        "workflow": "jinyu-combat", "params": {"height": 720, "seconds": 4},
        "prompt": "changed prompt", "input_hashes": {"image.1": "a" * 64},
        "quote_facts": facts | {"cny": 4.0}, "replaced_submission_id": "newer",
    }
    for name, value in variations.items():
        assert build_approval_digest(**(request | {name: value})) != baseline, name
    assert build_approval_digest(**(request | {"force_new": False, "replaced_submission_id": None})) != (
        build_approval_digest(**(request | {"replaced_submission_id": None}))
    )
    for status in ("pass", "warn"):
        preflight = PreflightResult("runninghub", status, baseline, "points", "https://provider.test")
        with pytest.raises(ApprovalRequiredError):
            require_approval(preflight, None)
        with pytest.raises(ApprovalRequiredError):
            require_approval(preflight, "wrong")
        require_approval(preflight, baseline)
    with pytest.raises(PreflightBlockedError):
        require_approval(PreflightResult("runninghub", "block", baseline, "points", "https://provider.test"), baseline)
    verify_input_hashes({"image.1": source}, inputs)
    source.write_bytes(b"changed")
    with pytest.raises(ProviderConflictError) as exc:
        verify_input_hashes({"image.1": source}, inputs)
    assert exc.value.code == "input_hash_mismatch"


def test_publish_collision_and_posix_unsupported_filesystem(tmp_path, monkeypatch):
    wanted = tmp_path / "output.mp4"
    wanted.write_bytes(b"existing")
    source = tmp_path / ".verified"
    source.write_bytes(b"new")
    reservations = []
    actual = publish_atomic_no_clobber(source, wanted, before_publish=reservations.append)
    assert actual.name == "output_v2.mp4" and actual.read_bytes() == b"new"
    assert wanted.read_bytes() == b"existing" and not source.exists()
    assert reservations == [actual]

    fake_os = Mock(wraps=os)
    fake_os.name = "posix"
    fake_os.link.side_effect = OSError(errno.ENOTSUP, "no hard links")
    fake_os.rename.side_effect = AssertionError("unexpected rename fallback")
    monkeypatch.setattr(fsutil, "os", fake_os)
    source.write_bytes(b"do not lose")
    with pytest.raises(ArtifactContractError, match="jobs fetch") as exc:
        publish_atomic_no_clobber(source, wanted)
    assert exc.value.code == "atomic_publish_unsupported"
    assert source.read_bytes() == b"do not lose"
    assert not (tmp_path / "output_v3.mp4").exists()


def test_windows_publish_retries_realistic_atomic_rename_race(tmp_path, monkeypatch):
    source = tmp_path / ".stage"
    source.write_bytes(b"verified")
    dest = tmp_path / "movie.mp4"
    attempts = []

    def rename(staged, candidate):
        attempts.append(candidate)
        if candidate == dest:
            dest.write_bytes(b"concurrent writer")
            raise FileExistsError(errno.EEXIST, "destination exists")
        os.rename(staged, candidate)

    fake_os = Mock(wraps=os)
    fake_os.name = "nt"
    fake_os.O_RDONLY = os.O_RDONLY
    fake_os.rename.side_effect = rename
    fake_os.link.side_effect = AssertionError("Windows must not hard-link")
    monkeypatch.setattr(fsutil, "os", fake_os)
    assert publish_atomic_no_clobber(source, dest) == tmp_path / "movie_v2.mp4"
    assert dest.read_bytes() == b"concurrent writer"
    assert attempts == [dest, tmp_path / "movie_v2.mp4"]


def test_ffmpeg_xerror_failure_prevents_publish(tmp_path, monkeypatch):
    corrupt = tmp_path / ".corrupt.mp4"
    corrupt.write_bytes(b"media")
    final = tmp_path / "final.mp4"
    monkeypatch.setattr(video_common, "ffmpeg_bin", lambda: "ffmpeg.exe")

    def run(args, **kwargs):
        assert args == ["ffmpeg.exe", "-v", "error", "-xerror", "-i", str(corrupt),
                        "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]
        assert kwargs["encoding"] == "utf-8" and kwargs["check"] is True
        raise subprocess.CalledProcessError(1, args, stderr="damaged packet")

    monkeypatch.setattr(video_common.subprocess, "run", run)
    with pytest.raises(ArtifactContractError) as exc:
        video_common.verify_video_decode(corrupt)
        publish_atomic_no_clobber(corrupt, final)
    assert exc.value.code == "video_decode_failed"
    assert corrupt.exists() and not final.exists()


def test_probe_uses_utf8_and_checks_h3_stereo_frames_dimensions(tmp_path, monkeypatch):
    monkeypatch.setattr(video_common, "ffprobe_bin", lambda: "ffprobe")
    def run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8" and kwargs["check"] is True
        return SimpleNamespace(stdout='{"tags":{"title":"成片"}}')

    monkeypatch.setattr(video_common.subprocess, "run", run)
    assert video_common.default_ffprobe(tmp_path / "片段.mp4")["tags"]["title"] == "成片"
    video = {"codec_type": "video", "width": 640, "height": 480, "nb_frames": "22",
             "avg_frame_rate": "24/1", "duration": str(22 / 24)}
    audio = {"codec_type": "audio", "channels": 2, "duration": str(22 / 24)}
    opts = dict(workflow="jinyu-hybrid", expected_frames=22, expected_size=(640, 480))
    assert video_common.verify_chenyu_video({"streams": [video, audio]}, **opts)["audio_channels"] == 2
    for updates, code in [
        ({"width": 700}, "h3_dimension_mismatch"), ({"nb_frames": 21}, "h3_frame_count_mismatch"),
        ({"avg_frame_rate": "23976/1000"}, "h3_invalid_fps"),
    ]:
        with pytest.raises(ArtifactContractError) as exc:
            video_common.verify_chenyu_video({"streams": [video | updates, audio]}, **opts)
        assert exc.value.code == code
    with pytest.raises(ArtifactContractError, match="双声道"):
        video_common.verify_chenyu_video({"streams": [video, audio | {"channels": 1}]}, **opts)


def test_prompt_checks_sections_shot_timing_and_media_refs():
    prompt = """integrated_multimodal_description: [Shot 1] Wide view. [Shot 2] At 00:01.000, Waves roll in.
overall_soundscape: The sea.
non_diegetic_music: N/A"""
    assert video_common.validate_h3_prompt(prompt, mode="t2va", seconds=2) == ([], [])
    errors, _ = video_common.validate_h3_prompt(prompt.replace("Wide view", "<Video 2>"), mode="t2va",
                                                seconds=1, n_videos=1)
    assert any("目标总时长" in item for item in errors) and any("只传入" in item for item in errors)
    assert video_common.validate_h3_prompt(prompt, mode="ref2va")[0]


def test_comfy_client_defaults_loopback_and_redacts_daemon_errors(tmp_path, monkeypatch):
    cfg = load_config(cwd=tmp_path, data_dir=tmp_path / "state", environ={"CHENYU_API_KEY": "synthetic-credential"})
    client = LocalDaemonClient(config=cfg, runtime_tokens=("runtime-token-test",))
    assert isinstance(client, ManagedComfyClient)
    assert client.origin == "http://127.0.0.1:7798"
    assert LocalDaemonClient(config=cfg, base_url="http://[::1]:7799").origin == "http://[::1]:7799"
    with pytest.raises(ValueError, match="回环"):
        LocalDaemonClient(config=cfg, host="external.test")
    with pytest.raises(ValueError):
        LocalDaemonClient(config=cfg, base_url="http://name:password@localhost")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (
        503, b'{"detail":"synthetic-credential runtime-token-test Bearer leaked","api_key":"other"}',
    ))
    with pytest.raises(DaemonHTTPError) as exc:
        client.health()
    assert exc.value.status == 503 and exc.value.exit_code == 3
    assert exc.value.response == {"detail": "[REDACTED] [REDACTED] [REDACTED]", "api_key": "[REDACTED]"}
    assert not cfg.data_dir.exists()
