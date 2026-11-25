"""Trusted ComfyUI manifests, public Drive LoRA registrations, and verified model files.

Adapted from WorldForge (MIT License); personal Google Drive OAuth / rclone sync removed.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

if __package__:
    from . import _util, setup_env
else:
    import _util
    import setup_env

canonical_json = _util.canonical_json
file_sha256 = _util.sha256_file

HEADROOM = 15 * 1024**3
CUSTOM_ASSET_MAX_BYTES = int(os.environ.get("YC_H3_CUSTOM_ASSET_MAX_BYTES", 4 * 1024**3))
DOWNLOAD_TIMEOUT = 1800
ASSET_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,128}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DTYPE_BYTES = dict.fromkeys(("BOOL", "U8", "I8", "F8_E4M3", "F8_E5M2", "F8_E8M0"), 1) | {
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
    "C64": 8,
}


def _size(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError("size_bytes 必须为正整数")
    return value


def _sha(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not SHA256.fullmatch(value.lower()):
        raise ValueError("sha256 必须为 64 位十六进制字符")
    return value.lower()


def normalize_custom_assets(table):
    if not isinstance(table, dict):
        raise ValueError("自定义资产表必须是以 id 为键的对象")
    normalized = {}
    for key, asset in table.items():
        if not isinstance(asset, dict) or set(asset) - {"id", "source", "gdrive_id", "sha256", "size_bytes"}:
            raise ValueError("自定义资产只允许 id、source、gdrive_id、sha256、size_bytes")
        if not isinstance(key, str) or not ASSET_ID.fullmatch(key) or asset.get("id") != key:
            raise ValueError(f"无效资产 id：{key}")
        drive_id = asset.get("gdrive_id")
        if asset.get("source") != "gdrive" or not isinstance(drive_id, str) or not DRIVE_ID.fullmatch(drive_id):
            raise ValueError(f"{key} 必须使用 source=gdrive 和规范化的 gdrive_id")
        normalized[key] = {
            "id": key,
            "source": "gdrive",
            "gdrive_id": drive_id,
            "sha256": _sha(asset.get("sha256"), optional=True),
            "size_bytes": _size(asset.get("size_bytes"), optional=True),
        }
    return normalized


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def read_custom_assets(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return {}
    return normalize_custom_assets(json.loads(raw, object_pairs_hook=_unique_pairs))


class MissingCustomAssets(ValueError):
    def __init__(self, ids):
        self.ids = ids
        super().__init__(f"未登记自定义 LoRA：{', '.join(ids)}")


def freeze_custom_assets(params, read_table):
    params = dict(params)
    if "_frozen_custom_assets" in params:
        raise ValueError("_frozen_custom_assets 由服务端生成")
    ids = params.get("custom_loras", [])
    single = params.get("custom_lora_id", params.get("custom_lora"))
    if single is not None:
        if ids:
            raise ValueError("请只使用一个自定义 LoRA 参数")
        ids = [single]
    if not isinstance(ids, list) or any(not isinstance(key, str) or not ASSET_ID.fullmatch(key) for key in ids):
        raise ValueError("custom_loras 须为资产 id 列表")
    refresh = params.get("refresh", False)
    if type(refresh) is not bool:
        raise ValueError("refresh 必须为布尔值")
    if ids:
        table = read_table()
        missing = sorted(set(ids) - table.keys())
        if missing:
            raise MissingCustomAssets(missing)
        frozen = {}
        for key in ids:
            frozen[key] = dict(table[key], refresh=refresh)
        params["_frozen_custom_assets"] = frozen
    return params


def _asset_path(root, relative):
    relative = Path(relative)
    if (
        relative.is_absolute()
        or len(relative.parts) < 2
        or ".." in relative.parts
        or relative.parts[0] not in setup_env.MODEL_FOLDERS
    ):
        raise ValueError(f"无效 ComfyUI 模型路径：{relative}")
    root = Path(root).resolve()
    target = root / relative
    if target.resolve() != target:
        raise ValueError(f"ComfyUI 模型路径包含符号链接：{relative}")
    return target


def _custom_relative(asset, digest=None):
    stem = f"{asset['id']}__{asset['gdrive_id'][:12]}"
    digest = asset.get("sha256") or digest
    suffix = f"__{digest[:8]}" if digest else ""
    return f"loras/custom/{stem}{suffix}.safetensors"


def custom_cache_spec(asset, models_dir):
    spec = dict(asset, comfy_rel_path=_custom_relative(asset))
    if spec.get("sha256"):
        return spec
    final = _asset_path(models_dir, spec["comfy_rel_path"])
    source_tag = _util.sha256_bytes(spec["gdrive_id"].encode())
    source_index = final.with_name(final.name + ".yc-h3-engine-source-" + source_tag)
    spec["_source_index"] = source_index
    try:
        digest = _sha(json.loads(source_index.read_bytes()))
    except (OSError, ValueError):
        return spec
    return dict(spec, comfy_rel_path=_custom_relative(spec, digest))


def _source_key(asset):
    source = asset.get("source", "huggingface")
    if source == "huggingface":
        return [source, asset["repo"], asset["revision"], asset["repo_path"]]
    return [source, asset["gdrive_id"]]


def validate_assets(assets, models_dir):
    signatures = {}
    for asset in assets:
        source = asset.get("source", "huggingface")
        if source not in {"huggingface", "gdrive"}:
            raise ValueError(f"不允许的资产来源：{source}")
        _sha(asset.get("sha256"), optional=source == "gdrive")
        _size(asset.get("size_bytes"), optional=source == "gdrive")
        if source == "gdrive" and not DRIVE_ID.fullmatch(str(asset.get("gdrive_id", ""))):
            raise ValueError("无效 gdrive_id")
        final = _asset_path(models_dir, asset["comfy_rel_path"])
        if asset.get("sha256"):
            previous = signatures.setdefault(final, asset["sha256"])
            if previous != asset["sha256"]:
                raise ValueError(f"同一模型路径声明了不同 SHA-256：{asset['comfy_rel_path']}")


def _file_signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _receipt_path(final):
    return final.with_name(final.name + ".yc-h3-engine-verified")


def _read_receipt(path):
    try:
        return json.loads(_receipt_path(path).read_bytes())
    except (OSError, ValueError):
        return None


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _util.atomic_write_bytes(path, canonical_json(value))


def _check_safetensors(path):
    try:
        from safetensors import safe_open

        with safe_open(str(path), framework="pt", device="cpu") as tensors:
            keys = tensors.keys()
            if not keys:
                raise ValueError("权重至少需要一个张量")
            for key in keys:
                tensors.get_slice(key).get_shape()
    except ImportError:
        _check_safetensors_binary(path)


def _check_safetensors_binary(path):
    with path.open("rb") as stream:
        raw_size = stream.read(8)
        header_size = int.from_bytes(raw_size, "little")
        if len(raw_size) != 8 or not 2 <= header_size <= min(100_000_000, path.stat().st_size - 8):
            raise ValueError("safetensors header 长度无效")
        header = json.loads(stream.read(header_size), object_pairs_hook=_unique_pairs)
    if not isinstance(header, dict):
        raise ValueError("safetensors header 必须是对象")
    metadata = header.pop("__metadata__", {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise ValueError("safetensors metadata 无效")
    intervals = []
    for tensor in header.values():
        if not isinstance(tensor, dict):
            raise ValueError("safetensors 张量无效")
        dtype, shape, offsets = tensor.get("dtype"), tensor.get("shape"), tensor.get("data_offsets")
        if (
            not isinstance(dtype, str)
            or dtype not in DTYPE_BYTES
            or not isinstance(shape, list)
            or any(type(n) is not int or n < 0 for n in shape)
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(type(n) is not int or n < 0 for n in offsets)
        ):
            raise ValueError("safetensors 张量类型、形状或偏移无效")
        start, end = offsets
        if end - start != math.prod(shape) * DTYPE_BYTES[dtype]:
            raise ValueError("safetensors 张量长度无效")
        intervals.append((start, end))
    end = 0
    for start, stop in sorted(intervals):
        if start != end:
            raise ValueError("safetensors 张量区间有空洞或重叠")
        end = stop
    if not intervals or path.stat().st_size != 8 + header_size + end:
        raise ValueError("safetensors 张量为空或文件截断")


def _payload_digest(path, asset, check_cancel):
    if not path.is_file() or path.is_symlink():
        return None
    size = path.stat().st_size
    expected_size = asset.get("size_bytes")
    if expected_size is None and size > CUSTOM_ASSET_MAX_BYTES:
        raise ValueError("Drive 资产超过 CUSTOM_ASSET_MAX_BYTES")
    if not size or (expected_size is not None and size != expected_size):
        return None
    if path.name.removesuffix(".part").lower().endswith(".safetensors"):
        try:
            _check_safetensors(path)
        except Exception as exc:
            raise ValueError(f"safetensors 校验失败：{path.name}") from exc
    digest = file_sha256(path, check_cancel)
    return digest if not asset.get("sha256") or digest == asset["sha256"] else None


def weight_ready(asset, models_dir):
    final = _asset_path(models_dir, asset["comfy_rel_path"])
    try:
        if not final.is_file():
            return False
        signature = _file_signature(final)
        expected = asset.get("sha256")
        size = asset.get("size_bytes")
        if size is not None and signature[2] != size:
            return False
        receipt = _read_receipt(final)
        if isinstance(receipt, dict):
            observed = receipt.get("sha256")
            if not isinstance(observed, str) or not SHA256.fullmatch(observed):
                return False
            if size is None and signature[2] > CUSTOM_ASSET_MAX_BYTES:
                return False
            return (
                receipt.get("source_key") == _source_key(asset)
                and receipt.get("sig") == signature
                and receipt.get("size_bytes") == signature[2]
                and (not expected or observed == expected)
            )
        if expected and receipt == [expected, signature]:
            if final.suffix.lower() == ".safetensors":
                _check_safetensors(final)
            return True
    except Exception:
        return False
    return False


def _has_digest(path, asset, check_cancel=lambda: None):
    return bool(_payload_digest(path, asset, check_cancel))


def _write_receipt(path, asset, digest=None):
    if digest is None:
        digest = _payload_digest(path, asset, lambda: None)
        if digest is None:
            raise ValueError("无法为未通过校验的权重写入回执")
    write_json_atomic(
        _receipt_path(path),
        {
            "source_key": _source_key(asset),
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "sig": _file_signature(path),
        },
    )


def _check_space(root, missing):
    need = sum(CUSTOM_ASSET_MAX_BYTES if asset.get("size_bytes") is None else asset["size_bytes"] for asset in missing) + HEADROOM
    free = shutil.disk_usage(root).free
    if free < need:
        raise RuntimeError(f"ComfyUI 实例盘空间不足：需 {need / 1024**3:.1f} GiB，可用 {free / 1024**3:.1f} GiB")


def _hub_download(asset, partial):
    code = (
        "import os,sys; from huggingface_hub import hf_hub_download; "
        "repo,rev,name,root,dst=sys.argv[1:]; "
        "src=hf_hub_download(repo_id=repo, revision=rev, filename=name, local_dir=root, cache_dir=os.path.join(root, '.hub')); "
        "os.replace(src,dst)"
    )
    with tempfile.TemporaryDirectory(prefix=".hf-", dir=partial.parent) as directory:
        env = dict(os.environ, HF_HUB_DISABLE_XET="1", HF_HUB_ETAG_TIMEOUT="30", HF_HUB_DOWNLOAD_TIMEOUT="90")
        setup_env._h3_run(
            [
                str(asset.get("_venv_py") or sys.executable),
                "-c",
                code,
                asset["repo"],
                asset["revision"],
                asset["repo_path"],
                directory,
                str(partial),
            ],
            env=env,
            timeout=DOWNLOAD_TIMEOUT,
        )


def _download_asset(asset, partial, models_dir, remaining, check_cancel):
    source = asset.get("source", "huggingface")
    if source == "gdrive":
        partial.unlink(missing_ok=True)
        setup_env._h3_run(
            [
                str(asset.get("_venv_py") or sys.executable),
                "-m",
                "gdown",
                f"https://drive.google.com/uc?id={asset['gdrive_id']}",
                "-O",
                str(partial),
            ],
            timeout=DOWNLOAD_TIMEOUT,
        )
        return
    aria2 = shutil.which("aria2c")
    control = partial.with_name(partial.name + ".aria2")
    url = "https://huggingface.co/{}/resolve/{}/{}".format(
        asset["repo"], asset["revision"], urllib.parse.quote(asset["repo_path"])
    )
    if aria2:
        try:
            setup_env._h3_run(
                [
                    aria2,
                    "-c",
                    "-x",
                    "16",
                    "-s",
                    "16",
                    "--max-tries=3",
                    "--timeout=30",
                    "--auto-file-renaming=false",
                    "--allow-overwrite=true",
                    "--dir",
                    str(partial.parent),
                    "--out",
                    partial.name,
                    url,
                ],
                timeout=DOWNLOAD_TIMEOUT,
            )
            if digest := _payload_digest(partial, asset, check_cancel):
                control.unlink(missing_ok=True)
                return digest
        except (subprocess.CalledProcessError, ValueError):
            pass
    check_cancel()
    partial.unlink(missing_ok=True)
    control.unlink(missing_ok=True)
    _check_space(models_dir, remaining)
    _hub_download(asset, partial)


def ensure_weights(
    assets,
    models_dir=None,
    report=lambda p, stage: None,
    check_cancel=lambda: None,
    *,
    venv_py=None,
    custom_assets=None,
):
    models_dir = Path(models_dir) if models_dir is not None else setup_env.COMFY_MODELS_DIR
    specs = {key: dict(asset, _venv_py=venv_py) for key, asset in assets.items()}
    validate_assets(specs.values(), models_dir)
    customs = {}
    for key, entry in (custom_assets or {}).items():
        spec = normalize_custom_assets({key: {k: v for k, v in entry.items() if k != "refresh"}})[key]
        if key in specs:
            raise ValueError(f"自定义资产与工作流资产同名：{key}")
        customs[key] = dict(
            spec,
            comfy_rel_path=_custom_relative(spec),
            _venv_py=venv_py,
            refresh=entry.get("refresh", False),
        )
    validate_assets([*specs.values(), *customs.values()], models_dir)
    specs.update(customs)
    resolved = {}
    with setup_env.comfy_models_lock(models_dir, check_cancel=check_cancel):
        missing = []
        for key, asset in specs.items():
            check_cancel()
            if key in customs:
                asset = custom_cache_spec(asset, models_dir)
            final = _asset_path(models_dir, asset["comfy_rel_path"])
            receipt = _read_receipt(final)
            if isinstance(receipt, dict):
                if (asset.get("sha256") and receipt.get("sha256") != asset["sha256"]) or (
                    key in customs and receipt.get("source_key") != _source_key(asset)
                ):
                    raise ValueError(f"模型来源或 SHA-256 与现有回执冲突：{asset['comfy_rel_path']}")
            if not asset.get("refresh") and weight_ready(asset, models_dir):
                if isinstance(receipt, list):
                    _write_receipt(final, asset, asset["sha256"])
                resolved[key] = asset["comfy_rel_path"]
                continue
            digest = None
            if key not in customs:
                try:
                    digest = _payload_digest(final, asset, check_cancel)
                except ValueError:
                    pass
            if digest:
                _write_receipt(final, asset, digest)
                resolved[key] = asset["comfy_rel_path"]
            else:
                missing.append((key, asset))
        remaining = [asset for _, asset in missing]
        if missing:
            _check_space(models_dir, remaining)
        for index, (key, asset) in enumerate(missing):
            check_cancel()
            final = _asset_path(models_dir, asset["comfy_rel_path"])
            final.parent.mkdir(parents=True, exist_ok=True)
            if key in customs:
                source_tag = _util.sha256_bytes(asset["gdrive_id"].encode())
                partial = final.parent / f".{asset['id']}.{source_tag}.safetensors.part"
            else:
                partial = final.with_name(final.name + ".part")
            if partial.is_symlink():
                raise ValueError("模型下载临时文件不可为符号链接")
            report(0.1 + 0.3 * index / len(missing), f"下载 {asset['id']}")
            try:
                digest = None
                if not asset.get("refresh"):
                    try:
                        digest = _payload_digest(partial, asset, check_cancel)
                    except ValueError:
                        partial.unlink(missing_ok=True)
                if not digest:
                    digest = _download_asset(asset, partial, models_dir, remaining[index:], check_cancel)
                    digest = digest or _payload_digest(partial, asset, check_cancel)
                if not digest:
                    raise ValueError(f"权重大小或 SHA-256 校验失败：{asset['id']}")
                if key in customs:
                    asset["comfy_rel_path"] = _custom_relative(asset, digest)
                    final = _asset_path(models_dir, asset["comfy_rel_path"])
                    receipt = _read_receipt(final)
                    if isinstance(receipt, dict) and (
                        receipt.get("source_key") != _source_key(asset) or receipt.get("sha256") != digest
                    ):
                        raise ValueError(f"自定义 LoRA 文件名指纹冲突：{final.name}")
                check_cancel()
                os.replace(partial, final)
                partial.with_name(partial.name + ".aria2").unlink(missing_ok=True)
                _write_receipt(final, asset, digest)
                if source_index := asset.get("_source_index"):
                    write_json_atomic(source_index, digest)
                resolved[key] = asset["comfy_rel_path"]
            except BaseException as exc:
                if asset.get("source") == "gdrive" or isinstance(exc, ValueError):
                    partial.unlink(missing_ok=True)
                raise
    return resolved
