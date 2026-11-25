"""HTTPS origin, credential and redirect rules shared by external providers."""
from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional, Tuple

from ..config import EngineConfig, load_config
from ..fsutil import is_secret_key
from .base import ProviderError

TRUSTED_HOSTS_ENV = "YC_H3_TRUSTED_API_HOSTS"
CLIENT_REJECT_HTTP_CODES = frozenset({400, 401, 403, 422})
TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


def allowed_hosts(
    defaults: Iterable[str], extra_hosts: Iterable[str] = (), *, config: EngineConfig | None = None,
) -> frozenset[str]:
    hosts = set()
    configured = (config or load_config()).get(TRUSTED_HOSTS_ENV, "")
    for item in (*defaults, *configured.split(","), *extra_hosts):
        cleaned = str(item or "").strip().lower()
        if cleaned:
            hosts.add(cleaned)
    return frozenset(hosts)


def validate_api_key(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Validate an API key for safe use in HTTP Authorization headers.

    Returns ``(cleaned_key, error_code)`` where ``error_code`` is ``None`` if valid,
    ``"missing_api_key"`` if empty, or ``"invalid_api_key"`` if containing CR/LF/NUL,
    control characters, embedded whitespace, or non-ASCII characters.
    """
    if raw is None:
        return None, "missing_api_key"
    raw_str = str(raw)
    if not raw_str:
        return None, "missing_api_key"
    if "\r" in raw_str or "\n" in raw_str or "\x00" in raw_str:
        return None, "invalid_api_key"
    cleaned = raw_str.strip(" \t")
    if not cleaned:
        return None, "missing_api_key"
    for ch in cleaned:
        code = ord(ch)
        if code < 0x21 or code >= 0x7F:
            return None, "invalid_api_key"
    return cleaned, None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block all 3xx redirects on authenticated API requests so Bearer tokens never leak."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise ProviderError(
            f"认证请求拒绝 HTTP {code} 重定向，已拦截以防密钥外泄",
            code="redirect_blocked",
            exit_code=3,
            details={"http_status": int(code)},
        ) from None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class HttpsOnlyDownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow HTTPS-only redirects for artifact downloads while stripping sensitive headers."""

    @staticmethod
    def _validate_redirect_url(req, code: int, newurl: Any) -> str:
        cleaned = str(newurl or "").strip()
        try:
            resolved = urllib.parse.urljoin(req.full_url, cleaned)
            parsed = urllib.parse.urlsplit(resolved)
            _ = parsed.port
        except ValueError:
            raise ProviderError(
                f"成品下载拒绝 HTTP {int(code)} 不安全重定向（reason=invalid_redirect_url，scheme=unknown）",
                code="insecure_download_redirect",
                exit_code=3,
                details={
                    "http_status": int(code),
                    "reason": "invalid_redirect_url",
                    "scheme": "unknown",
                },
            ) from None
        scheme = (parsed.scheme or "").lower()
        if scheme != "https" or not parsed.hostname or parsed.username is not None:
            if scheme != "https":
                reason = "non_https_scheme"
            elif parsed.username is not None:
                reason = "userinfo_not_allowed"
            else:
                reason = "missing_hostname"
            raise ProviderError(
                f"成品下载拒绝 HTTP {int(code)} 不安全重定向（reason={reason}，scheme={scheme or 'unknown'}）",
                code="insecure_download_redirect",
                exit_code=3,
                details={
                    "http_status": int(code),
                    "reason": reason,
                    "scheme": scheme or "unknown",
                },
            ) from None
        return resolved

    def http_error_302(self, req, fp, code, msg, headers):
        newurl = headers.get("location") or headers.get("uri")
        if newurl is not None:
            try:
                self._validate_redirect_url(req, code, newurl)
            except ProviderError:
                if fp is not None:
                    try:
                        fp.close()
                    except Exception:  # noqa: BLE001
                        pass
                raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = self._validate_redirect_url(req, code, newurl)
        new_req = super().redirect_request(req, fp, code, msg, headers, resolved)
        if new_req is not None:
            for hdr in list(new_req.headers):
                if is_secret_key(hdr):
                    new_req.headers.pop(hdr, None)
            for hdr in list(new_req.unredirected_hdrs):
                if is_secret_key(hdr):
                    new_req.unredirected_hdrs.pop(hdr, None)
        return new_req


def normalize_api_base(api_base: str) -> Tuple[str, str]:
    """Normalize ``api_base`` to ``https://host[:port]`` and return ``(origin, hostname)``.

    Rejects non-HTTPS schemes, userinfo, query strings, fragments, and non-root paths.
    """
    raw = str(api_base or "").strip()
    if not raw:
        raise ValueError("api_base 不能为空")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        raise ValueError("api_base 地址无效") from None
    if parsed.scheme.lower() != "https":
        raise ValueError("api_base 必须使用 https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_base 不允许包含 userinfo")
    if "?" in raw or "#" in raw:
        raise ValueError("api_base 不允许包含 query 或 fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("api_base 必须是纯源地址 scheme://host[:port]，不允许附加路径")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("api_base 缺少有效主机名")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("api_base 端口无效") from None
    if port == 0:
        raise ValueError("api_base 端口无效")
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"https://{host_display}" + (f":{port}" if port is not None else "")
    return origin, hostname
