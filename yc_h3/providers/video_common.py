"""Video artifact validation for hosted H3 workflows."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

from ..fsutil import find_bin
from .base import ArtifactContractError

def ffmpeg_bin() -> str | None:
    fallbacks = () if os.name == "nt" else ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
    return find_bin("ffmpeg", *fallbacks)


def ffprobe_bin() -> str | None:
    fallbacks = () if os.name == "nt" else ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe")
    return find_bin("ffprobe", *fallbacks)


def default_ffmpeg(args: list[str], *, timeout: float = 180) -> None:
    """Run FFmpeg with UTF-8 diagnostics and fail on a nonzero exit."""
    try:
        subprocess.run(
            [ffmpeg_bin() or "ffmpeg", *args], capture_output=True, encoding="utf-8",
            errors="replace", check=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise ArtifactContractError("FFmpeg 执行失败，请检查媒体文件和本机 FFmpeg", code="ffmpeg_failed") from None


def default_ffprobe(path: Path | str) -> dict:
    """Run ffprobe and parse its UTF-8 stream metadata."""
    try:
        result = subprocess.run(
            [ffprobe_bin() or "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            check=True, stdin=subprocess.DEVNULL,
        )
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    raise ArtifactContractError("ffprobe 探测失败，请检查媒体文件和本机 ffprobe", code="ffprobe_failed")


def verify_video_decode(path: Path | str) -> None:
    """Decode both primary streams in full before an artifact can be published."""
    try:
        default_ffmpeg(["-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    except ArtifactContractError:
        raise ArtifactContractError("成片未通过 FFmpeg 双流严格解码", code="video_decode_failed") from None


def parse_positive_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


REF2VA_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
BASE_SECTIONS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
VALID_PROMPT_MODES = ("ref2va", "t2va", "i2va", "fl2va", "l2va")

DIALOGUE_TAG_RE = re.compile(r"<d>.*?</d>", re.S)
QUOTED_TEXT_RE = re.compile(r'"[^"\n]*"')
NEGATIVE_HINT_RE = re.compile(
    r"\b(no|not|without|never|avoid|don't|do not|free of)\b[^.\n]{0,40}\b"
    r"(music|soundtrack|score|subtitles?|captions?|on-screen text|text overlays?|\btext\b|"
    r"watermarks?|logos?|brands?|narration|voice-?over|singing)\b",
    re.I,
)
SHOT_HEADER_RE = re.compile(r"\[Shot\s+(-?\d+)\]")
SHOT_TS_AFTER_RE = re.compile(r"^\s+At\s+(\d{2}):(\d{2})\.(\d{3})\b")


def _parse_fps(raw_rate: Any) -> Optional[float]:
    if raw_rate is None:
        return None
    if isinstance(raw_rate, (int, float)):
        val = float(raw_rate)
        return val if math.isfinite(val) and val > 0 else None
    text = str(raw_rate).strip()
    if not text or text in ("0/0", "0/1", "N/A"):
        return None
    try:
        val = float(Fraction(text))
        return val if math.isfinite(val) and val > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def verify_runninghub_video(info: dict, *, workflow: str, width: int, height: int, expected_frames: int) -> dict:
    """Check the hosted H3 result against its requested dimensions, timing and stereo audio."""
    return _verify_h3_video(info, workflow=workflow, expected_frames=expected_frames, expected_size=(width, height))


def verify_chenyu_video(
    info: dict, *, workflow: str, expected_frames: int, expected_size: tuple[int, int] | None = None,
    aspect: str | None = None, upscale_2x: bool = False,
) -> dict:
    """Check the frame grid and dimensions allowed by the selected Chenyu workflow."""
    if type(expected_frames) is not int or expected_frames < 5 or (expected_frames - 5) % 17:
        raise ArtifactContractError("晨羽视频预期帧数须符合 17k+5 网格", code="h3_invalid_expected_frames")
    contract = _verify_h3_video(
        info, workflow=workflow, expected_frames=expected_frames,
        expected_size=tuple(expected_size) if expected_size is not None else None, require_even=True,
    )
    if expected_size is None and aspect is not None:
        width, height = contract["width"], contract["height"]
        if aspect in ("16:9", "4:3", "3:2", "21:9"):
            matches = width > height
        elif aspect in ("9:16", "3:4", "2:3"):
            matches = height > width
        else:
            matches = aspect == "1:1" and width == height
        if not matches:
            raise ArtifactContractError(
                f"晨羽视频尺寸 {width}x{height} 与预期画幅 {aspect} 方向不一致",
                code="h3_aspect_mismatch", details=contract,
            )
    return dict(contract, aspect=aspect, upscale_2x=upscale_2x)


def _verify_h3_video(
    info: dict, *, workflow: str, expected_frames: int, expected_size: tuple[int, int] | None,
    require_even: bool = False,
) -> dict:
    streams = info.get("streams") if isinstance(info, dict) else None
    if not isinstance(streams, list):
        raise ArtifactContractError("ffprobe 响应缺少 streams 列表", code="invalid_video_probe")

    video = next(
        (
            s
            for s in streams
            if isinstance(s, dict)
            and s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ArtifactContractError(f"视频产物缺少视频流（{workflow}）", code="missing_video_stream")

    v_w = video.get("width")
    v_h = video.get("height")
    if not isinstance(v_w, int) or not isinstance(v_h, int) or v_w <= 0 or v_h <= 0:
        raise ArtifactContractError(
            f"视频产物宽高无效（{v_w!r}x{v_h!r}）",
            code="invalid_video_dimensions",
        )
    if require_even and (v_w % 2 or v_h % 2):
        raise ArtifactContractError("晨羽视频宽高必须为正偶数", code="invalid_video_dimensions")

    fmt_info = info.get("format") if isinstance(info.get("format"), dict) else {}
    duration = parse_positive_float(video.get("duration")) or parse_positive_float(fmt_info.get("duration"))
    if duration is None:
        raise ArtifactContractError(f"视频产物缺少正时长（{workflow}）", code="invalid_video_duration")

    fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(video.get("r_frame_rate"))
    nb_frames_raw = video.get("nb_frames")
    frames: Optional[int] = None
    has_explicit_nb_frames = False
    if nb_frames_raw not in (None, "", "N/A"):
        try:
            if require_even and not re.fullmatch(r"[0-9]+", str(nb_frames_raw)):
                raise ValueError("invalid nb_frames")
            frames = int(nb_frames_raw)
            has_explicit_nb_frames = True
        except (TypeError, ValueError):
            if require_even:
                raise ArtifactContractError("晨羽视频显式帧数无效", code="h3_invalid_frames") from None
            frames = None
    if frames is None and fps is not None:
        frames = int(round(duration * fps))

    contract: Dict[str, Any] = {
        "kind": "video",
        "workflow": workflow,
        "width": v_w,
        "height": v_h,
        "duration_s": round(duration, 4),
        "fps": round(fps, 4) if fps is not None else None,
        "frames": frames,
    }

    if fps is None or not math.isclose(fps, 24.0, rel_tol=0, abs_tol=0.001):
        raise ArtifactContractError(
            f"H3 视频帧率不符合 24fps 合同（实测 {fps!r}）",
            code="h3_invalid_fps",
            details=contract,
        )
    if frames is None or frames <= 0:
        raise ArtifactContractError(
            f"H3 视频帧数无效（实测 {frames!r}）",
            code="h3_invalid_frames",
            details=contract,
        )
    contract["expected_frames"] = expected_frames
    frame_tol = 0 if has_explicit_nb_frames else 1
    if abs(frames - expected_frames) > frame_tol:
        raise ArtifactContractError(
            f"H3 视频帧数 {frames} 与预期 {expected_frames} 不一致（{workflow}）",
            code="h3_frame_count_mismatch", details=contract,
        )
    if expected_size is not None and (v_w, v_h) != expected_size:
        width, height = expected_size
        raise ArtifactContractError(
            f"H3 视频尺寸 {v_w}x{v_h} 与预期 {width}x{height} 不一致",
            code="h3_dimension_mismatch", details=contract,
        )

    audio = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    if audio is None:
        raise ArtifactContractError(
            f"H3 视频产物缺少音频流（{workflow}）",
            code="h3_missing_audio_stream",
            details=contract,
        )
    channels = audio.get("channels")
    if not isinstance(channels, int) or isinstance(channels, bool) or channels != 2:
        raise ArtifactContractError(
            f"H3 视频音频声道不符合双声道合同（实测 channels={channels!r}）",
            code="h3_invalid_audio_channels",
            details=contract,
        )
    # Strictly require the audio stream's OWN duration — NEVER substitute video duration!
    audio_dur = parse_positive_float(audio.get("duration"))
    if audio_dur is None:
        raise ArtifactContractError(
            f"H3 视频音轨缺少自身有效时长（{workflow}）",
            code="h3_missing_audio_duration",
            details=contract,
        )
    contract["audio_channels"] = channels
    contract["audio_duration_s"] = round(audio_dur, 4)
    target_dur = expected_frames / 24.0
    if abs(audio_dur - target_dur) > 0.15 or abs(audio_dur - duration) > 0.15:
        raise ArtifactContractError(
            f"H3 视频音轨时长 {audio_dur:.4f}s 与视频/预期时长 {target_dur:.4f}s 不匹配（{workflow}）",
            code="h3_audio_duration_mismatch",
            details=contract,
        )

    return contract


def validate_h3_prompt(
    text: str,
    *,
    mode: str = "ref2va",
    n_images: int | None = None,
    n_audios: int | None = None,
    n_videos: int | None = None,
    seconds: int | float | None = None,
    media_caps: dict[str, int] | None = None,
) -> tuple[list[str], list[str]]:
    """离线校验 MiniMax H3 提示词的确定性结构，返回 (errors, warnings)。

    - errors：确定性结构与输入契约不变量（段落、首行对齐、切镜时间戳、工作流支持的媒体类型与 1-based 编号边界）；
    - warnings：语义/质量风险提示（叙述正文中疑似负面词、非 N/A 配乐、CJK 台词烧字幕提醒），不硬拦合法对白或语义。
    """
    mode = mode.strip().lower()
    if mode not in VALID_PROMPT_MODES:
        return [f"不支持的提示词模式：{mode!r}"], []

    errors: list[str] = []
    warnings: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        return ["提示词为空"], []
    if stripped.startswith("```") or stripped.endswith("```"):
        errors.append("提示词外层带有 Markdown 代码围栏（```），提交给 H3 前必须去掉")

    expected = REF2VA_SECTIONS if mode == "ref2va" else BASE_SECTIONS
    pattern = re.compile(r"^\s*(" + "|".join(expected) + r")\s*:", re.M)
    found = [(m.start(), m.group(1)) for m in pattern.finditer(stripped)]
    found_names = tuple(name for _, name in found)
    if found_names != expected:
        errors.append(
            f"{mode.upper()} 段落结构不全或顺序不对：期望 {' → '.join(expected)}，"
            f"实际 {' → '.join(found_names) if found_names else '无匹配段落'}"
        )
        return errors, warnings

    prefix = stripped[: found[0][0]].strip()
    parts: dict[str, str] = {}
    for idx, (pos, name) in enumerate(found):
        end = found[idx + 1][0] if idx + 1 < len(found) else len(stripped)
        parts[name] = stripped[pos:end].split(":", 1)[1].strip()

    for name, content in parts.items():
        if not content:
            errors.append(f"{name} 段落不能为空")

    # Base 模式首行对齐指令（对齐官方 base-en.txt）
    if mode == "ref2va" and prefix:
        errors.append("REF2VA 首行必须以 subject_definitions: 开始")
    elif mode == "t2va" and prefix:
        warnings.append("T2VA 模式不需要首行图片对齐指令，开头存在多余前置文本")
    elif mode == "i2va":
        if "<Picture 1>" not in prefix or "0.00 seconds" not in prefix:
            errors.append("I2VA 开头缺少官方首帧对齐指令（需包含 0.00 seconds 与 <Picture 1>）")
    elif mode == "fl2va":
        if "Picture 1" not in prefix or "Picture 2" not in prefix or "0.00-second" not in prefix:
            errors.append("FL2VA 开头缺少官方首尾帧对齐指令（需包含 Picture 1、Picture 2 与 0.00-second）")
    elif mode == "l2va":
        if "<Picture 1>" not in prefix or "-second mark" not in prefix:
            errors.append("L2VA 开头缺少官方尾帧对齐指令（需包含 <Picture 1> 与 S.SS-second mark）")

    body_key = "detailed_description" if mode == "ref2va" else "integrated_multimodal_description"
    body = parts[body_key]
    shot_scan_body = QUOTED_TEXT_RE.sub("", DIALOGUE_TAG_RE.sub("", body))
    headers = list(SHOT_HEADER_RE.finditer(shot_scan_body))
    if not headers:
        errors.append(f"{body_key} 中缺少 [Shot 1]")
    prev_t = 0.0
    for expected_number, m in enumerate(headers, start=1):
        shot_no = int(m.group(1))
        if shot_no != expected_number:
            errors.append(f"镜头编号必须从 1 连续递增，期望 [Shot {expected_number}]，实际 [Shot {shot_no}]")
        after = shot_scan_body[m.end() :]
        ts_m = SHOT_TS_AFTER_RE.match(after)
        if expected_number == 1:
            if ts_m:
                errors.append("[Shot 1] 不应带时间戳")
            continue
        if not ts_m:
            errors.append(f"[Shot {shot_no}] 缺少切镜时间戳（格式须为 `[Shot {shot_no}] At MM:SS.mmm, ...`）")
            continue
        if int(ts_m.group(2)) >= 60:
            errors.append(f"[Shot {shot_no}] 时间戳的秒数必须小于 60")
        ts = int(ts_m.group(1)) * 60 + int(ts_m.group(2)) + int(ts_m.group(3)) / 1000.0
        if ts <= prev_t:
            errors.append(f"[Shot {shot_no}] 时间戳 {ts:.3f}s 未严格大于前一切点 {prev_t:.3f}s")
        if seconds is not None and ts >= seconds:
            errors.append(f"[Shot {shot_no}] 时间戳 {ts:.3f}s 超出或等于目标总时长 {seconds}s")
        prev_t = ts

    # FL2VA 首尾帧模式至少需要 2 张参考图（官方首行对齐使用裸写 Picture 1 / Picture 2）
    if mode == "fl2va" and n_images is not None and n_images < 2:
        errors.append(f"FL2VA 首尾帧模式需要至少 2 张参考图，当前只传入了 {n_images} 张")
    elif mode in ("i2va", "l2va") and n_images is not None and n_images < 1:
        errors.append(f"{mode.upper()} 模式需要至少 1 张参考图，当前只传入了 {n_images} 张")

    # 媒体引用标签确定性边界：1-based、不超传入数、不引用当前工作流不支持的媒体类型
    def check_ref_tag(tag: str, provided: int | None, cap: int | None, label: str) -> set[int]:
        nums = {int(n) for n in re.findall(rf"<{tag}\s+(-?\d+)>", stripped)}
        if not nums:
            return nums
        if min(nums) < 1:
            errors.append(f"提示词包含非法的 <{tag} {min(nums)}>（编号必须从 1 开始）")
        if cap is not None and cap == 0:
            errors.append(f"当前工作流不支持 {label} 输入，但提示词引用了 <{tag} {min(nums)}>")
        elif cap is not None and max(nums) > cap:
            errors.append(f"当前工作流最多支持 {cap} 个 {label}，但提示词引用了 <{tag} {max(nums)}>")
        elif provided is not None and max(nums) > provided:
            errors.append(f"提示词引用了 <{tag} {max(nums)}>，但只传入了 {provided} 个 {label}")
        return nums

    pic_nums = check_ref_tag("Picture", n_images, (media_caps or {}).get("images"), "参考图")
    check_ref_tag("Audio", n_audios, (media_caps or {}).get("audios"), "参考音频")
    check_ref_tag("Video", n_videos, (media_caps or {}).get("videos"), "参考视频")

    if n_images is not None and mode == "ref2va" and n_images > 0 and not pic_nums:
        warnings.append(f"传入了 {n_images} 张参考图，但提示词未引用任何 <Picture N>")

    # 语义风险提醒（不阻断）：先剔除 <d>...</d> 对白与 "..." 可见画面文字，防止误报合法台词
    for sec in (body_key, "overall_soundscape"):
        narration_only = QUOTED_TEXT_RE.sub("", DIALOGUE_TAG_RE.sub("", parts[sec]))
        for m in NEGATIVE_HINT_RE.finditer(narration_only):
            warnings.append(
                f"{sec} 含疑似负面措辞 {m.group(0)!r}：H3 ComfyUI 工作流无负面分支，若是在禁止 BGM/字幕/文字请改删，只描述存在的内容"
            )

    music_val = parts["non_diegetic_music"].strip()
    if music_val.upper() != "N/A":
        warnings.append(f"non_diegetic_music 不是 N/A（当前为 {music_val[:40]!r}），将生成背景音乐")
    if any(0x2E80 <= ord(ch) <= 0x9FFF or 0xAC00 <= ord(ch) <= 0xD7AF for ch in body):
        warnings.append("提示词含中/日/韩台词或画面文字，成片后务必重点检查有无野生烧字幕")

    return errors, warnings
