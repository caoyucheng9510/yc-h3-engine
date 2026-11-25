"""Chenyu MiniMax H3 workflow API with recoverable, approval-gated submissions."""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, nullcontext, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import perf_history
from ..config import EngineConfig, credential_fingerprint, load_config
from ..ledger import parse_chenyu_balance
from ..fsutil import ensure_real_path, redact_secrets
from ..remote.presets import apply_no_music_policy
from ..remote.workflows.minimax_h3 import spec as h3
from ..tasks_store import TaskConflictError, TaskStore, submission_identity

from ._concurrency import ProviderSlots, count_active_tasks, resolve_positive_int_limit
from ._http import (
    TRANSIENT_HTTP_CODES,
    HttpsOnlyDownloadRedirectHandler,
    NoRedirectHandler,
    allowed_hosts,
    normalize_api_base,
    validate_api_key,
)
from .comfy_common import (
    already_delivered, approval_context, approve_generation, deliver_copy, inspect_references, last_frame,
    output_path as validate_output_path, publish_delivery, record_job_charge, remember_external_billing, request_target, video_metadata,
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
from .chenyu_workflows.catalog import get_workflow_spec, resolve_expected_size, workflow_media_caps
from .video_common import validate_h3_prompt, verify_chenyu_video

DEFAULT_API_BASE = "https://www.chenyu.cn"
API_KEY_ENV = "CHENYU_API_KEY"
PRICING_MODE = "post_billed_cny_estimated"
DEFAULT_MAX_CONCURRENCY = 10
DEFAULT_MAX_QUEUED = 50
DEFAULT_MAX_INFLIGHT = DEFAULT_MAX_CONCURRENCY + DEFAULT_MAX_QUEUED
MAX_CONCURRENCY_ENV = "YC_H3_CHENYU_MAX_CONCURRENCY"
MAX_INFLIGHT_ENV = "YC_H3_CHENYU_MAX_INFLIGHT"

_API = "/api/open/v2"
_SUBMIT_PATH = _API + "/workflow/run/submit"
_MEDIA_SUFFIXES = {
    "images": {".png", ".jpg", ".jpeg", ".webp"},
    "audios": {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"},
}
_PARAM_NAMES = {"aspect", "seconds", "megapixels", "upscale_2x", "save_last_frame", "no_music"}
_MAX_INPUT_BYTES = 100_000_000
_RUN_ID = re.compile(r"wfrun_[A-Za-z0-9_-]{1,122}")
_ACTIVE_STATES = {"queued", "pending", "running", "processing", "pending_review", "submitting", "initializing"}
_MISSING_MESSAGES = {
    "订单不存在", "运行订单不存在", "任务不存在", "run order not found", "order not found",
    "run_order_not_found", "run_order_id not found",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number < float("inf") else None


def _operation_lock(task_key: str) -> str:
    return "chenyu-op-" + sha256_bytes(task_key.encode())[:32]


def _download_url(url: Any, key: str) -> str:
    try:
        if not isinstance(url, str) or any(char.isspace() for char in url):
            raise ValueError()
        decoded = urllib.parse.unquote(url)
        if redact_secrets(decoded, key) != decoded:
            raise ValueError()
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
            raise ValueError()
    except ValueError:
        raise ArtifactContractError("晨羽成品下载地址须为无凭据 HTTPS URL") from None
    return url


class _DownloadRedirectHandler(HttpsOnlyDownloadRedirectHandler):
    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def _validate_redirect_url(self, req, code, newurl):
        try:
            resolved = super()._validate_redirect_url(req, code, newurl)
        except ProviderError as exc:
            raise ArtifactContractError("晨羽成品下载重定向不符合 HTTPS 合同", code=exc.code) from None
        return _download_url(resolved, self._key)


class _TransportError(ProviderError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, code="chenyu_transport_error", exit_code=3)
        self.status = status


class ChenyuProvider(Provider):
    provider_id = "chenyu"
    capabilities = ("video",)
    DEFAULT_API_BASE = DEFAULT_API_BASE
    API_KEY_ENV = API_KEY_ENV
    PRICING_MODE = PRICING_MODE

    def __init__(
        self, *, api_base: str | None = None, config: EngineConfig | None = None, auth_opener: Any = None, download_opener: Any = None,
        poll_interval_sec: float = 5.0, timeout_sec: float = 3600.0, settlement_wait_sec: float = 20.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_concurrency: int | None = None, max_inflight: int | None = None, queue_timeout_sec: float = 180.0,
        slot_poll_interval_sec: float = 0.25, slot_wait_timeout_sec: float | None = None,
    ) -> None:
        self.config = config or load_config()
        self._api_base = api_base if api_base is not None else self.config.get("CHENYU_API_BASE", DEFAULT_API_BASE)
        self._auth_opener = auth_opener
        self._download_opener = download_opener
        self._poll_interval = max(0.01, poll_interval_sec)
        self._timeout = max(0, timeout_sec)
        self._settlement_wait = max(0, settlement_wait_sec)
        self._queue_timeout = max(0, queue_timeout_sec)
        self._sleep = sleep_fn
        self._max_concurrency = resolve_positive_int_limit(
            MAX_CONCURRENCY_ENV, max_concurrency, default=DEFAULT_MAX_CONCURRENCY, maximum=DEFAULT_MAX_INFLIGHT, config=self.config,
        )
        self._max_inflight = resolve_positive_int_limit(
            MAX_INFLIGHT_ENV, max_inflight, default=DEFAULT_MAX_INFLIGHT, maximum=DEFAULT_MAX_INFLIGHT, config=self.config,
        )
        if self._max_concurrency > self._max_inflight:
            raise ValueError(f"{MAX_CONCURRENCY_ENV} 不能大于 {MAX_INFLIGHT_ENV}")
        self._slot_poll_interval = slot_poll_interval_sec
        self._slot_wait_timeout = slot_wait_timeout_sec

    def _identity(self) -> tuple[str, str, str]:
        key, error = validate_api_key(self.config.get(API_KEY_ENV))
        if error or key is None:
            raise ProviderError("CHENYU_API_KEY 缺失或无效", code=error or "invalid_api_key", exit_code=3)
        try:
            if any(char.isspace() for char in self._api_base):
                raise ValueError()
            origin, host = normalize_api_base(self._api_base)
            if self.config.redact(origin) != origin:
                raise ValueError()
        except (ValueError, TypeError):
            raise ProviderError("晨羽 API 地址必须为纯 HTTPS 源", code="invalid_api_base", exit_code=3) from None
        if host not in allowed_hosts(("www.chenyu.cn",), config=self.config):
            raise ProviderError("晨羽 API 主机未获信任", code="untrusted_api_host", exit_code=3)
        return origin, key, credential_fingerprint(key)

    def _api(
        self, origin: str, key: str, path: str, *, payload: dict | None = None,
        upload: tuple[Path, bytes, str] | None = None, timeout: float = 60,
    ) -> dict:
        headers = {"Authorization": f"Bearer {key}"}
        body = None
        if payload is not None:
            body = canonical_json_bytes(payload)
            headers["Content-Type"] = "application/json"
        if upload is not None:
            source, raw, media_type = upload
            boundary = "yh3-" + uuid.uuid4().hex
            filename = "reference" + source.suffix.lower()
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="media_type"\r\n\r\n{media_type}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\ntemp_input\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f'Content-Type: {mime}\r\n\r\n'
            ).encode("ascii") + raw + f"\r\n--{boundary}--\r\n".encode("ascii")
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        request = urllib.request.Request(origin + path, data=body, headers=headers)
        opener = self._auth_opener or urllib.request.build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                status = response.getcode()
                if status is not None and not 200 <= status < 300:
                    raise _TransportError(f"晨羽返回 HTTP {status}", status=status)
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _TransportError(f"晨羽返回 HTTP {status}", status=status) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise _TransportError(f"晨羽请求或响应异常（{type(exc).__name__}）") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise _TransportError("晨羽返回无效的响应状态")
        return result

    def probe(self, *, capability: str | None = None, check_remote: bool = False, workflow: str | None = None) -> ProbeResult:
        result = super().probe(capability=capability)
        if not result.available:
            return result
        try:
            origin, key, fingerprint = self._identity()
            _, spec = get_workflow_spec(workflow)
            details = {"endpoint_origin": origin, "account_fp": fingerprint}
            if check_remote:
                reply = self._api(origin, key, _API + "/balance/info", timeout=8)
                data = reply.get("data")
                if reply["code"] != 0 or not isinstance(data, dict):
                    raise ProviderError("晨羽账号查询失败", code="account_status_failed")
                try:
                    balances = parse_chenyu_balance(data)
                except ValueError:
                    raise ProviderError("晨羽账号查询缺少有效双钱包余额", code="account_status_failed") from None
                details.update(balances)
                if balances["cash_balance"] < spec["creator_fee_cny"]:
                    details["balance_warning"] = f"现金余额不足以支付单次创作者费 {spec['creator_fee_cny']:.2f} CNY"
        except ProviderError as exc:
            return ProbeResult(self.provider_id, False, exc.code, self.capabilities)
        except (OSError, TypeError, ValueError, KeyError):
            return ProbeResult(self.provider_id, False, "workflow_registry_invalid", self.capabilities)
        return ProbeResult(self.provider_id, True, capabilities=self.capabilities, details=details)

    def _blocked(self, code: str, detail: str, origin: str = "") -> PreflightResult:
        return PreflightResult(self.provider_id, "block", "", PRICING_MODE, origin,
                               signals=[{"severity": "block", "code": code, "detail": self.config.redact(detail)}])

    def _prepare(self, request: GenerationRequest, store: TaskStore) -> tuple[PreflightResult, dict]:
        try:
            origin, key, fingerprint = self._identity()
        except ProviderError as exc:
            return self._blocked(exc.code, str(exc)), {}
        try:
            workflow, spec = get_workflow_spec(request.workflow)
            task = request.task or ("r2v" if any(request.inputs.values()) else "t2v")
            if task == "i2v":
                raise ValueError("晨羽单图参考请使用 --task r2v")
            if request.capability != "video" or task not in spec["tasks"]:
                raise ValueError("晨羽仅支持 video 能力的 r2v 和 t2v")
            if request.model_id is not None or request.simulated_gpu is not None:
                raise ValueError("晨羽的模型与卡型由工作流指定")
            args = request.params
            unsupported = (args.keys() - _PARAM_NAMES) | (request.inputs.keys() - _MEDIA_SUFFIXES.keys())
            if unsupported:
                return self._blocked("unsupported_provider_params",
                                     redact_secrets(f"晨羽不支持参数或素材类型 {sorted(unsupported)!r}", key), origin), {}
            request_target(request, self.config)

            seconds = args.get("seconds", 5)
            if type(seconds) not in (int, float) or not spec["min_seconds"] <= seconds <= spec["max_seconds"] or int(seconds) != seconds:
                raise ValueError(f"晨羽 --seconds 必须为 {spec['min_seconds']}–{spec['max_seconds']} 的整数")
            seconds = int(seconds)
            megapixels = args.get("megapixels", spec["default_megapixels"])
            if type(megapixels) not in (int, float) or not spec["min_megapixels"] <= megapixels <= spec["max_megapixels"]:
                raise ValueError("晨羽 megapixels 必须为 0.2–2.0 的有限数值")
            aspect_input = args.get("aspect", spec["default_aspect"])
            aspect = next((name for name, option in spec["aspect_options"].items() if aspect_input in (name, option)), None)
            if aspect is None:
                raise ValueError("晨羽画幅只支持 " + ", ".join(spec["aspect_options"]))
            flags = {name: args.get(name, False) for name in ("upscale_2x", "save_last_frame", "no_music")}
            if any(type(value) is not bool for value in flags.values()):
                raise ValueError("晨羽 upscale_2x、save_last_frame 与 no_music 必须为布尔值")

            caps = workflow_media_caps(spec)
            media, file_hashes, hashes = self._references(request, caps)
            if task == "r2v" and not any(media.values()):
                raise ValueError("r2v 至少需要 1 项参考图或音频")
            if task == "t2v" and any(media.values()):
                raise ValueError("t2v 不接受参考素材，请使用 --task r2v")
            inspect_references(dict(media, seconds=seconds, videos=[]))
            prompt = apply_no_music_policy(request.prompt.strip(), flags["no_music"])
            if self.config.redact(prompt) != prompt:
                raise ValueError("晨羽提示词不得含凭据")
            errors, warnings = validate_h3_prompt(
                prompt, mode="t2va" if task == "t2v" else spec["prompt_mode"], seconds=seconds,
                n_images=len(media["images"]), n_audios=len(media["audios"]), n_videos=0, media_caps=caps,
            )
            if errors:
                return self._blocked("invalid_h3_prompt", redact_secrets("；".join(errors), key), origin), {}
            params = dict(task=task, prompt=prompt, seconds=seconds, aspect=aspect, megapixels=float(megapixels), **flags)
            expected_size = resolve_expected_size(spec, aspect, megapixels, flags["upscale_2x"])
            target = {name: spec[name] for name in ("workflow_id", "revision_id", "gpu_model")}
            target.update(workflow=workflow, credential_fingerprint=fingerprint)
            facts = {
                "compute_cny_per_minute": spec["compute_cny_per_minute"], "compute_wallet": "card_balance",
                "creator_fee_cny": spec["creator_fee_cny"], "creator_wallet": "balance",
                "expected_frames": h3.output_frames(params), "expected_size": list(expected_size) if expected_size else None,
                "workflow_spec_sha256": sha256_bytes(canonical_json_bytes(spec)),
            }
            facts["slots"] = caps
            request_facts = facts.copy()
            history = perf_history.estimate_from_history(
                store.config, provider=self.provider_id, workflow=workflow, output_seconds=seconds,
                has_ref_video=False, hardware=spec["gpu_model"],
            )
            estimate_note = ""
            if history:
                run_seconds = history["estimated_run_seconds"]
                wall_seconds = history["estimated_wall_seconds"]
                amount = history["estimated_quantity"]
                if run_seconds is not None:
                    amount = (run_seconds / 60.0) * spec["compute_cny_per_minute"] + spec["creator_fee_cny"]
                samples = history["samples"]
                facts.update(estimated_run_seconds=run_seconds, estimated_wall_seconds=wall_seconds,
                             estimated_cny=round(amount, 4) if amount is not None else None,
                             estimate_source="local_history", estimate_samples=samples)
                details = []
                if run_seconds is not None:
                    details.append(f"预计纯推理耗时 {run_seconds:.1f} 秒")
                if wall_seconds is not None:
                    details.append(f"近期端到端耗时中位数 {wall_seconds:.1f} 秒")
                estimate_note = f" 基于 {samples} 条本地实跑样本"
                estimate_note += ("，" + "，".join(details) if details else "") + "。"
            else:
                facts.update(estimated_run_seconds=None, estimated_wall_seconds=None, estimated_cny=None,
                             estimate_source="uncalibrated", estimate_samples=0)
            context = approval_context(
                request, store, config=self.config, provider=self.provider_id, origin=origin,
                fingerprint=fingerprint, workflow=workflow, params=params, hashes=hashes, facts=facts, target=target,
                request_facts=request_facts,
            )
        except (OSError, TypeError, ValueError, KeyError, AttributeError, ProviderError) as exc:
            return self._blocked("invalid_chenyu_request", redact_secrets(str(exc), key), origin), {}

        signals = [{
            "severity": "warn", "code": PRICING_MODE,
            "detail": (f"{spec['gpu_model']} 算力 {spec['compute_cny_per_minute']:.2f} CNY/分钟（算力卡），"
                       f"创作者费 {spec['creator_fee_cny']:.2f} CNY/次（现金余额），最终金额以结算为准。{estimate_note}"),
        }]
        signals.extend({"severity": "warn", "code": "h3_prompt_risk", "detail": self.config.redact(warning)}
                       for warning in warnings)
        return PreflightResult(
            self.provider_id, "warn", context["digest"], PRICING_MODE, origin,
            target_identity=target, input_hashes=hashes, signals=signals, facts={**facts, "task_key": context["task_key"],
                                   "replaced_submission_id": context["replaced_submission_id"]},
        ), {
            **context, "key": key,
            "spec": spec, "params": params, "media": media, "file_hashes": file_hashes,
        }

    @staticmethod
    def _references(request: GenerationRequest, caps: dict) -> tuple[dict, dict, dict]:
        media, file_hashes, hashes = {}, {}, {}
        for slot, suffixes in _MEDIA_SUFFIXES.items():
            supplied = request.inputs.get(slot, ())
            if not isinstance(supplied, (list, tuple)) or len(supplied) > caps[slot]:
                raise ValueError(f"{slot} 必须为列表，最多 {caps[slot]} 项")
            media[slot] = []
            for index, item in enumerate(supplied, 1):
                entry = item if isinstance(item, dict) else {"path": item}
                if set(entry) != {"path"}:
                    raise ValueError(f"{slot} 素材仅接受 path")
                path = ensure_real_path(entry["path"], must_exist=True, expect_dir=False)
                if path.suffix.lower() not in suffixes:
                    raise ValueError(f"{slot} 支持的文件后缀为 {', '.join(sorted(suffixes))}")
                if path not in file_hashes:
                    if not 0 < path.stat().st_size <= _MAX_INPUT_BYTES:
                        raise ValueError(f"{slot} 第 {index} 项不能为空或超过 100 MB")
                    file_hashes[path] = sha256_file(path)
                media[slot].append({"path": path})
                hashes[f"{slot}:{index}"] = file_hashes[path]
        return media, file_hashes, hashes

    def preflight(self, request: GenerationRequest, *, store: TaskStore | None = None) -> PreflightResult:
        return self._prepare(request, store or TaskStore(config=self.config))[0]

    def generate(self, request: GenerationRequest, *, store: TaskStore | None = None) -> ArtifactResult:
        store = store or TaskStore(config=self.config)
        preflight, ctx = self._prepare(request, store)
        approve_generation(request, preflight, store, ctx)
        task_key = ctx["task_key"]
        origin = preflight.endpoint_origin
        key = ctx["key"]
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
                ) as (_, new_submission):
                    with slots.lock_admission() if new_submission else nullcontext():
                        if new_submission and count_active_tasks(
                            store, provider=self.provider_id, endpoint_origin=origin, credential_fingerprint=fingerprint,
                        ) >= self._max_inflight:
                            raise ProviderError(
                                "晨羽在途与排队任务数已达上限（默认 60 = 10 并发 + 50 排队），请等待已有任务完成后重试",
                                code="queue_limit_reached", exit_code=3,
                            )
                        action, record = store.begin_external_submit(
                            task_key, provider=self.provider_id, strict_reuse=True, request_digest=ctx["request_digest"],
                            credential_fingerprint=fingerprint,
                            endpoint_origin=origin, logical_target=ctx["logical_target"], force_new=request.force_new,
                            replaced_submission_id=ctx["replaced_submission_id"],
                            extra={
                                "chenyu_idempotency_key": str(uuid.uuid4()), "wanted_output_path": str(ctx["wanted"]),
                                "workflow": preflight.target_identity["workflow"], "output_key": ctx["spec"]["output_key"],
                                "gpu_model": ctx["spec"]["gpu_model"],
                                "workflow_id": ctx["spec"]["workflow_id"], "revision_id": ctx["spec"]["revision_id"],
                                "tag": request.metadata.get("tag"), "approved_digest": preflight.digest,
                                "media_params": {name: value for name, value in ctx["params"].items() if name != "prompt"},
                                "expected_frames": preflight.facts["expected_frames"], "expected_size": preflight.facts["expected_size"],
                            },
                        )
                    if action == "delivered" and already_delivered(record):
                        return self._delivered(store, record, recovered=True)
                    if action == "submit":
                        record = self._submit(store, record, origin, ctx)
            except TaskConflictError as exc:
                raise ProviderConflictError(redact_secrets(str(exc), key), code=exc.code) from None
            return self._poll(store, record, origin, key, ctx["wanted"], recovered=action != "submit")

    def _upload(self, origin: str, key: str, path: Path, raw: bytes, media_type: str) -> str:
        reply = self._api(origin, key, _API + "/assets", upload=(path, raw, media_type), timeout=120)
        data = reply.get("data")
        uri = data.get("asset_uri") if isinstance(data, dict) else None
        if reply["code"] != 0 or not isinstance(uri, str) or not re.fullmatch(r"asset://[^\s]+", uri):
            raise ProviderError("晨羽素材上传未返回有效 asset_uri", code="upload_failed")
        decoded = urllib.parse.unquote(uri)
        if redact_secrets(decoded, key) != decoded:
            raise ProviderError("晨羽素材上传未返回有效 asset_uri", code="upload_failed")
        return uri

    def _submit(self, store: TaskStore, record: dict, origin: str, ctx: dict) -> dict:
        task_key, key = record["task_key"], ctx["key"]
        params, spec = ctx["params"], ctx["spec"]
        nodes = spec["nodes"]
        inputs = {
            nodes["combat"]: spec["combat"], nodes["upscale_2x"]: params["upscale_2x"],
            nodes["aspect_ratio"]: spec["aspect_options"][params["aspect"]], nodes["megapixels"]: params["megapixels"],
            nodes["seconds"]: params["seconds"], nodes["prompt"]: params["prompt"],
        }
        raw_by_path, uploaded = {}, {}
        try:
            for path, expected in ctx["file_hashes"].items():
                try:
                    with path.open("rb") as source:
                        raw = source.read(_MAX_INPUT_BYTES + 1)
                except OSError:
                    raw = b""
                if not 0 < len(raw) <= _MAX_INPUT_BYTES or sha256_bytes(raw) != expected:
                    raise ApprovalRequiredError(
                        "晨羽素材在预检审批后变更，请重新预检并使用 --force-new 或新输出路径",
                        code="input_changed_after_preflight",
                    )
                raw_by_path[path] = raw

            for slot, media_type in (("images", "image"), ("audios", "audio")):
                inputs.update(dict.fromkeys(nodes[slot]))
                for node, item in zip(nodes[slot], ctx["media"][slot]):
                    path = item["path"]
                    if path not in uploaded:
                        uploaded[path] = self._upload(origin, key, path, raw_by_path.pop(path), media_type)
                    inputs[node] = uploaded[path]
        except ProviderError as exc:
            raise

        payload = {
            "workflow_id": spec["workflow_id"], "revision_id": spec["revision_id"],
            "idempotency_key": record["chenyu_idempotency_key"], "accept_external_cost_risk": False, "inputs": inputs,
        }
        if self.config.redact(payload) != payload or store.config.redact(payload) != payload:
            store.mark_failed(task_key, error="晨羽请求包含不可保存的凭据")
            raise ProviderConflictError("晨羽请求包含不可保存的凭据", code="secret_in_request")
        # Persist the original idempotency key and payload before POST.
        record = store.mark_ambiguous(task_key, reason="晨羽建单请求待确认", extra={
            "submit_payload": payload, "submit_requested_at": datetime.now(timezone.utc).isoformat(),
        })
        return self._submit_order(store, record, origin, key)

    @staticmethod
    def _valid_run_id(value: Any, key: str) -> bool:
        return isinstance(value, str) and _RUN_ID.fullmatch(value) is not None and key not in value

    def _submit_order(self, store: TaskStore, record: dict, origin: str, key: str, *, replay: bool = False) -> dict:
        task_key = record["task_key"]
        payload = record["submit_payload"]
        deadline = time.monotonic() + self._queue_timeout
        delay = 2.0
        while True:
            try:
                reply = self._api(origin, key, _SUBMIT_PATH, payload=payload)
                data = reply.get("data")
                run_id = data.get("run_order_id") if isinstance(data, dict) else None
                if reply["code"] == 0 and self._valid_run_id(run_id, key):
                    return store.mark_external_submitted(task_key, run_id, extra={"accepted_at": time.time()})
                status = reply["code"] if not run_id else None
            except _TransportError as exc:
                status = exc.status
            except ProviderError as exc:
                raise ProviderConflictError(redact_secrets(str(exc), key), code=exc.code) from None

            remaining = deadline - time.monotonic()
            if status != 429 or remaining <= 0:
                break
            self._sleep(min(remaining, delay))
            if time.monotonic() >= deadline:
                break
            delay = min(delay * 2, 15)

        if status is not None and 400 <= status < 500 and status not in (408, 409, 425, 429, 499):
            # A rejection of a replay does not establish what happened to the original request.
            if not replay:
                store.mark_failed(task_key, error=f"晨羽明确拒单（{status}）")
            raise ProviderError(f"晨羽建单被拒绝（{status}）", code="submit_rejected")
        if status is not None and 300 <= status < 400:
            raise ProviderConflictError("晨羽认证请求拒绝重定向", code="redirect_blocked")
        raise ProviderConflictError("晨羽建单结果不明，已保留断点，可通过 jobs resume 恢复", code="ambiguous_submit")

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

    def cancel(self, task_key: str, *, store: TaskStore, **kwargs: Any) -> dict:
        raise ProviderError("晨羽工作流 API 暂不提供远端取消接口", code="unsupported_operation", exit_code=1)

    def _recover(
        self, store: TaskStore, task_key: str, output_path: Path | str | None, *,
        remote_task_id: str | None = None, single_poll: bool = False,
    ) -> ArtifactResult:
        origin, key, fingerprint = self._identity()
        if redact_secrets(task_key, key) != task_key:
            raise ProviderConflictError("晨羽任务键不得含凭据")
        with store.lock(_operation_lock(task_key)):
            record = store.get(task_key)
            if record is None or record.get("provider") != self.provider_id:
                raise ProviderConflictError("任务缺少晨羽断点")
            if (record.get("endpoint_origin"), record.get("credential_fingerprint")) != (origin, fingerprint):
                raise ProviderConflictError("晨羽凭据或源与旧断点不匹配", code="credential_or_origin_mismatch")
            try:
                wanted = validate_output_path(output_path or record["wanted_output_path"])
                if self.config.redact(str(wanted)) != str(wanted):
                    raise ValueError("晨羽输出路径不得含凭据")
            except (ValueError, OSError) as exc:
                raise ProviderConflictError(redact_secrets(str(exc), key)) from None
            record = store.reconcile_delivery(task_key) or record
            if remote_task_id is not None and remote_task_id != record.get("remote_task_id"):
                if record["state"] not in ("submitting", "ambiguous") or not self._valid_run_id(remote_task_id, key):
                    raise ProviderConflictError("仅提交结果不明的晨羽断点可以认领有效的 run_order_id")
                record = store.mark_external_submitted(task_key, remote_task_id)
            elif record["state"] == "ambiguous" and not single_poll:
                payload = record.get("submit_payload")
                idem = record.get("chenyu_idempotency_key")
                if (isinstance(payload, dict) and isinstance(idem, str) and idem
                        and payload.get("idempotency_key") == idem
                        and self.config.redact(payload) == payload):
                    record = self._submit_order(store, record, origin, key, replay=True)
            if already_delivered(record):
                if output_path and not already_delivered(record, wanted):
                    record = deliver_copy(store, record, wanted)
                return self._delivered(store, record, recovered=True)
            if record["state"] not in ("submitted", "delivered") or not self._valid_run_id(record.get("remote_task_id"), key):
                raise ProviderConflictError(
                    f"晨羽断点当前为 {record['state']}，无法续查。请用 --remote-job-id 认领或确认后 --force-new",
                    code=record["state"],
                )
        return self._poll(store, record, origin, key, wanted, recovered=True, single_poll=single_poll, override=output_path is not None)

    @staticmethod
    def _current(store: TaskStore, record: dict) -> dict:
        current = store.get(record["task_key"])
        if not current or (submission_identity(current), current.get("remote_task_id")) != (
            submission_identity(record), record.get("remote_task_id"),
        ):
            raise ProviderConflictError("晨羽断点已被其他提交替换")
        if current["state"] not in ("submitted", "delivered"):
            raise ProviderConflictError(f"晨羽断点当前为 {current['state']}", code=current["state"])
        return current

    def _poll(
        self, store: TaskStore, record: dict, origin: str, key: str, wanted: Path, *,
        recovered: bool, single_poll: bool = False, override: bool = False,
    ) -> ArtifactResult:
        task_key = record["task_key"]
        query = urllib.parse.urlencode({"run_order_id": record["remote_task_id"]})
        path = _API + "/workflow/run/info?" + query
        deadline = time.monotonic() + self._timeout
        completed = {}
        completed_at = None

        while True:
            with store.lock(_operation_lock(task_key)):
                current = self._current(store, record)
                if already_delivered(current):
                    if override and not already_delivered(current, wanted):
                        current = deliver_copy(store, current, wanted)
                    return self._delivered(store, current, recovered=True)
            remaining = deadline - time.monotonic()
            if completed and remaining <= 0:
                break
            try:
                reply = self._api(origin, key, path, timeout=min(60, max(0.001, remaining)))
            except _TransportError as exc:
                if exc.status in (404, 410):
                    self._terminal(store, record, "expired_or_missing", exc.status)
                if single_poll or (exc.status is not None and exc.status not in TRANSIENT_HTTP_CODES):
                    raise
                if not completed and time.monotonic() >= deadline:
                    raise
            else:
                if reply["code"] != 0:
                    message = str(reply.get("message") or reply.get("msg") or "").strip().lower()
                    if reply["code"] in (404, 410) or message in _MISSING_MESSAGES:
                        self._terminal(store, record, "expired_or_missing")
                    raise ProviderError("晨羽任务查询被拒绝", code="poll_rejected", exit_code=3)
                data = reply.get("data")
                state = data.get("run_status") if isinstance(data, dict) else None
                if state in ("failed", "cancelled", "canceled", "expired_or_missing", "not_found"):
                    self._terminal(store, record, state, data=data)
                if state == "succeeded":
                    if not completed:
                        completed_at = time.time()
                        deadline = time.monotonic() + self._settlement_wait
                    completed.update(data)
                    if single_poll or data.get("settlement_status") == "settled":
                        break
                elif not isinstance(state, str) or state not in _ACTIVE_STATES:
                    raise ProviderError("晨羽任务查询返回未知状态", code="poll_rejected", exit_code=3)

            remaining = deadline - time.monotonic()
            if remaining <= 0 and completed:
                break
            if single_poll or remaining <= 0:
                raise ProviderConflictError("晨羽任务尚未完成，可用 jobs resume 续查", code="task_not_completed")
            self._sleep(min(remaining, self._poll_interval))

        return self._deliver(store, record, completed, wanted, key=key, recovered=recovered,
                             completed_at=completed_at, override=override)

    @staticmethod
    def _run_timing(data):
        for field in ("run_seconds", "run_duration_seconds"):
            if (seconds := _number(data.get(field))) is not None:
                return {"platform_run_s": seconds, "platform_run_s_source": f"chenyu_{field}"}
        return {}

    def _terminal(self, store: TaskStore, record: dict, state: str, status: int | None = None, *, data=None) -> None:
        task_key = record["task_key"]
        with store.lock(_operation_lock(task_key)):
            if self._current(store, record)["state"] == "submitted":
                if state in ("expired_or_missing", "not_found"):
                    current = store.mark_expired_or_missing(task_key, reason="晨羽订单不存在或已过期", http_status=status)
                else:
                    extra = {"completed_at": time.time(), **self._run_timing(data or {})}
                    if data is not None:
                        extra["billing"] = self._billing(record, data)
                    if state == "failed":
                        current = store.mark_failed(task_key, error="晨羽远端生成失败", extra=extra)
                    else:
                        current = store.mark_cancelled(task_key, reason="晨羽远端任务已取消", extra=extra)
                self._record_charge(store, current)
        if state in ("expired_or_missing", "not_found"):
            raise ProviderConflictError("晨羽订单不存在或已过期，断点已保留", code="expired_or_missing")
        raise ProviderError("晨羽远端任务失败或已取消", code="remote_task_failed")

    @staticmethod
    def _video_url(data: dict, fallback: str) -> Any:
        outputs = data.get("outputs")
        if not isinstance(outputs, dict):
            raise ArtifactContractError("晨羽订单缺少成品输出")
        manifest = data.get("candidate_output_manifest")
        names = [
            item["name"] for item in manifest if isinstance(item, dict)
            and item.get("type") == "video" and isinstance(item.get("name"), str)
        ] if isinstance(manifest, list) else []
        for name in (*names, fallback):
            urls = outputs.get(name)
            if isinstance(urls, list) and urls:
                return urls[0]
        raise ArtifactContractError("晨羽订单未包含视频输出")

    def _download(self, url: Any, dest: Path, *, key: str) -> None:
        request = urllib.request.Request(_download_url(url, key), method="GET")
        opener = self._download_opener or urllib.request.build_opener(_DownloadRedirectHandler(key))
        try:
            with opener.open(request, timeout=120) as response, dest.open("wb") as stream:
                status = response.getcode()
                if status is not None and not 200 <= status < 300:
                    raise ProviderError(f"晨羽成品下载失败（HTTP {status}）", code="download_failed")
                header = response.headers.get("Content-Length")
                expected = parse_content_length(header)
                if header is not None and expected is None:
                    raise ArtifactContractError("晨羽成品 Content-Length 无效")
                count = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
                    count += len(chunk)
                if not count or (expected is not None and count != expected):
                    raise ArtifactContractError("晨羽成品为空或 Content-Length 不匹配")
                stream.flush()
                os.fsync(stream.fileno())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProviderError(f"晨羽成品下载失败（HTTP {status}）", code="download_failed") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ArtifactContractError("晨羽成品下载不完整或无法读取") from None

    @staticmethod
    def _billing(record: dict, data: dict) -> dict:
        fields = {
            name: _number(data.get(name)) for name in ("final_amount", "platform_final_amount", "creator_final_amount", "platform_card_amount")
        }
        return {
            **fields, "pricing_mode": PRICING_MODE, "unit": "CNY", "task_id": record["remote_task_id"],
            "credential_fingerprint": record["credential_fingerprint"], "submitted_at": record.get("submit_requested_at"),
            "estimated": data.get("settlement_status") != "settled" or fields["final_amount"] is None,
        }

    def _deliver(self, store, record, data, wanted, *, key, recovered, completed_at, override):
        task_key = record["task_key"]
        with store.lock(_operation_lock(task_key)):
            record = self._current(store, record)
            record = remember_external_billing(store, record, self._billing(record, data))
            self._record_charge(store, record)
        wanted.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".chenyu-", dir=wanted.parent) as staging:
            tmp = Path(staging) / "clip.mp4"
            self._download(self._video_url(data, record["output_key"]), tmp, key=key)
            params, size = record["media_params"], record["expected_size"]
            contract = verify_chenyu_video(
                video_metadata(tmp), workflow=record["workflow"], expected_frames=record["expected_frames"],
                expected_size=tuple(size) if size else None, aspect=params["aspect"], upscale_2x=params["upscale_2x"],
            )
            with store.lock(_operation_lock(task_key)):
                current = self._current(store, record)
                current = store.reconcile_delivery(task_key) or current
                if already_delivered(current):
                    if override and not already_delivered(current, wanted):
                        current = deliver_copy(store, current, wanted)
                    return self._delivered(store, current, recovered=True)
                extra = {"contract": contract, "billing": current.get("billing") or self._billing(record, data),
                         "completed_at": completed_at, **self._run_timing(data),
                         "last_frame_reserved_path": None, "last_frame_sha256": None}
                result = publish_delivery(store, current, tmp, wanted, extra)
                return self._delivered(store, result, recovered=recovered)

    @staticmethod
    def _record_charge(store, record):
        billing = record.get("billing") or {}
        record_job_charge(store.config, record, billing.get("final_amount"), "CNY",
                          estimated=billing.get("estimated", False))

    def _delivered(self, store, record, *, recovered):
        self._record_charge(store, record)
        contract = dict(record["contract"])
        if record["media_params"]["save_last_frame"]:
            contract["last_frame_path"] = last_frame(store, record, contract)
        billing = record.get("billing") or {}
        with suppress(OSError, ValueError):
            perf_history.record_observation(
                store.config, idempotency_key=f"{self.provider_id}:{record['remote_task_id']}",
                provider=self.provider_id, workflow=record["workflow"], hardware=record.get("gpu_model"),
                width=contract.get("width"), height=contract.get("height"), output_seconds=record["media_params"]["seconds"],
                run_seconds=record.get("platform_run_s") or None,
                wall_seconds=perf_history.elapsed_seconds(record.get("completed_at"), record.get("submit_requested_at"),
                                                          record.get("accepted_at")),
                billed_quantity=billing.get("final_amount"), unit="CNY", estimated_billing=billing.get("estimated", True),
            )
        return ArtifactResult(self.provider_id, record["task_key"], "delivered", artifact_path=record["artifact_path"],
                              artifact_sha256=record["artifact_sha256"], remote_id=record["remote_task_id"],
                              recovered=recovered, contract=contract, billing=billing)
