"""Shared H3 workflow, hardware, media and API graph contracts."""

from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

if __package__.startswith("workflows."):
    import comfy_assets
    import setup_env
else:
    from ... import comfy_assets, setup_env

from . import spec
from .spec import (
    DEFAULT_WORKFLOW, FPS, JINYU_ASPECT_SIZES, JINYU_WORKFLOWS,
    check_resolution_for_profile, jinyu_size, output_frames, resolution_limits,
    resolve_hardware_profile, snap_down_17k5, snap_up_17k5, trim_frames,
)

H3_FAMILY = "minimax_h3"
WORKFLOW_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Workflow:
    directory: Path
    manifest: dict

    @property
    def id(self):
        return self.manifest["id"]

    @property
    def family(self):
        return self.directory.parent.name

    @property
    def model_id(self):
        return "minimax-h3"

    @property
    def profile_id(self):
        return self.manifest.get("profile_id", setup_env.COMFY_MAIN)

    @property
    def revision(self):
        return self.manifest.get("comfyui_revision", setup_env.COMFY_REVISION)

    @property
    def output_kind(self):
        return self.manifest["output_kind"]

    @property
    def builder(self):
        return importlib.import_module(f".{self.directory.name}.builder", __package__)

    def normalize(self, task, params, profile, root=None, probe=False, enforce_hardware=True):
        return normalize_request(task, params, profile, root=root, probe=probe,
                                 enforce_hardware=enforce_hardware)

    def assets(self, profile, params):
        return assets_for(self.id, profile, params)

    def plugins(self, profile, params):
        return plugins_for(self.id, profile, params)

    def required_nodes(self, plugins):
        return set(self.manifest["required_nodes"]) | {self.manifest["plugin_nodes"][p] for p in plugins}

    def steps(self, params, assets):
        return self.manifest["steps"]

    def highvram(self, profile, params):
        return highvram(profile, params)

    def stage(self, params, profile, root, jid, cancel, warmup):
        return warmup_params(params) if warmup else prepare_inputs(params, profile, root, jid, cancel)

    def build(self, params, profile, prefix):
        return self.builder.build(params, profile, prefix)

    def prepare_output(self, source, target, params):
        prepare_output(source, target, params)

    def verify_output(self, path, params, profile):
        return verify_output(path, params, profile)


def load_workflows(root=WORKFLOW_ROOT):
    workflows = {}
    for manifest_path in sorted(root.glob(f"{H3_FAMILY}/*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        workflow = Workflow(manifest_path.parent, manifest)
        setup_env.comfy_runtime(workflow.profile_id)
        if workflow.revision != setup_env.COMFY_REVISION:
            raise ValueError(f"{workflow.id} 的 ComfyUI revision 无效")
        if workflow.id in workflows or workflow.output_kind != "video":
            raise ValueError(f"重复工作流或无效 output_kind：{workflow.id}")
        if not (workflow.directory / "builder.py").is_file():
            raise ValueError(f"工作流缺少 builder.py：{workflow.id}")
        plugins, plugin_nodes = manifest.get("plugins"), manifest.get("plugin_nodes")
        if (not isinstance(plugins, list) or not all(isinstance(plugin, str) for plugin in plugins)
                or not isinstance(plugin_nodes, dict)
                or not all(isinstance(node, str) and node.strip() for node in plugin_nodes.values())
                or len(set(plugins)) != len(plugins) or set(plugins) != plugin_nodes.keys()):
            raise ValueError(f"{workflow.id} 的 plugins 与 plugin_nodes 无效或不一致")
        unsupported = set(plugins) - setup_env.CUSTOM_NODES.keys()
        if unsupported:
            raise ValueError(f"{workflow.id} 包含不受支持的插件：{', '.join(sorted(unsupported))}")
        workflows[workflow.id] = workflow
    comfy_assets.validate_assets((item for workflow in workflows.values() for item in workflow.manifest.get("weights", [])),
                                 setup_env.COMFY_MODELS_DIR)
    return workflows


WORKFLOWS = load_workflows()


def get_workflow(workflow_id):
    if not isinstance(workflow_id, str) or workflow_id not in WORKFLOWS:
        raise ValueError(f"未知 ComfyUI 工作流：{workflow_id}")
    return WORKFLOWS[workflow_id]


def for_model(model, params):
    if model.get("engine") != "minimax_h3":
        raise ValueError("模型不支持 MiniMax-H3 工作流")
    return get_workflow(workflow_id(params.get("workflow")))


def probe_hardware():
    name, vram, ram = "unavailable", 0.0, 0.0
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, encoding="utf-8", errors="replace", timeout=5)
        name, mib = result.stdout.splitlines()[0].rsplit(",", 1)
        vram = float(mib) / 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) / 1048576
                    break
    except OSError:
        pass
    return resolve_hardware_profile(vram, ram, name.strip())


