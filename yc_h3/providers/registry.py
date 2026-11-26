"""Public provider selection and portable workflow discovery."""
from __future__ import annotations

import sys
from dataclasses import dataclass

from ..config import EngineConfig
from ..remote.workflows.minimax_h3 import spec as h3
from .base import ProbeResult, Provider, ProviderError
from .chenyu_workflows import catalog as chenyu_catalog
from .comfy_common import ComfyProvider, json_manifest
from .runninghub_workflows import catalog as runninghub_catalog

PROVIDER_ORDER = ("colab-comfy", "chenyu-comfy", "runninghub", "chenyu")
PROVIDER_ORDER_ENV = "YC_H3_PROVIDER_ORDER"
_CATALOGS = {"runninghub": runninghub_catalog, "chenyu": chenyu_catalog}
_LEGACY_PREFIXES = {"h3-": "runninghub", "chenyu-": "chenyu"}
WINDOWS_COLAB_REASON = "colab_requires_posix_or_wsl2"


def configured_provider_order(config: EngineConfig | None) -> tuple[str, ...]:
    raw = config.get(PROVIDER_ORDER_ENV) if config is not None else None
    if raw is None or not raw.strip():
        return PROVIDER_ORDER
    items = [part.strip() for part in raw.split(",") if part.strip()]
    if not items:
        raise ValueError(f"{PROVIDER_ORDER_ENV} 至少包含一个有效通道（允许值：{', '.join(PROVIDER_ORDER)}）")
    unknown = [item for item in items if item not in PROVIDER_ORDER]
    if unknown:
        raise ValueError(f"{PROVIDER_ORDER_ENV} 包含未知通道：{', '.join(unknown)}（允许值：{', '.join(PROVIDER_ORDER)}）")
    return tuple(dict.fromkeys(items))


def display_provider_order(config: EngineConfig | None) -> tuple[str, ...]:
    preferred = configured_provider_order(config)
    return preferred + tuple(name for name in PROVIDER_ORDER if name not in preferred)


def normalize_workflow(workflow, provider=None):
    if workflow is None:
        return h3.DEFAULT_WORKFLOW
    for prefix, historical_provider in _LEGACY_PREFIXES.items():
        if workflow.startswith(prefix) and workflow.removeprefix(prefix) in h3.JINYU_WORKFLOWS:
            logical = workflow.removeprefix(prefix)
            if provider is not None and provider != historical_provider:
                raise ValueError(f"旧别名 {workflow} 与 --provider {provider} 冲突，请改为 --workflow {logical}")
            return logical
    return workflow


def get_provider(provider_id: str, config: EngineConfig) -> Provider:
    if provider_id == "colab-comfy":
        from .colab_comfy import ColabComfyProvider

        return ColabComfyProvider(config=config)
    if provider_id == "chenyu-comfy":
        from .chenyu_comfy import ChenyuComfyProvider

        return ChenyuComfyProvider(config=config)
    if provider_id == "runninghub":
        from .runninghub import RunningHubProvider

        return RunningHubProvider(config=config)
    if provider_id == "chenyu":
        from .chenyu import ChenyuProvider

        return ChenyuProvider(config=config)
    raise ValueError(f"未知 Provider {provider_id}")


def _workflow_resolution(provider_id, spec):
    if provider_id == "runninghub":
        return {
            "mode": "aspect_or_dimensions",
            "default_aspect": spec["default_aspect"],
            "default_size": list(spec["sizes"][spec["default_aspect"]]),
            "sizes": {name: list(size) for name, size in spec["sizes"].items()},
            "multiple_of": 32,
            "max_edge": spec["max_edge"],
            "max_pixels": spec["max_pixels"],
            "supports_custom_dimensions": True,
        }
    if provider_id == "chenyu":
        default_size = chenyu_catalog.resolve_expected_size(
            spec, spec["default_aspect"], spec["default_megapixels"],
        )
        return {
            "mode": "aspect_and_megapixels",
            "default_aspect": spec["default_aspect"],
            "default_megapixels": spec["default_megapixels"],
            "min_megapixels": spec["min_megapixels"],
            "max_megapixels": spec["max_megapixels"],
            "default_size": list(default_size) if default_size is not None else None,
            "aspects": list(spec["aspect_options"]),
            "known_16_9_sizes": {level: list(size) for level, size in spec["known_16_9_sizes"].items()},
            "supports_custom_dimensions": False,
            "supports_upscale_2x": True,
        }
    return {
        "mode": "aspect_or_dimensions",
        "default_aspect": "16:9",
        "default_size": list(h3.JINYU_ASPECT_SIZES["16:9"]),
        "sizes": {name: list(size) for name, size in h3.JINYU_ASPECT_SIZES.items()},
        "multiple_of": 32,
        "max_edge": 1664,
        "max_pixels": h3.JINYU_MAX_PIXELS_HIGH,
        "tier3_max_edge": 1024,
        "tier3_max_pixels": 1024 * 544,
        "tier3_max_size": [1024, 544],
        "supports_custom_dimensions": True,
    }


