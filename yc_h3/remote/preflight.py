"""Strictly read-only ComfyUI preflight planning, GPU alias resolution, and status contracts.

Adapted from WorldForge (MIT License).
"""
from __future__ import annotations

import copy
import math
import shutil
from dataclasses import asdict
from pathlib import Path

if __package__:
    from . import _util, comfy_assets, setup_env
    from .workflows import registry
    from .workflows.image import inspect_image
else:
    import _util
    import comfy_assets
    import setup_env
    from workflows import registry
    from workflows.image import inspect_image

h3 = registry.h3
GIB = 1024**3
IMAGE_PATHS = ("image_path", "last_frame_path")
MEDIA_LISTS = ("images", "videos", "audios", "guides")
WORKLOAD_FIELDS = (
    "workflow_id",
    "runtime_profile",
    "gpu_name",
    "quant_profile",
    "effective_steps",
    "auto_keyframe",
    "upscale",
    "highvram",
    "resolution_bucket",
    "frames_bucket",
)

VALID_PREFLIGHT_STATUSES = ("pass", "warn", "block")

# Default simulated GPU profiles supporting both short names and manifest card keys.
SIMULATED_GPU_PROFILES = {
    "G4": (96.0, 175.0, "G4"),
    "G4-96": (96.0, 175.0, "G4"),
    "H100": (80.0, 85.0, "H100"),
    "H100-80": (80.0, 85.0, "H100"),
    "A100": (80.0, 85.0, "A100"),
    "A100-80": (80.0, 85.0, "A100"),
    "A100-40": (40.0, 52.0, "A100"),
    "L4": (24.0, 52.0, "L4"),
    "L4-24": (24.0, 52.0, "L4"),
    "T4": (16.0, 52.0, "T4"),
    "T4-16": (16.0, 52.0, "T4"),
}


def normalize_preflight_status(status) -> str:
    """Normalize preflight status to ``pass | warn | block``; unknown values map conservatively to ``block``."""
    if isinstance(status, str) and status in VALID_PREFLIGHT_STATUSES:
        return status
    return "block"


def resolve_simulated_gpu(value):
    """Resolve ``--simulated-gpu`` / ``simulated_gpu`` from short names, manifest card keys, or dicts.

    Returns ``(HardwareProfile, vram_free_gb_or_None)``.
    """
    if isinstance(value, str):
        key = value.strip().upper()
        if key in SIMULATED_GPU_PROFILES:
            vram, ram, gpu_name = SIMULATED_GPU_PROFILES[key]
            return h3.resolve_hardware_profile(vram, ram, gpu_name), None
        raise ValueError(
            "simulated_gpu 需为已知卡型（" + "/".join(SIMULATED_GPU_PROFILES) + "）或包含 gpu_name/vram_total_gb/host_ram_gb 的对象"
        )
    if not isinstance(value, dict) or not isinstance(value.get("gpu_name"), str) or not value["gpu_name"]:
        raise ValueError("simulated_gpu 需包含 gpu_name、vram_total_gb、host_ram_gb")
    vram = value.get("vram_total_gb", value.get("vram_gb"))
    ram = value.get("host_ram_gb")
    free = value.get("vram_free_gb")
    if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in (vram, ram)) or (
        free is not None and (type(free) not in (int, float) or not 0 <= free <= vram)
    ):
        raise ValueError("simulated_gpu 的显存与 RAM 必须为非负数")
    return h3.resolve_hardware_profile(float(vram), float(ram), value["gpu_name"]), free


def media_slots(params):
    for key in IMAGE_PATHS:
        if params.get(key):
            yield key, params[key], params, key
    for key in MEDIA_LISTS:
        for index, entry in enumerate(params.get(key) or []):
            if isinstance(entry, dict) and "path" in entry:
                yield f"{key}.{index}", entry["path"], entry, "path"


