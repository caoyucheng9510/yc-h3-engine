"""Host-local admission controls for account-scoped external jobs."""
from __future__ import annotations

import math
import re
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Iterator

from ..config import EngineConfig, load_config
from ..fsutil import file_lock
from ..tasks_store import VALID_STATES, TaskConflictError, TaskStore, normalize_logical_target

from .base import ProviderError, canonical_json_bytes, sha256_bytes


def resolve_positive_int_limit(
    env_name: str, explicit: int | None, *, default: int, minimum: int = 1, maximum: int | None = None,
    config: EngineConfig | None = None,
) -> int:
    if explicit is not None:
        value = explicit
    elif (raw := (config or load_config()).get(env_name)) is not None:
        value = raw.strip()
        if re.fullmatch(r"[0-9]+", value):
            try:
                value = int(value)
            except ValueError:
                pass
    else:
        value = default
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f"至少 {minimum}"
        raise ValueError(f"{env_name} 或对应构造参数必须是整数（{bounds}）")
    return value


def count_active_tasks(
    store: TaskStore, *, provider: str, endpoint_origin: str, credential_fingerprint: str,
) -> int:
    """Count visible active checkpoints for the selected account without taking locks."""
    return sum(
        record.get("endpoint_origin") == endpoint_origin
        and record.get("credential_fingerprint") == credential_fingerprint
        for record in store.list_tasks(provider=provider, states={"submitting", "submitted", "ambiguous"})
    )


class ProviderSlots:
    def __init__(
        self, provider: str, endpoint_origin: str, credential_fingerprint: str, *, max_slots: int,
        sleep_fn: Callable[[float], None], poll_interval_sec: float, wait_timeout_sec: float | None,
        data_dir: Path | str | None = None, config: EngineConfig | None = None,
    ) -> None:
        if not math.isfinite(poll_interval_sec) or poll_interval_sec <= 0:
            raise ValueError("slot_poll_interval_sec 必须是有限正数")
        if wait_timeout_sec is not None and (not math.isfinite(wait_timeout_sec) or wait_timeout_sec < 0):
            raise ValueError("slot_wait_timeout_sec 必须是有限非负数")
        scope = sha256_bytes(canonical_json_bytes((provider, endpoint_origin, credential_fingerprint)))[:24]
        self._directory = (config or load_config(data_dir=data_dir)).data_dir / "locks" / scope
        self._provider = provider
        self._max_slots = max_slots
        self._sleep = sleep_fn
        self._poll_interval = poll_interval_sec
        self._wait_timeout = wait_timeout_sec

    def _try_acquire(self, held_slot: ExitStack) -> bool:
        for index in range(self._max_slots):
            path = self._directory / f"slot-{index:02d}.lock"
            with ExitStack() as candidate:
                try:
                    candidate.enter_context(file_lock(path, timeout=0))
                except TimeoutError:
                    continue
                held_slot.enter_context(candidate.pop_all())
                return True
        return False

    @contextmanager
    def lock_for_submission(
        self, store: TaskStore, task_key: str, operation_lock: str, *, logical_target: str,
        force_new: bool, held_slot: ExitStack,
    ) -> Iterator[tuple[dict | None, bool]]:
        """Lock the task for begin/submit and retain any new slot in the caller's stack for polling."""
        deadline = None if self._wait_timeout is None else time.monotonic() + self._wait_timeout
        while True:
            with store.lock(operation_lock):
                existing = store.get(task_key)
                if existing is not None:
                    state = existing.get("state")
                    if not force_new:
                        if state not in VALID_STATES:
                            raise TaskConflictError(f"任务 {task_key} 的断点状态无效", record=existing)
                        # For an existing valid record, strict begin either reuses it or raises.
                        yield existing, False
                        return
                    store._check_existing_ownership(
                        task_key, existing, expected_provider=self._provider,
                        normalized_target=normalize_logical_target(logical_target), force_new=force_new,
                    )
                if self._try_acquire(held_slot):
                    yield existing, True
                    return

            # Pollers and recovery also take the task lock. Recheck after waiting with it released.
            if deadline is None:
                self._sleep(self._poll_interval)
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._sleep(min(remaining, self._poll_interval))
                if time.monotonic() < deadline:
                    continue
            raise ProviderError(f"{self._provider} 等待本地并发槽位超时", code="local_concurrency_timeout", exit_code=3)

    @contextmanager
    def lock_admission(self) -> Iterator[None]:
        """Serialize the active-checkpoint count and new checkpoint write after taking a slot."""
        with file_lock(self._directory / "admission.lock"):
            yield
