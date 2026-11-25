"""Keep Colab's POSIX runtime off the native Windows execution path."""
from __future__ import annotations

import sys

COLAB_PLATFORM_MESSAGE = (
    "原生 Windows 无法运行 Colab 通道。请在 WSL2 中运行，"
    "或使用 runninghub、chenyu、chenyu-comfy。"
)


class ColabPlatformError(RuntimeError):
    exit_code = 3
    code = "colab_platform_unsupported"


def colab_supported() -> bool:
    return sys.platform != "win32"


def require_colab_platform() -> None:
    if not colab_supported():
        raise ColabPlatformError(COLAB_PLATFORM_MESSAGE)
