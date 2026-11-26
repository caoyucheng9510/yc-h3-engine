"""RunningHub MiniMax H3 integration. See runninghub_workflows/README.md for the graph contract."""
from __future__ import annotations

import http.client
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, suppress
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .. import perf_history
from ..config import EngineConfig, credential_fingerprint, load_config
from ..ledger import lookup_cny_rate
from ..fsutil import ensure_real_path, redact_secrets
from ..remote.presets import apply_no_music_policy
from ..remote.workflows.minimax_h3 import spec as h3
from ..tasks_store import TaskConflictError, TaskStore, submission_identity

from ._concurrency import ProviderSlots, resolve_positive_int_limit
from ._http import (
    CLIENT_REJECT_HTTP_CODES as _CLIENT_REJECTIONS,
    TRANSIENT_HTTP_CODES as _TRANSIENT,
    HttpsOnlyDownloadRedirectHandler,
    NoRedirectHandler,
    allowed_hosts,
    normalize_api_base,
    validate_api_key,
)
from .base import (
    ApprovalRequiredError,
    ArtifactContractError,
    ArtifactResult,
    GenerationRequest,
    PreflightResult,
    ProbeResult,
    Provider,
    ProviderConflictError,
    ProviderError,
    canonical_json_bytes,
    parse_content_length,
    sha256_bytes,
    sha256_file,
)
from .comfy_common import (
    media_stream, stream_seconds, already_delivered, approval_context, approve_generation, deliver_copy, inspect_references, last_frame,
    output_path as validate_output_path, prompt_bindings, publish_delivery, record_job_charge, remember_external_billing, request_target, video_metadata,
)
from .video_common import default_ffmpeg, default_ffprobe, verify_runninghub_video
from .runninghub_workflows.catalog import (
    build_workflow_graph,
    get_workflow_spec,
    load_and_verify_graph,
    validate_h3_prompt,
    workflow_media_caps,
)

DEFAULT_API_BASE = "https://www.runninghub.cn"
API_KEY_ENV = "RUNNINGHUB_API_KEY"
PRICING_MODE = "post_billed_points_estimated"
DEFAULT_MAX_CONCURRENCY = 3
MAX_CONCURRENCY_ENV = "YC_H3_RUNNINGHUB_MAX_CONCURRENCY"

_MEDIA_SUFFIXES = {
    "images": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "audios": frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}),
    "videos": frozenset({".mp4", ".mov", ".mkv", ".webm"}),
}
_PARAM_NAMES = frozenset({"width", "height", "seconds", "steps", "seed", "aspect", "save_last_frame", "no_music"})
_MAX_VIDEO_BYTES = 30_000_000
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SESSION_FIELDS = re.compile(r"""(?i)["']?(?:netWssUrl|clientId)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)""")
def _sanitize_secret(value: Any, key: str) -> str:
    return redact_secrets(_SESSION_FIELDS.sub("[REDACTED]", str(value)), key)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _operation_lock(task_key: str) -> str:
    return "runninghub-op-" + sha256_bytes(task_key.encode())[:32]


class _TransportError(ProviderError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, code="runninghub_transport_error", exit_code=3)
        self.status = status


