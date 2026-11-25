"""Offline catalog for the pinned Chenyu workflow revision."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ...fsutil import ensure_real_path

REGISTRY_PATH = Path(__file__).with_name("registry.json")
_ASPECTS = {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"}
_SIZE_LEVELS = {"0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9", "0.98", "1.0", "1.2", "1.5", "1.8", "2.0"}
_SCALAR_NODES = {"combat", "upscale_2x", "aspect_ratio", "megapixels", "seconds", "prompt"}
_MEDIA_SLOTS = ("images", "audios")


def load_registry() -> dict:
    path = ensure_real_path(REGISTRY_PATH, must_exist=True, expect_dir=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"default", "workflows"}:
        raise ValueError("晨羽工作流注册表格式无效")
    workflows = data["workflows"]
    if not isinstance(workflows, dict) or not isinstance(data["default"], str) or data["default"] not in workflows:
        raise ValueError("晨羽注册表缺少有效的默认工作流")
    for spec in workflows.values():
        _validate_spec(spec)
    return data


def _validate_spec(spec: dict) -> None:
    contract = {"combat", "tasks", "prompt_mode", "min_seconds", "max_seconds", "default_aspect"}
    numbers = ("compute_cny_per_minute", "creator_fee_cny", "default_megapixels", "min_megapixels", "max_megapixels")
    strings = {
        "workflow_id": r"wf_[A-Za-z0-9]+", "revision_id": r"wfr_[A-Za-z0-9]+", "gpu_model": r"[0-9]+",
        "output_key": r"result_[0-9]+_gifs_[0-9]+",
    }
    if not isinstance(spec, dict) or set(spec) != {
        *contract, *numbers, *strings, "aspect_options", "known_16_9_sizes", "nodes",
    }:
        raise ValueError("晨羽工作流字段不完整或包含未知字段")
    if (type(spec["combat"]) is not bool or not isinstance(spec["tasks"], list) or not spec["tasks"]
            or any(task not in ("r2v", "t2v") for task in spec["tasks"]) or spec["prompt_mode"] != "ref2va"
            or any(type(spec[key]) is not int for key in ("min_seconds", "max_seconds"))
            or not 1 <= spec["min_seconds"] <= spec["max_seconds"] <= 15
            or not isinstance(spec["default_aspect"], str)):
        raise ValueError("晨羽工作流任务、模式或秒数配置无效")
    if any(not isinstance(spec[key], str) or not re.fullmatch(pattern, spec[key]) for key, pattern in strings.items()):
        raise ValueError("晨羽工作流标识或输出键无效")
    if any(type(spec[key]) not in (int, float) or not 0 <= spec[key] < math.inf for key in numbers):
        raise ValueError("晨羽工作流费率或像素配置无效")
    low, default, high = (spec[key] for key in ("min_megapixels", "default_megapixels", "max_megapixels"))
    if not 0 < low <= default <= high:
        raise ValueError("晨羽工作流像素范围无效")
    options = spec["aspect_options"]
    if not isinstance(options, dict) or spec["default_aspect"] not in options or not options.keys() <= _ASPECTS or any(
        not isinstance(value, str) or not re.fullmatch(re.escape(aspect) + r" \([A-Za-z ]+\)", value)
        for aspect, value in options.items()
    ):
        raise ValueError("晨羽工作流画幅选项无效")
    sizes = spec["known_16_9_sizes"]
    if not isinstance(sizes, dict) or not sizes.keys() <= _SIZE_LEVELS or any(
        not isinstance(size, list) or len(size) != 2
        or any(type(edge) is not int or edge <= 0 or edge % 2 for edge in size) or size[0] <= size[1]
        for size in sizes.values()
    ):
        raise ValueError("晨羽工作流 16:9 尺寸表无效")
    nodes = spec["nodes"]
    if not isinstance(nodes, dict) or set(nodes) != _SCALAR_NODES | set(_MEDIA_SLOTS):
        raise ValueError("晨羽工作流节点字段无效")
    input_names = [nodes[key] for key in _SCALAR_NODES]
    for slot in _MEDIA_SLOTS:
        names = nodes[slot]
        if not isinstance(names, list) or len(names) > (9 if slot == "images" else 3):
            raise ValueError(f"晨羽工作流 {slot} 槽位数量无效")
        input_names.extend(names)
    if any(not isinstance(node, str) or not re.fullmatch(r"node_[0-9]+_[a-z_]+", node) for node in input_names):
        raise ValueError("晨羽工作流节点名无效")
    if len(set(input_names)) != len(input_names):
        raise ValueError("晨羽工作流包含重复节点输入")


def get_workflow_spec(name: str | None = None) -> tuple[str, dict]:
    registry = load_registry()
    workflow = name or registry["default"]
    if workflow not in registry["workflows"]:
        raise ValueError(f"未知晨羽工作流 {workflow!r}")
    return workflow, registry["workflows"][workflow]


def workflow_media_caps(spec: dict) -> dict[str, int]:
    return {"images": len(spec["nodes"]["images"]), "audios": len(spec["nodes"]["audios"]), "videos": 0}


def resolve_expected_size(
    spec: dict, aspect: str, megapixels: float, upscale_2x: bool = False,
) -> tuple[int, int] | None:
    if aspect != "16:9" or upscale_2x:
        return None
    size = spec["known_16_9_sizes"].get(str(float(megapixels)))
    return (size[0], size[1]) if size is not None else None
