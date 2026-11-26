"""Shared managed ComfyUI provider and generation delivery primitives."""
from __future__ import annotations

import copy
import http.client
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .. import perf_history
from ..config import EngineConfig, load_config
from ..fsutil import ensure_real_path
from ..ledger import lookup_cny_rate, record_usage
from ..remote.workflows.minimax_h3 import spec as h3
from ..tasks_store import TaskStore, make_task_key, submission_identity
from . import video_common as video
from .base import (
    ArtifactContractError, ArtifactResult, PreflightResult, ProbeResult, Provider,
    ProviderConflictError, ProviderError, build_approval_digest, canonical_json_bytes,
    local_path, normalize_status, publish_atomic_no_clobber, require_approval, sha256_bytes, sha256_file,
)
from .comfy_client import DaemonHTTPError, ManagedComfyClient

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MEDIA = ("images", "audios", "videos")


def output_path(value):
    if not value:
        raise ValueError("必须指定 --out")
    path = ensure_real_path(value, expect_dir=False, label="成片输出路径")
    if path.suffix.lower() != ".mp4":
        raise ValueError("--out 须以 .mp4 结尾")
    return path


def request_target(request, config):
    wanted = output_path(request.output_path)
    logical = request.resolved_logical_target()
    task_key = make_task_key(logical, job_key=request.job_key)
    values = [str(wanted), logical, task_key, request.metadata.get("tag")]
    if config.redact(values) != values:
        raise ValueError("输出路径、任务键和 --tag 不得包含凭据")
    return wanted, logical, task_key


def approval_context(request, store, *, config, provider, origin, fingerprint, workflow, params, hashes, facts, target,
                     request_facts):
    wanted, logical, task_key = request_target(request, config)
    replaced = submission_identity(store.get(task_key)) if request.force_new else None
    bound_params = {**params, "target_identity": target, "output_path": os.path.normcase(str(wanted)),
                    "logical_target": logical, "tag": request.metadata.get("tag"),
                    "data_dir_hash": config.data_dir_hash}
    identity = dict(task_key=task_key, data_dir=store.data_dir, provider=provider,
                    endpoint_origin=origin, credential_fingerprint=fingerprint, workflow=workflow,
                    params=bound_params, input_hashes=hashes, quote_facts=facts, prompt=request.prompt)
    checkpoint = {**identity, "quote_facts": request_facts}
    request_digest = build_approval_digest(**checkpoint, force_new=False, replaced_submission_id=None)
    # Rolling estimates affect approval for this quote without changing checkpoint identity.
    digest = build_approval_digest(**identity, force_new=request.force_new, replaced_submission_id=replaced)
    return dict(wanted=wanted, logical_target=logical, task_key=task_key, replaced_submission_id=replaced,
                request_digest=request_digest, digest=digest)


def approve_generation(request, preflight, store, context):
    if request.force_new and request.approve_digest and preflight.status != "block" and request.approve_digest != preflight.digest:
        record = store.get(context["task_key"])
        if record and record.get("approved_digest") == request.approve_digest:
            raise ProviderConflictError(
                f"该次 --force-new 审批已消费，请使用 yc-h3 jobs resume {context['task_key']} 恢复",
                code="replacement_approval_stale",
            )
    require_approval(preflight, request.approve_digest)


def already_delivered(record, wanted=None):
    if record["state"] != "delivered" or not record.get("artifact_path"):
        return False
    path = Path(record["artifact_path"])
    requested = record.get("delivery_requested_path")
    target_matches = wanted is None or path == wanted or requested == str(wanted)
    return target_matches and path.is_file() and sha256_file(path) == record.get("artifact_sha256")


def video_metadata(path):
    info = video.default_ffprobe(path)
    if "mp4" not in str((info.get("format") or {}).get("format_name", "")).split(","):
        raise ArtifactContractError("成片容器须为 MP4")
    return info


def publish_delivery(store, record, tmp, wanted, extra, *, history=None):
    """Publish only a decoded artifact and attach proof to the matching attempt."""
    if store.config.redact(str(wanted)) != str(wanted):
        raise ValueError("交付路径不得含凭据")
    video.verify_video_decode(tmp)
    digest = sha256_file(tmp)
    task_key = record["task_key"]
    identity = submission_identity(record)

    def reserve(path):
        store.reserve_delivery(
            task_key, reserved_output_path=path, artifact_sha256=digest, extra=extra,
            expected_submission_id=identity,
        )

    extra = dict(extra, delivery_requested_path=str(wanted))
    current_pending = history is None and record["state"] != "delivered"
    path = publish_atomic_no_clobber(tmp, wanted, before_publish=reserve if current_pending else None)
    if history is not None:
        store.mark_remote_job_delivered(
            task_key, history["remote_job_id"], artifact_path=path, artifact_sha256=digest,
            instance_uuid=history.get("instance_uuid"), expected_submission_id=history.get("submission_id"),
        )
        return {**record, **extra, "state": "delivered", "remote_job_id": history["remote_job_id"],
                "artifact_path": str(path), "artifact_sha256": digest}
    return store.mark_delivered(task_key, artifact_path=path, artifact_sha256=digest, extra=extra,
                                expected_submission_id=identity)