class RunningHubProvider(Provider):
    provider_id = "runninghub"
    capabilities = ("video",)

    def __init__(
        self,
        *,
        api_base: str | None = None,
        config: EngineConfig | None = None,
        auth_opener: Any = None,
        download_opener: Any = None,
        poll_interval_sec: float = 10.0,
        timeout_sec: float = 2400.0,
        queue_timeout_sec: float = 180.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_concurrency: int | None = None,
        slot_poll_interval_sec: float = 0.25,
        slot_wait_timeout_sec: float | None = None,
    ) -> None:
        self.config = config or load_config()
        self._api_base = api_base if api_base is not None else self.config.get("RUNNINGHUB_API_BASE", DEFAULT_API_BASE)
        self._auth_opener = auth_opener
        self._download_opener = download_opener
        self._poll_interval = max(0.01, poll_interval_sec)
        self._timeout = max(0, timeout_sec)
        self._queue_timeout = max(0, queue_timeout_sec)
        self._sleep = sleep_fn
        self._max_concurrency = resolve_positive_int_limit(
            MAX_CONCURRENCY_ENV, max_concurrency, default=DEFAULT_MAX_CONCURRENCY, config=self.config,
        )
        self._slot_poll_interval = slot_poll_interval_sec
        self._slot_wait_timeout = slot_wait_timeout_sec

    def _identity(self) -> tuple[str, str, str]:
        key, error = validate_api_key(self.config.get(API_KEY_ENV))
        if error:
            raise ProviderError("RUNNINGHUB_API_KEY 缺失或无效", code=error, exit_code=3)
        try:
            if any(ch.isspace() or ch in "?#" for ch in self._api_base):
                raise ValueError()
            origin, host = normalize_api_base(self._api_base)
            if self.config.redact(origin) != origin:
                raise ValueError()
        except (ValueError, TypeError):
            raise ProviderError("RunningHub API 地址必须为纯 HTTPS 源", code="invalid_api_base", exit_code=3) from None
        if host not in allowed_hosts(("www.runninghub.cn",), config=self.config):
            raise ProviderError("RunningHub API 主机未获信任", code="untrusted_api_host", exit_code=3)
        return origin, key, credential_fingerprint(key)

    def _workflow(self, workflow):
        name, spec = get_workflow_spec(workflow)
        specific_key = {"jinyu-hybrid": "RUNNINGHUB_WORKFLOW_ID_HYBRID",
                        "jinyu-combat": "RUNNINGHUB_WORKFLOW_ID_COMBAT"}.get(name)
        workflow_id = (self.config.get(specific_key) if specific_key else None) or self.config.get("RUNNINGHUB_WORKFLOW_ID") or spec["workflow_id"]
        if not isinstance(workflow_id, str) or not re.fullmatch(r"[0-9]+", workflow_id):
            setting = f"{specific_key} 或 RUNNINGHUB_WORKFLOW_ID" if specific_key else "RUNNINGHUB_WORKFLOW_ID"
            raise ProviderError(
                f"请在 RunningHub 复制所选工作流并从网页成功运行一次，再配置数字副本 ID（{setting}）。"
                "金鱼原帖：https://www.runninghub.cn/post/2109907990638850050",
                code="invalid_workflow_id" if workflow_id else "missing_workflow_id", exit_code=3,
            )
        return name, dict(spec, workflow_id=workflow_id)

    def _post(
        self,
        origin: str,
        key: str,
        path: str,
        payload: dict,
        *,
        upload: tuple[Path, bytes] | None = None,
        timeout: float = 60,
    ) -> dict:
        if upload is None:
            body = canonical_json_bytes({"apiKey": key, **payload})
            content_type = "application/json"
        else:
            path_obj, raw = upload
            boundary = "yh3-" + uuid.uuid4().hex
            filename = re.sub(r'[^a-zA-Z0-9._-]', '_', path_obj.name)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"apiKey\"\r\n\r\n{key}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileType\"\r\n\r\ninput\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("ascii") + raw + f"\r\n--{boundary}--\r\n".encode("ascii")
            content_type = f"multipart/form-data; boundary={boundary}"

        request = urllib.request.Request(
            origin + path,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        )
        opener = self._auth_opener or urllib.request.build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                status = response.getcode()
                if status is not None and not 200 <= status < 300:
                    raise _TransportError(f"RunningHub 返回 HTTP {status}", status=status)
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _TransportError(f"RunningHub 返回 HTTP {status}", status=status) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # The request body and response may contain credentials and websocket tokens.
            raise _TransportError(f"RunningHub 请求或响应异常（{type(exc).__name__}）") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise _TransportError("RunningHub 返回无效的响应状态")
        return result

    def probe(self, *, capability: str | None = None, check_remote: bool = False, workflow: str | None = None) -> ProbeResult:
        result = super().probe(capability=capability)
        if not result.available:
            return result
        try:
            if workflow is not None:
                _, spec = self._workflow(workflow)
                load_and_verify_graph(spec)
            origin, key, fingerprint = self._identity()
            details: dict[str, Any] = {"endpoint_origin": origin, "account_fp": fingerprint}
            if check_remote:
                response = self._post(origin, key, "/uc/openapi/accountStatus", {"apikey": key}, timeout=8)
                data = response.get("data")
                if response.get("code") != 0 or not isinstance(data, dict):
                    raise ProviderError("RunningHub 账号查询失败", code="account_status_failed")
                for name in ("remainPoint", "remainCoins", "remainMoney", "currentTaskCounts"):
                    val = _number(data.get(name))
                    if val is not None:
                        details[name] = val
                if data.get("currency") in ("CNY", "USD"):
                    details["currency"] = data["currency"]
        except ProviderError as exc:
            return ProbeResult(self.provider_id, False, exc.code, self.capabilities, {"detail": self.config.redact(str(exc))})
        except (OSError, TypeError, ValueError, KeyError):
            return ProbeResult(self.provider_id, False, "workflow_registry_invalid", self.capabilities)
        return ProbeResult(self.provider_id, True, capabilities=self.capabilities, details=details)

    def _prepare(self, request: GenerationRequest, store: TaskStore) -> tuple[PreflightResult, dict]:
        try:
            workflow, spec = self._workflow(request.workflow)
            origin, key, fingerprint = self._identity()
        except ProviderError as exc:
            return self._blocked(exc.code, str(exc)), {}
        except (OSError, TypeError, ValueError, KeyError) as exc:
            return self._blocked("invalid_workflow", str(exc)), {}
        try:
            task = request.task or ("r2v" if any(request.inputs.values()) else "t2v")
            if task == "i2v":
                raise ValueError("金鱼工作流基于 MiniMaxH3ReferenceToVideo，单图首帧请用 --task r2v")
            if request.capability != "video" or task not in spec["tasks"]:
                raise ValueError("该 RunningHub 工作流只接受 video 能力下的 r2v 或 t2v")
            if request.model_id is not None or request.simulated_gpu is not None:
                raise ValueError("RunningHub 的模型与卡型由工作流指定")
            if set(request.params) - _PARAM_NAMES or set(request.inputs) - _MEDIA_SUFFIXES.keys():
                raise ValueError("RunningHub 请求包含未支持的参数或输入类型")
            request_target(request, self.config)
            args = request.params
            aspect, (width, height) = h3.jinyu_size(args, spec["sizes"], spec["default_aspect"])
            no_music = args.get("no_music", False)
            save_last_frame = args.get("save_last_frame", False)
            if any(type(flag) is not bool for flag in (no_music, save_last_frame)):
                raise ValueError("no_music 与 save_last_frame 必须为布尔值")
            norm = {
                "task": task, "prompt": apply_no_music_policy(request.prompt.strip(), no_music),
                "seconds": args.get("seconds", 5), "aspect": aspect, "width": width, "height": height,
                "steps": args.get("steps", spec["default_steps"]),
                "seed": args.get("seed", spec["default_seed"]),
                "no_music": no_music, "save_last_frame": save_last_frame, "instance_type": spec["instance_type"],
            }
            if self.config.redact(norm["prompt"]) != norm["prompt"]:
                raise ValueError("提示词不得含凭据")
            if type(norm["seed"]) is not int or not -1 <= norm["seed"] < 2**64:
                raise ValueError("seed 必须是 -1 或 64 位无符号整数")
            if type(norm["seconds"]) not in (int, float) or not spec["min_seconds"] <= norm["seconds"] <= spec["max_seconds"]:
                raise ValueError(f"--seconds 必须在 {spec['min_seconds']}–{spec['max_seconds']} 之间")

            caps = workflow_media_caps(spec)
            media = {}
            for slot, suffixes in _MEDIA_SUFFIXES.items():
                supplied = request.inputs.get(slot, ())
                if not isinstance(supplied, (list, tuple)) or len(supplied) > caps[slot]:
                    raise ValueError(f"{slot} 必须为列表，最多 {caps[slot]} 项")
                media[slot] = []
                for item in supplied:
                    entry = dict(item) if isinstance(item, dict) else {"path": item}
                    if set(entry) - ({"path", "use_audio"} if slot == "videos" else {"path"}):
                        raise ValueError(f"{slot} 含未支持的素材选项")
                    if "use_audio" in entry and type(entry["use_audio"]) is not bool:
                        raise ValueError("视频 use_audio 必须是布尔值")
                    path = ensure_real_path(entry.get("path"), must_exist=True, expect_dir=False)
                    if path.suffix.lower() not in suffixes:
                        raise ValueError(f"{slot} 支持的文件后缀为 {', '.join(sorted(suffixes))}")
                    media[slot].append(dict(entry, path=path))
            if task == "r2v" and not any(media.values()):
                raise ValueError("r2v 至少需要 1 项参考图、音频或视频")
            if task == "t2v" and any(media.values()):
                raise ValueError("t2v 不接受参考素材，请使用 --task r2v")

            file_hashes = {}
            hashes = {}
            slots = []
            for slot, items in media.items():
                cfg = spec["nodes"][slot]
                for index, (item, node_id) in enumerate(zip(items, cfg["ids"]), 1):
                    path = item["path"]
                    if not path.stat().st_size:
                        raise ValueError(f"{slot} 第 {index} 项为空文件")
                    if path not in file_hashes:
                        file_hashes[path] = sha256_file(path)
                    slot_key = f"{slot}:{index}"
                    hashes[slot_key] = file_hashes[path]
                    slots.append((slot_key, path, hashes[slot_key], node_id, cfg["input"]))

            video_specs = [
                _reference_video_spec(video, audio, window)
                for _, video, audio, window in inspect_references(dict(media, seconds=norm["seconds"]))
            ]
            video_by_slot = {f"videos:{index}": video for index, video in enumerate(video_specs, 1)}
            n_audio = len(media["audios"]) + sum(video["use_audio"] for video in video_specs)
            errors, warnings = validate_h3_prompt(
                norm["prompt"], mode="t2va" if task == "t2v" else spec["prompt_mode"], seconds=norm["seconds"],
                n_images=len(media["images"]), n_audios=n_audio, n_videos=len(video_specs), media_caps=caps,
            )
            if errors:
                return self._blocked("invalid_h3_prompt", "；".join(_sanitize_secret(e, key) for e in errors), origin), {}
            graph = build_workflow_graph(
                spec, prompt=norm["prompt"], image_names=["None"] * len(media["images"]),
                audio_names=["None"] * len(media["audios"]), seconds=norm["seconds"],
                aspect=aspect, size=(width, height), seed=norm["seed"], steps=norm["steps"], videos=video_specs,
            )
            history = perf_history.estimate_from_history(
                store.config, provider=self.provider_id, workflow=workflow, output_seconds=norm["seconds"],
                has_ref_video=bool(video_specs), hardware=norm["instance_type"],
            )
            calibrated = (
                spec["estimate_calibrated_for_point"] and norm["steps"] == spec["default_steps"]
                and norm["instance_type"] == spec["instance_type"] and (width, height) == tuple(spec["sizes"]["16:9"])
                and not video_specs
            )
            if calibrated:
                estimated_seconds = round(norm["seconds"] * spec["run_seconds_per_output_second"])
                estimated_points = round(estimated_seconds * spec["reference_coins_per_run_second"], 1)
                estimate_note = "估算基于单条 Plus 48GB 横屏 1664×928 样本外推，最终以 chargedPoint 为准。"
            else:
                estimated_seconds = estimated_points = None
                estimate_note = "单条 Plus 48GB 横屏 1664×928 样本未覆盖当前配置，暂无积分估算，最终以 chargedPoint 为准。"
            graph_hash = sha256_bytes(canonical_json_bytes(graph))
            display_aspect = None if ("width" in args or "height" in args) else aspect
            bindings = prompt_bindings(
                images=media["images"],
                audios=media["audios"],
                videos=[(item["path"], video_spec["use_audio"]) for item, video_spec in zip(media["videos"], video_specs)],
                redact=lambda value: store.config.redact(self.config.redact(value)),
            )
            facts = {
                "estimated_run_seconds": estimated_seconds,
                "estimated_reference_points": estimated_points,
                "estimate_calibrated_for_point": calibrated,
                "estimate_note": estimate_note,
                "instance_type": norm["instance_type"],
                "width": width, "height": height, "aspect": aspect, "seconds": norm["seconds"],
                "seed": norm["seed"], "steps": norm["steps"], "frames": h3.output_frames(norm),
                "workflow_graph_sha256": graph_hash,
                "reference_videos": video_by_slot,
                "effective_audio_count": n_audio,
            }
            target = {"workflow": workflow, "workflow_id": spec["workflow_id"], "credential_fingerprint": fingerprint,
                      "instance_type": norm["instance_type"]}
            rate = lookup_cny_rate(self.provider_id, "point", config=store.config)
            facts["cny_per_point"] = rate
            facts["estimated_cny"] = (
                round(estimated_points * rate, 4) if estimated_points is not None and rate is not None else None
            )
            facts["slots"] = caps
            request_facts = facts.copy()
            if history and (history["estimated_run_seconds"] is not None or history["estimated_quantity"] is not None):
                estimated_seconds = history["estimated_run_seconds"]
                estimated_points = history["estimated_quantity"]
                reference_rate = spec.get("reference_coins_per_run_second")
                if estimated_points is None and estimated_seconds is not None and reference_rate is not None:
                    estimated_points = estimated_seconds * reference_rate
                if estimated_points is not None:
                    estimated_points = round(estimated_points, 1)
                samples = history["samples"]
                facts.update(
                    estimated_run_seconds=estimated_seconds, estimated_wall_seconds=history["estimated_wall_seconds"],
                    estimated_reference_points=estimated_points, estimate_calibrated_for_point=estimated_points is not None,
                    estimated_cny=round(estimated_points * rate, 4) if estimated_points is not None and rate is not None else None,
                    estimate_source="local_history", estimate_samples=samples,
                    estimate_note=f"基于本地 {samples} 条历史实跑样本估算，最终以 chargedPoint 为准。",
                )
            else:
                facts.update(estimate_source="initial_baseline" if calibrated else "uncalibrated",
                             estimate_samples=0, estimated_wall_seconds=None)
            context = approval_context(
                request, store, config=self.config, provider=self.provider_id, origin=origin,
                fingerprint=fingerprint, workflow=workflow, params=norm, hashes=hashes, facts=facts, target=target,
                request_facts=request_facts,
            )
        except (OSError, TypeError, ValueError, KeyError, AttributeError, ProviderError) as exc:
            return self._blocked("invalid_runninghub_request", _sanitize_secret(exc, key), origin), {}

        signals = [{"severity": "warn", "code": PRICING_MODE, "detail": facts["estimate_note"]}]
        signals.extend({"severity": "warn", "code": "h3_prompt_risk", "detail": self.config.redact(_sanitize_secret(w, key))}
                       for w in warnings)
        preflight = PreflightResult(
            self.provider_id, "warn", context["digest"], PRICING_MODE, origin,
            target_identity=target, input_hashes=hashes, signals=signals, facts={
                **facts, "aspect": display_aspect, "prompt_bindings": bindings, "task_key": context["task_key"],
                "replaced_submission_id": context["replaced_submission_id"],
            },
        )
        return preflight, {
            **context, "key": key,
            "spec": spec, "norm": norm, "slots": slots, "video_by_slot": video_by_slot, "graph": graph,
        }

    def _blocked(self, code: str, detail: str, origin: str = "") -> PreflightResult:
        return PreflightResult(self.provider_id, "block", "", PRICING_MODE, origin,
                               signals=[{"severity": "block", "code": code, "detail": self.config.redact(detail)}])

    def preflight(self, request: GenerationRequest, *, store: TaskStore | None = None) -> PreflightResult:
        result, _ = self._prepare(request, store or TaskStore(config=self.config))
        return result

    def _upload(self, origin: str, key: str, path: Path, raw: bytes) -> str:
        result = self._post(origin, key, "/task/openapi/upload", {}, upload=(path, raw), timeout=120)
        data = result.get("data")
        name = data.get("fileName") if isinstance(data, dict) else None
        if result.get("code") != 0 or not isinstance(name, str) or not name.strip() or key in name:
            raise ProviderError("RunningHub 素材上传未返回有效文件名", code="upload_failed")
        return name

    def _submit(self, store: TaskStore, task_key: str, origin: str, context: dict) -> dict:
        key = context["key"]
        spec = context["spec"]
        graph = context["graph"]
        raw_by_path = {}
        uploaded = {}
        try:
            for _, path, expected_hash, _, _ in context["slots"]:
                if path in raw_by_path:
                    continue
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = None
                if raw is None or sha256_bytes(raw) != expected_hash:
                    raise ApprovalRequiredError(
                        "输入素材在预检审批后已变更，请重新预检审批；沿用原输出路径请传 --force-new，或改用新输出版本路径",
                        code="input_changed_after_preflight",
                    )
                raw_by_path[path] = raw

            for slot, path, _, node_id, field in context["slots"]:
                video = context["video_by_slot"].get(slot)
                upload_key = path, canonical_json_bytes(video) if video is not None else None
                if upload_key not in uploaded:
                    raw = raw_by_path[path]
                    upload_path = path
                    if video is not None:
                        upload_path = path.with_name(f"{path.stem}_normalized.mp4")
                        with tempfile.TemporaryDirectory(prefix="runninghub-ref-") as staging:
                            source = Path(staging) / ("source" + path.suffix.lower())
                            target = Path(staging) / "normalized.mp4"
                            source.write_bytes(raw)
                            _normalize_reference_video(source, target, video)
                            if target.stat().st_size > _MAX_VIDEO_BYTES:
                                raise ProviderError("规范化参考视频超过 30 MB", code="normalized_video_too_large", exit_code=3)
                            raw = target.read_bytes()
                    uploaded[upload_key] = self._upload(origin, key, upload_path, raw)
                graph[node_id]["inputs"][field] = uploaded[upload_key]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = ProviderError(_sanitize_secret(f"RunningHub 素材准备失败：{exc}", key),
                                  code="input_preparation_failed", exit_code=3)
            raise error from None
        except ProviderError as exc:
            raise

        resolved_seed = context["norm"]["seed"]
        if resolved_seed == -1:
            resolved_seed = secrets.randbelow(2**31 - 1) + 1
        seed = spec["nodes"]["seed"]
        graph[seed["id"]]["inputs"][seed["input"]] = resolved_seed
        payload = {"workflowId": str(spec["workflow_id"]), "workflow": canonical_json_bytes(graph).decode(),
                   "instanceType": context["norm"]["instance_type"], "addMetadata": False}
        deadline = time.monotonic() + self._queue_timeout
        delay = 2.0
        timing = {"pre_accept_retries": 0, "pre_accept_backoff_s": 0.0}
        while True:
            try:
                attempt_started_at = datetime.now(timezone.utc).isoformat()
                timing.setdefault("first_create_requested_at", attempt_started_at)
                reply = self._post(origin, key, "/task/openapi/create", payload)
            except _TransportError as exc:
                if exc.status == 421:
                    reply = {"code": 421}
                elif exc.status in _CLIENT_REJECTIONS:
                    reply = {"code": exc.status, "msg": "HTTP 客户端拒单"}
                else:
                    store.mark_ambiguous(task_key, reason=str(exc), extra=timing)
                    raise ProviderConflictError("RunningHub 提交结果不明，已保留断点", code="ambiguous_submit") from None
            except ProviderError as exc:
                store.mark_ambiguous(task_key, reason=_sanitize_secret(exc, key), extra=timing)
                raise ProviderConflictError(
                    _sanitize_secret(exc, key), code=exc.code, details={"task_key": task_key}
                ) from None

            code = reply.get("code")
            msg = _sanitize_secret(reply.get("msg") or "", key)[:300]
            data = reply.get("data")
            remote_id = data.get("taskId") if isinstance(data, dict) else None
            if type(remote_id) is int:
                remote_id = str(remote_id)
            if code == 0 and isinstance(remote_id, str) and _TASK_ID.fullmatch(remote_id) and key not in remote_id:
                return store.mark_external_submitted(task_key, remote_id, extra={
                    **timing, "accepted_at": time.time(),
                    "create_requested_at": attempt_started_at, "resolved_seed": resolved_seed,
                })
            if not remote_id and (code == 421 or "QUEUE_MAXED" in msg.upper()):
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    backoff_started = time.monotonic()
                    self._sleep(min(remaining, delay))
                    backoff_finished = time.monotonic()
                    timing["pre_accept_backoff_s"] += backoff_finished - backoff_started
                    delay = min(delay * 2, 15)
                    if backoff_finished < deadline:
                        timing["pre_accept_retries"] += 1
                        continue
                store.mark_failed(task_key, error="RunningHub 并发队列已满", extra=timing)
                raise ProviderError("RunningHub 并发队列等待超时", code="queue_maxed")
            if not remote_id and type(code) is int and code not in (0, 500, 804, 813, 1000, 1005, 1006, 1010):
                store.mark_failed(task_key, error=f"RunningHub 拒单 {code} {msg}", extra=timing)
                raise ProviderError(f"RunningHub 拒单 {code} {msg}", code="submit_rejected")
            store.mark_ambiguous(task_key, reason="RunningHub 返回不明确的入队结果或无有效 taskId", extra=timing)
            raise ProviderConflictError("RunningHub 入队结果不明，已保留断点", code="ambiguous_submit")

    def generate(self, request: GenerationRequest, *, store: TaskStore | None = None) -> ArtifactResult:
        store = store or TaskStore(config=self.config)
        preflight, ctx = self._prepare(request, store)
        approve_generation(request, preflight, store, ctx)
        task_key = ctx["task_key"]
        origin = preflight.endpoint_origin
        fingerprint = preflight.target_identity["credential_fingerprint"]
        slots = ProviderSlots(
            self.provider_id, origin, fingerprint, max_slots=self._max_concurrency, sleep_fn=self._sleep,
            poll_interval_sec=self._slot_poll_interval, wait_timeout_sec=self._slot_wait_timeout, data_dir=store.data_dir,
        )
        with ExitStack() as held_slot:
            try:
                with slots.lock_for_submission(
                    store, task_key, _operation_lock(task_key), logical_target=ctx["logical_target"],
                    force_new=request.force_new, held_slot=held_slot,
                ):
                    action, record = store.begin_external_submit(
                        task_key, provider=self.provider_id, strict_reuse=True, request_digest=ctx["request_digest"],
                        credential_fingerprint=fingerprint,
                        endpoint_origin=origin, logical_target=ctx["logical_target"], force_new=request.force_new,
                        replaced_submission_id=ctx["replaced_submission_id"],
                        extra={
                            "wanted_output_path": str(ctx["wanted"]),
                            "workflow_id": ctx["spec"]["workflow_id"], "cny_per_point": preflight.facts["cny_per_point"],
                            "tag": request.metadata.get("tag"), "approved_digest": preflight.digest,
                            "workflow": preflight.target_identity["workflow"],
                            "media_params": {name: value for name, value in ctx["norm"].items() if name != "prompt"},
                            "has_ref_video": bool(ctx["video_by_slot"]),
                            "estimated_reference_points": preflight.facts["estimated_reference_points"],
                            "estimate_calibrated_for_point": preflight.facts["estimate_calibrated_for_point"],
                        },
                    )
                    if action == "delivered" and already_delivered(record):
                        return self._delivered(store, record, recovered=True)
                    if action == "submit":
                        record = self._submit(store, task_key, origin, ctx)
            except TaskConflictError as exc:
                raise ProviderConflictError(str(exc), code=exc.code, record=exc.record) from None
            return self._poll(store, record, origin, ctx["key"], ctx["wanted"], recovered=action != "submit")

    def _restore(self, store: TaskStore, task_key: str) -> tuple[dict, str, str]:
        origin, key, fingerprint = self._identity()
        record = store.get(task_key)
        if record is None or record.get("provider") != self.provider_id:
            raise ProviderConflictError(f"任务 {task_key} 缺少 RunningHub 断点")
        if record.get("endpoint_origin") != origin or record.get("credential_fingerprint") != fingerprint:
            raise ProviderConflictError("RunningHub 凭据或源与旧断点不匹配", code="credential_or_origin_mismatch")
        if reserved := record.get("reserved_output_path"):
            ensure_real_path(reserved, expect_dir=False)
        return store.reconcile_delivery(task_key) or record, origin, key

    def _current(self, store: TaskStore, record: dict) -> dict:
        latest = store.get(record["task_key"])
        if not latest or (
            submission_identity(latest), latest.get("remote_task_id")
        ) != (submission_identity(record), record.get("remote_task_id")):
            raise ProviderConflictError("RunningHub 断点已被其他提交替换")
        if latest["state"] not in ("submitted", "delivered"):
            raise ProviderConflictError(f"RunningHub 断点当前状态为 {latest['state']}", code=latest["state"])
        return latest

    def resume(
        self, task_key: str, *, store: TaskStore, remote_job_id: str | None = None, remote_task_id: str | None = None,
        output_path: Path | str | None = None, **kwargs: Any,
    ) -> ArtifactResult:
        return self._recover(store, task_key, output_path, remote_task_id=remote_job_id or remote_task_id)

    def fetch(
        self, task_key: str, *, store: TaskStore, output_path: Path | str | None = None,
        remote_job_id: str | None = None, **kwargs: Any,
    ) -> ArtifactResult:
        return self._recover(store, task_key, output_path, remote_task_id=remote_job_id, single_poll=True)

    def _recover(
        self, store: TaskStore, task_key: str, output_path: Path | str | None, *,
        remote_task_id: str | None = None, single_poll: bool = False,
    ) -> ArtifactResult:
        with store.lock(_operation_lock(task_key)):
            record, origin, key = self._restore(store, task_key)
            wanted = validate_output_path(output_path or record["wanted_output_path"])
            if self.config.redact(str(wanted)) != str(wanted):
                raise ValueError("交付路径不得含凭据")
            if remote_task_id is not None and remote_task_id != record.get("remote_task_id"):
                if record["state"] not in ("submitting", "ambiguous"):
                    raise ProviderConflictError("仅提交结果不明的 RunningHub 断点可认领 taskId")
                if not _TASK_ID.fullmatch(remote_task_id) or key in remote_task_id:
                    raise ProviderConflictError("RunningHub taskId 格式无效")
                record = store.mark_external_submitted(task_key, remote_task_id)
            if already_delivered(record):
                if output_path and not already_delivered(record, wanted):
                    record = deliver_copy(store, record, wanted)
                return self._delivered(store, record, recovered=True)
            if record["state"] not in ("submitted", "delivered") or not record.get("remote_task_id"):
                raise ProviderConflictError(
                    f"断点当前为 {record['state']}；提交结果不明时可通过 --remote-job-id 认领",
                    code=record["state"],
                )
        return self._poll(store, record, origin, key, wanted, recovered=True, single_poll=single_poll, override=output_path is not None)

    def cancel(self, task_key: str, *, store: TaskStore, reason: str = "用户取消", **kwargs: Any) -> dict:
        with store.lock(_operation_lock(task_key)):
            record, origin, key = self._restore(store, task_key)
            if record["state"] == "cancelled":
                return record
            remote_id = record.get("remote_task_id")
            if record["state"] != "submitted" or not remote_id:
                raise ProviderConflictError(f"当前状态 {record['state']} 无法确认远端取消")
            try:
                result = self._post(origin, key, "/task/openapi/cancel", {"taskId": remote_id})
                if result.get("code") != 0:
                    msg = _sanitize_secret(result.get("msg") or "远端拒绝取消", key)[:300]
                    raise ProviderError(msg, code="cancel_failed", exit_code=3)
            except ProviderError as exc:
                store.record_cancel_error(task_key, error=_sanitize_secret(exc, key))
                raise
            record = store.mark_cancelled(task_key, reason=_sanitize_secret(reason, key),
                                          extra={"cancel_error": None, "completed_at": time.time()})
            return record

    def _poll(
        self, store: TaskStore, record: dict, origin: str, key: str, wanted: Path, *,
        recovered: bool, single_poll: bool = False, override: bool = False,
    ) -> ArtifactResult:
        task_key = record["task_key"]
        remote_id = record["remote_task_id"]
        deadline = time.monotonic() + self._timeout

        def terminal(*, missing: bool, message: str, status: int | None = None, data: dict | None = None) -> None:
            with store.lock(_operation_lock(task_key)):
                current = self._current(store, record)
                if current["state"] == "submitted":
                    if missing:
                        current = store.mark_expired_or_missing(task_key, reason=message, http_status=status)
                    else:
                        extra = {"completed_at": time.time()}
                        if data is not None:
                            extra["billing"] = self._billing(current, data, allow_estimate=False)
                        current = store.mark_failed(task_key, error=message, extra=extra)
                        self._record_charge(store, current)
            if missing:
                raise ProviderConflictError(message, code="expired_or_missing")
            raise ProviderError(message, code="remote_task_failed")

        while True:
            self._current(store, record)
            try:
                response = self._post(
                    origin, key, "/task/openapi/outputs", {"taskId": remote_id},
                    timeout=min(60, max(1, deadline - time.monotonic())),
                )
            except _TransportError as exc:
                if exc.status in (404, 410):
                    terminal(missing=True, message=str(exc), status=exc.status)
                if single_poll or time.monotonic() >= deadline or (
                    exc.status is not None and exc.status not in _TRANSIENT
                ):
                    raise
                response = {"code": 804}

            code = response.get("code")
            data = response.get("data")
            if code in (423, 807, 1004):
                terminal(missing=True, message=f"RunningHub 找不到任务，code={code}")
            if code == 805:
                reason = data.get("failedReason") if isinstance(data, dict) else None
                terminal(
                    missing=False, message=_sanitize_secret(reason or response.get("msg") or "远端生成失败", key)[:300],
                    data=data if isinstance(data, dict) else None,
                )
            if code == 0 and isinstance(data, list) and data:
                for item in data:
                    if isinstance(item, dict) and str(item.get("fileType", "")).lower() == "mp4":
                        return self._deliver(
                            store, record, item, wanted, key=key, recovered=recovered, completed_at=time.time(), override=override,
                        )
                raise ArtifactContractError("RunningHub 输出未包含 MP4 成品")
            if code not in (804, 813) and not (code == 0 and data in (None, [], {})):
                raise ProviderError(
                    "RunningHub 输出查询返回异常状态：" + _sanitize_secret(response.get("msg") or code, key)[:300],
                    code="poll_rejected", exit_code=3,
                )
            remaining = deadline - time.monotonic()
            if single_poll or remaining <= 0:
                raise ProviderConflictError("RunningHub 任务尚未完成，可用 jobs resume 续查", code="task_not_completed")
            self._sleep(min(remaining, self._poll_interval))

    def _download(self, url: Any, dest: Path, *, key: str) -> None:
        try:
            if not isinstance(url, str) or self.config.redact(urllib.parse.unquote(url), key) != urllib.parse.unquote(url):
                raise ValueError()
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
                raise ValueError()
        except ValueError:
            raise ArtifactContractError("RunningHub 成品下载地址必须为无凭据 HTTPS URL") from None

        opener = self._download_opener or urllib.request.build_opener(HttpsOnlyDownloadRedirectHandler())
        try:
            request = urllib.request.Request(url, method="GET")
            with opener.open(request, timeout=120) as response, dest.open("wb") as stream:
                size = 0
                header = response.headers.get("Content-Length")
                expected = parse_content_length(header)
                if header is not None and expected is None:
                    raise ArtifactContractError("RunningHub 成品 Content-Length 无效")
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
                    size += len(chunk)
                if not size or (expected is not None and size != expected):
                    raise ArtifactContractError("RunningHub 成品为空或 Content-Length 不匹配")
                stream.flush()
                os.fsync(stream.fileno())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProviderError(f"RunningHub 成品下载失败（HTTP {status}）", code="download_failed") from None
        except ProviderError as exc:
            if isinstance(exc, ArtifactContractError):
                raise
            raise ArtifactContractError("RunningHub 成品下载重定向不符合 HTTPS 合同") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ArtifactContractError("RunningHub 成品下载不完整或无法读取") from None

    @staticmethod
    def _billing(record: dict, item: dict, *, allow_estimate: bool = True) -> dict:
        points = next((v for name in ("chargedPoint", "consumePoint", "consumePoints")
                       if (v := _number(item.get(name))) is not None), None)
        estimated = points is None
        if estimated and allow_estimate:
            points = record.get("estimated_reference_points")
        rate = record.get("cny_per_point")
        return {
            "pricing_mode": PRICING_MODE,
            "task_id": record["remote_task_id"],
            "run_seconds": _number(item.get("taskCostTime")),
            "points": points,
            "est_cny": round(points * rate, 4) if points is not None and rate is not None else None,
            "estimated": estimated,
            "estimate_calibrated_for_point": record.get("estimate_calibrated_for_point", False),
            "submitted_at": record.get("create_requested_at"),
        }

    def _deliver(self, store, record, item, wanted, *, key, recovered, completed_at, override):
        task_key = record["task_key"]
        with store.lock(_operation_lock(task_key)):
            record = self._current(store, record)
            record = remember_external_billing(store, record, self._billing(record, item))
            self._record_charge(store, record)
        wanted.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".runninghub-", dir=wanted.parent) as staging:
            tmp = Path(staging) / "clip.mp4"
            self._download(item.get("fileUrl"), tmp, key=key)
            media = record["media_params"]
            contract = verify_runninghub_video(
                video_metadata(tmp), workflow=record["workflow"], width=media["width"], height=media["height"],
                expected_frames=h3.output_frames(media),
            )
            with store.lock(_operation_lock(task_key)):
                current = self._current(store, record)
                current = store.reconcile_delivery(task_key) or current
                if already_delivered(current):
                    if override and not already_delivered(current, wanted):
                        current = deliver_copy(store, current, wanted)
                    return self._delivered(store, current, recovered=True)
                extra = {"contract": contract, "billing": current.get("billing") or self._billing(record, item),
                         "completed_at": completed_at, "last_frame_reserved_path": None, "last_frame_sha256": None}
                result = publish_delivery(store, current, tmp, wanted, extra)
                return self._delivered(store, result, recovered=recovered)

    @staticmethod
    def _record_charge(store, record):
        billing = record.get("billing") or {}
        record_job_charge(store.config, record, billing.get("points"), "point",
                          est_cny=billing.get("est_cny"), estimated=billing.get("estimated", False))

    def _delivered(self, store, record, *, recovered):
        self._record_charge(store, record)
        contract = dict(record["contract"])
        media = record["media_params"]
        if media["save_last_frame"]:
            contract["last_frame_path"] = last_frame(store, record, contract)
        billing = record.get("billing") or {}
        has_ref_video = record.get("has_ref_video")
        # Older r2v checkpoints cannot distinguish image or audio references from video.
        if has_ref_video is not None or media.get("task") == "t2v":
            with suppress(OSError, ValueError):
                perf_history.record_observation(
                    store.config, idempotency_key=f"{self.provider_id}:{record['remote_task_id']}",
                    provider=self.provider_id, workflow=record["workflow"], hardware=media.get("instance_type"),
                    width=media.get("width"), height=media.get("height"), output_seconds=media["seconds"],
                    has_ref_video=bool(has_ref_video), run_seconds=billing.get("run_seconds") or None,
                    wall_seconds=perf_history.elapsed_seconds(record.get("completed_at"), record.get("accepted_at"),
                                                              record.get("first_create_requested_at")),
                    billed_quantity=billing.get("points"), unit="point", estimated_billing=billing.get("estimated", True),
                )
        return ArtifactResult(self.provider_id, record["task_key"], "delivered", artifact_path=record["artifact_path"],
                              artifact_sha256=record["artifact_sha256"], remote_id=record["remote_task_id"],
                              recovered=recovered, contract=contract, billing=billing)