def workflow_id(value=None):
    return spec.workflow_id(value, WORKFLOWS)


def plugins_for(workflow, profile, params):
    manifest = get_workflow(workflow_id(workflow)).manifest
    plugins = manifest.get("plugins") or tuple(manifest.get("plugin_nodes", ()))
    return tuple(
        plugin for plugin in plugins
        if plugin != "comfyui-SelfLift" or params.get("use_selflift", True)
    )


def assets_for(workflow, profile, params):
    manifest = get_workflow(workflow_id(workflow)).manifest
    weights = {item["id"]: item for item in manifest["weights"]}
    encoder = "heretic" if params.get("text_encoder", "heretic") == "heretic" else "text_enc_nvfp4"
    optional = {"heretic", "text_enc_nvfp4", "selflift_lms", "speed_slider"}
    required = [key for key in weights if key not in optional] + [encoder]
    if params.get("use_selflift", True):
        required.append("selflift_lms")
    if params.get("speed_slider"):
        required.append("speed_slider")
    return {key: weights[key] for key in required}


def input_path(value, root):
    if not isinstance(value, (str, os.PathLike)) or not value:
        raise ValueError("素材必须有有效的远端路径")
    base = Path(root).resolve()
    path = Path(value).resolve()
    if base not in path.parents or not path.is_file():
        raise ValueError(f"素材必须是 {base} 目录下的文件")
    return path


def media_info(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, check=True, encoding="utf-8", errors="replace", timeout=20)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(f"无法读取媒体 {Path(path).name}：{error}") from error


def media_stream(info, kind):
    return next((s for s in info.get("streams", []) if s.get("codec_type") == kind and
                 not s.get("disposition", {}).get("attached_pic")), None)


def stream_seconds(info, stream):
    for value in (stream.get("duration"), info.get("format", {}).get("duration")):
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds > 0:
                return seconds
        except (TypeError, ValueError):
            pass
    raise ValueError("参考媒体没有可验证的时长")


def normalize_request(task, params, profile=None, root=None, probe=False, enforce_hardware=True):
    """Validate workflow parameters, then optionally validate and probe remote media."""
    if not isinstance(params, dict):
        raise ValueError("params 必须是 JSON 对象")
    workflow = get_workflow(workflow_id(params.get("workflow")))
    result = spec.normalize_request(task, params, profile, enforce_hardware=enforce_hardware,
                                    available=WORKFLOWS, tasks=workflow.manifest["tasks"])
    if root is not None:
        if result.get("image_path"):
            result["image_path"] = str(input_path(result["image_path"], root))
        for item in result["images"] + result["videos"] + result["audios"]:
            item["path"] = str(input_path(item["path"], root))
    if probe:
        inspect_references(result)
    return result


def inspect_references(params):
    video_specs = []
    video_seconds = audio_seconds = 0.0
    audio_count = len(params["audios"])
    for item in params["videos"]:
        info = media_info(item["path"])
        video, audio = media_stream(info, "video"), media_stream(info, "audio")
        if not video:
            raise ValueError("参考视频缺少画面轨")
        seconds = stream_seconds(info, video)
        video_seconds += seconds
        use_audio = bool(audio and item.get("use_audio", True))
        if use_audio:
            audio_count += 1
            audio_seconds += stream_seconds(info, audio)
        cfr_frames = math.floor(seconds * FPS + 1e-6)
        window = snap_down_17k5(min(cfr_frames, output_frames(params)))
        video_specs.append((item, video, audio if use_audio else None, window))
    for item in params["audios"]:
        info = media_info(item["path"])
        audio = media_stream(info, "audio")
        if not audio:
            raise ValueError("独立参考音频缺少音轨")
        audio_seconds += stream_seconds(info, audio)
    if video_seconds > 15.000001 or audio_seconds > 15.000001 or audio_count > 3:
        raise ValueError("参考视频和音频各限单段及累计 15 秒，音频最多 3 段（含视频伴音）")
    if len(params["images"]) + len(params["videos"]) + audio_count > 12:
        raise ValueError("参考素材合计最多 12 项（含视频伴音）")
    return video_specs


