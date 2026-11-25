"""Portable MiniMax-H3 workflow names, hardware limits and frame geometry."""
from __future__ import annotations

import os
from dataclasses import dataclass

if __package__.startswith("workflows."):
    from presets import apply_no_music_policy
else:
    from ...presets import apply_no_music_policy

FPS = 24
JINYU_WORKFLOWS = ("jinyu-hybrid", "jinyu-combat")
DEFAULT_WORKFLOW = JINYU_WORKFLOWS[0]
JINYU_ASPECT_SIZES = {"16:9": (1664, 928), "9:16": (928, 1664), "1:1": (1248, 1248)}
JINYU_MAX_PIXELS_HIGH = 1_572_864
COMFY_MAIN = "comfy-main"
COMFY_REVISION = "c59e67bd5a2757ddba1c6c6a993c0b6c811b54da"


def workflow_id(value=None, available=JINYU_WORKFLOWS):
    name = DEFAULT_WORKFLOW if value is None else value
    if isinstance(name, str):
        for prefix in ("h3-", "chenyu-"):
            if name.startswith(prefix) and name.removeprefix(prefix) in JINYU_WORKFLOWS:
                name = name.removeprefix(prefix)
                break
    if not isinstance(name, str) or name not in available:
        raise ValueError(f"未知工作流 {name!r}，可选：{', '.join(available)}")
    return name


@dataclass(frozen=True)
class HardwareProfile:
    tier: int
    gpu_name: str
    vram_gb: float
    host_ram_gb: float
    width: int
    height: int
    max_seconds: int
    min_ram_gb: int = 0
    rejection: str | None = None
    highvram_safe: bool = False


def resolve_hardware_profile(vram_gb, host_ram_gb, gpu_name):
    if vram_gb >= 75:
        return HardwareProfile(1, gpu_name, vram_gb, host_ram_gb, 1344, 768, 15, highvram_safe=vram_gb >= 90)
    if vram_gb >= 36:
        tier, width, height, duration, min_ram = 2, 1344, 768, 15, 48
    else:
        tier, width, height, duration, min_ram = (3 if vram_gb >= 20 else 4), 1024, 544, 8, 40
    rejection = None
    if tier == 4:
        rejection = "MiniMax-H3 需要至少 20 GiB 显存，低显存试验须显式设置 allow_low_vram=True"
    elif host_ram_gb < min_ram:
        rejection = f"Tier {tier} 需要至少 {min_ram} GiB 主机 RAM（当前 {host_ram_gb:.1f} GiB），请换用 --high-mem 或更高档实例"
    return HardwareProfile(tier, gpu_name, vram_gb, host_ram_gb, width, height, duration, min_ram, rejection)


def resolution_limits(workflow, profile):
    if profile is None:
        return None
    return (1024, 544) if profile.tier >= 3 else (1664, 928)


def check_resolution_for_profile(workflow, profile, width, height):
    max_edge, max_pixels = (1024, 1024 * 544) if profile and profile.tier >= 3 else (1664, JINYU_MAX_PIXELS_HIGH)
    if max(width, height) > max_edge or width * height > max_pixels:
        raise ValueError(f"当前档位最长边上限 {max_edge}，总像素上限 {max_pixels}")


def jinyu_size(params, sizes=JINYU_ASPECT_SIZES, default_aspect="16:9"):
    aspect = params.get("aspect", default_aspect)
    if not isinstance(aspect, str) or aspect not in sizes:
        raise ValueError("画幅只支持 " + ", ".join(sizes))
    if "width" in params or "height" in params:
        if "aspect" in params:
            raise ValueError("aspect 与 width/height 不能同时使用")
        size = params.get("width"), params.get("height")
        if any(type(value) is not int or value <= 0 or value % 32 for value in size):
            raise ValueError("width 与 height 须同时提供，且为 32 的正整数倍")
        return aspect, size
    return aspect, tuple(sizes[aspect])


def snap_up_17k5(frames):
    return max(5, frames + (5 - frames) % 17)


def snap_down_17k5(frames):
    frames = int(frames)
    if frames < 5:
        raise ValueError("参考视频至少需要 5 帧 @ 24fps")
    return frames - (frames - 5) % 17


def output_frames(params):
    raw = max(5, round(params["seconds"] * FPS))
    return snap_up_17k5(raw)


def trim_frames(params):
    if not params.get("trim_to_requested_seconds"):
        return None
    return max(1, int(params["seconds"] * FPS))


