"""Managed Chenyu exclusive instances and the authenticated ComfyUI data plane."""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import shlex
import socket
import statistics
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path

from .config import EngineConfig, credential_fingerprint, load_config
from .fsutil import atomic_write_bytes, atomic_write_json, ensure_real_subpath, file_lock, fsync_dir, sha256_bytes
from .ledger import parse_chenyu_balance, record_chenyu_gpu_stop
from .providers._http import NoRedirectHandler, allowed_hosts, normalize_api_base, validate_api_key
from .providers.base import (
    ApprovalRequiredError, ArtifactContractError, PreflightBlockedError, ProviderError,
    canonical_json_bytes, normalize_status, parse_content_length,
)
from .providers.comfy_client import DaemonHTTPError, ManagedComfyClient
from .remote_bundle import pack_remote_tar, remote_source_version
from .tasks_store import TaskStore, delivery_confirmed

DEFAULT_API_BASE = "https://www.chenyu.cn"
API_KEY_ENV = "CHENYU_API_KEY"
DEFAULT_BASE_IMAGE_UUID = "b8b68e2e7c6b46d7b9c7eea3835ef494"
CHENYU_REMOTE_ROOT = "/root/yc_h3_engine"
CHENYU_MODELS_DIR = "/usrdata/yc_h3_engine/models"
GIB = 1024**3
FREE_IMAGE_LIMIT_GB = 50
FREE_USRDATA_LIMIT_GB = 50
STORAGE_OVERAGE_CNY_PER_GB_HOUR = 0.00036
IDLE_MINUTE_CHOICES = (0, 10, 30, 60, 120)
GPU_PRESETS = {
    "6000D-84G": {"gpu_uuid": "6e7f8a9d2b4c01573890acebdf2467519", "platform_name": "6000D-84G", "hourly_cny": 5.68},
    "nGPU-48GB": {"gpu_uuid": "d92a57488465f63af5eaab49b403952f", "platform_name": "nGPU-48GB", "hourly_cny": 2.98},
    "5090": {"gpu_uuid": "a7f3d92e4c5b4a7e9b8c6f1e2d3a4b5c", "platform_name": "RTX 5090", "hourly_cny": 3.13},
    "nGPU-32GB": {"gpu_uuid": "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9", "platform_name": "nGPU-32GB", "hourly_cny": 2.19},
    "4090D": {"gpu_uuid": "32ceabe6c96e44bdb1e4d80358a3b8ae", "platform_name": "RTX 4090 D", "hourly_cny": 2.38},
}
DEFAULT_GPU_FALLBACK = ("6000D-84G", "nGPU-48GB", "5090")
DEFAULT_SETUP_PLUGINS = ("ComfyUI-KJNodes", "comfyui-SelfLift")
STOPPED_STATUSES = (22, 23)
INACTIVE_STATUSES = (4, *STOPPED_STATUSES)
_ALLOCATION_PROPAGATION_SECONDS = 60
_API = "/api/open/v2"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_VISIBLE_ASCII = re.compile(r"[!-~]{32,}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WORKFLOW_NAMES = ["jinyu-combat", "jinyu-hybrid"]
_SESSION_NAME = re.compile(r"yh3-[A-Za-z0-9_-]{1,36}")


def _normalize_session_name(value):
    name = str(value).strip()
    if not name.startswith("yh3-"):
        name = "yh3-" + name
    if not _SESSION_NAME.fullmatch(name):
        raise ValueError("晨羽受管会话须使用 yh3- 前缀，名称只接受字母、数字、下划线和连字符")
    return name


def _setup_parameters(params):
    values = {"plugins": list(DEFAULT_SETUP_PLUGINS), **(params or {})}
    if values.keys() - {"component", "profile", "revision", "plugins"}:
        raise ValueError("晨羽 setup 包含未知参数")
    plugins = values["plugins"]
    if isinstance(plugins, str):
        plugins = plugins.split(",")
    if not isinstance(plugins, (tuple, list)) or any(not isinstance(item, str) for item in plugins):
        raise ValueError("晨羽 setup 的 plugins 须为字符串列表")
    values["plugins"] = sorted({item.strip() for item in plugins if item.strip()})
    for field in ("component", "profile", "revision"):
        if field in values and (not isinstance(values[field], str) or not values[field].strip()):
            raise ValueError("晨羽 setup 参数须为非空字符串")
    return values


def _error(message, code):
    return ProviderError(message, code=code, exit_code=3)


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def validate_instance_endpoint(url: str, instance_uuid: str, *, config: EngineConfig | None = None) -> str:
    try:
        if not _valid_id(instance_uuid) or not isinstance(url, str) or any(ch.isspace() for ch in url):
            raise ValueError()
        origin, host = normalize_api_base(url)
        if (config or load_config()).redact(origin) != origin:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("晨羽实例端点必须是纯 HTTPS 源") from None
    label = host.split(".")[0]
    instance_label = instance_uuid.lower()
    routed_label = re.fullmatch(re.escape(instance_label) + r"(?:[0-9]+|-[a-z0-9-]+)?", label)
    if host not in allowed_hosts((), config=config) and not (host.endswith(".chenyu.cn") and routed_label):
        raise ValueError("晨羽实例端点主机与受管实例不匹配")
    return origin


def _candidates(gpu):
    raw = str(gpu or "AUTO").strip()
    if raw.upper() == "AUTO":
        return DEFAULT_GPU_FALLBACK
    aliases = {name.lower(): name for name in GPU_PRESETS}
    aliases.update({"rtx-5090": "5090", "rtx 5090": "5090", "rtx 4090 d": "4090D"})
    try:
        return tuple(dict.fromkeys(aliases[item.strip().lower()] for item in raw.split(",")))
    except KeyError:
        raise ValueError("不支持该晨羽卡型，请使用 AUTO 或预设卡型") from None


def _signed_number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    parsed = _signed_number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _status(row):
    value = row.get("status")
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value):
        return int(value)
    raise _error("晨羽实例响应缺少有效状态", "invalid_platform_response")


def _rows(data, *keys):
    if isinstance(data, dict):
        matched = next((k for k in (*keys, "list", "items", "records", "data") if k in data), None)
        if matched is None:
            raise _error("晨羽平台返回无效列表", "invalid_platform_response")
        rows = data[matched]
        total = data.get("total")
        if rows is None and type(total) is int and total == 0:
            return []
        data = rows
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise _error("晨羽平台返回无效列表", "invalid_platform_response")
    return data


def _timestamp(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class ChenyuGpuState:
    """Single private state file. Writers hold the sibling lock across API mutations."""

    def __init__(self, path=None, *, config: EngineConfig | None = None):
        self.config = config or load_config()
        self.path = Path(path) if path is not None else self.config.data_dir / "chenyu_gpu.json"

    def _token_path(self, instance_uuid):
        return self.path.with_name(f".{self.path.name}.{instance_uuid}.token")

    @staticmethod
    def _read_private(path):
        if path.is_symlink():
            raise _error("晨羽受管状态文件不能是符号链接", "invalid_gpu_state")
        with path.open("rb") as stream:
            if os.name != "nt" and os.fstat(stream.fileno()).st_mode & 0o077:
                raise _error("晨羽受管状态权限过宽，请核对并恢复为 0600", "unsafe_state_permissions")
            return stream.read()

    def read(self):
        try:
            value = json.loads(self._read_private(self.path))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise _error("晨羽受管状态无法读取", "invalid_gpu_state") from None
        if not isinstance(value, dict):
            raise _error("晨羽受管状态无效", "invalid_gpu_state")
        instance_uuid = value.get("instance_uuid")
        if _valid_id(instance_uuid) and "runtime_auth_token" not in value:
            try:
                token = self._read_private(self._token_path(instance_uuid)).decode("ascii")
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                raise _error("晨羽远端认证状态无法读取", "invalid_gpu_state") from None
            else:
                if not _VISIBLE_ASCII.fullmatch(token):
                    raise _error("晨羽远端认证状态无效", "invalid_gpu_state")
                value["runtime_auth_token"] = token
        return value

    def lock(self):
        return file_lock(
            self.path.with_name(f".{self.path.name}.lock"), timeout=3,
            timeout_message="晨羽 GPU 生命周期操作正在执行",
        )

    def write(self, record):
        previous = self.read()
        if record:
            data = dict(record)
            token = data.pop("runtime_auth_token", None)
            instance_uuid = data.get("instance_uuid")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if token:
                if not _valid_id(instance_uuid) or not _VISIBLE_ASCII.fullmatch(token):
                    raise _error("晨羽远端认证状态无效", "invalid_gpu_state")
                atomic_write_bytes(self._token_path(instance_uuid), token.encode("ascii"))
            atomic_write_json(self.path, data, allow_nan=False)
        else:
            token = None
            self.path.unlink(missing_ok=True)
            if self.path.parent.is_dir():
                fsync_dir(self.path.parent)
        previous_id = previous.get("instance_uuid")
        if _valid_id(previous_id) and (not token or previous_id != record.get("instance_uuid")):
            self._token_path(previous_id).unlink(missing_ok=True)


class _PlatformRejected(ProviderError):
    """The platform explicitly rejected an operation before allocation."""


class _Platform:
    def __init__(self, origin, key, opener, config):
        self.origin = origin
        self.key = key
        self.fingerprint = credential_fingerprint(key)
        self.config = config
        self.opener = opener or urllib.request.build_opener(NoRedirectHandler())

    def request(self, route, *, payload=None, query=None):
        path = _API + route
        if query:
            path += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self.key}"}
        data = None
        if payload is not None:
            data = canonical_json_bytes(payload)
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                status = response.getcode()
                reply = json.loads(response.read()) if 200 <= status < 300 else None
        except urllib.error.HTTPError as error:
            status, reply = error.code, None
            error.close()
        except (OSError, http.client.HTTPException, ValueError):
            raise _error("晨羽平台请求或响应中断", "platform_transport_error") from None

        if status in (400, 401, 403, 422):
            raise _PlatformRejected(f"晨羽平台拒绝请求（HTTP {status}）", code="platform_rejected", exit_code=3)
        if not isinstance(reply, dict) or type(reply.get("code")) is not int:
            raise ProviderError("晨羽平台返回无效响应", code="invalid_platform_response", exit_code=3,
                                details={"http_status": status})
        if reply["code"] != 0:
            message = self.config.redact(str(reply.get("msg") or reply.get("message") or ""), self.key)[:250]
            # Ambiguous, transient, or empty error envelopes are never clear platform rejections.
            ambiguous = re.search(
                r"超时|结果不明|待确认|处理中|繁忙|拥挤|timeout|unknown|processing|busy|internal|服务器错误|服务器内部|系统错误",
                message,
                re.I,
            )
            if not message.strip() or ambiguous:
                raise _error(f"晨羽平台返回结果不明（code={reply['code']}）{message}", "platform_transport_error")
            raise _PlatformRejected(
                f"晨羽平台拒绝请求（code={reply['code']}）{message}",
                code="platform_rejected", exit_code=3,
            )
        return reply.get("data", reply.get("result"))

    def instances(self):
        data = self.request("/instance/list", query={"page": 1, "page_size": 100})
        rows = _rows(data, "instances", "instance_list")
        total = _number(data.get("total")) if isinstance(data, dict) else None
        if len(rows) >= 100 or (total is not None and total != len(rows)):
            raise _error("晨羽实例列表达到单页核对上限", "instance_list_incomplete")
        for row in rows:
            if not _valid_id(row.get("instance_uuid")):
                raise _error("晨羽实例列表缺少有效实例 ID", "invalid_platform_response")
            _status(row)
        return rows

    def instance(self, instance_uuid):
        row = self.request("/instance/info", query={"instance_uuid": instance_uuid})
        if not isinstance(row, dict) or row.get("instance_uuid", instance_uuid) != instance_uuid:
            raise _error("晨羽实例详情与请求不一致", "invalid_platform_response")
        _status(row)
        return row

    def balance(self):
        data = self.request("/balance/info")
        if not isinstance(data, dict):
            raise _error("晨羽双钱包响应无效", "invalid_platform_response")
        try:
            balances = parse_chenyu_balance(data)
        except ValueError:
            raise _error("晨羽双钱包响应缺少余额", "invalid_platform_response") from None
        return {"balance": balances["cash_balance"], "card_balance": balances["card_balance"]}


