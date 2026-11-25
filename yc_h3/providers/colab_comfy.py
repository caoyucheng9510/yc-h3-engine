"""Managed ComfyUI over the isolated, loopback Colab daemon."""
from __future__ import annotations

from ..config import load_config
from .base import ProbeResult, ProviderConflictError
from .comfy_client import LocalDaemonClient
from .comfy_common import ComfyProvider


def require_colab():
    # This Batch 2 entry point remains safe to import even on native Windows.
    from ..local.platform_guard import require_colab_platform

    require_colab_platform()


def check_daemon_identity(client, config):
    health = client.health()
    if (health.get("ok") is not True or health.get("app") != "yc-h3-engine"
            or health.get("data_dir_hash") != config.data_dir_hash
            or health.get("config_identity") != config.config_identity):
        raise ProviderConflictError(
            f"守护进程身份不匹配（PID {health.get('pid', '未知')}，端口 {getattr(client, 'port', '未知')}）。"
            "请使用原配置停止守护进程，或为当前配置选择其他端口。",
            code="daemon_identity_conflict",
        )
    return health


def colab_cli_status(config, *, remote=False):
    import os

    from ..fsutil import find_bin
    from ..local.config import Settings, colab_bin

    path = colab_bin(config)
    details = {"colab": os.path.isfile(path) and os.access(path, os.X_OK), "ssh": bool(find_bin("ssh"))}
    if remote and details["colab"]:
        from ..local.colab import Colab

        data, _ = Colab(Settings(engine_config=config)).usage()
        details.update({key: data.get(key) for key in ("balance", "rate", "cli_ok")})
    return details


class ColabComfyProvider(ComfyProvider):
    provider_id = "colab-comfy"
    pricing_mode = "managed_colab_session"

    def __init__(self, *, client=None, config=None, host=None, port=None, **options):
        require_colab()
        config = config or load_config()
        super().__init__(client=client if client is not None else LocalDaemonClient(
            config=config, host=host, port=port,
        ), config=config, **options)

    def _check_endpoint(self):
        require_colab()
        check_daemon_identity(self.client, self.config)

    def probe(self, *, capability=None, check_remote=False, workflow=None):
        result = super().probe(capability=capability, workflow=workflow)
        if not check_remote:
            return result
        details = {**result.details, **colab_cli_status(self.config, remote=True)}
        return ProbeResult(self.provider_id, result.available, result.reason, self.capabilities, details)

    def remote_workflows(self):
        self._check_endpoint()
        status, result = self.client._request_json("GET", "/api/local/workflows", timeout=20)
        if status != 200:
            raise self.client._error(status, result)
        return result