def last_frame(store, record, contract):
    path = Path(record["artifact_path"])
    claimed = record.get("last_frame_reserved_path")
    if claimed:
        frame = Path(claimed)
        if frame.is_file() and sha256_file(frame) == record.get("last_frame_sha256"):
            return str(frame)
    with tempfile.TemporaryDirectory(prefix=".yh3-frame-", dir=path.parent) as staging:
        frame = Path(staging) / "last.jpg"
        video.default_ffmpeg(["-v", "error", "-xerror", "-i", str(path), "-vf",
                              f"select=eq(n\\,{contract['frames'] - 1})", "-frames:v", "1", "-q:v", "2", str(frame)])
        if not frame.is_file():
            raise ArtifactContractError("未能提取视频末帧")
        digest = sha256_file(frame)

        def reserve(candidate):
            store.update_delivered_metadata(record["task_key"], extra={
                "last_frame_reserved_path": str(candidate), "last_frame_sha256": digest,
            })

        return str(publish_atomic_no_clobber(frame, path.with_name(f"{path.stem}_last_frame.jpg"), before_publish=reserve))


def deliver_copy(store, record, wanted):
    """Recover to a new destination from a locally verified delivered artifact."""
    source = Path(record["artifact_path"])
    wanted.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yh3-copy-", dir=wanted.parent) as staging:
        tmp = Path(staging) / "clip.mp4"
        shutil.copyfile(source, tmp)
        if sha256_file(tmp) != record["artifact_sha256"]:
            raise ArtifactContractError("已交付成片在复制期间发生变化")
        return publish_delivery(store, record, tmp, wanted, {
            "contract": record.get("contract", {}), "billing": record.get("billing", {}),
            "last_frame_reserved_path": None, "last_frame_sha256": None,
        })


def remember_external_billing(store, record, billing):
    """Keep the first reported charge available even when media delivery has to be retried."""
    if "billing" in record or record["state"] != "submitted":
        return record
    task_key = record["task_key"]
    with store.lock(task_key):
        current = store._read_unlocked(task_key)
        if not current or submission_identity(current) != submission_identity(record) or current["state"] != "submitted":
            raise ProviderConflictError("保存费用时断点已变更", code="submission_id_mismatch")
        current.update(billing=billing, updated_at=time.time())
        return store._write_unlocked(task_key, current)


def record_job_charge(config, record, amount, unit, *, est_cny=None, estimated=False, note=None, action="generation"):
    if amount is None:
        return
    billing = record.get("billing") or {}
    record_usage(
        config=config, provider=record["provider"], account_fp=record.get("credential_fingerprint"),
        record_type="job", action=action, project=record.get("tag"), quantity=amount, unit=unit,
        estimated=estimated, est_cny=est_cny,
        idempotency_key="job:" + (record.get("remote_job_id") or record["remote_task_id"]),
        submitted_at=billing.get("submitted_at"), note=note,
    )


def media_stream(info, kind):
    return next((stream for stream in info.get("streams", []) if stream.get("codec_type") == kind
                 and not stream.get("disposition", {}).get("attached_pic")), None)


def stream_seconds(info, stream):
    for value in (stream.get("duration"), info.get("format", {}).get("duration")):
        if (seconds := video.parse_positive_float(value)) is not None:
            return seconds
    raise ValueError("参考媒体没有可验证的时长")


def inspect_references(params):
    """Validate local reference durations and resolve the H3 video frame windows."""
    results = []
    video_seconds = audio_seconds = 0.0
    audio_count = len(params["audios"])
    for item in params["videos"]:
        info = video.default_ffprobe(item["path"])
        stream, audio = media_stream(info, "video"), media_stream(info, "audio")
        if not stream:
            raise ValueError("参考视频缺少画面轨")
        seconds = stream_seconds(info, stream)
        video_seconds += seconds
        use_audio = bool(audio and item.get("use_audio", True))
        if use_audio:
            audio_count += 1
            audio_seconds += stream_seconds(info, audio)
        window = h3.snap_down_17k5(min(math.floor(seconds * h3.FPS + 1e-6), h3.output_frames(params)))
        results.append((item, stream, audio if use_audio else None, window))
    for item in params["audios"]:
        info = video.default_ffprobe(item["path"])
        audio = media_stream(info, "audio")
        if not audio:
            raise ValueError("独立参考音频缺少音轨")
        audio_seconds += stream_seconds(info, audio)
    if video_seconds > 15.000001 or audio_seconds > 15.000001 or audio_count > 3:
        raise ValueError("参考视频和音频各限单段及累计 15 秒，音频最多 3 段（含视频伴音）")
    if len(params["images"]) + len(params["videos"]) + audio_count > 12:
        raise ValueError("参考素材合计最多 12 项（含视频伴音）")
    return results


def _media_slots(params):
    if params.get("image_path"):
        yield "image_path", params, "image_path"
    for group in _MEDIA:
        for index, entry in enumerate(params[group]):
            yield f"{group}.{index}", entry, "path"