def workflow_catalog(provider_id):
    catalog = _CATALOGS.get(provider_id)
    if catalog is not None:
        registry = catalog.load_registry()
        return [
            {"workflow": name, "default": name == registry["default"], "provider_id": provider_id,
             "tasks": spec["tasks"], "slots": catalog.workflow_media_caps(spec),
             "resolution": _workflow_resolution(provider_id, spec),
             "seconds": {"min": spec["min_seconds"], "max": spec["max_seconds"], "integer_only": provider_id == "chenyu"},
             "description": spec.get("description"), "workflow_id": spec["workflow_id"],
             **({"revision_id": spec["revision_id"]} if "revision_id" in spec else {})}
            for name, spec in registry["workflows"].items()
        ]
    if provider_id in PROVIDER_ORDER:
        return [
            {"workflow": name, "default": name == h3.DEFAULT_WORKFLOW, "provider_id": provider_id,
             "tasks": json_manifest(name)["tasks"], "slots": ComfyProvider.workflow_slots(),
             "resolution": _workflow_resolution(provider_id, None),
             "seconds": {"min": 0.2, "max": 15.0, "tier3_max": 8, "integer_only": False}}
            for name in h3.JINYU_WORKFLOWS
        ]
    raise ValueError(f"未知 Provider {provider_id}")


def capability_reason(provider_id, request):
    params = request.params
    def supplied(slot):
        return request.inputs.get(slot) or params.get(slot)

    catalog = _CATALOGS.get(provider_id)
    try:
        if catalog is None:
            spec = json_manifest(h3.workflow_id(request.workflow))
            slots = ComfyProvider.workflow_slots()
        else:
            _, spec = catalog.get_workflow_spec(request.workflow)
            slots = catalog.workflow_media_caps(spec)
    except ValueError:
        return "workflow_unavailable"

    if request.task is not None and request.task not in spec["tasks"]:
        return "unsupported_task"
    if catalog is not None and (supplied("image_path") or supplied("first_frame_path")):
        return "unsupported_first_frame"
    for slot in ("images", "audios", "videos"):
        media = supplied(slot)
        if isinstance(media, (list, tuple)) and len(media) > slots[slot]:
            return f"unsupported_{slot}" if not slots[slot] else f"{slot}_slot_limit"
    if catalog is not None and "trim_to_requested_seconds" in params:
        return "unsupported_trim_to_requested_seconds"
    if "aspect" in params:
        aspect = params["aspect"]
        if provider_id == "chenyu":
            options = spec["aspect_options"]
            supported_aspects = (*options, *options.values())
        else:
            supported_aspects = h3.JINYU_ASPECT_SIZES if catalog is None else spec["sizes"]
        if not isinstance(aspect, str) or aspect not in supported_aspects:
            return "unsupported_aspect"
    if provider_id == "chenyu":
        if "seed" in params:
            return "unsupported_seed"
        if "steps" in params:
            return "unsupported_steps"
        seconds = params.get("seconds", 5)
        if type(seconds) in (int, float) and not float(seconds).is_integer():
            return "unsupported_fractional_seconds"
        if params.keys() & {"width", "height"}:
            return "unsupported_dimensions"
    elif catalog is None:
        if "steps" in params and params["steps"] != spec["steps"]:
            return "unsupported_steps"
        if params.keys() & {"megapixels", "upscale_2x", "save_last_frame"}:
            return "unsupported_provider_params"
    elif params.keys() & {"megapixels", "upscale_2x"}:
        return "unsupported_provider_params"
    if request.simulated_gpu is not None and catalog is not None:
        return "unsupported_simulated_gpu"
    return None


def probe_provider(provider_id, config, *, remote=False, workflow=None):
    if provider_id == "colab-comfy" and sys.platform == "win32":
        return ProbeResult(provider_id, False, WINDOWS_COLAB_REASON, ("video",))
    try:
        return get_provider(provider_id, config).probe(capability="video", check_remote=remote, workflow=workflow)
    except ProviderError as exc:
        return ProbeResult(provider_id, False, exc.code, ("video",), {"detail": config.redact(str(exc))})
    except (OSError, ValueError) as exc:
        return ProbeResult(provider_id, False, "provider_config_invalid", ("video",), {"detail": config.redact(str(exc))})


@dataclass
class Route:
    provider: Provider | None
    skipped: list[dict]


def resolve_provider(request, config):
    request.workflow = normalize_workflow(request.workflow, request.provider_id)
    if request.provider_id is not None:
        return Route(get_provider(request.provider_id, config), [])
    skipped = []
    for provider_id in configured_provider_order(config):
        reason = (WINDOWS_COLAB_REASON if provider_id == "colab-comfy" and sys.platform == "win32"
                  else capability_reason(provider_id, request))
        if reason:
            skipped.append({"provider_id": provider_id, "reason": reason})
            continue
        try:
            provider = get_provider(provider_id, config)
            probe = provider.probe(capability=request.capability, workflow=request.workflow)
        except (OSError, ValueError):
            skipped.append({"provider_id": provider_id, "reason": "provider_config_invalid"})
            continue
        if probe.available:
            return Route(provider, skipped)
        skipped.append({"provider_id": provider_id, "reason": probe.reason, **dict(probe.details)})
    return Route(None, skipped)
