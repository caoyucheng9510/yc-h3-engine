"""RunningHub workflow catalog.

Adapted from the author's video-studio/tools/runninghub_gen.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from ...fsutil import ensure_real_path, ensure_real_subpath

from ..video_common import VALID_PROMPT_MODES, validate_h3_prompt as validate_h3_prompt

WORKFLOWS_DIR = Path(__file__).parent
REGISTRY_PATH = WORKFLOWS_DIR / "registry.json"

def load_registry() -> dict:
    path = ensure_real_path(REGISTRY_PATH, must_exist=True, expect_dir=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), dict):
        raise ValueError("RunningHub 工作流注册表格式无效")
    workflows = data["workflows"]
    default = data.get("default")
    if not isinstance(default, str) or default not in workflows:
        raise ValueError("RunningHub 注册表缺少有效的默认工作流")
    for spec in workflows.values():
        if not isinstance(spec, dict) or "workflow_id" not in spec:
            raise ValueError("RunningHub 工作流注册表须声明 workflow_id")
        if (workflow_id := spec["workflow_id"]) is not None and (
            not isinstance(workflow_id, str) or not workflow_id.isascii() or not workflow_id.isdecimal()
        ):
            raise ValueError("RunningHub 工作流注册表缺少有效的 workflow_id")
        if spec.get("prompt_mode") not in VALID_PROMPT_MODES:
            raise ValueError("RunningHub 工作流注册表提示词模式无效")
    return data


def get_workflow_spec(name: str | None = None) -> tuple[str, dict]:
    registry = load_registry()
    workflow = name or registry["default"]
    if workflow not in registry["workflows"]:
        raise ValueError(f"未知 RunningHub 工作流 {workflow!r}")
    return workflow, registry["workflows"][workflow]


def workflow_media_caps(spec: dict) -> dict[str, int]:
    nodes = spec.get("nodes") or {}
    return {
        "images": len((nodes.get("images") or {}).get("ids") or []),
        "audios": len((nodes.get("audios") or {}).get("ids") or []),
        "videos": len((nodes.get("videos") or {}).get("ids") or []),
    }


def load_and_verify_graph(spec: dict, workflows_dir: Path = WORKFLOWS_DIR) -> dict:
    raw_graph_file = str(spec.get("graph_file") or "").strip()
    if not raw_graph_file:
        raise ValueError("工作流配置缺少 graph_file")
    graph_path = ensure_real_subpath(workflows_dir, workflows_dir / raw_graph_file)
    graph = json.loads(ensure_real_path(graph_path, must_exist=True, expect_dir=False).read_text(encoding="utf-8"))
    if not isinstance(graph, dict):
        raise ValueError("工作流节点图必须为 JSON 对象")
    nodes = spec.get("nodes") or {}
    mismatches: list[str] = []

    def check_node(nid: str, expected_type: str, required_inputs: tuple[str, ...]) -> None:
        node = graph.get(str(nid))
        actual = node.get("class_type") if isinstance(node, dict) else None
        if actual != expected_type:
            mismatches.append(f"节点 {nid} 期望 class_type={expected_type}，实际 {actual}")
            return
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            mismatches.append(f"节点 {nid} 缺少 inputs 字典")
            return
        for key_name in required_inputs:
            if key_name not in inputs:
                mismatches.append(f"节点 {nid} 的 inputs 中不存在字段 {key_name!r}")

    for key in ("prompt", "length", "steps", "seed"):
        cfg = nodes.get(key)
        if cfg:
            check_node(cfg["id"], cfg["class_type"], (cfg["input"],))
    if cfg := nodes.get("selflift"):
        check_node(cfg["id"], cfg["class_type"], (cfg["transition_step_input"],))
    if s_cfg := nodes.get("size"):
        check_node(nodes["target_node"], s_cfg["class_type"], (s_cfg["width_input"], s_cfg["height_input"]))
    for key in ("images", "audios", "videos"):
        cfg = nodes.get(key)
        if cfg:
            for nid in cfg.get("ids") or []:
                check_node(nid, cfg["class_type"], (cfg["input"],))

    if mismatches:
        raise ValueError(f"{graph_path.name} 节点或字段签名不匹配，请同步检查 workflows/registry.json：" + "；".join(mismatches))
    return graph


def build_workflow_graph(
    spec: dict,
    *,
    prompt: str,
    image_names: list[str],
    audio_names: list[str],
    seconds: int | float,
    aspect: str,
    seed: int,
    steps: int | None = None,
    size: tuple[int, int] | None = None,
    videos: tuple | list = (),
    workflows_dir: Path = WORKFLOWS_DIR,
) -> dict:
    min_sec, max_sec = spec["min_seconds"], spec["max_seconds"]
    if type(seconds) not in (int, float) or not min_sec <= seconds <= max_sec:
        raise ValueError(f"--seconds 必须在 {min_sec}–{max_sec} 之间：{seconds}")

    sizes = spec["sizes"]
    if aspect not in sizes:
        raise ValueError(f"画幅只支持 {', '.join(sizes)}：{aspect}")
    width, height = sizes[aspect] if size is None else size
    if (any(type(n) is not int or n <= 0 or n % 32 for n in (width, height))
            or max(width, height) > spec["max_edge"] or width * height > spec["max_pixels"]):
        raise ValueError(f"宽高须为 32 的正整数倍，最长边不超过 {spec['max_edge']}，总像素不超过 {spec['max_pixels']}")

    graph = load_and_verify_graph(spec, workflows_dir=workflows_dir)
    nodes = spec["nodes"]
    selflift = nodes["selflift"]
    transition_step = graph[selflift["id"]]["inputs"][selflift["transition_step_input"]]
    steps = spec["default_steps"] if steps is None else steps
    if type(transition_step) is not int or type(steps) is not int or not (
        spec["min_steps"] <= steps <= 100 and 1 <= transition_step < steps
    ):
        raise ValueError(f"SelfLift 需要 1 <= transition_step ({transition_step}) < steps（{spec['min_steps']}–100）")
    for slot, value in (("prompt", prompt), ("length", seconds), ("steps", steps), ("seed", seed)):
        cfg = nodes[slot]
        graph[cfg["id"]]["inputs"][cfg["input"]] = value

    target = graph[nodes["target_node"]]["inputs"]
    size_cfg = nodes["size"]
    target[size_cfg["width_input"]], target[size_cfg["height_input"]] = width, height

    media = {"images": image_names, "audios": audio_names, "videos": videos}
    for slot, values in media.items():
        cfg = nodes[slot]
        if len(values) > len(cfg["ids"]):
            raise ValueError(f"{slot} 最多 {len(cfg['ids'])} 项")
        for index, nid in enumerate(cfg["ids"]):
            if index >= len(values):
                del graph[nid]
                for field, link in list(target.items()):
                    if isinstance(link, list) and link[0] == nid:
                        del target[field]
                continue

            inputs = graph[nid]["inputs"]
            if slot == "videos":
                video = values[index]
                inputs.update(video=video.get("name", ""), force_rate=24, frame_load_cap=video["window"])
                audio_field = f"ref_video_audios.ref_video_audio_{index}"
                if video["use_audio"]:
                    target[audio_field] = [nid, cfg["audio_output_index"]]
                else:
                    target.pop(audio_field, None)
            else:
                inputs[cfg["input"]] = values[index]
    return graph