def prompt_bindings(*, images=(), audios=(), videos=(), first_frame=None, redact=None):
    """Build deterministic prompt tag bindings (<Picture/Video/Audio N>) matching model input order."""
    bindings = {}
    if first_frame:
        path = Path(first_frame)
        bindings["<Picture 1>"] = {"kind": "image", "source": "first_frame", "file": path.name, "path": str(path)}
    else:
        for index, item in enumerate(images, 1):
            path = Path(item["path"] if isinstance(item, dict) else item)
            bindings[f"<Picture {index}>"] = {"kind": "image", "source": "image", "file": path.name, "path": str(path)}

    audio_index = 0
    video_audio_entries = []
    for index, entry in enumerate(videos, 1):
        if isinstance(entry, dict):
            raw_path = entry["path"]
            use_audio = entry.get("use_audio", True)
        elif isinstance(entry, (str, os.PathLike)):
            raw_path, use_audio = entry, True
        else:
            raw_path, use_audio = entry
        path = Path(raw_path)
        tag = f"<Video {index}>"
        bindings[tag] = {
            "kind": "video", "source": "video", "file": path.name, "path": str(path), "use_audio": bool(use_audio),
        }
        if use_audio:
            video_audio_entries.append((tag, path))

    for source_tag, path in video_audio_entries:
        audio_index += 1
        bindings[f"<Audio {audio_index}>"] = {
            "kind": "audio", "source": "video_audio", "from": source_tag, "file": path.name, "path": str(path),
        }

    for item in audios:
        audio_index += 1
        path = Path(item["path"] if isinstance(item, dict) else item)
        bindings[f"<Audio {audio_index}>"] = {"kind": "audio", "source": "audio", "file": path.name, "path": str(path)}

    return redact(bindings) if redact is not None else bindings


def _live_target(state):
    tunnel = state.get("tunnel") or {}
    remote = state.get("remote") or {}
    session = tunnel.get("session")
    if tunnel.get("status", "up") != "up" or remote.get("status", "ok") != "ok":
        return None, None, None, None
    managed = (state.get("settings") or {}).get("managed_sessions", {}).get(session) or {}
    return session, managed.get("host"), remote.get("instance_id"), remote.get("platform_instance_id")


def _operation_lock(task_key):
    return "comfy-op-" + sha256_bytes(task_key.encode())[:32]