def scaled_size(width, height, max_width, max_height):
    scale = min(1, max_width / width, max_height / height)
    size = tuple(math.floor(dim * scale / 32 + 1e-9) * 32 for dim in (width, height))
    if min(size) < 32:
        raise ValueError("参考视频尺寸过小，无法在不放大的情况下做 32 像素对齐")
    return size


def _stage_file(value, root, stem):
    source = input_path(value, root)
    target = Path(root) / (stem + source.suffix.lower())
    if source != target:
        shutil.copyfile(source, target)
    return target.name


def reference_video_spec(video, audio, window, caps=JINYU_ASPECT_SIZES["16:9"]):
    source_width, source_height = video["width"], video["height"]
    rotation = next((side_data["rotation"] for side_data in video.get("side_data_list", [])
                     if "rotation" in side_data), video.get("tags", {}).get("rotate", 0))
    if float(rotation) % 180 == 90:
        source_width, source_height = source_height, source_width
    max_w, max_h = caps
    width_cap, height_cap = (max_w, max_h) if source_width >= source_height else (max_h, max_w)
    width, height = scaled_size(source_width, source_height, width_cap, height_cap)
    return {
        "norm_w": width, "norm_h": height, "window": window, "use_audio": audio is not None,
        "video_index": video["index"], "audio_index": audio["index"] if audio is not None else None,
        "start": float(video.get("start_time") or 0),
    }


def normalize_reference_video(source, target, spec, *, ffmpeg="ffmpeg"):
    width, height = spec["norm_w"], spec["norm_h"]
    window, video_index, audio_index = spec["window"], spec["video_index"], spec["audio_index"]
    start = spec["start"]
    duration = window / FPS
    vf = f"fps=24:start_time={start:.9f},scale={width}:{height},setpts=PTS-STARTPTS,setsar=1"
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-copyts",
           "-i", str(source), "-map", f"0:{video_index}", "-vf", vf,
           "-t", f"{duration:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if audio_index is not None:
        af = f"asetpts=PTS-({start:.9f})/TB,aresample=async=1:first_pts=0,apad,atrim=duration={duration:.9f}"
        cmd += ["-map", f"0:{audio_index}", "-af", af, "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        cmd.append("-an")
    subprocess.run([*cmd, str(target)], timeout=120, check=True, capture_output=True)
    checked = media_info(target)
    rendered = media_stream(checked, "video")
    if (not rendered or int(rendered.get("nb_frames", 0)) != window or Fraction(rendered["avg_frame_rate"]) != FPS
            or (rendered["width"], rendered["height"]) != (width, height)):
        raise ValueError("参考视频 24fps 对齐失败")
    if audio_index is not None:
        rendered_audio = media_stream(checked, "audio")
        if (not rendered_audio or rendered_audio.get("channels") != 2 or rendered_audio.get("sample_rate") != "48000"
                or abs(stream_seconds(checked, rendered_audio) - duration) > 1 / FPS):
            raise ValueError("参考视频伴音未对齐画面裁剪窗口")
    elif media_stream(checked, "audio") is not None:
        raise ValueError("静音参考视频仍含音轨")


def prepare_inputs(params, profile, root, job_id, check_cancel=lambda: None):
    """Produce top-level input-directory filenames for the pinned native loaders."""
    if not job_id or not all(c.isascii() and (c.isalnum() or c in "-_") for c in job_id):
        raise ValueError("无效 job_id")
    result = dict(params)
    specs = inspect_references(params)
    if params.get("image_path"):
        check_cancel()
        result["image_path"] = _stage_file(params["image_path"], root, f"{job_id}_image_path")
    for key in ("images", "audios"):
        staged = []
        for index, entry in enumerate(params[key]):
            check_cancel()
            path = _stage_file(entry["path"], root, f"{job_id}_{key}_{index}")
            staged.append(dict(entry, path=path))
        result[key] = staged
    result["videos"] = []
    caps = JINYU_ASPECT_SIZES["16:9"]
    for index, (entry, video, audio, window) in enumerate(specs):
        check_cancel()
        target = Path(root) / f"{job_id}_videos_{index}.mp4"
        video_spec = reference_video_spec(video, audio, window, caps)
        normalize_reference_video(entry["path"], target, video_spec)
        result["videos"].append({"path": target.name, "use_audio": bool(audio), "frames": window})
    return result


def build_graph(workflow, params, profile, job_id):
    return get_workflow(workflow_id(workflow)).build(params, profile, job_id)


def highvram(profile, params):
    if params.get("workflow") in JINYU_WORKFLOWS and params.get("text_encoder", "heretic") == "heretic":
        return profile.highvram_safe
    return profile.tier == 1 and (profile.highvram_safe or
                                 not (params.get("seconds", 0) > 8 and
                                      max(params.get("width", 0), params.get("height", 0)) > 1024))


def warmup_params(params):
    return dict(params, prompt="A static gray background.", width=256, height=256, seconds=5 / FPS,
                image_path=None, last_frame_path=None, images=[], videos=[], audios=[], guides=[], seed=0)


def prepare_output(source, target, params):
    frames = trim_frames(params)
    if frames is None:
        shutil.copyfile(source, target)
        return
    duration = frames / FPS
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-t", f"{duration:.9f}",
                    "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-c:a", "aac", str(target)],
                   timeout=180, check=True, capture_output=True)


