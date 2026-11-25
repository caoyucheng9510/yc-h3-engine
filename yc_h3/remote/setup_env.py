"""Pure ComfyUI runtime, venv, and custom node installer (idempotent; safe to re-run).

Adapted from WorldForge (MIT License); all Lyra 2.0, Qwen-Image, Breeze-TTS,
and Diffusers branches have been removed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


if __package__:
    from . import _util
    from .workflows.minimax_h3.spec import COMFY_MAIN, COMFY_REVISION
else:
    import _util
    from workflows.minimax_h3.spec import COMFY_MAIN, COMFY_REVISION

remote_source_files = _util.remote_source_files
remote_source_version = _util.remote_source_version


BASE_DIR = _util.remote_root()
COMFY_DIR = Path(os.environ.get("YC_H3_COMFY_DIR") or f"{BASE_DIR}/ComfyUI")
H3_VENV = Path(os.environ.get("YC_H3_H3_VENV") or f"{BASE_DIR}/h3_venv")
H3_PY = H3_VENV / "bin/python"
COMFY_MODELS_DIR = Path(os.environ.get("YC_H3_MODELS_DIR") or COMFY_DIR / "models").expanduser().absolute()
COMFY_EXTRA_DEPS = ("websocket-client", "huggingface_hub", "Pillow", "gdown==6.4.2", "safetensors")
MODEL_FOLDERS = frozenset({
    "diffusion_models",
    "text_encoders",
    "vae",
    "loras",
    "latent_upscale_models",
    "checkpoints",
    "controlnet",
})
_COMFY_LOCK_FDS = ContextVar("comfy_lock_fds", default=())
CUSTOM_NODES = {
    "comfyui-SelfLift": ("https://github.com/facok/comfyui-SelfLift.git", "835c3cf7919410f71f414688f049c6ca497789d0"),
    "ComfyUI-KJNodes": ("https://github.com/kijai/ComfyUI-KJNodes.git", "aa2b5ea5e7f61b51f5d26d726f23c1d74e951016"),
    "ComfyUI-YCNodes-MiniMax-H3": (
        "https://github.com/yichengup/ComfyUI-YCNodes-MiniMax-H3.git",
        "efbe83f69ffa97a251a575ce42edc7bf8005da0c",
    ),
}


@dataclass(frozen=True)
class ComfyRuntime:
    profile_id: str
    checkout: Path
    venv: Path
    python: Path
    marker: Path


def comfy_runtime(profile_id=COMFY_MAIN):
    if profile_id != COMFY_MAIN:
        raise ValueError(f"无效 ComfyUI Profile：{profile_id}")
    return ComfyRuntime(profile_id, COMFY_DIR, H3_VENV, H3_PY, H3_VENV / ".yc-h3-engine-h3")


@contextmanager
def _comfy_lock(path, label, timeout, check_cancel):
    import fcntl

    if _COMFY_LOCK_FDS.get():
        raise RuntimeError("ComfyUI 运行时锁与模型锁不可嵌套")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            check_cancel()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待 ComfyUI {label}锁超时")
                time.sleep(0.2)
        held = _COMFY_LOCK_FDS.set((lock.fileno(),))
        try:
            yield
        finally:
            _COMFY_LOCK_FDS.reset(held)


def comfy_runtime_lock(profile_id=COMFY_MAIN, timeout=7200, check_cancel=lambda: None):
    comfy_runtime(profile_id)
    path = os.environ.get("YC_H3_H3_LOCK", "/tmp/yc-h3-engine-h3-runtime.lock")
    return _comfy_lock(path, "运行时", timeout, check_cancel)


def h3_runtime_lock(timeout=7200, check_cancel=lambda: None):
    return comfy_runtime_lock(COMFY_MAIN, timeout, check_cancel)


def comfy_models_lock(models_dir=None, timeout=7200, check_cancel=lambda: None):
    root = Path(models_dir) if models_dir is not None else COMFY_MODELS_DIR
    return _comfy_lock(root / ".yc-h3-engine-models.lock", "模型目录", timeout, check_cancel)


def _h3_run(args, timeout=900, **kw):
    return subprocess.run(args, check=True, timeout=timeout, pass_fds=_COMFY_LOCK_FDS.get(), **kw)


def _pin_checkout(directory, repo, revision):
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").is_dir():
        _h3_run(["git", "init", str(directory)])
    current = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True,
        encoding="utf-8", errors="replace",
        timeout=15,
    )
    if current.stdout.strip() != revision:
        _h3_run(["git", "-C", str(directory), "fetch", "--depth", "1", repo, revision], timeout=300)
    if (directory / ".git" / "HEAD").read_text().strip() != revision:
        _h3_run(["git", "-C", str(directory), "checkout", "--detach", revision], timeout=120)


def _torch_stack(python):
    probe = (
        "import json, torch, triton; "
        "print(json.dumps([torch.__version__, torch.version.cuda, triton.__version__, "
        "torch.cuda.is_available()]))"
    )
    result = _h3_run([str(python), "-c", probe], timeout=45, capture_output=True, encoding="utf-8", errors="replace")
    torch, cuda, triton, available = json.loads(result.stdout.strip().splitlines()[-1])

    def _ver(value):
        return tuple(map(int, re.match(r"^[0-9]+(?:\.[0-9]+)*", value).group().split(".")))

    if not available or not cuda or _ver(cuda) < (12, 0) or _ver(torch) < (2, 8) or _ver(triton) < (3, 4):
        raise RuntimeError(f"ComfyUI CUDA 环境不兼容：Torch {torch}, CUDA {cuda}, Triton {triton}")
    return torch, cuda, triton


def _install_comfy_requirements(venv_py, requirements, extras=()):
    packages = []
    for raw in requirements.read_text().splitlines() if requirements.is_file() else ():
        line = raw.partition("#")[0].strip()
        name = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0].lower()
        if line and name not in {"torch", "torchvision", "torchaudio", "triton"}:
            packages.append(line)
    if packages or extras:
        pinned, excluded = [], []
        for library in ("torch", "torchvision", "torchaudio", "triton"):
            try:
                pinned.append(f"{library}=={version(library)}")
                excluded.append(library)
            except PackageNotFoundError:
                pass
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as constraints, tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt"
        ) as excludes:
            constraints.write("\n".join(pinned) + "\n")
            constraints.flush()
            excludes.write("\n".join(excluded) + "\n")
            excludes.flush()
            uv = shutil.which("uv")
            cmd = (
                [uv, "pip", "install", "--python", str(venv_py), "--no-cache"]
                + (["--excludes", excludes.name] if excluded else [])
                if uv
                else [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir"]
            )
            _h3_run([*cmd, "--constraint", constraints.name, *packages, *extras])


def _install_h3_requirements(requirements, extras=()):
    _install_comfy_requirements(H3_PY, requirements, extras)


def _deps_fingerprint():
    return _util.sha256_bytes("\n".join(COMFY_EXTRA_DEPS).encode())


def _comfy_dependencies_ready(python):
    code = (
        "import gdown, PIL, safetensors, websocket, huggingface_hub; "
        "from importlib.metadata import version; "
        "raise SystemExit(version('gdown') != '6.4.2')"
    )
    try:
        _h3_run([str(python), "-c", code], timeout=20, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=128)
def _cached_comfy_dependencies_ready(profile_id, comfy_dir, venv_py, revision, marker_mtime_ns):
    return _comfy_dependencies_ready(venv_py)


def comfy_runtime_installed(profile_id=COMFY_MAIN, revision=COMFY_REVISION):
    runtime = comfy_runtime(profile_id)
    try:
        return (
            runtime.python.is_file()
            and (runtime.checkout / "main.py").is_file()
            and (runtime.checkout / ".git" / "HEAD").read_text().strip() == revision
            and runtime.marker.read_text().strip() == f"{revision}\n{_deps_fingerprint()}"
            and _cached_comfy_dependencies_ready(
                profile_id,
                str(runtime.checkout),
                str(runtime.python),
                revision,
                runtime.marker.stat().st_mtime_ns,
            )
        )
    except OSError:
        return False


def h3_runtime_installed():
    return comfy_runtime_installed()


def custom_node_installed(name, profile_id=COMFY_MAIN):
    """Strictly read-only check whether a whitelisted custom node is pinned and installed."""
    if name not in CUSTOM_NODES:
        return False
    runtime = comfy_runtime(profile_id)
    _url, revision = CUSTOM_NODES[name]
    directory = runtime.checkout / "custom_nodes" / name
    marker = runtime.venv / (".yc-h3-engine-" + name)
    try:
        return (
            directory.is_dir()
            and (directory / ".git" / "HEAD").read_text(encoding="utf-8").strip() == revision
            and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == revision
        )
    except OSError:
        return False


def setup_comfy(profile_id=COMFY_MAIN, revision=COMFY_REVISION, plugins=()):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("ComfyUI revision 必须为完整 Git SHA")
    if revision != COMFY_REVISION:
        raise ValueError("comfy-main 必须使用固定的 ComfyUI revision")
    requested_plugins = tuple(dict.fromkeys(plugins or ()))
    for plugin in requested_plugins:
        if plugin not in CUSTOM_NODES:
            raise ValueError(f"未授权的 ComfyUI 插件：{plugin}")
    runtime = comfy_runtime(profile_id)
    with comfy_runtime_lock(profile_id):
        if not comfy_runtime_installed(profile_id, revision):
            emit(0.1, "检查系统 Torch/CUDA/Triton")
            stack = _torch_stack(sys.executable)
            _pin_checkout(runtime.checkout, "https://github.com/Comfy-Org/ComfyUI.git", revision)
            if not runtime.python.is_file():
                uv = shutil.which("uv")
                cmd = (
                    [uv, "venv", "--system-site-packages", "--python", sys.executable, str(runtime.venv)]
                    if uv
                    else [sys.executable, "-m", "venv", "--system-site-packages", str(runtime.venv)]
                )
                try:
                    _h3_run(cmd, timeout=180)
                except BaseException:
                    shutil.rmtree(runtime.venv, ignore_errors=True)
                    raise
            emit(0.55, "安装 ComfyUI 依赖")
            _install_comfy_requirements(runtime.python, runtime.checkout / "requirements.txt", COMFY_EXTRA_DEPS)
            if _torch_stack(runtime.python) != stack:
                raise RuntimeError("ComfyUI 解释器必须复用已核验的系统 Torch/CUDA/Triton")
            if not _comfy_dependencies_ready(runtime.python):
                raise RuntimeError("ComfyUI 扩展依赖未就绪")
            temp = runtime.marker.with_suffix(".tmp")
            _util.atomic_write_text(runtime.marker, f"{revision}\n{_deps_fingerprint()}\n", temp=temp)
            _cached_comfy_dependencies_ready.cache_clear()
        for plugin in requested_plugins:
            emit(0.85, f"安装 ComfyUI 插件 {plugin}")
            _ensure_custom_node_locked(plugin, profile_id)
    emit(1.0, f"{profile_id} 运行时已安装")


def setup_minimax(profile_id=COMFY_MAIN, revision=COMFY_REVISION, plugins=()):
    setup_comfy(profile_id=profile_id, revision=revision, plugins=plugins)


def ensure_custom_node(name):
    with h3_runtime_lock():
        _ensure_custom_node_locked(name)


def _ensure_custom_node_locked(name, profile_id=COMFY_MAIN):
    if name not in CUSTOM_NODES:
        raise ValueError(f"未授权的 ComfyUI 插件：{name}")
    runtime = comfy_runtime(profile_id)
    url, revision = CUSTOM_NODES[name]
    directory = runtime.checkout / "custom_nodes" / name
    _pin_checkout(directory, url, revision)
    marker = runtime.venv / (".yc-h3-engine-" + name)
    if not marker.is_file() or marker.read_text().strip() != revision:
        _install_comfy_requirements(runtime.python, directory / "requirements.txt")
        marker.write_text(revision + "\n")


def emit(p, stage):
    print("@@FORGE@@ " + json.dumps({"event": "progress", "p": p, "stage": stage}, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="yc-h3-engine ComfyUI runtime setup")
    ap.add_argument("--component", default="comfy", choices=["comfy", "minimax"])
    ap.add_argument("--profile", default=COMFY_MAIN)
    ap.add_argument("--revision", default=COMFY_REVISION)
    ap.add_argument("--plugins", default="", help="逗号分隔的 ComfyUI 插件列表")
    args = ap.parse_args(argv)
    plugins_list = [p.strip() for p in (args.plugins or "").split(",") if p.strip()]
    Path(BASE_DIR).mkdir(parents=True, exist_ok=True)
    print(f"数据目录 ({BASE_DIR}) 磁盘剩余: {shutil.disk_usage(BASE_DIR).free / 1e9:.0f} GB", flush=True)
    if args.component == "minimax":
        setup_minimax(profile_id=args.profile, revision=args.revision, plugins=plugins_list)
    else:
        setup_comfy(profile_id=args.profile, revision=args.revision, plugins=plugins_list)
    emit(1.0, "全部完成")
    print("SETUP_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