class ComfyProvider(Provider):
    capabilities = ("video",)

    def __init__(self, *, client: ManagedComfyClient, config: EngineConfig | None = None,
                 poll_interval_sec=2.0, poll_timeout_sec=14400, sleep_fn=time.sleep):
        self.config = config or load_config()
        self.client = client
        self._poll_interval = max(0.01, poll_interval_sec)
        self._poll_timeout = max(0, poll_timeout_sec)
        self._sleep = sleep_fn

    def _check_endpoint(self):
        """Backends may add identity checks before querying the managed instance."""

    def _fingerprint(self):
        return self.config.config_identity

    def _redact(self, value):
        return self.config.redact(value)

    def _live(self):
        self._check_endpoint()
        session, host, instance, platform_instance = _live_target(self.client.state())
        if not all(isinstance(value, str) and value for value in (session, host, instance)):
            raise ProviderConflictError("没有已连接的受管 GPU 会话", code="no_active_gpu_session")
        return dict(session_name=session, vm_host=host, instance_id=instance,
                    instance_uuid=platform_instance or instance,
                    endpoint_origin=self.client.origin, credential_fingerprint=self._fingerprint())

    def probe(self, *, capability=None, check_remote=False, workflow=None):
        supported = super().probe(capability=capability)
        if not supported.available:
            return supported
        try:
            target = self._live()
        except (OSError, http.client.HTTPException, ProviderError) as exc:
            return ProbeResult(self.provider_id, False, getattr(exc, "code", "daemon_unreachable"), self.capabilities)
        return ProbeResult(self.provider_id, True, capabilities=self.capabilities,
                           details={"session_name": target["session_name"]})

    def _blocked(self, code, message):
        return PreflightResult(self.provider_id, "block", "", self.pricing_mode, "",
                               signals=[dict(severity="block", code=code, detail=self._redact(message))])

    def _prepare(self, request, store, *, submit=False):
        try:
            target = self._live()
            wanted, logical, task_key = request_target(request, self.config)
            metadata = [str(wanted), logical, task_key, request.metadata.get("tag"), target]
            if self._redact(metadata) != metadata:
                raise ValueError("输出路径、任务键和 --tag 不得包含凭据")
            workflow = h3.workflow_id(request.workflow)
            manifest = json_manifest(workflow)
            if request.model_id not in (None, "minimax-h3"):
                raise ValueError("仅支持 minimax-h3")
            if request.simulated_gpu is not None and submit:
                raise ValueError("模拟显卡预检不能用于生成审批")
            if request.inputs.keys() - {*_MEDIA, "image_path", "first_frame_path"}:
                raise ValueError("当前 ComfyUI 工作流收到未知的素材类型")
            params = dict(copy.deepcopy(request.params), **copy.deepcopy(request.inputs))
            unsupported = params.keys() & {"save_last_frame", "megapixels", "upscale_2x"}
            if unsupported:
                raise ValueError("当前 ComfyUI 工作流不支持 " + ", ".join(sorted(unsupported)))
            if "steps" in params and params["steps"] != manifest["steps"]:
                raise ValueError(f"{workflow} 固定使用 {manifest['steps']} 步，请移除 --steps 或使用 runninghub")
            params.update(workflow=workflow, prompt=request.prompt.strip())
            if self._redact(params["prompt"]) != params["prompt"]:
                raise ValueError("提示词不得含凭据")
            for field in ("image_path", "first_frame_path"):
                if params.get(field):
                    params[field] = str(params[field])
            for group in _MEDIA:
                if isinstance(params.get(group), (tuple, list)):
                    params[group] = [
                        dict(item, path=str(item["path"])) if isinstance(item, dict) and "path" in item
                        else str(item) if isinstance(item, os.PathLike) else item for item in params[group]
                    ]
            task = request.task or ("i2v" if params.get("image_path") or params.get("first_frame_path")
                                    else "r2v" if any(params.get(key) for key in _MEDIA) else "t2v")
            normalized = h3.normalize_request(task, params, enforce_hardware=False, tasks=manifest["tasks"])
            # Remote applies the no-music policy and geometry normalization from the original request.
            wire = dict(normalized, prompt=params["prompt"])
            paths, hashes, facts = {}, {}, {}
            for slot, parent, key in _media_slots(wire):
                path = local_path(parent[key])
                parent[key] = str(path)
                if str(path) not in facts:
                    facts[str(path)] = {"sha256": sha256_file(path)}
                if slot == "image_path":
                    with Image.open(path) as image:
                        width, height = ImageOps.exif_transpose(image).size
                    facts[str(path)].update(width=width, height=height)
                paths[slot], hashes[slot] = path, facts[str(path)]["sha256"]
            references = inspect_references(wire)
            errors, warnings = video.validate_h3_prompt(
                normalized["prompt"], mode={"t2v": "t2va", "i2v": "i2va", "r2v": "ref2va"}[task],
                seconds=normalized["seconds"],
                n_images=len(wire["images"]) + int(task == "i2v"),
                n_audios=len(wire["audios"]) + sum(audio is not None for _, _, audio, _ in references),
                n_videos=len(references),
            )
            if errors:
                return self._blocked("invalid_h3_prompt", "；".join(errors)), {}
            params_for_wire = dict(wire)
            if "aspect" in params:
                params_for_wire.pop("width")
                params_for_wire.pop("height")
                params_for_wire["aspect"] = params["aspect"]
            body = dict(task=task, model_id="minimax-h3", params=params_for_wire, input_facts=facts)
            if submit:
                body["for_submission"] = True
            elif request.simulated_gpu is not None:
                body["simulated_gpu"] = request.simulated_gpu
            status_code, pack = self.client.preflight(body)
            if status_code not in (200, 409):
                return self._blocked("preflight_rejected", f"远端预检返回 HTTP {status_code}"), {}
            status = normalize_status(pack.get("status"))
            if status_code == 409 or status == "block":
                signals = pack.get("signals") or []
                result = self._blocked(pack.get("code") or "remote_preflight_blocked",
                                       pack.get("reason") or pack.get("detail") or "远端预检阻断")
                result.signals.extend(self._redact(signals))
                return result, {}
            endpoint_digest = pack.get("preflight_digest")
            billing = pack.get("billing_context") or {}
            if (not isinstance(endpoint_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", endpoint_digest)
                    or pack.get("instance_id") != target["instance_id"]
                    or billing.get("session_name") != target["session_name"]
                    or billing.get("vm_host") != target["vm_host"]
                    or billing.get("provider_id", self.provider_id) != self.provider_id
                    or (self.provider_id == "chenyu-comfy" and pack.get("managed_instance_id") != target["instance_uuid"])
                    or (pack.get("input_content_sha256s") is not None and pack["input_content_sha256s"] != hashes)):
                return self._blocked("remote_identity_mismatch", "远端实例、输入摘要或审批身份不一致"), {}
            hardware = pack.get("hardware")
            gpu_name = hardware.get("gpu_name") if isinstance(hardware, dict) else None
            gpu_name = gpu_name.strip() if isinstance(gpu_name, str) else ""
            if self._redact(gpu_name) != gpu_name or store.config.redact(gpu_name) != gpu_name:
                gpu_name = ""
            quote, request_facts = self._quote(
                pack, config=store.config, workflow=workflow, output_seconds=normalized["seconds"],
                has_ref_video=bool(normalized.get("videos")), gpu_name=gpu_name,
            )
            params_for_approval = {**normalized, "task": task, "model_id": body["model_id"],
                                   "endpoint_digest": endpoint_digest}
            context = approval_context(
                request, store, config=self.config, provider=self.provider_id, origin=target["endpoint_origin"],
                fingerprint=target["credential_fingerprint"], workflow=workflow,
                params=params_for_approval, hashes=hashes, facts=quote, target=target, request_facts=request_facts,
            )
            signals = list(pack.get("signals") or [])
            signals.extend(dict(severity="warn", code="h3_prompt_risk", detail=warning) for warning in warnings)
            resolved_aspect = (
                None if ("width" in request.params or "height" in request.params)
                else request.params.get("aspect", "16:9")
            )
            bindings = prompt_bindings(
                images=wire["images"],
                audios=wire["audios"],
                videos=[(item["path"], audio is not None) for item, _, audio, _ in references],
                first_frame=wire.get("image_path"),
                redact=lambda value: store.config.redact(self._redact(value)),
            )
            facts_for_user = {
                **quote, "task": task, "workflow": workflow, "aspect": resolved_aspect,
                "width": normalized["width"], "height": normalized["height"], "seconds": normalized["seconds"],
                "frames": h3.trim_frames(normalized) or h3.output_frames(normalized), "steps": manifest["steps"],
                "slots": self.workflow_slots(), "prompt_bindings": bindings,
                "task_key": context["task_key"],
                "replaced_submission_id": context["replaced_submission_id"],
                "data_dir_hash": self.config.data_dir_hash,
            }
            result = PreflightResult(
                self.provider_id, "warn" if warnings else status, context["digest"], self.pricing_mode,
                target["endpoint_origin"], target_identity={**target, "workflow": workflow},
                input_hashes=hashes, signals=self._redact(signals), facts=facts_for_user,
            )
            context.update(target=target, body=body, endpoint_digest=endpoint_digest,
                           submission_handle=pack.get("submission_preflight"), paths=paths,
                           normalized=normalized, workflow=workflow, gpu_name=gpu_name or None)
            return result, context
        except (OSError, http.client.HTTPException, ProviderError, ValueError, KeyError, TypeError) as exc:
            return self._blocked(getattr(exc, "code", "invalid_comfy_request"), str(exc)), {}

    @staticmethod
    def workflow_slots():
        return {"images": 9, "audios": 3, "videos": 3, "total": 12, "first_frame": 1}

    def _quote(self, pack, *, config, workflow, output_seconds, has_ref_video, gpu_name):
        context = pack["billing_context"]
        rate = video.parse_positive_float(context.get("billing_rate"))
        if rate is None:
            raise ValueError("当前 GPU 会话无法核验实时费率")
        unit = "CNY" if self.provider_id == "chenyu-comfy" else "CU"
        cny_per_unit = 1.0 if unit == "CNY" else lookup_cny_rate(self.provider_id, unit, config=self.config)
        history = pack.get("perf") or {}
        warm = history.get("warm_run") or {}
        cold = history.get("cold_start") or {}
        history_scope = "warm_run" if warm.get("median_total_s") is not None else "cold_start"
        seconds = video.parse_positive_float((warm if history_scope == "warm_run" else cold).get("median_total_s"))
        estimated = round(rate * seconds / 3600, 6) if seconds is not None else None
        request_facts = dict(
            billing_rate_per_hour=rate, unit=unit, cny_per_unit=cny_per_unit,
            estimated_quantity=estimated, estimate_samples=history.get("comparable_samples", 0),
            estimate_seconds=seconds, estimate_scope=history_scope if seconds is not None else None,
            estimated_cny=round(estimated * cny_per_unit, 6) if estimated is not None and cny_per_unit is not None else None,
            billing_scope="gpu_session", billing_endpoint=context.get("endpoint_origin"),
        )
        quote = {**request_facts, "estimate_source": "local_history_exact", "estimated_wall_seconds": seconds}
        if seconds is not None:
            return quote, request_facts

        local = perf_history.estimate_from_history(
            config, provider=self.provider_id, workflow=workflow, output_seconds=output_seconds,
            has_ref_video=has_ref_video, hardware=gpu_name,
        )
        if local is None or local["estimated_run_seconds"] is None:
            quote.update(estimate_source="uncalibrated", estimate_samples=0)
        else:
            seconds = local["estimated_run_seconds"]
            estimated = round(rate * seconds / 3600, 6)
            quote.update(
                estimate_source="local_history_normalized", estimate_samples=local["samples"],
                estimate_scope="warm_run", estimate_seconds=seconds, estimated_quantity=estimated,
                estimated_wall_seconds=local["estimated_wall_seconds"],
                estimated_cny=round(estimated * cny_per_unit, 6) if cny_per_unit is not None else None,
            )
        return quote, request_facts

    def preflight(self, request, *, store=None):
        return self._prepare(request, store or TaskStore(config=self.config))[0]

    def generate(self, request, *, store=None):
        store = store or TaskStore(config=self.config)
        pf, ctx = self._prepare(request, store, submit=True)
        approve_generation(request, pf, store, ctx)
        task_key, target = ctx["task_key"], ctx["target"]
        with store.lock(_operation_lock(task_key)):
            existing = store.get(task_key)
            if existing and not request.force_new:
                self._check_owner(existing, target)
                if existing.get("request_digest") != ctx["request_digest"]:
                    raise ProviderConflictError("当前请求与旧断点不同，请检查 jobs 或重新审批 --force-new",
                                                code="checkpoint_identity_mismatch")
            action, record = store.begin_comfy_submit(
                task_key, provider=self.provider_id, **target, logical_target=ctx["logical_target"],
                request_digest=ctx["request_digest"], force_new=request.force_new,
                replaced_submission_id=ctx["replaced_submission_id"],
                extra={
                    "wanted_output_path": str(ctx["wanted"]), "workflow": ctx["workflow"], "tag": request.metadata.get("tag"),
                    "approved_digest": pf.digest,
                    "quote": {key: val for key, val in pf.facts.items() if key != "prompt_bindings"},
                    "expected_size": [pf.facts["width"], pf.facts["height"]],
                    "expected_frames": pf.facts["frames"],
                    "has_ref_video": bool(ctx["normalized"].get("videos")),
                    "output_seconds": ctx["normalized"]["seconds"],
                    **({"gpu_name": ctx["gpu_name"]} if ctx["gpu_name"] else {}),
                },
            )
            if action == "delivered" and already_delivered(record):
                return self._result(store, record, recovered=True)
            if action == "retry_idempotent":
                raise ProviderConflictError(f"原提交结果待确认，请使用 yc-h3 jobs resume {task_key}", code="ambiguous_submit")
            if action == "submit":
                record = self._submit(store, record, ctx, pf)
        return self._poll(store, record, ctx["wanted"], recovered=action != "submit")

    def _submit(self, store, record, ctx, pf):
        task_key, identity = record["task_key"], submission_identity(record)
        try:
            expected = pf.input_hashes
            snapshots = {}
            for slot, path in ctx["paths"].items():
                if path not in snapshots:
                    snapshots[path] = path.read_bytes()
                if sha256_bytes(snapshots[path]) != expected[slot]:
                    raise ProviderConflictError("输入素材已变更，请重新预检", code="input_hash_mismatch")
            self._check_owner(record, self._live())
            wire = copy.deepcopy(ctx["body"]["params"])
            uploaded = {}
            for slot, parent, key in _media_slots(wire):
                path = ctx["paths"][slot]
                if path not in uploaded:
                    row = self.client.upload("input" + path.suffix.lower(), snapshots.pop(path))
                    remote_path = row.get("remote_path")
                    if (not isinstance(remote_path, str) or not remote_path.startswith("/")
                            or self._redact(remote_path) != remote_path):
                        raise ProviderError("素材上传未返回有效的远端路径", code="upload_failed")
                    uploaded[path] = remote_path
                parent[key] = uploaded[path]
            self._check_owner(record, self._live())
            payload = dict(task=ctx["body"]["task"], model_id=ctx["body"]["model_id"], params=wire,
                           approve_digest=ctx["endpoint_digest"], client_request_id=record["client_request_id"],
                           request_digest=record["request_digest"])
            if self._redact(payload) != payload or store.config.redact(payload) != payload:
                raise ProviderConflictError("请求包含凭据，无法安全保存幂等回放", code="secret_in_request")
            store.cache_comfy_submit(task_key, payload, expected_submission_id=identity)
        except (OSError, http.client.HTTPException, ProviderError, ValueError) as exc:
            store.mark_failed(task_key, error=self._redact(str(exc)), expected_submission_id=identity)
            raise
        handle = ctx["submission_handle"]
        return self._send(store, record, dict(payload, submission_preflight=handle) if handle else payload)

    def _send(self, store, record, payload, *, replay=False):
        task_key, identity = record["task_key"], submission_identity(record)
        try:
            status, response = self.client.submit_job(payload)
        except (OSError, http.client.HTTPException, ProviderError, ValueError):
            store.mark_ambiguous(task_key, reason="ComfyUI 提交结果不明", expected_submission_id=identity)
            raise ProviderConflictError(f"提交结果不明，请使用 yc-h3 jobs resume {task_key}",
                                        code="ambiguous_submit") from None
        job_id = response.get("job_id")
        if 200 <= status < 300 and isinstance(job_id, str) and _ID.fullmatch(job_id) and self._redact(job_id) == job_id:
            return store.mark_comfy_submitted(task_key, job_id, expected_submission_id=identity)
        detail = response.get("detail", response)
        code = detail.get("code") if isinstance(detail, dict) else None
        if not replay and (status in (400, 401, 403, 422) or
                           (status == 409 and isinstance(detail, dict) and
                            (detail.get("approval_status") or detail.get("status") == "block") and not job_id)):
            store.mark_failed(task_key, error="ComfyUI 拒绝入队", expected_submission_id=identity)
            raise ProviderConflictError("ComfyUI 拒绝入队，请检查远端会话并重新预检",
                                        code=code or "submit_rejected")
        store.mark_ambiguous(task_key, reason="ComfyUI 未返回可确认的作业 ID", expected_submission_id=identity)
        raise ProviderConflictError(f"无法确认入队，请使用 yc-h3 jobs resume {task_key}", code="ambiguous_submit")

    def _check_owner(self, record, target, *, historical=False):
        fields = ["session_name", "vm_host", "instance_uuid", "endpoint_origin", "credential_fingerprint"]
        if not historical:
            fields.append("instance_id")
        if record.get("provider") != self.provider_id or any(record.get(key) != target.get(key) for key in fields):
            raise ProviderConflictError("断点的账号、端点或 GPU 会话与当前连接不符", code="gpu_session_owner_mismatch")

    def _restore(self, store, task_key, remote_job_id, *, replay=False):
        record = store.get(task_key)
        if not record or record.get("provider") != self.provider_id:
            raise ProviderConflictError("缺少 ComfyUI 断点")
        target = self._live()
        if self._redact([task_key, remote_job_id]) != [task_key, remote_job_id]:
            raise ValueError("任务键与远端作业 ID 不得含凭据")
        if remote_job_id and record.get("remote_job_id") != remote_job_id:
            if not _ID.fullmatch(remote_job_id):
                raise ValueError("--remote-job-id 格式无效")
            history = store.find_remote_job(remote_job_id, task_key=task_key, provider=self.provider_id,
                                            instance_uuid=target["instance_uuid"],
                                            credential_fingerprint=target["credential_fingerprint"])
            if history:
                self._check_owner(history, target, historical=True)
                return record, history
            self._check_owner(record, target)
            if record["state"] not in ("submitting", "ambiguous") or record.get("remote_job_id"):
                raise ProviderConflictError("指定作业不属于该断点", code="remote_job_mismatch")
            if self._redact(remote_job_id) != remote_job_id:
                raise ValueError("远端作业 ID 不得含凭据")
            job = self.client.get_job(remote_job_id)
            if job.get("client_request_id") != record.get("client_request_id"):
                raise ProviderConflictError("指定作业的请求 ID 与断点不符", code="remote_job_mismatch")
            return store.mark_comfy_submitted(
                task_key, remote_job_id, expected_submission_id=submission_identity(record)
            ), None
        self._check_owner(record, target)
        record = store.reconcile_delivery(task_key) or record
        if record["state"] in ("submitting", "ambiguous"):
            client_request_id = record.get("client_request_id")
            matches = [
                job["id"] for job in self.client.list_jobs()
                if job.get("client_request_id") == client_request_id
                and isinstance(job.get("id"), str) and _ID.fullmatch(job["id"])
                and self._redact(job["id"]) == job["id"]
            ] if client_request_id else []
            if len(matches) == 1:
                record = store.mark_comfy_submitted(task_key, matches[0], expected_submission_id=submission_identity(record))
            elif not matches and replay and (payload := record.get("cached_submit_payload")):
                if (payload.get("client_request_id") != client_request_id
                        or payload.get("request_digest") != record.get("request_digest")
                        or self._redact(payload) != payload):
                    raise ProviderConflictError("原提交的幂等回放参数无法核验", code="ambiguous_submit")
                record = self._send(store, record, payload, replay=True)
        if record["state"] not in ("submitted", "delivered") or not record.get("remote_job_id"):
            raise ProviderConflictError(f"断点当前为 {record['state']}，无法领取成片", code=record["state"])
        return record, None

    def _recover(self, task_key, store, output_path_override, remote_job_id, *, single_poll):
        with store.lock(_operation_lock(task_key)):
            record, history = self._restore(store, task_key, remote_job_id, replay=not single_poll)
            wanted = output_path(output_path_override or record["wanted_output_path"])
            if self._redact(str(wanted)) != str(wanted):
                raise ValueError("交付路径不得含凭据")
            if history is not None and history.get("status") == "delivered":
                delivered = history.get("delivered_path")
                if delivered and (output_path_override is None or str(wanted) == delivered):
                    path = Path(delivered)
                    if path.is_file() and sha256_file(path) == history.get("artifact_sha256"):
                        return ArtifactResult(self.provider_id, task_key, "delivered", artifact_path=delivered,
                                              artifact_sha256=history["artifact_sha256"],
                                              remote_id=history["remote_job_id"], recovered=True)
            if history is None and already_delivered(record):
                if output_path_override and not already_delivered(record, wanted):
                    record = deliver_copy(store, record, wanted)
                return self._result(store, record, recovered=True)
        return self._poll(store, record, wanted, recovered=True, single_poll=single_poll, history=history,
                          override=output_path_override is not None)

    def resume(self, task_key, *, store, output_path=None, remote_job_id=None, **kwargs):
        return self._recover(task_key, store, output_path, remote_job_id, single_poll=False)

    def fetch(self, task_key, *, store, output_path=None, remote_job_id=None, **kwargs):
        return self._recover(task_key, store, output_path, remote_job_id, single_poll=True)

    def _poll(self, store, record, wanted, *, recovered, single_poll=False, history=None, override=False):
        job_id = history["remote_job_id"] if history else record["remote_job_id"]
        task_key, identity = record["task_key"], submission_identity(record)
        deadline = time.monotonic() + self._poll_timeout
        while True:
            self._check_owner(history or record, self._live(), historical=history is not None)
            try:
                job = self.client.get_job(job_id)
            except DaemonHTTPError as exc:
                if history is None and exc.status in (404, 410) and record["state"] == "submitted":
                    store.mark_expired_or_missing(task_key, http_status=exc.status, expected_submission_id=identity)
                    raise ProviderConflictError("远端作业已过期或不存在", code="expired_or_missing") from None
                raise
            state = job.get("status")
            if state == "done":
                break
            if state in ("error", "cancelled", "canceled", "cancelled_in_queue"):
                if history is None and record["state"] == "submitted":
                    if state == "error":
                        store.mark_failed(task_key, error="远端生成失败", expected_submission_id=identity)
                    else:
                        store.mark_cancelled(task_key, reason="远端已取消", expected_submission_id=identity)
                raise ProviderError("远端作业失败或取消", code="remote_job_failed")
            remaining = deadline - time.monotonic()
            if single_poll or remaining <= 0:
                raise ProviderConflictError("ComfyUI 作业尚未完成，请使用 jobs resume 续查", code="job_not_done")
            self._sleep(min(self._poll_interval, remaining))

        output = next((row for row in job.get("outputs") or [] if isinstance(row, dict)
                       and isinstance(row.get("path"), str) and row["path"].lower().endswith(".mp4")), None)
        if output is None:
            raise ArtifactContractError("ComfyUI 作业缺少 MP4 成片")
        if history and (job.get("client_request_id") != history.get("client_request_id") or not history.get("client_request_id")):
            raise ProviderConflictError("历史作业的请求 ID 与断点不符", code="remote_job_mismatch")
        historical_params = job.get("params") if history else None
        if history and not isinstance(historical_params, dict):
            raise ArtifactContractError("历史作业缺少原始分辨率与帧数")
        workflow = historical_params.get("workflow") if history else record["workflow"]
        size = [historical_params[k] for k in ("width", "height")] if history else record["expected_size"]
        expected_frames = (h3.trim_frames(historical_params) or h3.output_frames(historical_params)) if history else record["expected_frames"]
        billing = self._job_billing(record, job) if history is None else {}
        wanted.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".yh3-comfy-", dir=wanted.parent) as staging:
            tmp = Path(staging) / "clip.mp4"
            self.client.download_file(output["path"], tmp)
            if sha256_file(tmp) != output.get("sha256"):
                raise ArtifactContractError("成片 SHA-256 与远端不一致", code="artifact_digest_mismatch")
            contract = video.verify_runninghub_video(video_metadata(tmp), workflow=workflow, width=size[0], height=size[1],
                                                     expected_frames=expected_frames)
            with store.lock(_operation_lock(task_key)):
                self._check_owner(history or record, self._live(), historical=history is not None)
                latest = store.get(task_key)
                if history is None:
                    if submission_identity(latest) != identity or latest["state"] not in ("submitted", "delivered"):
                        raise ProviderConflictError("交付期间断点已变更", code="submission_id_mismatch")
                    latest = store.reconcile_delivery(task_key) or latest
                    if already_delivered(latest):
                        if override and not already_delivered(latest, wanted):
                            latest = deliver_copy(store, latest, wanted)
                        return self._result(store, latest, recovered=True)
                    record = latest
                run_seconds = billing.get("run_seconds") or None
                wall_seconds = perf_history.elapsed_seconds(job.get("finished"), job.get("created")) or run_seconds
                perf_meta = job.get("_perf")
                default_cold_start = isinstance(perf_meta, dict) and bool(perf_meta.get("cold_start"))
                extra = {"contract": contract, "billing": billing}
                if history is None:
                    extra.update(wall_seconds=wall_seconds, cold_start=default_cold_start)
                delivered = publish_delivery(store, record, tmp, wanted, extra, history=history)
                if history is None:
                    cold_start = self._archive(store, delivered, job_id)
                    if cold_start is not None and cold_start != default_cold_start:
                        with suppress(OSError, ValueError):
                            store.update_delivered_metadata(task_key, extra={"cold_start": cold_start})
                    return self._result(store, store.get(task_key), recovered=recovered)
        return ArtifactResult(self.provider_id, task_key, "delivered", artifact_path=delivered["artifact_path"],
                              artifact_sha256=delivered["artifact_sha256"], remote_id=job_id, recovered=True, contract=contract)

    def _archive(self, store, record, job_id) -> bool | None:
        try:
            result = self.client.archive_perf(job_id)
        except (OSError, http.client.HTTPException, ProviderError, ValueError):
            return
        if result.get("status") == "archived" or result.get("archived") is True:
            store.mark_perf_archived(record["task_key"])
            # Public job responses omit _perf, while both archive transports expose this flag.
            cold_start = result.get("cold_start")
            return cold_start if type(cold_start) is bool else None

    def _job_billing(self, record, job):
        if record.get("billing"):
            return record["billing"]
        quote = record["quote"]
        started = video.parse_positive_float(job.get("started"))
        finished = video.parse_positive_float(job.get("finished"))
        seconds = finished - started if started is not None and finished is not None and finished >= started else None
        quantity = round(seconds * quote["billing_rate_per_hour"] / 3600, 6) if seconds is not None else None
        unit_rate = quote["cny_per_unit"]
        return dict(unit=quote["unit"], quantity=quantity, estimated=True, run_seconds=seconds,
                    est_cny=round(quantity * unit_rate, 6) if quantity is not None and unit_rate is not None else None,
                    submitted_at=datetime.fromtimestamp(record["created_at"], timezone.utc).isoformat())

    @staticmethod
    def _record_attribution(store, record):
        # GPU session shutdown writes the actual wallet debit. A job's share is attribution only.
        bill = record.get("billing") or {}
        record_job_charge(store.config, record, 1, "call", est_cny=bill.get("est_cny"), estimated=True,
                          action="generation_attribution", note="GPU 会话扣款由 session 流水对账")

    def _record_observation(self, store, record):
        job_id = record.get("remote_job_id")
        contract = record.get("contract") or {}
        if not job_id or not contract:
            return
        bill = record.get("billing") or {}
        run_seconds = bill.get("run_seconds") or None
        wall_seconds = record.get("wall_seconds") or run_seconds
        with suppress(OSError, ValueError):
            perf_history.record_observation(
                store.config, idempotency_key=f"{self.provider_id}:{job_id}",
                provider=self.provider_id, workflow=record["workflow"], hardware=record.get("gpu_name"),
                width=contract.get("width"), height=contract.get("height"),
                output_seconds=record.get("output_seconds") or contract.get("duration_s"),
                has_ref_video=bool(record.get("has_ref_video")), run_seconds=run_seconds,
                wall_seconds=wall_seconds, billed_quantity=bill.get("quantity"), unit=bill.get("unit"),
                estimated_billing=True, cold_start=bool(record.get("cold_start", False)),
            )

    def _result(self, store, record, *, recovered):
        bill = record.get("billing") or {}
        if self.provider_id == "chenyu-comfy":
            self._record_attribution(store, record)
        else:
            record_job_charge(store.config, record, bill.get("quantity"), bill.get("unit"), est_cny=bill.get("est_cny"),
                              estimated=True)
        self._record_observation(store, record)
        return ArtifactResult(self.provider_id, record["task_key"], "delivered", artifact_path=record["artifact_path"],
                              artifact_sha256=record["artifact_sha256"], remote_id=record["remote_job_id"],
                              recovered=recovered, contract=record.get("contract") or {}, billing=bill,
                              perf_archived=record.get("perf_archived"))

    def cancel(self, task_key, *, store, **kwargs):
        with store.lock(_operation_lock(task_key)):
            record = store.get(task_key)
            if record and record.get("provider") == self.provider_id and record["state"] == "cancelled":
                return record
            record, _ = self._restore(store, task_key, None)
            if record["state"] != "submitted":
                raise ProviderConflictError("当前断点不能取消")
            reply = self.client.cancel_job(record["remote_job_id"])
            state = reply.get("status")
            if reply.get("ok") is True and state is None:
                state = self.client.get_job(record["remote_job_id"]).get("status")
            if state not in ("cancelled", "canceled", "cancelled_in_queue"):
                raise ProviderConflictError("远端尚未确认取消，请用 jobs resume 查看最终状态", code="cancel_not_confirmed")
            return store.mark_cancelled(task_key, reason="用户请求取消", expected_submission_id=submission_identity(record))


def json_manifest(workflow):
    name = workflow.replace("-", "_")
    path = Path(h3.__file__).parent / name / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))