def verify_output(path, params, profile):
    info = media_info(path)
    video, audio = (media_stream(info, kind) for kind in ("video", "audio"))
    expected_frames = trim_frames(params) or output_frames(params)
    if (not video or not audio or audio.get("channels") != 2 or
            Fraction(video.get("avg_frame_rate", "0/1")) != FPS or
            int(video.get("nb_frames", 0)) != expected_frames or
            (video.get("width"), video.get("height")) != (params["width"], params["height"]) or
            abs(stream_seconds(info, audio) - expected_frames / FPS) > 0.15):
        raise RuntimeError("MiniMax-H3 MP4 未通过尺寸、24fps、帧数或双声道完整性校验")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
        timeout=180, check=True, capture_output=True,
    )
    return {"width": video["width"], "height": video["height"], "frames": expected_frames, "fps": FPS,
            "duration_s": expected_frames / FPS, "audio_channels": audio["channels"],
            "audio_duration_s": stream_seconds(info, audio)}



class Graph:
    def __init__(self, assets):
        self.nodes = {}
        self.assets = assets

    def add(self, class_type, **inputs):
        key = str(len(self.nodes) + 1)
        self.nodes[key] = {"class_type": class_type, "inputs": inputs}
        return [key, 0]

    def model_file(self, key):
        return Path(self.assets[key]["comfy_rel_path"]).name

    def loaders(self, model_key, text_key, vae_key="video_vae_int8"):
        model = self.add("UNETLoader", unet_name=self.model_file(model_key), weight_dtype="default")
        clip = self.add("CLIPLoader", clip_name=self.model_file(text_key), type="minimax", device="default")
        vae = self.add("VAELoader", vae_name=self.model_file(vae_key))
        audio = self.add("VAELoader", vae_name=self.model_file("audio_vae_fp32"))
        return model, clip, vae, audio

    def lora(self, model, key, strength):
        return self.add("LoraLoaderModelOnly", model=model, lora_name=self.model_file(key), strength_model=strength)

    def custom_loras(self, model, params):
        for path in params.get("_custom_model_paths", {}).values():
            # ComfyUI loader values are relative to the named 'loras' folder.
            model = self.add("LoraLoaderModelOnly", model=model, lora_name=path.split("/", 1)[1],
                             strength_model=1.0)
        return model

    def sparse(self, model):
        model = self.add("ModelAttentionBackend", model=model, attention="comfy kitchen attention")
        return self.add("BlockSparseAttention", model=model, selection="sol-attn", start_percent=0.2,
                        end_percent=1.0, dense_blocks="", min_tokens=12288, extra_tokens=256,
                        sink_conditioning="exact_kv_and_rows", verbose=False, **{"selection.tau": 1.3})

    def sample(self, model, positive, latent, sampler, sigmas, seed):
        guider = self.add("BasicGuider", model=model, conditioning=positive)
        noise = self.add("RandomNoise", noise_seed=seed)
        return self.add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                        sigmas=sigmas, latent_image=latent)

    def save(self, samples, vae, audio_vae, job_id, *, tiled_vae=True):
        if tiled_vae:
            frames = self.add("VAEDecodeTiled", samples=samples, vae=vae, tile_size=512, overlap=64,
                              temporal_size=64, temporal_overlap=8)
        else:
            frames = self.add("VAEDecode", samples=samples, vae=vae)
        audio = self.add("VAEDecodeAudio", samples=samples, vae=audio_vae)
        video = self.add("CreateVideo", images=frames, audio=audio, fps=24.0)
        save = self.add("SaveVideo", video=video, filename_prefix=job_id, format="mp4", **{"format.codec": "h264"})
        return self.nodes, save[0]
