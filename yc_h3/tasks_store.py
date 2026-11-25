"""Durable checkpoints for paid and managed GPU jobs."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import EngineConfig, load_config
from .fsutil import atomic_write_json, ensure_real_subpath, file_lock, sha256_bytes
from .fsutil import sha256_file as file_sha256

VALID_STATES = (
    "submitting",
    "submitted",
    "delivered",
    "ambiguous",
    "expired_or_missing",
    "failed",
    "cancelled",
)

BLOCKING_RECOVERY_STATES = frozenset({"ambiguous", "expired_or_missing"})

ALLOWED_TRANSITIONS: Dict[str, frozenset[str]] = {
    "submitting": frozenset(
        {"submitted", "ambiguous", "failed", "cancelled", "delivered"}
    ),
    "submitted": frozenset(
        {"submitted", "delivered", "ambiguous", "expired_or_missing", "failed", "cancelled"}
    ),
    "ambiguous": frozenset(
        {"submitted", "delivered", "expired_or_missing", "failed", "cancelled"}
    ),
    "expired_or_missing": frozenset({"failed", "cancelled"}),
    "delivered": frozenset({"delivered"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
}

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TaskStoreError(RuntimeError):
    """Base error for checkpoint store failures."""


class TaskConflictError(TaskStoreError):
    """Raised when an operation violates checkpoint state-machine invariants."""

    exit_code = 3

    def __init__(self, message: str, *, record: Optional[Dict[str, Any]] = None, code: str = "task_conflict"):
        super().__init__(message)
        self.record = record
        self.code = code


class AmbiguousTaskError(TaskConflictError):
    """Raised when an existing checkpoint has an unknown remote outcome (submitting/ambiguous)."""

    def __init__(self, message: str, *, record: Optional[Dict[str, Any]] = None):
        super().__init__(message, record=record, code="ambiguous_task")


class ExpiredTaskError(TaskConflictError):
    """Raised when an existing checkpoint was marked expired_or_missing (404/410)."""

    def __init__(self, message: str, *, record: Optional[Dict[str, Any]] = None):
        super().__init__(message, record=record, code="expired_or_missing")


class InvalidTransitionError(TaskStoreError):
    """Raised when a state transition is disallowed by the state machine."""


def resolve_tasks_dir(data_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(data_dir).expanduser().resolve() if data_dir is not None else load_config().data_dir
    return ensure_real_subpath(root, root / "tasks")


def _iter_records(directory: Path) -> Iterator[Dict[str, Any]]:
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def submission_identity(record: dict | None) -> str | None:
    """Return the value a preflight must bind for an explicitly replaced attempt."""
    if record is None:
        return None
    if record.get("submission_id"):
        return record["submission_id"]
    for field in ("client_request_id", "remote_task_id", "remote_job_id", "chenyu_idempotency_key"):
        if record.get(field):
            return "legacy-" + sha256_bytes(f"{field}:{record[field]}".encode("utf-8"))
    legacy = [record.get(field) for field in ("task_key", "created_at", "request_digest")]
    return "legacy-" + sha256_bytes(json.dumps(legacy).encode("utf-8"))


def _job_history(record: dict | None) -> list[dict]:
    if record is None:
        return []
    history = [dict(entry) for entry in record.get("job_history", [])]
    job_id = record.get("remote_job_id") or record.get("remote_task_id")
    if not job_id:
        return history
    identity = submission_identity(record)
    delivered = record["state"] == "delivered"
    current = {
        "remote_job_id": job_id,
        "session_name": record.get("session_name"),
        "vm_host": record.get("vm_host"),
        "instance_uuid": record.get("instance_uuid") or record.get("managed_instance_id") or record.get("instance_id"),
        "status": record["state"],
        "delivered_path": record.get("artifact_path") if delivered else None,
        "artifact_sha256": record.get("artifact_sha256") if delivered else None,
        "submission_id": identity,
        "provider": record.get("provider"),
        "credential_fingerprint": record.get("credential_fingerprint"),
        "endpoint_origin": record.get("endpoint_origin"),
        "client_request_id": record.get("client_request_id"),
    }
    for index, entry in enumerate(history):
        if entry.get("submission_id") == identity and entry.get("remote_job_id") == job_id:
            history[index] = current
            break
    else:
        history.append(current)
    return history


def delivery_confirmed(job: dict) -> bool:
    """Check that a delivered job still has a matching local artifact."""
    path, digest = job.get("delivered_path"), job.get("artifact_sha256")
    if job.get("status") != "delivered" or not path or not digest:
        return False
    try:
        return file_sha256(path) == digest.lower()
    except OSError:
        return False


def list_pending_comfy_deliveries(
    data_dir: str | os.PathLike[str] | None = None, *, task_store: TaskStore | None = None,
    session_name: str | None = None, vm_host: str | None = None,
    instance_uuid: str | None = None, provider: str = "colab-comfy",
) -> list[dict]:
    store = task_store or TaskStore(data_dir=data_dir)
    return store.list_pending_comfy_deliveries(
        provider=provider, session_name=session_name, vm_host=vm_host, instance_uuid=instance_uuid,
    )


def comfy_job_delivery_state(
    remote_job_id: str, *, data_dir: str | os.PathLike[str] | None = None,
    task_store: TaskStore | None = None, provider: str = "colab-comfy",
    session_name: str | None = None, instance_uuid: str | None = None,
) -> str | None:
    store = task_store or TaskStore(data_dir=data_dir)
    job = store.find_remote_job(
        remote_job_id, provider=provider, session_name=session_name, instance_uuid=instance_uuid,
    )
    if job is None:
        return None
    return job["status"] if job["status"] != "delivered" or delivery_confirmed(job) else None


def normalize_logical_target(logical_target: str | os.PathLike[str]) -> str:
    """Normalize a logical target path while preserving full extension and version suffixes."""
    raw = str(logical_target or "").strip()
    if os.name == "nt":
        raw = os.path.normcase(raw)
    raw = raw.replace("\\", "/")
    if not raw:
        raise ValueError("logical_target 不能为空")
    parts: List[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("logical_target 不允许包含 '..'")
        parts.append(part)
    if not parts:
        raise ValueError("logical_target 无效")
    return "/".join(parts)


def make_task_key(
    logical_target: Optional[str | os.PathLike[str]] = None,
    *,
    job_key: Optional[str] = None,
) -> str:
    """Build a collision-free filesystem-safe task key.

    Keys derived from ``logical_target`` incorporate the full normalized path
    including extension and version suffix (e.g. ``k1.png`` vs ``k1.jpg`` vs
    ``k1_v2.png`` always produce distinct keys).
    """
    if job_key is not None:
        cleaned = os.path.normcase(str(job_key).strip())
        if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
            raise ValueError("无效 job_key")
        if _SAFE_KEY_RE.fullmatch(cleaned) and not _reserved_filename(cleaned) and not cleaned.endswith("."):
            return cleaned
        slug = _SLUG_RE.sub("_", cleaned).strip("._-")[:48] or "job"
        suffix = sha256_bytes(cleaned.encode("utf-8"))[:12]
        return f"{slug.replace('.', '_')}-{suffix}"

    if logical_target is None:
        raise ValueError("必须提供 logical_target 或 job_key")
    normalized = normalize_logical_target(logical_target)
    slug = _SLUG_RE.sub("_", normalized.replace("/", "_")).strip("._-").replace("..", "__")
    if len(slug) > 64:
        slug = slug[-64:].lstrip("._-") or "task"
    digest = sha256_bytes(normalized.encode("utf-8"))[:12]
    key = f"{slug or 'task'}-{digest}"
    return f"task-{key}" if _reserved_filename(key) else key


def _reserved_filename(key: str) -> bool:
    return bool(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", key.split(".")[0]))


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, newline=True, sort_keys=True, indent=2, allow_nan=False)


class TaskStore:
    """A namespaced JSON checkpoint store with cross-process mutation locks."""

    def __init__(
        self, tasks_dir: str | os.PathLike[str] | None = None, *,
        data_dir: str | os.PathLike[str] | None = None, config: EngineConfig | None = None,
        runtime_tokens: tuple[str, ...] = (),
    ) -> None:
        self.config = config or load_config(data_dir=data_dir)
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else self.config.data_dir
        if tasks_dir is None:
            self.tasks_dir = resolve_tasks_dir(self.data_dir)
        else:
            self.tasks_dir = Path(tasks_dir).expanduser().resolve()
            if data_dir is None and config is None:
                self.data_dir = self.tasks_dir.parent
        self._runtime_tokens = runtime_tokens

    def _validate_key(self, task_key: str) -> str:
        if (
            not isinstance(task_key, str) or not _SAFE_KEY_RE.fullmatch(task_key)
            or ".." in task_key or task_key.endswith(".") or _reserved_filename(task_key)
        ):
            raise ValueError("无效 task_key")
        return os.path.normcase(task_key)

    def task_path(self, task_key: str) -> Path:
        key = self._validate_key(task_key)
        return self.tasks_dir / f"{key}.json"

    def _lock_path(self, task_key: str) -> Path:
        key = self._validate_key(task_key)
        return self.tasks_dir / f".{key}.lock"

    @contextlib.contextmanager
    def lock(self, task_key: str, *, timeout: float = 30.0) -> Iterator[None]:
        """Acquire the task's mutation lock."""
        lock_file = self._lock_path(task_key)
        with file_lock(lock_file, timeout=timeout, timeout_message=f"获取任务锁超时：{task_key}"):
            yield

    def _read_unlocked(self, task_key: str) -> Optional[Dict[str, Any]]:
        path = self.task_path(task_key)
        if path.is_symlink():
            raise TaskStoreError("任务断点不能是符号链接")
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            raise TaskStoreError(f"损坏的任务断点文件：{path.name}") from None
        if not isinstance(data, dict) or data.get("state") not in VALID_STATES:
            raise TaskStoreError(f"损坏的任务断点文件：{path.name}")
        return data

    def _write_unlocked(self, task_key: str, record: Dict[str, Any]) -> Dict[str, Any]:
        if record.get("state") not in VALID_STATES:
            raise ValueError("无效任务状态")
        record["job_history"] = _job_history(record)
        safe_record = self.config.redact(record, *self._runtime_tokens)
        _atomic_write_json(self.task_path(task_key), safe_record)
        return safe_record

    @staticmethod
    def _apply_extra(record: dict, extra: dict | None) -> None:
        if not extra:
            return
        reserved = {"task_key", "provider", "submission_id", "client_request_id", "request_digest",
                    "logical_target", "session_name", "instance_id", "instance_uuid", "job_history",
                    "state", "remote_job_id", "remote_task_id", "artifact_path", "reserved_output_path",
                    "artifact_sha256", "replaced_submission_id", "credential_fingerprint", "endpoint_origin",
                    "vm_host", "created_at", "updated_at"}
        if reserved.intersection(extra):
            raise ValueError("附加元数据不能覆盖断点核心字段")
        record.update(extra)

    def _assert_transition(self, current_state: str, next_state: str) -> None:
        if next_state not in VALID_STATES:
            raise ValueError(f"无效目标状态：{next_state!r}")
        if next_state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
            raise InvalidTransitionError(f"非法状态转换：{current_state} -> {next_state}")

    def get(self, task_key: str) -> Optional[Dict[str, Any]]:
        """Read an atomically published snapshot without writing a lock or directory."""
        return self._read_unlocked(task_key)

    def list_tasks(
        self,
        *,
        provider: Optional[str] = None,
        states: Optional[set[str] | tuple[str, ...]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.tasks_dir.is_dir():
            return []
        state_filter = set(states) if states is not None else None
        results: List[Dict[str, Any]] = []
        for data in _iter_records(self.tasks_dir):
            if provider is not None and data.get("provider") != provider:
                continue
            if state_filter is not None and data.get("state") not in state_filter:
                continue
            results.append(data)
        results.sort(key=lambda item: item.get("created_at") or 0.0, reverse=True)
        return results

    def list_job_history(
        self, *, provider: str | None = None, session_name: str | None = None,
        instance_uuid: str | None = None, credential_fingerprint: str | None = None,
    ) -> list[dict]:
        """Return proof records for current and superseded jobs."""
        filters = {
            "provider": provider, "session_name": session_name, "instance_uuid": instance_uuid,
            "credential_fingerprint": credential_fingerprint,
        }
        return [
            {**entry, "task_key": record["task_key"]}
            for record in self.list_tasks() for entry in _job_history(record)
            if all(value is None or entry.get(field) == value for field, value in filters.items())
        ]

    def find_remote_job(
        self, remote_job_id: str, *, task_key: str | None = None, provider: str | None = None,
        session_name: str | None = None, instance_uuid: str | None = None,
        credential_fingerprint: str | None = None,
    ) -> dict | None:
        matches = [
            row for row in self.list_job_history(
                provider=provider, session_name=session_name, instance_uuid=instance_uuid,
                credential_fingerprint=credential_fingerprint,
            )
            if row["remote_job_id"] == remote_job_id and (task_key is None or row["task_key"] == task_key)
        ]
        if len(matches) > 1:
            raise TaskConflictError("远端作业 ID 对应多条记录，请指定任务和实例", code="remote_job_ambiguous")
        return matches[0] if matches else None

    def list_pending_comfy_deliveries(
        self, *, provider: str = "colab-comfy", session_name: str | None = None,
        instance_uuid: str | None = None, vm_host: str | None = None,
    ) -> list[dict]:
        pending = [
            row for row in self.list_job_history(
                provider=provider, session_name=session_name, instance_uuid=instance_uuid,
            )
            if (vm_host is None or row.get("vm_host") in (None, vm_host))
            and row["status"] not in {"failed", "cancelled"} and not delivery_confirmed(row)
        ]
        # Include current submissions whose remote ID has not been recovered yet.
        for record in self.list_tasks(provider=provider, states={"submitting", "submitted", "ambiguous"}):
            if record.get("remote_job_id") or record.get("remote_task_id"):
                continue
            if session_name is not None and record.get("session_name") != session_name:
                continue
            if instance_uuid is not None and (record.get("instance_uuid") or record.get("managed_instance_id")
                                              or record.get("instance_id")) != instance_uuid:
                continue
            if vm_host is not None and record.get("vm_host") not in (None, vm_host):
                continue
            pending.append(record)
        return pending

    def has_pending_comfy_deliveries(self, **filters: Any) -> bool:
        return bool(self.list_pending_comfy_deliveries(**filters))

    def mark_remote_job_delivered(
        self, task_key: str, remote_job_id: str, *, artifact_path: str | os.PathLike[str],
        artifact_sha256: str, instance_uuid: str | None = None,
        expected_submission_id: str | None = None,
    ) -> dict:
        """Persist verified delivery for a specific job without changing a newer attempt."""
        if not re.fullmatch(r"[a-fA-F0-9]{64}", artifact_sha256):
            raise ValueError("artifact_sha256 必须为 64 位十六进制摘要")
        path = Path(artifact_path).expanduser().resolve()
        digest = artifact_sha256.lower()
        if file_sha256(path) != digest:
            raise TaskStoreError("交付文件 SHA-256 与断点不符")
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            history = _job_history(record)
            matches = [
                row for row in history if row["remote_job_id"] == remote_job_id
                and (instance_uuid is None or row.get("instance_uuid") == instance_uuid)
                and (expected_submission_id is None or row.get("submission_id") == expected_submission_id)
            ]
            if len(matches) != 1:
                raise TaskConflictError("无法唯一定位该远端作业", code="remote_job_mismatch")
            job = matches[0]
            self._assert_transition(job["status"], "delivered")
            if (record.get("remote_job_id") or record.get("remote_task_id")) == remote_job_id and (
                job.get("submission_id") == submission_identity(record)
            ):
                now = time.time()
                record.update(state="delivered", artifact_path=str(path), artifact_sha256=digest,
                              reserved_output_path=str(path), delivered_at=now, updated_at=now, error=None)
            job.update(status="delivered", delivered_path=str(path), artifact_sha256=digest)
            record["job_history"] = history
            self._write_unlocked(task_key, record)
            return {**job, "task_key": task_key}

    def _check_replacement(
        self, task_key: str, existing: dict | None, *, force_new: bool, replaced_submission_id: str | None,
    ) -> None:
        if force_new and submission_identity(existing) != replaced_submission_id:
            raise TaskConflictError(
                f"任务 {task_key} 的新建审批已失效，请使用 yc-h3 jobs resume {task_key} 恢复原提交",
                code="replacement_approval_stale",
            )
        if not force_new and replaced_submission_id is not None:
            raise ValueError("replaced_submission_id 仅用于 --force-new")

    def _assert_submission_owner(
        self,
        task_key: str,
        record: Dict[str, Any],
        expected_submission_id: Optional[str],
    ) -> None:
        if expected_submission_id is None:
            return
        actual = submission_identity(record)
        if actual != expected_submission_id:
            raise TaskConflictError(
                f"任务 {task_key} 断点归属已变更，拒绝覆写",
                record=dict(record),
                code="submission_id_mismatch",
            )

    @staticmethod
    def _verify_reserved_delivery_files(
        record: Dict[str, Any],
    ) -> Optional[Path]:
        reserved = record.get("reserved_output_path")
        expected_sha = record.get("artifact_sha256")
        if not reserved or not expected_sha:
            return None
        target = Path(reserved)
        if not target.is_file():
            return None
        if file_sha256(target).lower() != str(expected_sha).lower():
            return None

        return target

    # ------------------------------------------------------------------
    # Delivery reservation & crash-recovery claim
    # ------------------------------------------------------------------
    def reserve_delivery(
        self,
        task_key: str,
        *,
        reserved_output_path: str | os.PathLike[str],
        artifact_sha256: str,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically persist reserved destination path and verified SHA-256 before final file publish."""
        if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", artifact_sha256):
            raise ValueError("artifact_sha256 必须为 64 位十六进制摘要")
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            self._assert_submission_owner(task_key, record, expected_submission_id)
            if record["state"] not in ("submitted", "submitting", "ambiguous"):
                raise InvalidTransitionError(f"当前状态 {record['state']} 不可预留交付路径")
            record["reserved_output_path"] = str(Path(reserved_output_path).expanduser().absolute())
            record["artifact_sha256"] = artifact_sha256.lower()
            record["updated_at"] = time.time()
            self._apply_extra(record, extra)
            return self._write_unlocked(task_key, record)

    def mark_delivered(
        self,
        task_key: str,
        *,
        artifact_path: str | os.PathLike[str],
        artifact_sha256: str,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark checkpoint as delivered after artifact atomic publish."""
        if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", artifact_sha256):
            raise ValueError("artifact_sha256 必须为 64 位十六进制摘要")
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            self._assert_submission_owner(task_key, record, expected_submission_id)
            self._assert_transition(record["state"], "delivered")
            resolved = str(Path(artifact_path).expanduser().resolve())
            record["state"] = "delivered"
            record["artifact_path"] = resolved
            record["reserved_output_path"] = record.get("reserved_output_path") or resolved
            record["artifact_sha256"] = artifact_sha256.lower()
            record["delivered_at"] = time.time()
            record["updated_at"] = record["delivered_at"]
            record["error"] = None
            self._apply_extra(record, extra)
            return self._write_unlocked(task_key, record)

    def update_delivered_metadata(self, task_key: str, *, extra: Dict[str, Any]) -> Dict[str, Any]:
        """Persist secondary artifact metadata without changing the primary delivery."""
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None or record["state"] != "delivered":
                raise TaskStoreError(f"任务尚未交付：{task_key}")
            self._apply_extra(record, extra)
            record["updated_at"] = time.time()
            return self._write_unlocked(task_key, record)

    def reconcile_delivery(self, task_key: str) -> Optional[Dict[str, Any]]:
        """Recover a published delivery while holding the checkpoint lock."""
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None or record["state"] == "delivered":
                return record
            return self._claim_reserved(task_key, record, check_transition=True)

    def _claim_reserved(
        self, task_key: str, record: Dict[str, Any], *, check_transition: bool = False,
    ) -> Optional[Dict[str, Any]]:
        target = self._verify_reserved_delivery_files(record)
        if target is None:
            return None
        if check_transition:
            self._assert_transition(record["state"], "delivered")
        now = time.time()
        record.update(state="delivered", artifact_path=str(target.resolve()), delivered_at=now,
                      updated_at=now, recovered_from_reserved_delivery=True, error=None)
        return self._write_unlocked(task_key, record)

    def _check_existing_ownership(
        self,
        task_key: str,
        existing: Dict[str, Any],
        *,
        expected_provider: str,
        normalized_target: str,
        force_new: bool,
    ) -> None:
        """Prevent cross-provider or cross-target collisions on an existing checkpoint."""
        existing_provider = existing.get("provider")
        existing_state = existing.get("state")
        if existing_provider and existing_provider != expected_provider:
            if not force_new or existing_state not in ("failed", "cancelled"):
                raise TaskConflictError(
                    f"跨 Provider 断点冲突：任务键 {task_key} 已归属 {existing_provider}（state={existing_state}），"
                    f"拒绝被 {expected_provider} 复用或覆写",
                    record=dict(existing),
                    code="provider_conflict",
                )
        existing_target = existing.get("logical_target")
        if existing_target and existing_target != normalized_target:
            if not force_new or existing_state not in ("failed", "cancelled"):
                raise TaskConflictError(
                    f"逻辑目标冲突：任务键 {task_key} 已绑定目标 {existing_target}，"
                    f"与当前 {normalized_target} 不一致",
                    record=dict(existing),
                    code="logical_target_conflict",
                )

    # ------------------------------------------------------------------
    # External API checkpoint semantics
    # ------------------------------------------------------------------
    def begin_external_submit(
        self,
        task_key: str,
        *,
        provider: str,
        strict_reuse: bool = False,
        request_digest: str,
        credential_fingerprint: str,
        endpoint_origin: str,
        logical_target: str,
        extra: Optional[Dict[str, Any]] = None,
        force_new: bool = False,
        replaced_submission_id: str | None = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Atomically inspect or initialize an external API checkpoint before POST.

        Returns ``(action, record)`` where ``action`` is one of:
        - ``"submit"``: freshly persisted ``state="submitting"``; caller may send POST.
        - ``"resume"``: existing ``state="submitted"`` with ``remote_task_id``; caller should poll.
        - ``"delivered"``: already delivered (or claimed via reserved path + SHA-256).

        Raises ``TaskConflictError`` on cross-provider, cross-target, or credential/origin mismatch.
        Raises ``AmbiguousTaskError`` when an existing checkpoint is ``ambiguous``
        or ``submitting`` without ``remote_task_id`` (unless ``force_new=True``).
        Raises ``ExpiredTaskError`` when existing checkpoint is ``expired_or_missing``
        (unless ``force_new=True``).
        """
        task_key = self._validate_key(task_key)
        normalized_target = normalize_logical_target(logical_target)
        with self.lock(task_key):
            existing = self._read_unlocked(task_key)
            self._check_replacement(task_key, existing, force_new=force_new, replaced_submission_id=replaced_submission_id)
            if existing is not None:
                self._check_existing_ownership(
                    task_key,
                    existing,
                    expected_provider=provider,
                    normalized_target=normalized_target,
                    force_new=force_new,
                )
            if existing is not None and not force_new:
                if strict_reuse and (
                    existing.get("credential_fingerprint") != credential_fingerprint
                    or existing.get("endpoint_origin") != endpoint_origin
                    or existing.get("request_digest") != request_digest
                ):
                    raise TaskConflictError(
                        f"任务 {task_key} 的凭据、源或请求摘要与现有断点不匹配",
                        record=dict(existing),
                        code="checkpoint_identity_mismatch",
                    )
                if strict_reuse and existing["state"] in ("failed", "cancelled"):
                    raise TaskConflictError(
                        f"任务 {task_key} 已终止，重新提交须显式使用 --force-new",
                        record=dict(existing),
                    )
                state = existing["state"]
                if state == "delivered":
                    return "delivered", dict(existing)
                claimed = self._claim_reserved(task_key, existing)
                if claimed is not None:
                    return "delivered", claimed
                if state == "submitted" and existing.get("remote_task_id"):
                    if (
                        existing.get("credential_fingerprint") != credential_fingerprint
                        or existing.get("endpoint_origin") != endpoint_origin
                    ):
                        raise TaskConflictError(
                            f"凭据或源不匹配：任务 {task_key} 原属 "
                            f"origin={existing.get('endpoint_origin')} fp={existing.get('credential_fingerprint')}，"
                            f"与当前 origin={endpoint_origin} fp={credential_fingerprint} 不一致，拒绝复用 remote_task_id",
                            record=dict(existing),
                            code="credential_or_origin_mismatch",
                        )
                    return "resume", dict(existing)
                if state == "submitting":
                    raise AmbiguousTaskError(
                        f"任务 {task_key} 上次 POST 提交未完成交付（state=submitting），"
                        "已拦截重提以防重复扣费；请核对账户与磁盘产物后使用 resume 或显式 --force-new",
                        record=dict(existing),
                    )
                if state == "submitted":
                    raise AmbiguousTaskError(
                        f"任务 {task_key} 处于异常 submitted 状态且缺少 remote_task_id，"
                        "已拦截重提以防重复扣费；请核对账户后使用 resume 或显式 --force-new",
                        record=dict(existing),
                    )
                if state == "ambiguous":
                    raise AmbiguousTaskError(
                        f"任务 {task_key} 处于结果不明状态（ambiguous），"
                        "已拦截重提以防重复扣费；请核对账户后使用 resume 或显式 --force-new",
                        record=dict(existing),
                    )
                if state == "expired_or_missing":
                    raise ExpiredTaskError(
                        f"任务 {task_key} 远端任务已过期或不存在（expired_or_missing），"
                        "断点已保留待确认，确认未扣费后请使用 --force-new",
                        record=dict(existing),
                    )
                # failed / cancelled -> allow creating a new attempt below

            now = time.time()
            record: Dict[str, Any] = {
                "task_key": task_key,
                "provider": provider,
                "state": "submitting",
                "submission_id": str(uuid.uuid4()),
                "replaced_submission_id": replaced_submission_id,
                "job_history": _job_history(existing),
                "request_digest": request_digest,
                "credential_fingerprint": credential_fingerprint,
                "endpoint_origin": endpoint_origin,
                "logical_target": normalized_target,
                "remote_task_id": None,
                "reserved_output_path": None,
                "artifact_path": None,
                "artifact_sha256": None,
                "created_at": now,
                "updated_at": now,
                "error": None,
            }
            if existing is not None and force_new:
                record["previous_state"] = existing.get("state")
                record["previous_remote_task_id"] = existing.get("remote_task_id")
            self._apply_extra(record, extra)
            return "submit", self._write_unlocked(task_key, record)

    def mark_external_submitted(
        self,
        task_key: str,
        remote_task_id: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: str | None = None,
    ) -> Dict[str, Any]:
        if not isinstance(remote_task_id, str) or not remote_task_id.strip():
            raise ValueError("remote_task_id 必须为非空字符串")
        return self._transition(task_key, "submitted", remote_task_id=remote_task_id.strip(), extra=extra,
                                expected_submission_id=expected_submission_id)

    # ------------------------------------------------------------------
    # ComfyUI checkpoint semantics
    # ------------------------------------------------------------------
    def begin_comfy_submit(
        self,
        task_key: str,
        *,
        request_digest: str,
        session_name: str,
        vm_host: str,
        instance_id: str,
        logical_target: str,
        instance_uuid: str | None = None,
        credential_fingerprint: str | None = None,
        endpoint_origin: str | None = None,
        provider: str = "colab-comfy",
        client_request_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        force_new: bool = False,
        replaced_submission_id: str | None = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Atomically persist stable UUID4 ``client_request_id`` and VM ownership before /api/jobs."""
        task_key = self._validate_key(task_key)
        normalized_target = normalize_logical_target(logical_target)
        with self.lock(task_key):
            existing = self._read_unlocked(task_key)
            self._check_replacement(task_key, existing, force_new=force_new, replaced_submission_id=replaced_submission_id)
            if existing is not None:
                self._check_existing_ownership(
                    task_key,
                    existing,
                    expected_provider=provider,
                    normalized_target=normalized_target,
                    force_new=force_new,
                )
            if existing is not None and not force_new:
                state = existing["state"]
                if state == "delivered":
                    return "delivered", dict(existing)
                claimed = self._claim_reserved(task_key, existing)
                if claimed is not None:
                    return "delivered", claimed
                if state in {"submitted", "submitting"} and (
                    existing.get("vm_host") != vm_host
                    or existing.get("session_name") not in (None, session_name)
                    or existing.get("instance_id") not in (None, instance_id)
                    or (instance_uuid is not None and
                        (existing.get("instance_uuid") or existing.get("managed_instance_id")) != instance_uuid)
                    or existing.get("credential_fingerprint") != credential_fingerprint
                    or existing.get("endpoint_origin") != endpoint_origin
                ):
                    raise TaskConflictError(
                        f"任务 {task_key} 的实例、凭据或源已变更，请恢复原实例",
                        code="vm_host_mismatch",
                    )
                if state == "submitted" and existing.get("remote_job_id"):
                    return "resume", dict(existing)
                if state == "submitting" and existing.get("client_request_id"):
                    # Stable client_request_id enables idempotent re-submission to same VM /api/jobs.
                    if existing.get("request_digest") == request_digest:
                        return "retry_idempotent", dict(existing)
                    raise AmbiguousTaskError(
                        f"任务 {task_key} 存在未完成提交且请求摘要已变更，已拦截重提",
                        record=dict(existing),
                    )
                if state in {"ambiguous", "submitting", "submitted"}:
                    raise AmbiguousTaskError(
                        f"任务 {task_key} 处于结果不明状态，请使用 jobs resume/fetch 或显式 --force-new",
                        record=dict(existing),
                    )
                if state == "expired_or_missing":
                    raise ExpiredTaskError(
                        f"任务 {task_key} 远端作业已过期或丢失，请确认后使用 --force-new",
                        record=dict(existing),
                    )
                if state in {"failed", "cancelled"}:
                    raise TaskConflictError(
                        f"任务 {task_key} 已终止，重新提交须显式使用 --force-new", record=dict(existing),
                    )

            now = time.time()
            req_id = client_request_id or str(uuid.uuid4())
            history = _job_history(existing)
            if existing is not None and (
                req_id == existing.get("client_request_id")
                or any(req_id == job.get("client_request_id") for job in history)
            ):
                raise TaskConflictError("新提交必须使用新的 client_request_id", code="client_request_id_reused")
            record: Dict[str, Any] = {
                "task_key": task_key,
                "provider": provider,
                "state": "submitting",
                "submission_id": str(uuid.uuid4()),
                "replaced_submission_id": replaced_submission_id,
                "job_history": history,
                "client_request_id": req_id,
                "request_digest": request_digest,
                "session_name": session_name,
                "vm_host": vm_host,
                "instance_id": instance_id,
                "instance_uuid": instance_uuid,
                "credential_fingerprint": credential_fingerprint,
                "endpoint_origin": endpoint_origin,
                "logical_target": normalized_target,
                "remote_job_id": None,
                "reserved_output_path": None,
                "artifact_path": None,
                "artifact_sha256": None,
                "perf_archived": False,
                "created_at": now,
                "updated_at": now,
                "error": None,
            }
            if existing is not None and force_new:
                record["previous_state"] = existing.get("state")
                record["previous_remote_job_id"] = existing.get("remote_job_id")
            self._apply_extra(record, extra)
            return "submit", self._write_unlocked(task_key, record)

    def mark_comfy_submitted(
        self,
        task_key: str,
        remote_job_id: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: str | None = None,
    ) -> Dict[str, Any]:
        if not isinstance(remote_job_id, str) or not remote_job_id.strip():
            raise ValueError("remote_job_id 必须为非空字符串")
        return self._transition(task_key, "submitted", remote_job_id=remote_job_id.strip(), extra=extra,
                                expected_submission_id=expected_submission_id)

    def mark_perf_archived(
        self,
        task_key: str,
        *,
        archived: bool = True,
        perf_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Independently record performance archival status (supports post-delivery backfill)."""
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            record["perf_archived"] = bool(archived)
            if perf_summary is not None:
                record["perf_summary"] = perf_summary
            record["updated_at"] = time.time()
            return self._write_unlocked(task_key, record)

    # ------------------------------------------------------------------
    # Shared terminal / exception transitions
    # ------------------------------------------------------------------
    def _transition(
        self, task_key: str, state: str, *, error: Optional[str] = None,
        expected_submission_id: Optional[str] = None, extra: Optional[Dict[str, Any]] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            self._assert_submission_owner(task_key, record, expected_submission_id)
            self._assert_transition(record["state"], state)
            now = time.time()
            if state == "submitted":
                record["submitted_at"] = now
            record.update(state=state, error=error, updated_at=now, **fields)
            self._apply_extra(record, extra)
            return self._write_unlocked(task_key, record)

    def cache_comfy_submit(self, task_key: str, payload: Dict[str, Any], *, expected_submission_id: str | None = None) -> None:
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is not None and record["state"] == "submitting":
                self._assert_submission_owner(task_key, record, expected_submission_id)
                record.update(cached_submit_payload=payload, updated_at=time.time())
                self._write_unlocked(task_key, record)

    def mark_ambiguous(
        self,
        task_key: str,
        *,
        reason: str = "提交结果不明",
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._transition(task_key, "ambiguous", error=reason, extra=extra,
                                expected_submission_id=expected_submission_id)

    def mark_expired_or_missing(
        self,
        task_key: str,
        *,
        reason: str = "远端任务返回 404/410",
        http_status: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Keep checkpoint on disk and mark as expired_or_missing (never delete)."""
        if http_status is not None:
            extra = {"last_http_status": int(http_status), **(extra or {})}
        return self._transition(task_key, "expired_or_missing", error=reason, extra=extra,
                                expected_submission_id=expected_submission_id)

    def mark_failed(
        self,
        task_key: str,
        *,
        error: str,
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._transition(task_key, "failed", error=error, extra=extra,
                                expected_submission_id=expected_submission_id)

    def record_cancel_error(self, task_key: str, *, error: str) -> Dict[str, Any]:
        """Keep the current state when remote cancellation cannot be confirmed."""
        with self.lock(task_key):
            record = self._read_unlocked(task_key)
            if record is None:
                raise TaskStoreError(f"断点不存在：{task_key}")
            record["cancel_error"] = error
            record["updated_at"] = time.time()
            return self._write_unlocked(task_key, record)

    def mark_cancelled(
        self,
        task_key: str,
        *,
        reason: str = "已取消",
        extra: Optional[Dict[str, Any]] = None,
        expected_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._transition(task_key, "cancelled", error=reason, extra=extra,
                                expected_submission_id=expected_submission_id)