def _media_item(value):
    if isinstance(value, (str, os.PathLike)):
        return {"path": str(value)}
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not value["path"]:
        raise ValueError("参考素材须为路径或带 path 的对象")
    return dict(value)


def normalize_request(task, params, profile=None, *, enforce_hardware=True,
                      available=JINYU_WORKFLOWS, tasks=("t2v", "i2v", "r2v")):
    """Validate workflow parameters without reading media or probing hardware."""
    if not isinstance(params, dict):
        raise ValueError("params 必须是 JSON 对象")
    result = dict(params)
    workflow = result["workflow"] = workflow_id(result.get("workflow"), available)
    if "combat_mode" in result:
        raise ValueError("combat_mode 已废弃，请使用 --workflow jinyu-combat")
    result.setdefault("text_encoder", "heretic")
    result.setdefault("use_selflift", True)
    result.setdefault("speed_slider", False)
    _, size = jinyu_size(result)
    result.pop("aspect", None)
    result["width"], result["height"] = size
    check_resolution_for_profile(workflow, profile if enforce_hardware else None, *size)
    for key in ("prompt", "cinema_camera"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError(f"{key} 必须是字符串")
    seed = result.get("seed")
    if seed is None or seed == "" or type(seed) is int and seed == -1:
        result["seed"] = -1
    elif str(seed).isdigit() and int(seed) < 2**64:
        result["seed"] = int(seed)
    else:
        raise ValueError("seed 必须是 -1 或 64 位无符号整数")
    for key in ("auto_keyframe", "allow_low_vram", "use_selflift", "speed_slider",
                "sparse_attention", "no_music", "trim_to_requested_seconds"):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    if result.get("no_music") and result.get("prompt") is not None:
        result["prompt"] = apply_no_music_policy(result["prompt"], True)
    for key in ("images", "videos", "audios"):
        values = result.get(key, [])
        if not isinstance(values, (tuple, list)):
            raise ValueError(f"{key} 必须是列表")
        result[key] = [_media_item(item) for item in values]
    images, videos, audios = (result[key] for key in ("images", "videos", "audios"))
    for video in videos:
        if "use_audio" in video and not isinstance(video["use_audio"], bool):
            raise ValueError("视频 use_audio 必须是布尔值")
    alternate_first = result.pop("first_frame_path", None)
    first = result.get("image_path") or alternate_first
    if first:
        result["image_path"] = first
    if result.pop("last_frame_path", None) or result.pop("guides", None):
        raise ValueError("当前工作流不支持尾帧或 guides")
    if task not in ("load", "download"):
        if task not in tasks:
            raise ValueError(f"{workflow} 不支持 {task}")
        if task == "t2v" and (first or images or videos or audios):
            raise ValueError("t2v 不接受显式素材，请选择 i2v 或 r2v")
        if task == "i2v" and (not first or images or videos or audios):
            raise ValueError("i2v 只接受单首帧，多参考素材请使用 r2v")
        if task == "r2v" and (first or not (images or videos or audios)):
            raise ValueError("r2v 请在 images、videos 或 audios 中提供参考素材")
        if len(images) > 9 or len(videos) > 3 or len(audios) > 3 or len(images + videos + audios) > 12:
            raise ValueError("参考素材最多 9 图、3 视频、3 音频及合计 12 项")
    if result["text_encoder"] not in ("nvfp4", "heretic"):
        raise ValueError("text_encoder 必须是 nvfp4 或 heretic")
    if result.get("scheduler", "simple") not in ("simple", "beta"):
        raise ValueError("scheduler 必须是 simple 或 beta")
    if "num_frames" in result:
        raise ValueError("请使用 seconds 指定视频时长")
    seconds = result.get("seconds", result.get("duration"))
    seconds = 5 if seconds is None else seconds
    try:
        if isinstance(seconds, bool):
            raise ValueError
        result["seconds"] = seconds if type(seconds) is int else float(seconds)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("seconds 必须是数字") from error
    max_seconds = profile.max_seconds if profile and enforce_hardware else 15
    if not (0.2 <= result["seconds"] <= max_seconds):
        raise ValueError(f"时长须介于 0.2 和 {max_seconds} 秒之间")
    if result.get("fps") not in (None, FPS, str(FPS)):
        raise ValueError("MiniMax-H3 只支持 24fps")
    result["fps"] = FPS
    if profile and enforce_hardware and profile.rejection:
        if not (profile.tier == 4 and profile.vram_gb > 0 and result.get("allow_low_vram")):
            raise ValueError(profile.rejection)
    return result