def _name(row):
    return row.get("instance_name") or row.get("name") or row.get("title") or ""


def _image_id(row):
    return row.get("image_uuid") or row.get("uuid")


def _image_size_bytes(row):
    for field, multiplier in (("size", 1), ("image_size", 1), ("size_gb", GIB)):
        size = _number(row.get(field))
        if size is not None and math.isfinite(size * multiplier):
            return int(size * multiplier)
    return 0


def _usrdata_models_gib(health):
    size = health.get("models_dir_bytes")
    if type(size) is int and size >= 0:
        return round(size / GIB, 2)
    return None


def _jupyter_origin(row, instance_uuid, *, config):
    servers = row.get("server_map")
    value = None
    if isinstance(servers, dict):
        value = next((v for k, v in servers.items() if k.lower() == "jupyter"), None)
    elif isinstance(servers, list):
        for item in servers:
            if isinstance(item, dict) and str(item.get("title") or item.get("name") or "").lower() == "jupyter":
                value = item.get("url") or item.get("address")
                break
    if isinstance(value, dict):
        value = value.get("url") or value.get("address")
    if not value:
        raw_url = row.get("server_url")
        if isinstance(raw_url, str):
            value = raw_url
        elif isinstance(raw_url, list) and raw_url and isinstance(raw_url[0], str):
            value = raw_url[0]
        elif isinstance(raw_url, dict):
            value = next((v for k, v in raw_url.items() if k.lower() == "jupyter"), None)
    if isinstance(value, str):
        try:
            parsed = urllib.parse.urlsplit(value)
            query_keys = set(urllib.parse.parse_qs(parsed.query, keep_blank_values=True).keys())
            if parsed.path in ("", "/", "/lab", "/lab/", "/tree") and not parsed.fragment and query_keys <= {"token"}:
                return validate_instance_endpoint(
                    urllib.parse.urlunsplit(parsed._replace(path="", query="", fragment="")),
                    instance_uuid, config=config,
                )
        except ValueError:
            pass
    raise _error("晨羽实例缺少受信的 Jupyter 端点", "missing_jupyter_endpoint")