def _reference_video_spec(stream, audio, window):
    source_width, source_height = stream["width"], stream["height"]
    rotation = next((row["rotation"] for row in stream.get("side_data_list", []) if "rotation" in row),
                    stream.get("tags", {}).get("rotate", 0))
    if float(rotation) % 180 == 90:
        source_width, source_height = source_height, source_width
    max_width, max_height = h3.JINYU_ASPECT_SIZES["16:9"]
    if source_width < source_height:
        max_width, max_height = max_height, max_width
    scale = min(1, max_width / source_width, max_height / source_height)
    size = tuple(math.floor(edge * scale / 32 + 1e-9) * 32 for edge in (source_width, source_height))
    if min(size) < 32:
        raise ValueError("参考视频尺寸过小，无法做 32 像素对齐")
    return dict(norm_w=size[0], norm_h=size[1], window=window, use_audio=audio is not None,
                video_index=stream["index"], audio_index=audio["index"] if audio is not None else None,
                start=float(stream.get("start_time") or 0))


def _normalize_reference_video(source, target, spec):
    width, height = spec["norm_w"], spec["norm_h"]
    window, start = spec["window"], spec["start"]
    duration = window / h3.FPS
    vf = f"fps=24:start_time={start:.9f},scale={width}:{height},setpts=PTS-STARTPTS,setsar=1"
    args = ["-v", "error", "-xerror", "-y", "-copyts", "-i", str(source), "-map", f"0:{spec['video_index']}",
            "-vf", vf, "-t", f"{duration:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if spec["use_audio"]:
        af = f"asetpts=PTS-({start:.9f})/TB,aresample=async=1:first_pts=0,apad,atrim=duration={duration:.9f}"
        args += ["-map", f"0:{spec['audio_index']}", "-af", af, "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        args.append("-an")
    default_ffmpeg([*args, str(target)], timeout=120)
    info = default_ffprobe(target)
    stream, audio = media_stream(info, "video"), media_stream(info, "audio")
    if (not stream or int(stream.get("nb_frames", 0)) != window or Fraction(stream["avg_frame_rate"]) != h3.FPS
            or (stream["width"], stream["height"]) != (width, height)):
        raise ArtifactContractError("参考视频 24fps 对齐失败")
    if spec["use_audio"]:
        if (not audio or audio.get("channels") != 2 or audio.get("sample_rate") != "48000"
                or abs(stream_seconds(info, audio) - duration) > 1 / h3.FPS):
            raise ArtifactContractError("参考视频伴音未对齐画面裁剪窗口")
    elif audio is not None:
        raise ArtifactContractError("静音参考视频仍含音轨")