def _input_facts(params, root, supplied, inspect_first, *, single_frame):
    canonical = copy.deepcopy(params)
    canonical.pop("first_frame_path", None)
    identities, first_size = {}, None
    checked = {}
    for slot, path, parent, key in media_slots(canonical):
        if not isinstance(path, str) or not path:
            raise ValueError("素材路径必须是非空字符串")
        if path not in checked:
            try:
                if path in supplied:
                    row = supplied[path]
                    if not isinstance(row, dict):
                        raise ValueError("input_facts 条目必须是对象")
                    digest = comfy_assets._sha(row.get("sha256"))
                    checked[path] = (digest, row)
                else:
                    remote = h3.input_path(path, root)
                    checked[path] = (comfy_assets.file_sha256(remote), None)
            except OSError as error:
                raise ValueError("无法读取输入素材") from error
        digest, row = checked[path]
        parent[key] = digest
        identities[slot] = digest
        if slot == "image_path" and inspect_first:
            first_size = row if row is not None else inspect_image(path, single_frame=single_frame)
            if any(type(first_size.get(k)) is not int or first_size[k] < 1 for k in ("width", "height")):
                raise ValueError("input_facts 需要 EXIF 矫正后的正整数宽高")
    return canonical, identities, first_size


def _card_key(profile):
    name = profile.gpu_name.upper()
    for gpu in ("G4", "H100", "A100", "L4", "T4"):
        if gpu in name or (gpu == "G4" and "RTX PRO 6000" in name and "BLACKWELL" in name):
            gb = next((value for value in (96, 80, 40, 24, 16) if abs(profile.vram_gb - value) <= 3), None)
            return f"{gpu}-{gb}" if gb is not None else None
    return None


