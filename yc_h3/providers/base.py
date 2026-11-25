"""Unified Provider abstraction, normalized status/exit-code contracts, and atomic publishing."""
from __future__ import annotations

import io
import json
import hmac
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..fsutil import AtomicPublishError, publish_no_clobber, redact_data, sha256_bytes, sha256_file
from ..tasks_store import TaskConflictError, TaskStore

CAPABILITIES: Tuple[str, ...] = ("video",)
CAPABILITY_SET: frozenset[str] = frozenset(CAPABILITIES)

PREFLIGHT_STATUSES: Tuple[str, ...] = ("pass", "warn", "block")
PREFLIGHT_EXIT_CODES: Dict[str, int] = {
    "pass": 0,
    "warn": 2,
    "block": 3,
}


def validate_capability(capability: Any) -> str:
    """Validate and normalize a provider capability identifier."""
    if not isinstance(capability, str):
        raise ValueError(f"能力类型必须是字符串，收到：{capability!r}")
    cleaned = capability.strip().lower()
    if cleaned not in CAPABILITY_SET:
        raise ValueError(
            f"不支持的能力类型 {capability!r}，允许范围：{', '.join(CAPABILITIES)}"
        )
    return cleaned


def normalize_status(status: Any) -> str:
    """Normalize preflight status to ``pass | warn | block``; any unknown value maps conservatively to ``block``."""
    if isinstance(status, str) and status in PREFLIGHT_STATUSES:
        return status
    return "block"


def exit_code_for_status(status: Any) -> int:
    """Map normalized preflight status to unified CLI exit codes (pass=0, warn=2, block=3)."""
    return PREFLIGHT_EXIT_CODES[normalize_status(status)]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ProviderError(RuntimeError):
    """Structured provider failure carrying machine-readable code and CLI exit code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        exit_code: int = 1,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = int(exit_code)
        self.details: Dict[str, Any] = dict(details) if details else {}


class PreflightBlockedError(ProviderError):
    """Raised when preflight returns ``block`` (exit code 3)."""

    def __init__(
        self,
        message: str = "预检阻断，拒绝提交",
        *,
        preflight: Optional["PreflightResult"] = None,
        code: str = "preflight_blocked",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details) if details else {}
        if preflight is not None:
            merged.setdefault("preflight", preflight.to_dict())
        super().__init__(message, code=code, exit_code=3, details=merged)
        self.preflight = preflight


class ApprovalRequiredError(ProviderError):
    """Raised when an operation requires explicit approval digest or digest is stale (exit code 2)."""

    def __init__(
        self,
        message: str = "需要提供匹配的审批摘要 approve_digest",
        *,
        preflight: Optional["PreflightResult"] = None,
        code: str = "approval_required",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details) if details else {}
        if preflight is not None:
            merged.setdefault("preflight", preflight.to_dict())
        super().__init__(message, code=code, exit_code=2, details=merged)
        self.preflight = preflight


class ArtifactContractError(ProviderError):
    """Raised when a downloaded artifact violates container, dimension, or stream contracts."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "artifact_contract_error",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, exit_code=1, details=details)