class ChenyuGpuManager:
    def __init__(
        self, *, api_base=None, api_opener=None, remote_opener=None, bootstrap_fn=None,
        state_path=None, config: EngineConfig | None = None, sleep_fn=time.sleep,
    ):
        self.config = config or load_config()
        self.api_base = api_base or self.config.get("CHENYU_API_BASE", DEFAULT_API_BASE)
        self.api_opener = api_opener
        self.remote_opener = remote_opener
        self.bootstrap_fn = bootstrap_fn or _bootstrap_remote
        self.state = ChenyuGpuState(state_path, config=self.config)
        self.sleep = sleep_fn

    def _platform(self):
        key, error = validate_api_key(self.config.get(API_KEY_ENV))
        if error or key is None:
            raise _error("CHENYU_API_KEY 缺失或无效", error or "invalid_api_key")
        try:
            if any(ch.isspace() for ch in self.api_base):
                raise ValueError()
            origin, host = normalize_api_base(self.api_base)
            if self.config.redact(origin, key) != origin:
                raise ValueError()
        except (TypeError, ValueError):
            raise _error("晨羽 API 地址必须是纯 HTTPS 源", "invalid_api_base") from None
        if host not in allowed_hosts(("www.chenyu.cn",), config=self.config):
            raise _error("晨羽 API 主机未获信任", "untrusted_api_host")
        return _Platform(origin, key, self.api_opener, self.config)

    def _owned(self, api, session=None):
        record = self.state.read()
        if not record:
            return record
        if record.get("endpoint_origin") != api.origin or record.get("credential_fingerprint") != api.fingerprint:
            raise _error("当前晨羽账号或 API 源与本地受管实例不符", "gpu_session_owner_mismatch")
        if session and _normalize_session_name(session) != record.get("session_name"):
            raise _error("指定的晨羽会话与本地受管记录不符", "gpu_session_mismatch")
        return record

    def gpu_plan(
        self, *, session=None, gpu="AUTO", image_uuid=None, pod_uuid=None, pod_tag=None,
        setup=False, setup_params=None, max_runtime_minutes=None, idle_minutes=None,
    ):
        """Read-only planning. An omitted limit on reconnect preserves the approved limit."""
        api = self._platform()
        record = self.state.read()
        name = _normalize_session_name(session if session is not None else record.get("session_name", "h3"))
        cards = _candidates(gpu)
        if image_uuid is not None and pod_uuid is not None:
            raise ValueError("image_uuid 与 pod_uuid 互斥")
        if pod_tag is not None and (not pod_uuid or not isinstance(pod_tag, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", pod_tag)):
            raise ValueError("pod_tag 须与 pod_uuid 同用并采用合法标识")
        source = pod_uuid if pod_uuid is not None else (image_uuid if image_uuid is not None else DEFAULT_BASE_IMAGE_UUID)
        if not _valid_id(source):
            raise ValueError("晨羽镜像或 Pod 标识无效")
        same = record.get("session_name") == name and record.get("status") not in INACTIVE_STATUSES
        runtime = max_runtime_minutes if max_runtime_minutes is not None else (record.get("max_runtime_minutes", 120) if same else 120)
        idle = idle_minutes if idle_minutes is not None else (record.get("idle_minutes", 30) if same else 30)
        if type(runtime) is not int or not 1 <= runtime <= 1440:
            raise ValueError("max_runtime_minutes 须介于 1 和 1440 之间")
        if type(idle) is not int or idle not in IDLE_MINUTE_CHOICES:
            raise ValueError("idle_minutes 仅允许 0、10、30、60、120")
        if type(setup) is not bool or (setup_params is not None and not isinstance(setup_params, dict)):
            raise ValueError("晨羽 setup 参数格式无效")
        setup_params = _setup_parameters(setup_params)

        balance = api.balance()
        catalog = _rows(api.request("/gpu/list"), "gpu_list", "gpus")
        listed = {row.get("gpu_uuid") or row.get("uuid"): row for row in catalog}
        all_instances = api.instances()
        active = [row for row in all_instances if _SESSION_NAME.fullmatch(_name(row)) and _status(row) not in INACTIVE_STATUSES]
        other = [row for row in active if _name(row) != name]
        matches = [row for row in active if _name(row) == name]
        existing = matches[0]["instance_uuid"] if len(matches) == 1 else None
        owned_uuid = record.get("instance_uuid")
        owned_row = next((row for row in all_instances if row["instance_uuid"] == owned_uuid), None) if owned_uuid else None
        if not matches and owned_row is not None and _status(owned_row) not in INACTIVE_STATUSES:
            if record.get("session_name") == name:
                existing = owned_uuid
                matches = [owned_row]
            else:
                other.append(owned_row)
        unreused_record = bool(owned_uuid and existing != owned_uuid)
        owned_status = _status(owned_row) if (unreused_record and owned_row is not None) else None
        if unreused_record and owned_row is not None and not matches and owned_status not in INACTIVE_STATUSES:
            if owned_row not in other:
                other.append(owned_row)
        pricing = {
            "unit": "CNY/hr", "candidates": {},
            "source_service_fee_cny_per_hour": 0 if pod_uuid is None and source == DEFAULT_BASE_IMAGE_UUID else None,
        }
        for card in cards:
            preset = GPU_PRESETS[card]
            live = listed.get(preset["gpu_uuid"], {})
            raw_price = live.get("price")
            hourly = raw_price.get("hour") if isinstance(raw_price, dict) else raw_price
            rate = _number(live.get("hourly_cny", live.get("price_per_hour", hourly)))
            pricing["candidates"][card] = {
                **preset, "hourly_cny": preset["hourly_cny"] if rate is None else rate,
                "rate_source": "reference" if rate is None else "api", "listed": bool(live),
                "available": live.get("status") == 1 if "status" in live else None,
                "stock": _number(live.get("stock", live.get("available_count"))),
            }
        deadline_changed = bool(existing and (
            runtime != record.get("max_runtime_minutes") or idle != record.get("idle_minutes")
        ))
        if existing and record.get("hourly_cny") is not None:
            pricing["active_hourly_cny"] = record["hourly_cny"]
        action = "allocate_and_setup" if setup else "allocate"
        status, code = "warn", "approval_required"
        notice = None
        if record and (record.get("credential_fingerprint") != api.fingerprint or record.get("endpoint_origin") != api.origin):
            status, code = "block", "gpu_session_owner_mismatch"
        elif owned_status in STOPPED_STATUSES:
            status, code = "block", "stopped_instance_requires_cleanup"
            if record.get("image_pending"):
                notice = ("晨羽镜像保存结果待核对，请先执行 gpu reconcile --backend chenyu，"
                          "再执行 gpu stop --backend chenyu 销毁并核销")
            else:
                notice = "晨羽上一个实例已关机，请先执行 gpu stop --backend chenyu 销毁并核销"
        elif unreused_record and owned_status in (None, 4):
            status, code = "block", "needs_reconcile"
            notice = "晨羽上一个实例已释放，请执行 gpu stop --backend chenyu 核销，或执行 gpu reconcile --backend chenyu 清理残留记录"
        elif (record.get("allocation_pending") or record.get("stop_pending") or record.get("image_pending")
              or existing and not record.get("guards_ready")):
            status, code = "block", "needs_reconcile"
        elif other:
            status, code = "block", "active_managed_session_exists"
        elif len(matches) > 1 or (existing and existing != record.get("instance_uuid")):
            status, code = "block", "needs_reconcile"
        elif record and not existing and record.get("status") not in INACTIVE_STATUSES:
            status, code = "block", "needs_reconcile"
        elif existing and time.time() >= record.get("deadline_at", 0):
            status, code = "block", "gpu_deadline_expired"
            notice = "晨羽已到硬截止，请执行 yc-h3 gpu reconcile --backend chenyu"
        elif existing:
            action = "setup" if setup else "deadline_changed" if deadline_changed else "reconnect"
            if _status(matches[0]) != 2 and not deadline_changed:
                status, code = "block", "instance_not_running"
            elif action == "reconnect":
                status, code = "pass", "ready"

        pricing_facts = {
            **pricing,
            "candidates": {
                card: {k: v for k, v in info.items() if k not in ("available", "stock")}
                for card, info in pricing["candidates"].items()
            },
        }
        facts = {
            "backend": "chenyu", "endpoint_origin": api.origin, "credential_fingerprint": api.fingerprint,
            "session_name": name, "candidates": list(cards), "source_kind": "pod" if pod_uuid else "image",
            "source_id": source, "pod_tag": pod_tag, "setup": setup, "setup_params": setup_params,
            "max_runtime_minutes": runtime, "idle_minutes": idle, "action": action,
            "existing_instance_uuid": existing, "pricing": pricing_facts,
            "data_dir_hash": self.config.data_dir_hash,
            "existing_deadline_at": record.get("deadline_at") if existing else None,
        }
        public = self.config.redact({**facts, "pricing": pricing}, api.key, record.get("runtime_auth_token"))
        return {
            **public, **balance, "status": status, "code": code, "requires_approval": status == "warn",
            "deadline_changed": deadline_changed, "digest": sha256_bytes(canonical_json_bytes(facts)),
            **({"notice": notice} if notice else {}),
        }

    @staticmethod
    def _approve(plan, digest):
        if plan["status"] == "block":
            raise PreflightBlockedError(plan.get("notice", "晨羽 GPU 预检阻断"), code=plan["code"], details={"preflight": plan})
        if (plan["requires_approval"] or digest is not None) and digest != plan["digest"]:
            raise ApprovalRequiredError("须核对晨羽 GPU 计划并提供匹配的审批摘要", details={"preflight": plan})

    def _wait(self, query, predicate, *, attempts, code):
        for attempt in range(attempts):
            value = query()
            if predicate(value):
                return value
            if attempt + 1 < attempts:
                self.sleep(2)
        raise _error("晨羽操作在等待窗口内未确认，请先核对受管状态", code)

    @staticmethod
    def _instance_status(api, instance_uuid):
        row = next((row for row in api.instances() if row["instance_uuid"] == instance_uuid), None)
        return _status(row) if row is not None else 4

    def _allocation_match(self, record, rows):
        known = set(record.get("known_instance_ids", []))
        expected_gpu = GPU_PRESETS[record["gpu"]]["gpu_uuid"]
        source_key = f'{record["source_kind"]}_uuid'
        matches = []
        for row in rows:
            created = _signed_number(row.get("create_time"))
            if (row["instance_uuid"] not in known
                    and (not _SESSION_NAME.fullmatch(_name(row)) or _name(row) == record["session_name"])
                    and row.get("gpu_uuid") == expected_gpu
                    and row.get(source_key) == record["source_id"]
                    and created is not None and int(record["allocated_at"]) - 60 <= created <= int(time.time()) + 60):
                matches.append(row)
        if len(matches) > 1:
            raise _error("晨羽发现多个可能的分配结果，请人工核对", "ambiguous_allocation")
        return matches[0] if matches else None

    def _recover_allocation(self, api, record, *, attempts, clear_missing_after=None):
        known = set(record.get("known_instance_ids", ()))
        confirmed_missing = (clear_missing_after is not None and "known_instance_ids" in record
                             and not record.get("instance_uuid"))
        for index in range(attempts):
            try:
                rows = api.instances()
            except ProviderError:
                confirmed_missing = False
            else:
                if row := self._allocation_match(record, rows):
                    return row["instance_uuid"]
                if confirmed_missing and any(row["instance_uuid"] not in known for row in rows):
                    confirmed_missing = False
            if index + 1 < attempts:
                self.sleep(2)
        if confirmed_missing and time.time() >= clear_missing_after:
            return None
        raise _error(
            "晨羽暂未确认分配结果，已保留占位。请执行 yc-h3 gpu reconcile --backend chenyu",
            "needs_reconcile",
        )

    def _bind_allocation(self, record, instance_uuid):
        record.update(instance_uuid=instance_uuid, allocation_pending=False)
        record.pop("known_instance_ids", None)
        self.state.write(record)
        return record

    def _allocate(self, api, plan, previous):
        snapshot = api.instances()
        if any(_SESSION_NAME.fullmatch(_name(row)) and _status(row) not in INACTIVE_STATUSES for row in snapshot):
            raise _error("晨羽受管实例状态在审批后发生变化", "active_managed_session_exists")
        record = {key: plan[key] for key in (
            "session_name", "endpoint_origin", "credential_fingerprint", "source_kind", "source_id", "pod_tag",
            "max_runtime_minutes", "idle_minutes",
        )}
        record.update(
            balance_before=plan["balance"], card_balance_before=plan["card_balance"],
            allocation_pending=True, status=0, guards_ready=False,
            known_instance_ids=[row["instance_uuid"] for row in snapshot],
        )
        for card in plan["candidates"]:
            preset = plan["pricing"]["candidates"][card]
            allocated_at = time.time()
            record.update(gpu=card, hourly_cny=preset["hourly_cny"], allocated_at=allocated_at,
                          deadline_at=int(allocated_at) + record["max_runtime_minutes"] * 60)
            self.state.write(record)
            source_kind = plan["source_kind"]
            body = {"gpu_uuid": preset["gpu_uuid"], "gpu_nums": 1, f"{source_kind}_uuid": plan["source_id"]}
            if plan["pod_tag"] is not None:
                body["pod_tag"] = plan["pod_tag"]
            try:
                data = api.request(f"/instance/create_by_{source_kind}", payload=body)
            except _PlatformRejected as exc:
                out_of_stock = re.search(
                    r"可用数量不足|无可用|库存不足|无库存|售罄|售空|sold.?out|no available|insufficient", str(exc), re.I
                )
                if out_of_stock or re.search(r"HTTP (400|422)", str(exc)):
                    try:
                        unchanged = all(
                            row["instance_uuid"] in record["known_instance_ids"] for row in api.instances()
                        )
                    except ProviderError:
                        unchanged = False
                    if unchanged:
                        continue
                instance_uuid = self._recover_allocation(api, record, attempts=4)
            except (ProviderError, OSError, http.client.HTTPException):
                instance_uuid = self._recover_allocation(api, record, attempts=4)
            else:
                instance_uuid = data.get("instance_uuid") if isinstance(data, dict) else data
                if not _valid_id(instance_uuid) or instance_uuid in record["known_instance_ids"]:
                    instance_uuid = self._recover_allocation(api, record, attempts=4)
            return self._bind_allocation(record, instance_uuid)
        self.state.write(previous)
        raise _error("批准的晨羽候选卡均明确分配失败", "gpu_allocation_failed")

    def _guards(self, api, record):
        instance_uuid = record["instance_uuid"]
        deadline = record["deadline_at"]
        if time.time() >= deadline:
            raise _error("晨羽实例已到原硬截止", "gpu_deadline_expired")
        try:
            api.request("/instance/shutdown_timer", payload={
                "instance_uuid": instance_uuid, "enable": True, "shutdown_time": deadline,
            })
            if time.time() < deadline:
                api.request("/instance/set_idle_close", payload={
                    "instance_uuid": instance_uuid, "idle_period_minutes": record["idle_minutes"], "destroy": True,
                })
        except ProviderError:
            raise _error("晨羽平台未确认计费护栏", "gpu_guard_unconfirmed") from None
        if time.time() >= deadline:
            raise _error("晨羽实例已到原硬截止", "gpu_deadline_expired")
        record["guards_ready"] = True
        self.state.write(record)

    def _await_guarded_instance(self, api, record, *, attempts):
        instance_uuid = record["instance_uuid"]
        if not record.get("guards_ready"):
            try:
                self._guards(api, record)
            except ProviderError as error:
                if error.code != "gpu_guard_unconfirmed":
                    raise
                # Some instance states reject timer changes until the VM reaches running.
        def query():
            if time.time() >= record["deadline_at"]:
                raise _error("晨羽实例已到原硬截止", "gpu_deadline_expired")
            return api.instance(instance_uuid)

        def boot_terminal(row):
            status = _status(row)
            if time.time() >= record["deadline_at"]:
                raise _error("晨羽实例已到原硬截止", "gpu_deadline_expired")
            if status in INACTIVE_STATUSES:
                raise _error("晨羽实例启动前已进入非运行态", "gpu_start_failed")
            if status != 2:
                return False
            if not record.get("guards_ready"):
                self._guards(api, record)
            return True

        return self._wait(query, boot_terminal, attempts=attempts, code="gpu_start_unconfirmed")

    def _destroy_owned(self, api, record, *, reason):
        instance_uuid = record["instance_uuid"]
        record.update(stop_pending=True, shutdown_only=False, record_ledger=True, stop_reason=reason)
        self.state.write(record)
        try:
            api.request("/instance/destroy", payload={"instance_uuid": instance_uuid})
        except ProviderError:
            if self._instance_status(api, instance_uuid) != 4:
                raise _error("晨羽平台未确认销毁，请运行 yc-h3 gpu reconcile --backend chenyu", "stop_unconfirmed") from None
        self._wait(
            lambda: self._instance_status(api, instance_uuid), lambda status: status == 4,
            attempts=4, code="stop_unconfirmed",
        )
        return self._complete_stop(api, record, 4, stopped_at=time.time())

    def gpu_up(self, *, approve_digest=None, connect=True, **options):
        if not connect and options.get("setup"):
            raise ValueError("--setup 需要连接晨羽实例")
        api = self._platform()
        with self.state.lock():
            plan = self.gpu_plan(**options)
            self._approve(plan, approve_digest)
            if api.origin != plan["endpoint_origin"] or api.fingerprint != plan["credential_fingerprint"]:
                raise _error("晨羽审批账号或 API 源发生变化", "gpu_session_owner_mismatch")
            allocate = plan["action"] in ("allocate", "allocate_and_setup")
            record = self._allocate(api, plan, self.state.read()) if allocate else self._owned(api, plan["session_name"])
            if plan["deadline_changed"]:
                runtime = plan["max_runtime_minutes"]
                deadline = record["deadline_at"]
                if runtime != record["max_runtime_minutes"]:
                    deadline = int(record["allocated_at"]) + runtime * 60
                record.update(max_runtime_minutes=runtime, idle_minutes=plan["idle_minutes"],
                              deadline_at=deadline, guards_ready=False)
                self.state.write(record)
            try:
                info = self._await_guarded_instance(api, record, attempts=300 if allocate else 4)
            except ProviderError as error:
                if allocate or error.code == "gpu_deadline_expired":
                    reason = "hard_deadline" if error.code == "gpu_deadline_expired" else "boot_rollback"
                    self._destroy_owned(api, record, reason=reason)
                raise
            record["status"] = 2
            record.setdefault("confirmed_allocated_at", time.time())
            self.state.write(record)
            if connect:
                self._connect(record, info, setup=plan["setup"], setup_params=plan["setup_params"])
            return {"ok": True, "action": plan["action"], "instance": self._public_record(record)}

    def _public_record(self, record):
        return self.config.redact(
            {key: value for key, value in record.items() if key != "runtime_auth_token"},
            record.get("runtime_auth_token"),
        )

    def _client(self, record=None):
        return ChenyuRemoteClient(
            state_path=self.state.path, config=self.config, remote_opener=self.remote_opener,
            api_base=self.api_base, record=record,
        )

    def _connect(self, record, info, *, setup, setup_params):
        if not record.get("endpoint"):
            record["endpoint"] = _jupyter_origin(info, record["instance_uuid"], config=self.config)
        if not record.get("runtime_auth_token"):
            record["runtime_auth_token"] = "yh3_" + secrets.token_urlsafe(32)
        self.state.write(record)
        client = self._client(record)
        try:
            health = client.health()
        except ProviderError:
            if record.get("connected"):
                raise _error("晨羽调度器身份或版本核验失败", "remote_unhealthy") from None
            try:
                self.bootstrap_fn(
                    endpoint=record["endpoint"], session=record["session_name"],
                    runtime_auth_token=record["runtime_auth_token"], archive=pack_remote_tar(),
                    instance_uuid=record["instance_uuid"],
                )
            except (OSError, EOFError, http.client.HTTPException, ValueError, ProviderError):
                raise _error("晨羽 Jupyter 冷部署未确认，请核对实例状态", "remote_bootstrap_failed") from None

            def probe():
                try:
                    return client.health()
                except ProviderError:
                    return None

            health = self._wait(probe, bool, attempts=120, code="remote_bootstrap_unconfirmed")
        record["connected"] = True
        self.state.write(record)
        if client.custom_assets_path.is_file():
            client.sync_custom_assets()
        if time.time() >= record["deadline_at"]:
            raise _error("晨羽实例已到原硬截止", "gpu_deadline_expired")
        if setup:
            prepared = client._json("POST", "/api/preflight", payload={"task": "setup", "params": setup_params})
            digest = prepared.get("approve_digest") if isinstance(prepared, dict) else None
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or prepared.get("instance_id") != health["instance_id"]:
                raise _error("晨羽 setup 预检缺少当前实例的审批摘要", "setup_preflight_failed")
            submitted = client._json("POST", "/api/jobs", payload={
                "task": "setup", "params": setup_params, "approve_digest": digest,
                "session_deadline_at": record["deadline_at"],
            })
            job_id = submitted.get("job_id") if isinstance(submitted, dict) else None
            job = self._wait(lambda: client.get_job(job_id), lambda job: job.get("status") in (
                "done", "error", "cancelled", "cancelled_in_queue",
            ), attempts=2400, code="setup_unconfirmed")
            if job["status"] != "done":
                raise _error("晨羽远端 setup 失败，请查看远端作业", "setup_failed")

    def gpu_status(self):
        api = self._platform()
        record = self._owned(api)
        rows = api.instances()
        instance_uuid = record.get("instance_uuid")
        current = next((row for row in rows if row["instance_uuid"] == instance_uuid), None)
        state = {"backend": "chenyu", "instance": self._public_record(record) or None, **api.balance()}
        if current is not None and _status(current) == 2 and record.get("runtime_auth_token"):
            try:
                state["health"] = self._client(record).health()
            except ProviderError as error:
                state["health"] = {"ok": False, "code": error.code}
            if (usage := _usrdata_models_gib(state["health"])) is not None:
                state.update(usrdata_models_gib=usage, free_usrdata_limit_gb=FREE_USRDATA_LIMIT_GB)
        state["platform_status"] = _status(current) if current is not None else None
        return state

    def gpu_reconcile(self, session=None, *, clear_missing=False):
        api = self._platform()
        with self.state.lock():
            record = self._owned(api, session)
            if not record:
                return {"status": "absent"}
            if record.get("allocation_pending"):
                clear_missing_after = -math.inf if clear_missing else min(
                    record["allocated_at"] + _ALLOCATION_PROPAGATION_SECONDS, record["deadline_at"],
                )
                instance_uuid = self._recover_allocation(api, record, attempts=4, clear_missing_after=clear_missing_after)
                if instance_uuid is None:
                    self.state.write({})
                    return {"status": "cleared", "session_name": record["session_name"]}
                self._bind_allocation(record, instance_uuid)
            rows = api.instances()
            row = next((row for row in rows if row["instance_uuid"] == record.get("instance_uuid")), None)
            observed = time.time()
            status = _status(row) if row is not None else 4
            if record.get("image_pending"):
                return self._finish_image(api, record)
            if record.get("stop_pending"):
                targets = INACTIVE_STATUSES if record.get("shutdown_only") else (4,)
                if status not in targets:
                    if record.get("shutdown_only"):
                        raise _error("晨羽关机尚未确认，请使用原参数重试 gpu stop", "stop_unconfirmed")
                    return self._destroy_owned(api, record, reason=record.get("stop_reason", "reconcile"))
                return self._complete_stop(api, record, status, stopped_at=observed)
            if status in INACTIVE_STATUSES:
                record["record_ledger"] = True
                return self._complete_stop(api, record, status, stopped_at=observed)
            if observed >= record["deadline_at"]:
                return self._destroy_owned(api, record, reason="hard_deadline")
            if not record.get("guards_ready"):
                try:
                    info = self._await_guarded_instance(api, record, attempts=4)
                except ProviderError as error:
                    if error.code == "gpu_deadline_expired":
                        return self._destroy_owned(api, record, reason="hard_deadline")
                    raise
                status = _status(info)
            record["status"] = status
            if status == 2:
                record.setdefault("confirmed_allocated_at", observed)
            self.state.write(record)
            return {"status": "reconciled", "instance": self._public_record(record)}

    def _stop_billing(self, api, record):
        instance_uuid = record["instance_uuid"]
        stopped_at = record["stopped_at"]
        if record.get("guards_ready") and record.get("stop_reason") != "hard_deadline":
            stopped_at = min(stopped_at, record["deadline_at"])
        uptime = max(0.0, stopped_at - record["allocated_at"])
        billing = {
            "session_id": instance_uuid, "session_name": record["session_name"], "uptime_seconds": uptime,
            "hourly_cny": record["hourly_cny"], "balance_before": record.get("balance_before"),
            "card_balance_before": record.get("card_balance_before"), "ts": _timestamp(record["stopped_at"]),
            "allocated_at": _timestamp(record["allocated_at"]),
        }
        bill_instance = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(instance_uuid)}(?![A-Za-z0-9_])")
        card = cash = 0.0
        estimated = True
        saw_debit = False
        for attempt in range(3):
            if attempt:
                self.sleep(2)
            raw_bills = api.request("/bill/list", query={"page": 1, "page_size": 50})
            bills = _rows(raw_bills, "order_list", "bills", "bill_list")
            signed_mode = (isinstance(raw_bills, dict) and "order_list" in raw_bills) or any(
                (_signed_number(row.get("amount", row.get("charge_amount"))) or 0.0) < 0
                for row in bills
            )
            charges = {1: [], 2: []}
            for row in bills:
                remark = row.get("remark")
                if not isinstance(remark, str) or not bill_instance.search(remark):
                    continue
                channel = str(row.get("charge_channel"))
                amount = _signed_number(row.get("amount", row.get("charge_amount")))
                if channel in ("1", "2") and amount is not None:
                    charges[int(channel)].append(amount)
            if signed_mode:
                card = max(0.0, -math.fsum(charges[2]))
                cash = max(0.0, -math.fsum(charges[1]))
                has_debit = any(value < 0 for ch in (1, 2) for value in charges[ch])
            else:
                card = math.fsum(value for value in charges[2] if value >= 0)
                cash = math.fsum(value for value in charges[1] if value >= 0)
                has_debit = any(value > 0 for ch in (1, 2) for value in charges[ch])
            saw_debit = has_debit
            if has_debit and card + cash > 0:
                estimated = False
                break
        if saw_debit:
            estimated = False
        balance = api.balance()
        billing.update(
            card_cny=None if estimated else card, cash_cny=None if estimated else cash,
            est_cny=round(uptime * record["hourly_cny"] / 3600 if estimated else card + cash, 8),
            estimated=estimated, balance_after=balance["balance"], card_balance_after=balance["card_balance"],
        )
        return billing

    def _complete_stop(self, api, record, platform_status, *, stopped_at):
        settled = record.get("settled", False)
        record["status"] = platform_status
        record.setdefault("stopped_at", record.get("confirmed_stopped_at", stopped_at))
        record.setdefault("confirmed_stopped_at", stopped_at)
        self.state.write(record)
        billing = record.get("billing")
        ledger_warning = None
        if record.get("record_ledger") and not settled:
            try:
                if billing is None:
                    billing = self._stop_billing(api, record)
                    record["billing"] = billing
                    self.state.write(record)
                ledger = record_chenyu_gpu_stop(billing, config=self.config, account_fp=api.fingerprint)
            except (ProviderError, OSError, ValueError):
                ledger = {"ledger_recorded": False, "ledger_balance_recorded": False}
                ledger_warning = "晨羽停机已确认，费用待核销。请重试 yc-h3 gpu reconcile --backend chenyu"
            if billing is not None:
                billing = {**billing, **ledger}
            settled = ledger["ledger_recorded"] and ledger["ledger_balance_recorded"]
        record.pop("stop_pending", None)
        record.pop("stop_reason", None)
        record.pop("runtime_auth_token", None)
        record["settled"] = settled
        record["connected"] = False
        keep_record = platform_status != 4 or (record.get("record_ledger") and not settled)
        self.state.write(record if keep_record else {})
        result = {
            "ok": True, "status": platform_status, "instance_uuid": record["instance_uuid"], "billing": billing,
            "ledger_recorded": settled, "settled": settled,
        }
        if ledger_warning:
            result["ledger_warning"] = ledger_warning
        return self.config.redact(result)

    def gpu_stop(self, session=None, shutdown_only=False, record_ledger=True):
        api = self._platform()
        with self.state.lock():
            record = self._owned(api, session)
            if not record:
                return {"ok": True, "status": "absent"}
            if not record.get("instance_uuid"):
                raise _error("晨羽分配结果不明，请先执行 gpu reconcile", "needs_reconcile")
            if record.get("image_pending"):
                raise _error("晨羽镜像保存尚未确认，请先执行 gpu reconcile", "needs_reconcile")
            instance_uuid = record["instance_uuid"]
            record.update(stop_pending=True, record_ledger=record_ledger, shutdown_only=shutdown_only)
            record.setdefault("stop_reason", "manual")
            self.state.write(record)

            def current_status():
                status = self._instance_status(api, instance_uuid)
                if status in INACTIVE_STATUSES:
                    record.setdefault("confirmed_stopped_at", time.time())
                return status

            target_statuses = INACTIVE_STATUSES if shutdown_only else (4,)
            before = current_status()
            if before not in target_statuses:
                action = "shutdown" if shutdown_only else "destroy"
                try:
                    api.request(f"/instance/{action}", payload={"instance_uuid": instance_uuid})
                except ProviderError:
                    if current_status() not in target_statuses:
                        raise _error("晨羽平台未确认停机请求，请核对或重试", "stop_unconfirmed") from None
            final = self._wait(current_status, lambda status: status in target_statuses, attempts=60, code="stop_unconfirmed")
            return self._complete_stop(api, record, final, stopped_at=time.time())

    def gpu_save_image(self, session=None, approve_digest=None):
        api = self._platform()
        record = self._owned(api, session)
        plan = self._save_image_plan(api, record)
        self._approve(plan, approve_digest)
        with self.state.lock():
            record = self._owned(api, session)
            private_images = self._private_images(api)
            plan = self._save_image_plan(api, record, private_images=private_images)
            self._approve(plan, approve_digest)
            client = self._client(record)
            self._verify_image_deliveries(record, client)
            cleanup = self._cleanup_for_image(client)
            record["image_pending"] = {
                "name": f'{record["session_name"]}-{uuid.uuid4().hex[:8]}',
                "old_ids": [_image_id(row) for row in private_images],
                "requested_at": time.time(),
            }
            self.state.write(record)
            try:
                result = api.request("/instance/save_image", payload={
                    "instance_uuid": record["instance_uuid"],
                })
            except _PlatformRejected:
                record.pop("image_pending")
                self.state.write(record)
                raise
            if isinstance(result, dict) and _valid_id(_image_id(result)):
                record["image_pending"]["image_uuid"] = _image_id(result)
                self.state.write(record)
            saved = self._finish_image(api, record)
            saved["cleanup"] = cleanup
            return saved

    def _verify_image_deliveries(self, record, client):
        try:
            health = client.health()
            inventory = client._json("GET", "/api/maintenance/job-inventory", timeout=30)
            jobs = inventory.get("jobs")
            if (inventory.get("complete") is not True or inventory.get("session") != record["session_name"]
                    or inventory.get("instance_id") != health.get("instance_id")
                    or inventory.get("platform_instance_id") != record["instance_uuid"]
                    or not isinstance(jobs, list)):
                raise ValueError
            by_id = {}
            for job in jobs:
                if (not isinstance(job, dict) or not _valid_id(job.get("id")) or job["id"] in by_id
                        or job.get("status") not in ("submitting", "queued", "running", "done", "error", "cancelled")):
                    raise ValueError
                by_id[job["id"]] = job
        except (ProviderError, OSError, ValueError):
            raise _error("无法核实晨羽远端作业清单，已阻止保存镜像", "job_inventory_unverified") from None

        store = TaskStore(config=self.config)
        filters = {"provider": "chenyu-comfy", "instance_uuid": record["instance_uuid"]}
        pending = store.list_pending_comfy_deliveries(**filters)
        recovery = {}
        associated_requests = {job.get("client_request_id") for job in by_id.values() if job.get("client_request_id")}
        for job_id, job in by_id.items():
            if (job["status"] in ("error", "cancelled") and not job.get("has_outputs")
                    and job.get("recovery_state") != "interrupted_unknown"):
                continue
            if job["status"] == "done" and job.get("task") in ("setup", "load", "download") and not job.get("has_outputs"):
                continue
            proof = store.find_remote_job(job_id, **filters, credential_fingerprint=record["credential_fingerprint"])
            if job["status"] == "done" and proof is not None and delivery_confirmed(proof):
                continue
            task_key = proof.get("task_key") if proof else None
            if task_key is None and job.get("client_request_id"):
                candidates = {
                    row["task_key"] for row in pending if row.get("client_request_id") == job["client_request_id"]
                }
                if len(candidates) == 1:
                    task_key = candidates.pop()
            if proof is not None:
                command = f"yc-h3 jobs fetch {shlex.quote(task_key)} --remote-job-id {shlex.quote(job_id)}"
            elif task_key is not None:
                command = f"yc-h3 jobs resume {shlex.quote(task_key)}"
            else:
                command = f"yc-h3 jobs fetch <task_key> --remote-job-id {shlex.quote(job_id)}（先用 yc-h3 jobs list 核对断点）"
            recovery[job_id] = command
        for row in pending:
            job_id = row.get("remote_job_id") or row.get("remote_task_id")
            if job_id not in by_id and (job_id or row.get("client_request_id") not in associated_requests):
                task_key = row["task_key"]
                recovery[job_id or task_key] = f"yc-h3 jobs resume {shlex.quote(task_key)}"
        if recovery:
            detail = "\n".join(f"{job_id} · {command}" for job_id, command in recovery.items())
            raise _error(f"晨羽作业尚未确认交付，已阻止保存镜像。\n{detail}", "pending_comfy_deliveries")

    def _cleanup_for_image(self, client):
        try:
            status, data = client._request_json(
                "POST", "/api/maintenance/cleanup-for-image", timeout=120,
            )
        except ProviderError:
            raise _error("晨羽远端镜像清理请求中断，已阻止保存镜像", "image_cleanup_failed") from None
        if status == 409:
            raise _error("晨羽远端仍有在途作业，请等待作业结束后再保存镜像", "active_jobs_present")
        if not (200 <= status < 300 and isinstance(data, dict) and data.get("ok") is True):
            raise _error(f"晨羽远端镜像清理未获确认（HTTP {status}），已阻止保存镜像", "image_cleanup_failed")
        return data

    def _save_image_plan(self, api, record, *, private_images=None):
        if not record.get("instance_uuid") or record.get("stop_pending") or record.get("image_pending"):
            raise _error("晨羽当前无可保存镜像的受管实例，或有操作待核对", "needs_reconcile")
        if _status(api.instance(record["instance_uuid"])) != 2:
            raise _error("保存镜像要求晨羽受管实例处于运行态", "instance_not_running")
        if private_images is None:
            private_images = self._private_images(api)
        existing_bytes = sum(_image_size_bytes(row) for row in private_images)
        existing_gib = round(existing_bytes / GIB, 2)
        remaining_gb = round(max(0.0, FREE_IMAGE_LIMIT_GB - existing_gib), 2)
        source_id = record.get("source_id")
        stacked = record.get("source_kind") == "image" and source_id != DEFAULT_BASE_IMAGE_UUID
        warnings = []
        facts = {
            "backend": "chenyu", "action": "save_image", "endpoint_origin": api.origin,
            "data_dir_hash": self.config.data_dir_hash,
            "credential_fingerprint": api.fingerprint, "session_name": record["session_name"],
            "instance_uuid": record["instance_uuid"], "auto_shutdown": True,
            "existing_private_image_count": len(private_images),
            "existing_private_images_bytes": existing_bytes,
            "existing_private_images_gib": existing_gib,
            "free_image_limit_gb": FREE_IMAGE_LIMIT_GB,
            "free_image_remaining_gb": remaining_gb,
            "overage_cny_per_gb_hour": STORAGE_OVERAGE_CNY_PER_GB_HOUR,
            "free_usrdata_limit_gb": FREE_USRDATA_LIMIT_GB,
            "notice": "晨羽镜像免费额度 50 GB。保存前核对所有作业的本地交付证明并清理可重建缓存。保存后实例自动关机。",
            "warnings": warnings,
        }
        if stacked:
            facts["stacked_from_private_image"] = True
            source = next((row for row in private_images if _image_id(row) == source_id), {})
            layer_count = _number(source.get("layer_count"))
            if layer_count is not None and layer_count.is_integer():
                facts["source_layer_count"] = int(layer_count)
            warnings.append("私有镜像叠层可能达到平台层数上限或双份占用 50 GB 免费额度。环境升级时建议从官方基础镜像重建并删除旧镜像。")
        if private_images or remaining_gb < 20:
            warnings.append(f"私有镜像合计超出 50 GB 的部分按 ¥{STORAGE_OVERAGE_CNY_PER_GB_HOUR}/GB/小时计费。")
        digest_facts = {**facts, "old_ids": sorted(_image_id(row) for row in private_images), "source_id": source_id}
        plan = {**self.config.redact(facts), "status": "warn", "requires_approval": True,
                "digest": sha256_bytes(canonical_json_bytes(digest_facts))}
        if record.get("connected"):
            try:
                usage = _usrdata_models_gib(self._client(record).health())
            except ProviderError:
                pass
            else:
                if usage is not None:
                    plan["usrdata_models_gib"] = usage
        return plan

    @staticmethod
    def _private_images(api):
        images = _rows(api.request("/image/private/list"), "images", "image_list")
        if any(not _valid_id(_image_id(row)) for row in images):
            raise _error("晨羽私有镜像清单缺少有效镜像 ID", "invalid_platform_response")
        return images

    def _finish_image(self, api, record):
        instance_uuid = record["instance_uuid"]
        pending = record["image_pending"]
        old_ids = pending["old_ids"]
        source_id = record.get("source_id")
        requested_at = pending.get("requested_at")

        def find_image(info):
            fresh = [row for row in self._private_images(api) if _image_id(row) not in old_ids]
            target_id = pending.get("image_uuid")
            if not target_id:
                info_img = info.get("image_uuid")
                if _valid_id(info_img) and info_img != source_id and info_img not in old_ids:
                    target_id = info_img
            if target_id:
                exact = [row for row in fresh if _image_id(row) == target_id]
                return exact[0] if len(exact) == 1 else None
            named = [row for row in fresh if (row.get("image_name") or row.get("name") or row.get("title")) == pending["name"]]
            if named:
                return named[0] if len(named) == 1 else None
            pod_uuid = info.get("pod_uuid")
            if requested_at is None:
                return None
            candidates = []
            for row in fresh:
                created = _signed_number(row.get("create_time"))
                if created is None or created < int(requested_at) - 120:
                    continue
                if pod_uuid and row.get("pod_uuid") != pod_uuid:
                    continue
                candidates.append(row)
            return candidates[0] if len(candidates) == 1 else None

        def poll_save():
            info = api.instance(instance_uuid)
            inst_status = str(info.get("save_image_status"))
            if inst_status == "3":
                return ("failed", info, None)
            image = find_image(info)
            if image is not None:
                img_status = image.get("save_image_status")
                if img_status is None:
                    if inst_status == "2":
                        return ("done", info, image)
                elif str(img_status) == "2":
                    return ("done", info, image)
                elif str(img_status) == "3":
                    return ("failed", info, image)
            elif inst_status == "2":
                return ("await_image", info, None)
            return None

        state, _info, image = self._wait(
            poll_save, lambda result: result is not None and result[0] in ("done", "failed"),
            attempts=600, code="save_image_unconfirmed",
        )
        if state == "failed":
            record.pop("image_pending")
            self.state.write(record)
            raise _error("晨羽报告镜像保存失败", "save_image_failed")
        saved_uuid = _image_id(image)
        terminal = self._wait(
            lambda: api.instance(instance_uuid), lambda row: _status(row) in INACTIVE_STATUSES,
            attempts=60, code="save_image_shutdown_unconfirmed",
        )
        record.update(connected=False, image_uuid=saved_uuid, record_ledger=True, stop_reason="save_image")
        record.pop("image_pending")
        stopped = self._complete_stop(api, record, _status(terminal), stopped_at=time.time())
        return self.config.redact({
            **stopped, "image_uuid": saved_uuid,
            "size": image.get("size", image.get("image_size", image.get("size_gb"))),
        })


def _perf_peak(record):
    gpu = record.get("gpu_stats") or {}
    return (gpu.get("mem_used_mb") or {}).get("max")


def _perf_median(records):
    values = [value for record in records if (value := (record.get("phases_s") or {}).get("total")) is not None]
    return statistics.median(values) if values else None


def _summarize_perf_history(root, workflow):
    """Summarize completed local archives by comparable workload."""
    groups = {}
    for path in root.glob("*/perf.json"):
        try:
            run_rec = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(run_rec, dict) or run_rec.get("status") != "done":
            continue
        key = run_rec.get("workload_key")
        if (
            not isinstance(key, dict)
            or not isinstance(key.get("workflow_id"), str)
            or (workflow and workflow != key["workflow_id"])
        ):
            continue
        groups.setdefault(canonical_json_bytes(key), []).append(run_rec)
    rows = []
    for encoded, samples in groups.items():
        partial = [r for r in samples if (r.get("markers") or {}).get("comfy_partial_load") is True]
        row = {
            "workload_key": json.loads(encoded),
            "comparable_samples": len(samples),
            "partial_load_samples": len(partial),
            "partial_load_median_total_s": _perf_median(partial),
        }
        for label, cold in (("cold_start", True), ("warm_run", False)):
            runs = [r for r in samples if r.get("cold_start") is cold]
            peaks = [value for r in runs if (value := _perf_peak(r)) is not None]
            row[label] = {
                "samples": len(runs),
                "median_total_s": _perf_median(runs),
                "peak_vram_mb": max(peaks, default=None),
                "median_peak_vram_mb": statistics.median(peaks) if peaks else None,
            }
        rows.append(row)
    return rows


class ChenyuRemoteClient(ManagedComfyClient):
    """Same job interface as the Colab daemon, pinned to one managed HTTPS endpoint."""

    def __init__(self, *, api_base=None, state_path=None, config: EngineConfig | None = None, remote_opener=None, record=None):
        self.config = config or load_config()
        self._state = ChenyuGpuState(state_path, config=self.config)
        self._record = record
        self._fixed_record = record is not None
        self.api_base = api_base or self.config.get("CHENYU_API_BASE", DEFAULT_API_BASE)
        self.timeout = 30
        self._opener = remote_opener or urllib.request.build_opener(NoRedirectHandler())
        self._submission = {}
        self._lock = threading.Lock()

    def _active(self):
        record = self._record if self._fixed_record else self._state.read()
        if not record:
            return None
        if self._record and any(record.get(key) != self._record.get(key) for key in (
            "instance_uuid", "endpoint", "runtime_auth_token", "credential_fingerprint",
        )):
            raise _error("晨羽受管实例已切换", "gpu_session_owner_mismatch")
        self._record = record
        token = record.get("runtime_auth_token")
        if (record.get("status") != 2 or record.get("stop_pending") or record.get("image_pending")
                or not isinstance(token, str) or not _VISIBLE_ASCII.fullmatch(token) or not record.get("endpoint")):
            return None
        key, _ = validate_api_key(self.config.get(API_KEY_ENV))
        if not key or record.get("credential_fingerprint") != credential_fingerprint(key):
            raise _error("当前晨羽账号与受管实例不符", "gpu_session_owner_mismatch")
        if self.api_base is not None:
            try:
                origin, _ = normalize_api_base(self.api_base)
            except ValueError:
                raise _error("晨羽 API 地址必须是纯 HTTPS 源", "invalid_api_base") from None
            if record.get("endpoint_origin") != origin:
                raise _error("当前晨羽 API 源与受管实例不符", "gpu_session_owner_mismatch")
        return record

    @property
    def origin(self):
        record = self._active()
        if record is None:
            return ""
        try:
            return validate_instance_endpoint(record["endpoint"], record["instance_uuid"], config=self.config)
        except (KeyError, ValueError):
            raise _error("晨羽受管实例端点未通过校验", "untrusted_instance_endpoint") from None

    def _open(self, method, path, *, body=None, headers=None, timeout=None):
        record = self._active()
        if record is None:
            raise DaemonHTTPError(503, {"detail": "没有已连接的晨羽受管 GPU"}, code="no_active_gpu_session")
        request = urllib.request.Request(
            self.origin + path, method=method, data=body,
            headers={**(headers or {}), "Authorization": "Bearer " + record["runtime_auth_token"]},
        )
        try:
            return self._opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as response:
            return response
        except ProviderError:
            raise _error("晨羽远端认证请求发生重定向", "redirect_blocked") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise DaemonHTTPError(503, {"detail": "晨羽远端请求中断"}, code="remote_transport_error") from None

    def _request(self, method, path, *, body=None, headers=None, timeout=None):
        path = path.replace("/api/local/", "/api/", 1) if path.startswith("/api/local/") else path
        with self._open(method, path, body=body, headers=headers, timeout=timeout) as response:
            status = response.getcode()
            self._reject_redirect(status)
            try:
                return status, response.read()
            except (OSError, http.client.HTTPException):
                raise DaemonHTTPError(503, {"detail": "晨羽远端响应中断"}, code="remote_transport_error") from None

    @staticmethod
    def _reject_redirect(status):
        if 300 <= status < 400:
            raise _error("晨羽远端认证请求发生重定向", "redirect_blocked")

    def _request_json(self, method, path, *, payload=None, timeout=None):
        if method == "GET" and path in ("/api/local/status", "/api/local/state"):
            return 200, self._state_snapshot(timeout=timeout or 5)
        status, data = super()._request_json(method, path, payload=payload, timeout=timeout)
        return status, self.config.redact(data, (self._record or {}).get("runtime_auth_token"))

    def _json(self, method, path, *, payload=None, timeout=None):
        status, data = self._request_json(method, path, payload=payload, timeout=timeout)
        if status != 200 or not isinstance(data, dict):
            raise DaemonHTTPError(status, data)
        return data

    def health(self, *, timeout=5):
        record = self._active()
        if record is None:
            raise _error("没有已连接的晨羽受管 GPU", "no_active_gpu_session")
        health = self._json("GET", "/api/health", timeout=timeout)
        try:
            version = remote_source_version()
        except (OSError, ValueError):
            raise _error("无法计算本地远端源码版本", "remote_identity_mismatch") from None
        if (
            health.get("ok") is not True or health.get("app") != "yc-h3-engine"
            or health.get("remote_root") != CHENYU_REMOTE_ROOT or health.get("session") != record["session_name"]
            or not version or health.get("version") != version
            or not set(_WORKFLOW_NAMES).issubset(health.get("workflows") or ())
            or health.get("platform_instance_id") != record["instance_uuid"] or not _valid_id(health.get("instance_id"))
        ):
            raise _error("晨羽远端调度器的身份、版本或工作流清单不匹配", "remote_identity_mismatch")
        return health

    def state(self):
        return self._state_snapshot(timeout=5)

    def _state_snapshot(self, *, timeout):
        record = self._active()
        if record is None:
            return {"tunnel": {"status": "down"}, "remote": {"status": "unavailable"}, "settings": {"managed_sessions": {}}}
        health = self.health(timeout=timeout)
        session = record["session_name"]
        return {
            "tunnel": {"status": "up", "session": session},
            "remote": {**health, "status": "ok"},
            "settings": {"managed_sessions": {session: {"host": record["instance_uuid"], "deadline_at": record["deadline_at"]}}},
        }

    @property
    def custom_assets_path(self):
        configured = self.config.get("YC_H3_CUSTOM_ASSETS")
        return self.config.cwd / Path(configured).expanduser() if configured else self.config.data_dir / "custom_assets.json"

    @property
    def perf_dir(self):
        configured = self.config.get("YC_H3_PERF_DIR")
        return self.config.cwd / Path(configured).expanduser() if configured else self.config.data_dir / "perf"

    def _asset_bytes(self):
        try:
            return self.custom_assets_path.read_bytes()
        except FileNotFoundError:
            return b"{}"

    def _assets_digest(self, record):
        local = sha256_bytes(self._asset_bytes())
        if local == record.get("custom_assets_file_sha256"):
            return record["custom_assets_sha256"]
        if local == sha256_bytes(b"{}"):
            return local
        return None

    def sync_custom_assets(self):
        record = self._active()
        if record is None or not self._fixed_record:
            raise _error("请通过受管连接同步晨羽自定义资产", "custom_assets_sync_failed")
        raw = self._asset_bytes()
        result = self._json("POST", "/api/custom-assets", payload=json.loads(raw))
        digest = result.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _error("晨羽远端未确认自定义资产摘要", "custom_assets_sync_failed")
        record.update(custom_assets_file_sha256=sha256_bytes(raw), custom_assets_sha256=digest)
        self._state.write(record)
        return result

    @staticmethod
    def _blocked(code, detail):
        return {"status": "block", "code": code, "detail": detail,
                "signals": [{"severity": "block", "code": code, "detail": detail}]}

    @staticmethod
    def _submission_digest(remote_digest, context, deadline):
        return sha256_bytes(canonical_json_bytes({
            "remote": remote_digest, "billing_context": context, "deadline_at": deadline,
        }))

    def _deadline_check(self, record):
        deadline = _number(record.get("deadline_at"))
        if not record.get("guards_ready") or deadline is None or time.time() >= deadline:
            return self._blocked("session_deadline_expired", "晨羽会话计费护栏未就绪或已到硬截止")
        return None

    def _prepare(self, body):
        record = self._active()
        if record is None:
            return 409, self._blocked("no_active_gpu_session", "没有已连接的晨羽受管 GPU"), []
        if blocked := self._deadline_check(record):
            return 409, blocked, []
        health = self.health()
        assets_digest = self._assets_digest(record)
        if assets_digest is None or health.get("custom_assets_sha256") != assets_digest:
            return 409, self._blocked("custom_assets_out_of_sync", "晨羽远端自定义资产登记与本地不一致"), []
        rate = _number(record.get("hourly_cny"))
        if rate is None:
            return 409, self._blocked("billing_rate_unknown", "晨羽会话小时费率无效"), []
        context = {
            "provider_id": "chenyu-comfy", "endpoint_origin": self.origin,
            "session_name": record["session_name"], "vm_host": record["instance_uuid"], "billing_rate": rate,
        }
        params = body.get("params")
        workflow = params.get("workflow") if isinstance(params, dict) else None
        history = _summarize_perf_history(self.perf_dir, workflow)
        status, reply = self._request_json(
            "POST", "/api/preflight", payload=dict(body, local_history=history, billing_context=context), timeout=90,
        )
        if status not in (200, 409) or not isinstance(reply, dict):
            return status, reply, history
        facts = reply.get("detail", reply) if status == 409 else reply
        if not isinstance(facts, dict):
            return status, reply, history
        if (facts.get("instance_id") != health.get("instance_id")
                or facts.get("custom_assets_sha256") != assets_digest):
            return 409, {"status": "block", "code": "remote_identity_mismatch"}, history
        remote_digest = facts.get("preflight_digest")
        if not isinstance(remote_digest, str) or not _SHA256.fullmatch(remote_digest):
            return 409, {"status": "block", "code": "invalid_preflight_digest"}, history
        normalized = normalize_status(facts.get("status"))
        facts = dict(
            facts, status=normalized, billing_context=context, remote_preflight_digest=remote_digest,
            preflight_digest=self._submission_digest(remote_digest, context, record["deadline_at"]),
            session={"name": record["session_name"], "hard_deadline_in": max(0, record["deadline_at"] - time.time())},
            remote_version=health.get("version"), managed_instance_id=record["instance_uuid"],
        )
        return (409 if normalized == "block" else 200), facts, history

    def preflight(self, body):
        body = dict(body)
        for_submission = body.pop("for_submission", False)
        if body.get("task") in ("setup", "download"):
            return 409, {"status": "block", "code": "unsupported_remote_task"}
        status, facts, history = self._prepare(body)
        if for_submission and body.get("simulated_gpu") is None and status == 200:
            handle = secrets.token_urlsafe(32)
            with self._lock:
                self._submission = {handle: (facts, history)}
            facts = dict(facts, submission_preflight=handle)
        return status, facts

    def submit_job(self, body):
        if self._active() is None:
            return 409, self._blocked("no_active_gpu_session", "没有已连接的晨羽受管 GPU")
        # Serialize final admission against lifecycle mutations, including shutdown and retiming.
        with nullcontext() if self._fixed_record else self._state.lock():
            return self._submit_job_locked(body)

    def _submit_job_locked(self, body):
        record = self._active()
        if record is None:
            return 409, self._blocked("no_active_gpu_session", "没有已连接的晨羽受管 GPU")
        if blocked := self._deadline_check(record):
            return 409, blocked
        review = {key: body[key] for key in ("model_id", "task", "params") if key in body}
        if review.get("task") in ("setup", "download"):
            return 409, {"status": "block", "code": "unsupported_remote_task"}
        token = body.get("submission_preflight")
        if token is not None:
            with self._lock:
                prepared = self._submission.pop(token, None) if isinstance(token, str) else None
            if not prepared:
                return 409, {"status": "warn", "approval_status": "stale"}
            facts, history = prepared
            health = self.health()
            if (facts["instance_id"] != health["instance_id"]
                    or facts["custom_assets_sha256"] != health["custom_assets_sha256"]
                    or facts["custom_assets_sha256"] != self._assets_digest(record)):
                return 409, {"status": "block", "code": "remote_identity_mismatch"}
        else:
            status, facts, history = self._prepare(review)
            if status != 200:
                return status, facts

        live_context = dict(facts["billing_context"], billing_rate=record["hourly_cny"])
        expected = self._submission_digest(facts["remote_preflight_digest"], live_context, record["deadline_at"])
        digest = body.get("approve_digest") or body.get("preflight_digest")
        acknowledged = body.get("approve_digest") == expected or body.get("ack_warnings") is True
        if digest != expected or expected != facts["preflight_digest"] or (facts["status"] == "warn" and not acknowledged):
            return 409, dict(facts, status="warn", approval_status="stale" if digest else "missing")
        if blocked := self._deadline_check(record):
            return 409, blocked
        forward = {**review, "session_deadline_at": record["deadline_at"],
                   "billing_context": facts["billing_context"], "local_history": history,
                   "preflight_digest": facts["remote_preflight_digest"], "ack_warnings": facts["status"] == "warn"}
        for key in ("client_request_id", "request_digest"):
            if key in body:
                forward[key] = body[key]
        return self._request_json("POST", "/api/jobs", payload=forward, timeout=120)

    def upload(self, filename, data):
        path = "/api/upload?" + urllib.parse.urlencode({"filename": filename})
        status, raw = self._request("POST", path, body=data, headers={"Content-Type": "application/octet-stream"}, timeout=180)
        try:
            reply = json.loads(raw)
        except ValueError:
            reply = {}
        reply = self.config.redact(reply, (self._record or {}).get("runtime_auth_token"))
        if status != 200 or not isinstance(reply, dict) or not reply.get("path"):
            raise DaemonHTTPError(status, reply, code="upload_failed")
        return {"remote_path": reply["path"]}

    @staticmethod
    def _job_id(job_id):
        if not _valid_id(job_id):
            raise ValueError("无效 job_id")
        return job_id

    def get_job(self, job_id):
        return super().get_job(self._job_id(job_id))

    def cancel_job(self, job_id):
        return super().cancel_job(self._job_id(job_id))

    def list_workflows(self):
        self.health()
        return self._json("GET", "/api/workflows", timeout=20)

    def download_file(self, remote_path, dst_path):
        path = "/api/files?" + urllib.parse.urlencode({"path": remote_path})
        with self._open("GET", path, timeout=300) as response:
            status = response.getcode()
            self._reject_redirect(status)
            if status != 200:
                raise DaemonHTTPError(status, {"detail": "晨羽远端成品下载失败"}, code="download_failed")
            expected = parse_content_length(response.headers.get("Content-Length"))
            try:
                with dst_path.open("wb") as stream:
                    written = 0
                    while chunk := response.read(256 * 1024):
                        stream.write(chunk)
                        written += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected is not None and written != expected:
                    raise ArtifactContractError("晨羽远端成品下载不完整", code="incomplete_download")
            except BaseException:
                dst_path.unlink(missing_ok=True)
                raise

    def archive_perf(self, job_id):
        route = "/api/jobs/" + self._job_id(job_id)
        status, raw_perf = self._request("GET", route + "/perf")
        if status == 404:
            job = self.get_job(job_id)
            return {"status": "missing" if job.get("status") in ("done", "error", "cancelled") else "pending"}
        if status != 200:
            raise DaemonHTTPError(status, {"detail": "晨羽远端 perf 无法下载"}, code="perf_archive_failed")
        status, raw_telemetry = self._request("GET", route + "/telemetry")
        if status != 200:
            raise DaemonHTTPError(status, {"detail": "晨羽远端 telemetry 无法下载"}, code="perf_archive_failed")
        token = (self._record or {}).get("runtime_auth_token")
        try:
            perf = self.config.redact(json.loads(raw_perf), token)
            if not isinstance(perf, dict) or perf.get("job_id") != job_id or perf.get("status") not in (
                "done", "error", "cancelled", "cancelled_in_queue",
            ):
                raise ValueError()
            telemetry = [self.config.redact(json.loads(line), token) for line in raw_telemetry.splitlines()]
            if any(not isinstance(row, dict) or type(row.get("ts")) not in (float, int) for row in telemetry):
                raise ValueError()
            perf.update(provider="chenyu-comfy", archived_at=datetime.now(timezone.utc).isoformat())
            clean_perf = canonical_json_bytes(perf)
            clean_telemetry = b"".join(canonical_json_bytes(row) + b"\n" for row in telemetry)
        except (TypeError, ValueError):
            raise _error("晨羽作业的性能记录或采样无效", "perf_archive_failed") from None

        perf_root = self.perf_dir
        path = ensure_real_subpath(perf_root, perf_root / job_id)
        path.mkdir(parents=True, exist_ok=True)
        telemetry_path = path / "telemetry.jsonl"
        perf_path = path / "perf.json"
        # Publish telemetry before the perf marker consumed by history and the CLI.
        atomic_write_bytes(telemetry_path, clean_telemetry)
        atomic_write_bytes(perf_path, clean_perf)
        return {
            "status": "archived", "perf_json": str(perf_path), "telemetry_jsonl": str(telemetry_path),
            "cold_start": perf.get("cold_start"), "total_s": (perf.get("phases_s") or {}).get("total"),
            "inference_total_s": (perf.get("phases_s") or {}).get("inference_total"),
            "peak_vram_mb": ((perf.get("gpu_stats") or {}).get("mem_used_mb") or {}).get("max"), "comfy_partial_load": (perf.get("markers") or {}).get("comfy_partial_load"),
        }


class _JupyterTerminal:
    """Jupyter's authenticated terminal protocol over a pinned TLS RFC 6455 connection."""

    def __init__(self, origin):
        self.url = urllib.parse.urlsplit(origin)
        self.origin = origin
        self.cookies = SimpleCookie()
        self._logged_in = False

    def _http(self, method, path, *, body=None, content_type=None):
        conn = http.client.HTTPSConnection(self.url.hostname, self.url.port, timeout=20)
        headers = {"Origin": self.origin, "Referer": self.origin + "/"}
        if self.cookies:
            headers["Cookie"] = self._cookie_header()
        if "_xsrf" in self.cookies:
            headers["X-XSRFToken"] = self.cookies["_xsrf"].value
        if content_type:
            headers["Content-Type"] = content_type
        try:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    self.cookies.load(value)
            return response.status, response.read(1024 * 1024)
        finally:
            conn.close()

    def _cookie_header(self):
        return "; ".join(f"{name}={morsel.coded_value}" for name, morsel in self.cookies.items())

    def _login(self):
        if self._logged_in:
            return
        status, _ = self._http("GET", "/login")
        if status not in (200, 302, 303):
            raise ValueError("Jupyter login unavailable")
        if status == 200:
            xsrf = self.cookies.get("_xsrf")
            body = urllib.parse.urlencode({
                "password": "Zeyun1234", "_xsrf": xsrf.value if xsrf else "",
            }).encode()
            status, _ = self._http("POST", "/login?next=%2Flab", body=body,
                                   content_type="application/x-www-form-urlencoded")
            if status not in (302, 303):
                raise ValueError("Jupyter login rejected")
        self._logged_in = True

    def put_file(self, name, data):
        self._login()
        body = canonical_json_bytes({"type": "file", "format": "base64", "content": base64.b64encode(data).decode("ascii")})
        path = "/api/contents/" + urllib.parse.quote(name, safe="")
        status, _ = self._http("PUT", path, body=body, content_type="application/json")
        if status not in (200, 201):
            raise ValueError("Jupyter file upload failed")

    def delete_file(self, name):
        self._login()
        path = "/api/contents/" + urllib.parse.quote(name, safe="")
        status, _ = self._http("DELETE", path)
        if status not in (204, 404):
            raise ValueError("Jupyter file cleanup failed")

    def _create_terminal(self):
        self._login()
        status, data = self._http("POST", "/api/terminals", body=b"{}", content_type="application/json")
        if status not in (200, 201):
            raise ValueError("Jupyter terminal unavailable")
        name = json.loads(data).get("name")
        if not _valid_id(name):
            raise ValueError("Jupyter terminal ID invalid")
        return name

    @contextmanager
    def connect(self):
        terminal = self._create_terminal()
        raw = socket.create_connection((self.url.hostname, self.url.port or 443), timeout=30)
        try:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=self.url.hostname) as stream:
                stream.settimeout(30)
                key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
                headers = [
                    f"GET /terminals/websocket/{terminal} HTTP/1.1", f"Host: {self.url.netloc}",
                    "Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                    f"Sec-WebSocket-Key: {key}", f"Origin: {self.origin}", f"Cookie: {self._cookie_header()}",
                ]
                stream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
                with stream.makefile("rb") as reader:
                    status = reader.readline(8192).split()
                    response_headers = http.client.parse_headers(reader)
                    accept = base64.b64encode(hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                    ).digest()).decode("ascii")
                    if (len(status) < 2 or status[1] != b"101"
                            or response_headers.get("Upgrade", "").lower() != "websocket"
                            or "upgrade" not in {part.strip().lower() for part in response_headers.get("Connection", "").split(",")}
                            or response_headers.get("Sec-WebSocket-Accept") != accept):
                        raise ValueError("Jupyter WebSocket handshake rejected")
                    yield stream, reader
        finally:
            raw.close()

    @staticmethod
    def send(stream, payload, opcode=1):
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        else:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
        stream.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))

    @classmethod
    def wait_for(cls, stream, reader, marker):
        def read(size):
            data = reader.read(size)
            if len(data) != size:
                raise EOFError("Jupyter WebSocket closed")
            return data

        output = ""
        message = bytearray()
        for _ in range(2000):
            first, second = read(2)
            opcode, final = first & 0x0F, bool(first & 0x80)
            if first & 0x70 or second & 0x80:
                raise ValueError("Invalid Jupyter WebSocket frame")
            length = second & 0x7F
            if length >= 126:
                length = int.from_bytes(read(2 if length == 126 else 8), "big")
            if length > 1024 * 1024 or len(message) + length > 1024 * 1024:
                raise ValueError("Jupyter WebSocket frame too large")
            payload = read(length)
            if opcode == 8:
                raise EOFError("Jupyter WebSocket closed")
            if opcode == 9:
                cls.send(stream, payload, opcode=10)
            if opcode not in (0, 1):
                continue
            if opcode == 1:
                message.clear()
            message.extend(payload)
            if not final:
                continue
            frame = json.loads(message)
            message.clear()
            if isinstance(frame, list) and len(frame) >= 2 and frame[0] == "stdout":
                output = (output + str(frame[1]))[-16384:]
                if marker in output:
                    return
        raise ValueError("Jupyter terminal did not confirm transfer")


