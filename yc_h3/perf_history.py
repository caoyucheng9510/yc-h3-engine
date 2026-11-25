"""Local, de-identified performance observations and read-only quotes."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from .config import EngineConfig
from .fsutil import atomic_write_bytes, ensure_real_subpath, file_lock

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")
_OBSERVATION_KEY = re.compile(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?")
_HARDWARE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]*")
_UNITS = ("point", "CNY", "CU")
_MANAGED_COMFY = {"colab-comfy", "chenyu-comfy"}


def _path(config: EngineConfig) -> Path:
    return ensure_real_subpath(config.data_dir, config.data_dir / "perf" / "observations.jsonl")


def _number(value, *, allow_zero=False):
    if type(value) not in (int, float):
        raise ValueError("性能观测数值必须为有限数")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("性能观测数值必须为有限数") from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError("性能观测数值超出有效范围")
    return number


def _validate(row: dict) -> dict:
    timestamp = datetime.fromisoformat(row["ts"])
    if timestamp.tzinfo is None:
        raise ValueError("性能观测时间须含时区")
    for key, pattern in (
        ("idempotency_key", _OBSERVATION_KEY), ("provider", _IDENTIFIER), ("workflow", _IDENTIFIER),
    ):
        if not isinstance(row[key], str) or not pattern.fullmatch(row[key]):
            raise ValueError(f"性能观测的 {key} 须为标识符")
    hardware = row["hardware"]
    if hardware is not None and (not isinstance(hardware, str) or not _HARDWARE.fullmatch(hardware.strip())):
        raise ValueError("性能观测硬件须为卡型名称")
    for key in ("width", "height"):
        if row[key] is not None and (type(row[key]) is not int or row[key] <= 0):
            raise ValueError("性能观测画面尺寸须为正整数")
    for key in ("has_ref_video", "estimated_billing", "cold_start"):
        if type(row[key]) is not bool:
            raise ValueError(f"性能观测的 {key} 须为布尔值")
    if row["unit"] is not None and row["unit"] not in _UNITS:
        raise ValueError("性能观测计量单位无效")

    return {
        "ts": timestamp.astimezone(timezone.utc).isoformat(),
        "idempotency_key": row["idempotency_key"],
        "provider": row["provider"],
        "workflow": row["workflow"],
        "hardware": hardware.strip() if hardware is not None else None,
        "width": row["width"],
        "height": row["height"],
        "output_seconds": _number(row["output_seconds"]),
        "has_ref_video": row["has_ref_video"],
        "run_seconds": _number(row["run_seconds"]) if row["run_seconds"] is not None else None,
        "wall_seconds": _number(row["wall_seconds"]) if row["wall_seconds"] is not None else None,
        "billed_quantity": (_number(row["billed_quantity"], allow_zero=True)
                            if row["billed_quantity"] is not None else None),
        "unit": row["unit"],
        "estimated_billing": row["estimated_billing"],
        "cold_start": row["cold_start"],
    }


def _observations(raw: bytes):
    for line in raw.splitlines():
        try:
            yield _validate(json.loads(line))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            continue


def record_observation(
    config: EngineConfig, *, idempotency_key: str, provider: str, workflow: str,
    output_seconds: float, hardware: str | None = None, width: int | None = None,
    height: int | None = None, has_ref_video: bool = False, run_seconds: float | None = None,
    wall_seconds: float | None = None, billed_quantity: float | None = None,
    unit: str | None = None, estimated_billing: bool = True, cold_start: bool = False,
) -> None:
    """Write one observation per remote job, using only typed calibration fields."""
    fields = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": idempotency_key,
        "provider": provider,
        "workflow": workflow,
        "hardware": hardware,
        "width": width,
        "height": height,
        "output_seconds": output_seconds,
        "has_ref_video": has_ref_video,
        "run_seconds": run_seconds,
        "wall_seconds": wall_seconds,
        "billed_quantity": billed_quantity,
        "unit": unit,
        "estimated_billing": estimated_billing,
        "cold_start": cold_start,
    }
    sanitized = config.redact(fields)
    if sanitized != fields:
        raise ValueError("性能观测不得含凭据")
    row = _validate(sanitized)
    line = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
    path = _path(config)
    with file_lock(path.with_name(f".{path.name}.lock")):
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = b""
        if any(item["idempotency_key"] == idempotency_key for item in _observations(previous)):
            return
        separator = b"\n" if previous and not previous.endswith(b"\n") else b""
        atomic_write_bytes(path, previous + separator + line)


def load_observations(config: EngineConfig, *, provider: str | None = None) -> list[dict]:
    """Read an atomic snapshot, skipping invalid rows without acquiring a write lock."""
    try:
        raw = _path(config).read_bytes()
    except (OSError, ValueError):
        return []
    return [row for row in _observations(raw) if provider is None or row["provider"] == provider]


def elapsed_seconds(finished, *starts) -> float | None:
    """Use the first valid start time, accepting UNIX timestamps and zoned ISO strings."""
    def timestamp(value):
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            value = parsed.timestamp() if parsed.tzinfo is not None else None
        return _number(value, allow_zero=True)

    try:
        end = timestamp(finished)
    except (OSError, ValueError, OverflowError):
        return None
    for start in starts:
        try:
            return _number(end - timestamp(start))
        except (OSError, ValueError, OverflowError):
            continue
    return None


def _scaled_median(values, scale=1.0, *, digits=1) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    estimate = median(finite) * scale
    return round(estimate, digits) if math.isfinite(estimate) else None


def estimate_from_history(
    config: EngineConfig, *, provider: str, workflow: str, output_seconds: float,
    has_ref_video: bool = False, hardware: str | None = None, recent_limit: int = 20,
) -> dict | None:
    duration = _number(output_seconds)
    card = hardware.strip().casefold() if hardware else ""
    if recent_limit <= 0 or (provider in _MANAGED_COMFY and not card):
        return None
    rows = [row for row in load_observations(config, provider=provider)
            if row["workflow"] == workflow and row["has_ref_video"] is has_ref_video and not row["cold_start"]]
    if card:
        matching = [row for row in rows if (row["hardware"] or "").casefold() == card]
        if matching or provider in _MANAGED_COMFY:
            rows = matching
    rows = sorted(rows, key=lambda row: row["ts"])[-recent_limit:]
    if not rows:
        return None

    run = _scaled_median((row["run_seconds"] / row["output_seconds"]
                          for row in rows if row["run_seconds"] is not None), duration)
    wall_samples = [row for row in rows if row["wall_seconds"] is not None]
    wall = (_scaled_median(row["wall_seconds"] for row in wall_samples) if provider == "chenyu"
            else _scaled_median((row["wall_seconds"] / row["output_seconds"] for row in wall_samples), duration))
    billed = [row for row in rows if not row["estimated_billing"] and row["billed_quantity"] is not None]
    per_run = [row["billed_quantity"] / row["run_seconds"]
               for row in billed if run is not None and row["run_seconds"] is not None]
    billing_rates = [rate for rate in per_run if math.isfinite(rate)]
    if billing_rates:
        billing_scale = run
    else:
        billing_scale = duration
        billing_rates = [rate for row in billed
                         if math.isfinite(rate := row["billed_quantity"] / row["output_seconds"])]
    return {
        "samples": len(rows),
        "estimated_run_seconds": run,
        "estimated_wall_seconds": wall,
        "estimated_quantity": _scaled_median(billing_rates, billing_scale, digits=4),
        "billing_samples": len(billing_rates),
    }