def _free_space(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _workload(workflow, profile, params, assets, task, size, highvram):
    upscale = bool(params.get("upscale"))
    frames = h3.output_frames(params)
    steps = workflow.steps(params, assets)
    auto_keyframe = task in ("i2v", "t2v") and not params.get("image_path") and bool(params.get("auto_keyframe"))
    return dict(
        zip(
            WORKLOAD_FIELDS,
            (
                workflow.id,
                workflow.profile_id,
                profile.gpu_name,
                sorted(assets),
                steps,
                auto_keyframe,
                upscale,
                highvram,
                list(size) if size else None,
                frames,
            ),
        )
    )


def _signal(signals, severity, code, **facts):
    signals.append({"severity": normalize_preflight_status(severity), "code": code, **facts})


def normalize_billing_context(billing_context=None):
    """Normalize billing & target identity context bound into generation approval digests."""
    ctx = billing_context if isinstance(billing_context, dict) else {}
    rate = ctx.get("billing_rate")
    if rate is None:
        rate = "unknown"
    return {
        "provider_id": str(ctx.get("provider_id") or "colab-comfy"),
        "endpoint_origin": str(ctx.get("endpoint_origin") or "http://127.0.0.1"),
        "session_name": ctx.get("session_name"),
        "vm_host": ctx.get("vm_host"),
        "billing_rate": rate,
    }


def _history_warnings(signals, history, key, verified, card=None):
    row = {}
    comparable = 0
    partial = 0
    if history is not None:
        if not isinstance(history, list):
            raise ValueError("local_history 必须是列表")
        row = next((item for item in history if isinstance(item, dict) and item.get("workload_key") == key), {})
        comparable = row.get("comparable_samples", 0)
        partial = row.get("partial_load_samples", 0)
        if type(comparable) is not int or type(partial) is not int or not 0 <= partial <= comparable:
            raise ValueError("无效可比历史样本数")
    # Unverified GPU card in manifest ALWAYS retains unverified_gpu warning regardless of
    # whether local_history is omitted, empty, or contains comparable samples.
    if not verified:
        _signal(
            signals,
            "warn",
            "unverified_gpu",
            gpu_name=key["gpu_name"],
            card=card,
            comparable_samples=comparable,
        )
    if history is None:
        return None
    if partial:
        _signal(
            signals,
            "warn",
            "history_partial_load_seen",
            comparable_samples=comparable,
            partial_load_samples=partial,
            partial_load_median_total_s=row.get("partial_load_median_total_s"),
        )
    return row or {
        "workload_key": key,
        "comparable_samples": 0,
        "partial_load_samples": 0,
        "cold_start": {"samples": 0},
        "warm_run": {"samples": 0},
    }


def evaluate_preflight(
    *,
    instance_id,
    profile,
    model,
    workflow,
    task,
    params,
    input_root,
    read_assets,
    vram_free_gb=None,
    input_facts=None,
    local_history=None,
    simulated=False,
    billing_context=None,
    check_runtime=True,
):
    """Return strictly read-only fact pack and normalized parameters."""
    signals = []
    vram = min(profile.vram_gb, vram_free_gb) if vram_free_gb is not None else profile.vram_gb
    params = workflow.normalize(task, params, profile, root=None if input_facts else input_root, enforce_hardware=False)
    supplied = input_facts if input_facts is not None else {}
    if not isinstance(supplied, dict):
        raise ValueError("input_facts 必须是对象")
    inspect_first = task == "i2v"
    canonical, hashes, first = _input_facts(params, input_root, supplied, inspect_first, single_frame=False)
    if not supplied and task not in ("download", "load"):
        h3.inspect_references(params)
    asset_table = read_assets()
    try:
        params = comfy_assets.freeze_custom_assets(params, lambda: asset_table)
    except comfy_assets.MissingCustomAssets as error:
        _signal(signals, "block", "custom_asset_missing", asset_ids=error.ids)
    frozen = params.get("_frozen_custom_assets", {})
    assets = workflow.assets(profile, params)
    if set(frozen) & set(assets):
        raise ValueError("自定义 LoRA id 与工作流内置资产冲突")

    highvram = workflow.highvram(profile, params)
    effective = h3.warmup_params(params) if task == "load" else params
    width, height = effective.get("width"), effective.get("height")
    size = (width, height) if width and height else None

    if task != "download":
        low_vram = profile.vram_gb < 20
        if vram <= 0 or low_vram and not params.get("allow_low_vram") or vram < 20 <= profile.vram_gb:
            _signal(
                signals,
                "block",
                "vram_insufficient",
                minimum_gib=20,
                available_gib=vram,
                allow_low_vram=params.get("allow_low_vram", False),
            )
        ram_min = profile.min_ram_gb
        resolution_error = None
        try:
            h3.check_resolution_for_profile(workflow.id, profile, params["width"], params["height"])
        except ValueError as error:
            resolution_error = str(error)
        if params["seconds"] > profile.max_seconds or resolution_error:
            max_width, max_height = h3.resolution_limits(workflow.id, profile)
            if workflow.id in h3.JINYU_WORKFLOWS:
                max_width = max_height = 1664 if profile.tier <= 2 else 1024
            _signal(
                signals,
                "block",
                "vram_insufficient",
                tier=profile.tier,
                max_seconds=profile.max_seconds,
                max_width=max_width,
                max_height=max_height,
                available_gib=vram,
                **({"detail": resolution_error} if resolution_error else {}),
            )
        if profile.tier >= 3:
            _signal(
                signals,
                "warn",
                "tier_degraded",
                tier=profile.tier,
                effective_steps=workflow.steps(params, assets),
            )
        if profile.tier >= 2:
            _signal(signals, "warn", "cpu_offload_expected", tier=profile.tier)
        if highvram and vram < 90:
            _signal(signals, "warn", "vram_highvram_tight", available_gib=vram)
        if task != "load" and params["seconds"] > 8:
            _signal(
                signals,
                "warn",
                "long_duration_quadratic",
                seconds=params["seconds"],
                frames=h3.output_frames(params),
            )
        if first:
            source_ratio = first["width"] / first["height"]
            output_ratio = width / height
            delta = abs(source_ratio / output_ratio - 1)
            if delta > 0.03:
                _signal(
                    signals,
                    "warn",
                    "aspect_mismatch",
                    input_width=first["width"],
                    input_height=first["height"],
                    output_width=width,
                    output_height=height,
                    relative_difference=round(delta, 5),
                )
        if ram_min and profile.host_ram_gb < ram_min:
            _signal(
                signals,
                "block",
                "host_ram_insufficient",
                minimum_gib=ram_min,
                total_gib=profile.host_ram_gb,
                high_mem_option="--high-mem",
            )

    disk = setup_env.COMFY_MODELS_DIR
    free = _free_space(disk)
    missing = {key: asset["size_bytes"] for key, asset in assets.items() if not comfy_assets.weight_ready(asset, disk)}
    for key, asset in frozen.items():
        if asset["refresh"] or not comfy_assets.weight_ready(comfy_assets.custom_cache_spec(asset, disk), disk):
            missing[key] = asset["size_bytes"] or comfy_assets.CUSTOM_ASSET_MAX_BYTES
    needed = sum(missing.values())
    if needed:
        needed += comfy_assets.HEADROOM
    if free < needed or (free == 0 and task != "download"):
        _signal(signals, "block", "disk_insufficient", required_bytes=needed, free_bytes=free)

    if check_runtime and not simulated:
        runtime_ready = setup_env.comfy_runtime_installed(workflow.profile_id, workflow.revision)
        required_plugins = workflow.plugins(profile, params) if task != "download" else ()
        missing_plugins = [p for p in required_plugins if not setup_env.custom_node_installed(p, workflow.profile_id)]
        if not runtime_ready or missing_plugins:
            _signal(
                signals,
                "block",
                "runtime_or_plugins_not_ready",
                profile_id=workflow.profile_id,
                revision=workflow.revision,
                runtime_installed=runtime_ready,
                missing_plugins=missing_plugins,
                hint="请先通过经审批的 setup 流程安装 ComfyUI 运行时与所需插件",
            )

    key = _workload(workflow, profile, effective, assets, task, size, highvram)
    card = _card_key(profile)
    verified = workflow.manifest.get("verification", {}).get(card) == "verified"
    history = _history_warnings(signals, local_history, key, verified, card=card) if task != "download" else None
    warning_codes = sorted({item["code"] for item in signals if item["severity"] == "warn"})
    bound_billing = normalize_billing_context(billing_context)
    digest = _util.sha256_bytes(
        comfy_assets.canonical_json(
            (
                bound_billing,
                instance_id,
                profile.gpu_name,
                profile.vram_gb,
                model["id"],
                key["workflow_id"],
                key["runtime_profile"],
                task,
                canonical,
                hashes,
                frozen,
                warning_codes,
            )
        )
    )
    raw_status = "block" if any(item["severity"] == "block" for item in signals) else "warn" if warning_codes else "pass"
    status = normalize_preflight_status(raw_status)
    facts = {
        "status": status,
        "instance_id": instance_id,
        "billing_context": bound_billing,
        "model_id": model["id"],
        "task": task,
        "workflow_id": key["workflow_id"],
        "runtime_profile": key["runtime_profile"],
        "simulated_gpu": simulated,
        "hardware": {**asdict(profile), "vram_total_gb": profile.vram_gb, "vram_available_gb": vram},
        "signals": signals,
        "effective_params": canonical,
        "input_content_sha256s": hashes,
        "disk": {"free_bytes": free, "required_bytes": needed, "missing_weights": missing},
        "custom_asset_ids": sorted(frozen),
        "custom_assets_sha256": _util.sha256_bytes(comfy_assets.canonical_json(asset_table)),
        "workload_key": key,
        "manifest_verified": verified,
        "verification_card": card,
        "warning_codes": warning_codes,
        "preflight_digest": digest,
    }
    if history is not None:
        facts["perf"] = history
    return facts, params