def _bootstrap_remote(*, endpoint, session, runtime_auth_token, archive, instance_uuid):
    """Stage the remote package via Jupyter, then detach the managed 8888 takeover."""
    env = {
        "YC_H3_ALLOW_NON_LOOPBACK": "1",
        "YC_H3_REMOTE_AUTH_TOKEN_FILE": CHENYU_REMOTE_ROOT + "/run/auth.token",
        "YC_H3_MODELS_DIR": CHENYU_MODELS_DIR,
        "YC_H3_CHENYU_TORCH": "1",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HUB_ENABLE_HF_TRANSFER": "1",
        "YC_H3_SESSION": session,
        "YC_H3_PLATFORM_INSTANCE_ID": instance_uuid,
    }
    # The instance filesystem is snapshotted by save_image. Place the real 0600 token on
    # volatile storage and use the stable run/auth.token pathname in the service environment.
    script = f"""import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

Path(sys.argv[1]).unlink(missing_ok=True)
Path(__file__).unlink(missing_ok=True)
root = Path({CHENYU_REMOTE_ROOT!r})
app = root / 'app'
bundle = Path(sys.argv[2])
with bundle.open('rb') as archive:
    bundle.unlink(missing_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.deploy-', dir=root) as stage_dir:
        stage = Path(stage_dir) / 'app'
        stage.mkdir()
        with tarfile.open(fileobj=archive) as tar:
            if hasattr(tarfile, 'data_filter'):
                tar.extractall(stage, filter='data')
            else:
                tar.extractall(stage)
        backup = Path(stage_dir) / 'previous'
        if app.exists() or app.is_symlink():
            app.rename(backup)
        try:
            stage.rename(app)
        except OSError:
            if backup.exists() or backup.is_symlink():
                backup.rename(app)
            raise
run = root / 'run'
run.mkdir(mode=0o700, exist_ok=True)
fd, secret_path = tempfile.mkstemp(prefix='yc-h3-engine-auth-', dir='/dev/shm')
with os.fdopen(fd, 'w') as stream:
    stream.write({runtime_auth_token!r} + '\\n')
auth = run / 'auth.token'
auth.unlink(missing_ok=True)
auth.symlink_to(secret_path)

def jupyter_running(proc):
    try:
        args = (proc / 'cmdline').read_bytes().split(b'\\0')[:2]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return any(Path(os.fsdecode(arg)).name == 'jupyter-lab' for arg in args)

def signal_jupyter(pids, sig):
    for proc in pids:
        if jupyter_running(proc):
            try:
                os.kill(int(proc.name), sig)
            except (ProcessLookupError, PermissionError):
                pass

def wait_for_exit(pids, attempts):
    for _ in range(attempts):
        pids = [proc for proc in pids if jupyter_running(proc)]
        if not pids:
            return []
        time.sleep(0.2)
    return [proc for proc in pids if jupyter_running(proc)]

time.sleep(0.2)
pids = [proc for proc in Path('/proc').iterdir() if proc.name.isdecimal() and jupyter_running(proc)]
signal_jupyter(pids, signal.SIGTERM)
pids = wait_for_exit(pids, 30)
if pids:
    signal_jupyter(pids, signal.SIGKILL)
    if wait_for_exit(pids, 10):
        raise RuntimeError('JupyterLab did not exit after SIGKILL')
subprocess.run(['bash', str(app / 'bootstrap_remote.sh'), '--daemon', '--host',
                '0.0.0.0', '--port', '8888', '--root', str(root)],
               env={{**os.environ, **{env!r}}}, cwd=app, check=True)
"""
    encoded = base64.b64encode(gzip.compress(script.encode(), mtime=0)).decode("ascii")
    terminal = _JupyterTerminal(endpoint)
    bundle_name = f"yc-h3-engine-bootstrap-{secrets.token_hex(16)}.tar.gz"
    marker = secrets.token_hex(16)
    verified_marker = secrets.token_hex(16)
    launch_marker = secrets.token_hex(16)
    launched = False
    try:
        terminal.put_file(bundle_name, archive)
        with terminal.connect() as (stream, reader):
            def command(line):
                terminal.send(stream, json.dumps(["stdin", line + "\n"]).encode("utf-8"))

            def print_marker(value):
                # Keep the full marker out of the input so shell echo cannot confirm it.
                return f"printf '\\n%s%s\\n' {value[:16]} {value[16:]}"

            command(" unset HISTFILE; set +o history; stty -echo && umask 077 "
                    "&& stage=$(mktemp /dev/shm/yc-h3-engine.XXXXXXXX) && " + print_marker(marker))
            terminal.wait_for(stream, reader, marker)
            command(f"printf %s '{encoded}' > \"$stage\"")
            stage_digest = sha256_bytes(encoded.encode())
            bundle_digest = sha256_bytes(archive)
            command(f'bundle="$PWD/{bundle_name}"; test -f "$bundle" || bundle=/root/{bundle_name}; '
                    f'if test "$(sha256sum "$bundle" | cut -d" " -f1)" = {bundle_digest}'
                    f' && test "$(sha256sum "$stage" | cut -d" " -f1)" = {stage_digest}'
                    ' && base64 -d < "$stage" | gzip -d > "$stage.py"; then '
                    + print_marker(verified_marker) + '; else rm -f "$stage" "$stage.py"; fi')
            terminal.wait_for(stream, reader, verified_marker)
            command('nohup setsid python3 "$stage.py" "$stage" "$bundle" '
                    '</dev/null >/tmp/yc-h3-engine-bootstrap.log 2>&1 & ' + print_marker(launch_marker))
            terminal.wait_for(stream, reader, launch_marker)
            launched = True
    finally:
        if not launched:
            try:
                terminal.delete_file(bundle_name)
            except Exception:
                pass
