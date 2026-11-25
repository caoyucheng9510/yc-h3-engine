"""Managed ComfyUI on a Chenyu exclusive GPU instance."""
from __future__ import annotations

from ..chenyu_gpu import ChenyuGpuManager
from ..config import credential_fingerprint, load_config
from ._http import validate_api_key
from .base import ProbeResult, ProviderConflictError
from .comfy_common import ComfyProvider


class ChenyuComfyProvider(ComfyProvider):
    provider_id = "chenyu-comfy"
    pricing_mode = "managed_chenyu_session"

    def __init__(self, *, client=None, manager=None, config=None, api_base=None, **options):
        self.manager = manager or ChenyuGpuManager(config=config or load_config(), api_base=api_base)
        super().__init__(client=client if client is not None else self.manager._client(),
                         config=self.manager.config, **options)

    def _fingerprint(self):
        key, problem = validate_api_key(self.config.get("CHENYU_API_KEY"))
        if problem:
            raise ProviderConflictError("CHENYU_API_KEY 缺失或无效", code=problem)
        return credential_fingerprint(key)

    def _redact(self, value):
        if hasattr(self.client, "_record"):
            token = (self.client._record or {}).get("runtime_auth_token")
            return self.config.redact(value, token)
        return self.config.redact(value)

    def probe(self, *, capability=None, check_remote=False, workflow=None):
        result = super().probe(capability=capability, workflow=workflow)
        if not check_remote:
            return result
        from .chenyu import ChenyuProvider

        wallet = ChenyuProvider(config=self.config, api_base=self.manager.api_base,
                                auth_opener=self.manager.api_opener).probe(check_remote=True, workflow=workflow)
        return ProbeResult(self.provider_id, result.available, result.reason, self.capabilities, {
            **result.details, **dict(wallet.details), "balance_status": wallet.reason or "available",
        })

    def remote_workflows(self):
        return self.client.list_workflows()