class ProviderConflictError(ProviderError, TaskConflictError):
    """Conflict error satisfying both ``ProviderError`` (exit_code=3) and ``TaskConflictError``."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "task_conflict",
        exit_code: int = 3,
        record: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details) if details else {}
        if record is not None:
            merged.setdefault("record", {
                key: record[key] for key in ("task_key", "provider", "state") if key in record
            })
        ProviderError.__init__(self, message, code=code, exit_code=exit_code, details=merged)
        self.record = dict(record) if record is not None else None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class GenerationRequest:
    """Provider-agnostic generation request payload."""

    capability: str
    prompt: str = ""
    output_path: Optional[Path | str] = None
    logical_target: Optional[str] = None
    job_key: Optional[str] = None
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    workflow: Optional[str] = None
    task: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    approve_digest: Optional[str] = None
    force_new: bool = False
    simulated_gpu: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.capability = validate_capability(self.capability)
        if self.output_path is not None and not isinstance(self.output_path, Path):
            self.output_path = Path(self.output_path)
        if self.params is None:
            self.params = {}
        if self.inputs is None:
            self.inputs = {}
        if self.metadata is None:
            self.metadata = {}

    def resolved_logical_target(self) -> str:
        """Return a stable logical target string preserving extension and version suffix.

        When neither ``logical_target`` nor ``job_key`` is explicitly given, normalizes
        ``output_path`` via ``local_path()`` so relative and absolute
        spellings of the same output file always map to the same ``task_key``.
        """
        if self.logical_target is not None and str(self.logical_target).strip():
            return str(self.logical_target).strip()
        if self.output_path is not None:
            return str(local_path(self.output_path))
        if self.job_key is not None and str(self.job_key).strip():
            return str(self.job_key).strip()
        raise ValueError("必须提供 logical_target、output_path 或 job_key")

    def to_dict(self) -> Dict[str, Any]:
        return redact_data({
            "capability": self.capability,
            "prompt": self.prompt,
            "output_path": str(self.output_path) if self.output_path is not None else None,
            "logical_target": self.logical_target,
            "job_key": self.job_key,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "workflow": self.workflow,
            "task": self.task,
            "params": dict(self.params),
            "inputs": dict(self.inputs),
            "approve_digest": self.approve_digest,
            "force_new": bool(self.force_new),
            "simulated_gpu": self.simulated_gpu,
            "metadata": dict(self.metadata),
        })


@dataclass(frozen=True)
class ProbeResult:
    """Read-only snapshot of availability for a provider."""

    provider_id: str
    available: bool
    reason: str = ""
    capabilities: Tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "available": self.available,
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            "details": dict(self.details),
        }


@dataclass
class PreflightResult:
    """Normalized strictly read-only preflight result."""

    provider_id: str
    status: str
    digest: str
    pricing_mode: str
    endpoint_origin: str
    target_identity: Dict[str, Any] = field(default_factory=dict)
    input_hashes: Dict[str, str] = field(default_factory=dict)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = True
    exit_code: Optional[int] = None

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)
        self.exit_code = exit_code_for_status(self.status)
        self.requires_approval = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "digest": self.digest,
            "pricing_mode": self.pricing_mode,
            "endpoint_origin": self.endpoint_origin,
            "target_identity": dict(self.target_identity),
            "input_hashes": dict(self.input_hashes),
            "signals": [dict(item) if isinstance(item, dict) else item for item in self.signals],
            "facts": dict(self.facts),
            "requires_approval": bool(self.requires_approval),
            "exit_code": self.exit_code,
        }


@dataclass
class ArtifactResult:
    """Normalized provider generation / recovery / fetch result."""

    provider_id: str
    task_key: str
    state: str
    artifact_path: Optional[str] = None
    artifact_sha256: Optional[str] = None
    remote_id: Optional[str] = None
    recovered: bool = False
    perf_archived: Optional[bool] = None
    contract: Dict[str, Any] = field(default_factory=dict)
    billing: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "task_key": self.task_key,
            "state": self.state,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "remote_id": self.remote_id,
            "recovered": bool(self.recovered),
            "perf_archived": self.perf_archived,
            "contract": dict(self.contract),
            "billing": dict(self.billing),
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Hashing, atomic no-clobber & image inspection primitives
# ---------------------------------------------------------------------------
def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` deterministically to UTF-8 canonical JSON bytes."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def local_path(path: str | os.PathLike[str]) -> Path:
    if not str(path).strip():
        raise ValueError("文件路径不能为空")
    path = Path(path).expanduser()
    return path.parent.resolve() / path.name


def read_file_with_digest(path: str | os.PathLike[str]) -> Tuple[bytes, str]:
    raw = local_path(path).read_bytes()
    return raw, sha256_bytes(raw)


def build_approval_digest(
    *, task_key: str, data_dir: str | os.PathLike[str], provider: str, endpoint_origin: str,
    credential_fingerprint: str, workflow: str, params: Mapping[str, Any], input_hashes: Mapping[str, str],
    quote_facts: Mapping[str, Any], force_new: bool, replaced_submission_id: str | None,
    prompt: str = "",
) -> str:
    """Bind user approval to a normalized request and every displayed pricing fact."""
    if not force_new and replaced_submission_id is not None:
        raise ValueError("replaced_submission_id 仅用于 --force-new")
    return sha256_bytes(canonical_json_bytes({
        "schema": "yc-h3-approval-v1",
        "task_key": os.path.normcase(task_key),
        "data_dir": os.path.normcase(str(Path(data_dir).expanduser().resolve())),
        "provider": provider,
        "endpoint_origin": endpoint_origin,
        "credential_fingerprint": credential_fingerprint,
        "workflow": workflow,
        "params": dict(params),
        "prompt": prompt,
        "inputs": dict(input_hashes),
        "quote": dict(quote_facts),
        "force_new": force_new,
        "replaced_submission_id": replaced_submission_id,
    }))


def require_approval(preflight: PreflightResult, approved_digest: str | None) -> None:
    if preflight.status == "block":
        raise PreflightBlockedError(preflight=preflight)
    if not approved_digest or not approved_digest.isascii() or not hmac.compare_digest(approved_digest, preflight.digest):
        raise ApprovalRequiredError(
            preflight=preflight, code="approval_stale" if approved_digest else "approval_missing",
        )


def verify_input_hashes(inputs: Mapping[str, str | os.PathLike[str]], expected: Mapping[str, str]) -> None:
    """Recheck material hashes before sending the approved request."""
    try:
        valid = inputs.keys() == expected.keys() and all(
            sha256_file(path) == expected[name] for name, path in inputs.items()
        )
    except OSError:
        valid = False
    if not valid:
        raise ProviderConflictError("输入素材已变更或无法读取，请重新预检", code="input_hash_mismatch")


def publish_atomic_no_clobber(
    tmp_path: str | os.PathLike[str],
    wanted_path: str | os.PathLike[str],
    *,
    before_publish: Optional[Callable[[Path], None]] = None,
) -> Path:
    """Commit a verified artifact beside its destination, using a version on collision."""
    source = local_path(tmp_path)
    wanted = local_path(wanted_path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("待发布成片须为普通暂存文件")
    if wanted.is_dir():
        raise ValueError("目标路径不能是已存在的目录")
    wanted.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source.parent == wanted.parent and source.name.startswith("."):
            return publish_no_clobber(source, wanted, before_publish=before_publish)
        if source == wanted:
            raise ValueError("待发布成片暂存路径不能与目标相同")
        fd, filename = tempfile.mkstemp(prefix=f".{wanted.stem}.", suffix=".tmp", dir=wanted.parent)
        staged = Path(filename)
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, out)
            published = publish_no_clobber(staged, wanted, before_publish=before_publish)
        finally:
            staged.unlink(missing_ok=True)
        source.unlink()
        return published
    except AtomicPublishError as exc:
        raise ArtifactContractError(exc.strerror, code=exc.code) from None


def parse_content_length(header_val: Any) -> Optional[int]:
    """Parse a valid non-negative integer HTTP Content-Length header, or return None."""
    if header_val is None:
        return None
    text = str(header_val).strip()
    if not text or not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def inspect_image_bytes(data: bytes) -> Tuple[str, int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format)
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError(f"不支持的图像格式：{image.format}")
            return mime, *image.size
    except OSError as exc:
        raise ValueError(f"无法打开图像：{exc}") from exc


class Provider(ABC):
    """Abstract base class for all generation providers."""

    provider_id: str = ""
    capabilities: Tuple[str, ...] = ()

    def supports(self, capability: str) -> bool:
        return validate_capability(capability) in self.capabilities

    def probe(self, *, capability: Optional[str] = None, check_remote: bool = False) -> ProbeResult:
        available = capability is None or self.supports(capability)
        return ProbeResult(
            self.provider_id, available, "" if available else "unsupported_capability", self.capabilities
        )

    @abstractmethod
    def preflight(
        self,
        request: GenerationRequest,
        *,
        store: Optional[TaskStore] = None,
    ) -> PreflightResult:
        """Strictly read-only preflight returning status, digest, pricing facts, and signals."""

    @abstractmethod
    def generate(
        self,
        request: GenerationRequest,
        *,
        store: Optional[TaskStore] = None,
    ) -> ArtifactResult:
        """Generate and publish the requested artifact after preflight."""

    def resume(
        self,
        task_key: str,
        *,
        store: TaskStore,
        **kwargs: Any,
    ) -> ArtifactResult:
        """Resume a remote checkpoint when supported."""
        raise ProviderError(f"{self.provider_id} 同步执行，不支持 resume", code="unsupported_operation")

    def fetch(
        self,
        task_key: str,
        *,
        store: TaskStore,
        **kwargs: Any,
    ) -> ArtifactResult:
        """Fetch an already completed or reserved artifact without re-submitting."""
        raise ProviderError(f"{self.provider_id} 同步执行，不支持 fetch", code="unsupported_operation")

    def cancel(
        self,
        task_key: str,
        *,
        store: TaskStore,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Cancel a remote/local task and mark its checkpoint cancelled."""
        raise ProviderError(f"{self.provider_id} 同步执行，不支持 cancel", code="unsupported_operation")

    def describe(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capabilities": list(self.capabilities),
        }
