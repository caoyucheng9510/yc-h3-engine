"""Managed ComfyUI job protocol and local daemon HTTP transport."""
from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import DEFAULT_HOST, DEFAULT_PORT, EngineConfig, load_config
from ..fsutil import atomic_writer
from .base import ArtifactContractError, ProviderError, parse_content_length


class DaemonHTTPError(ProviderError):
    """HTTP error raised by LocalDaemonClient."""

    def __init__(
        self, status: int, detail: Any, *, code: str = "daemon_http_error",
        config: EngineConfig | None = None, runtime_tokens: tuple[str, ...] = (),
    ) -> None:
        detail = (config or load_config()).redact(detail, *runtime_tokens)
        msg = detail.get("detail") if isinstance(detail, dict) and detail.get("detail") else str(detail)
        super().__init__(
            f"本地守护进程返回 HTTP {status}：{msg}",
            code=code,
            exit_code=3 if status in (409, 502, 503, 504) else 1,
            details={"http_status": int(status), "response": detail},
        )
        self.status = int(status)
        self.response = detail


class ManagedComfyClient:
    """Shared HTTP job operations for managed ComfyUI runtimes."""

    def _request(
        self, method: str, path: str, *, body: bytes | None = None,
        headers: dict[str, str] | None = None, timeout: float | None = None,
    ) -> tuple[int, bytes]:
        raise NotImplementedError

    def _error(self, status: int, detail: Any, *, code: str = "daemon_http_error") -> DaemonHTTPError:
        return DaemonHTTPError(status, detail, code=code, config=getattr(self, "config", None),
                               runtime_tokens=getattr(self, "runtime_tokens", ()))

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        status, raw = self._request(method, path, body=body, headers=headers, timeout=timeout)
        if not raw:
            return status, {}
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, {"detail": raw.decode("utf-8", errors="replace")[:400]}

    def health(self) -> Dict[str, Any]:
        status, data = self._request_json("GET", "/api/local/health", timeout=10.0)
        if status != 200 or not isinstance(data, dict):
            raise self._error(status, data)
        return data

    def state(self) -> Dict[str, Any]:
        status, data = self._request_json("GET", "/api/local/state", timeout=15.0)
        if status != 200 or not isinstance(data, dict):
            raise self._error(status, data)
        return data

    def preflight(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        status, data = self._request_json("POST", "/api/local/preflight", payload=body, timeout=90.0)
        if not isinstance(data, dict):
            raise self._error(status, data)
        return status, data

    def upload(self, filename: str, data: bytes) -> Dict[str, Any]:
        q = urllib.parse.urlencode({"filename": filename})
        status, raw = self._request(
            "POST",
            f"/api/local/upload?{q}",
            body=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=180.0,
        )
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {"detail": raw.decode("utf-8", errors="replace")[:400]}
        if status != 200 or not isinstance(parsed, dict) or not parsed.get("remote_path"):
            raise self._error(status, parsed, code="upload_failed")
        return parsed

    def submit_job(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        status, data = self._request_json("POST", "/api/local/jobs", payload=body, timeout=120.0)
        if not isinstance(data, dict):
            raise self._error(status, data)
        return status, data

    def list_jobs(self, limit: int = 200) -> List[Dict[str, Any]]:
        status, data = self._request_json("GET", f"/api/local/jobs?limit={int(limit)}", timeout=30.0)
        if status != 200 or not isinstance(data, list):
            raise self._error(status, data)
        return data

    def get_job(self, job_id: str) -> Dict[str, Any]:
        qid = urllib.parse.quote(job_id, safe="")
        status, data = self._request_json("GET", f"/api/local/jobs/{qid}", timeout=30.0)
        if status != 200 or not isinstance(data, dict):
            raise self._error(status, data)
        return data

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        qid = urllib.parse.quote(job_id, safe="")
        status, data = self._request_json("POST", f"/api/local/jobs/{qid}/cancel", payload={}, timeout=30.0)
        if status != 200 or not isinstance(data, dict):
            raise self._error(status, data)
        return data

    def archive_perf(self, job_id: str) -> Dict[str, Any]:
        qid = urllib.parse.quote(job_id, safe="")
        status, data = self._request_json("POST", f"/api/local/jobs/{qid}/archive_perf", payload={}, timeout=60.0)
        if status != 200 or not isinstance(data, dict):
            raise self._error(status, data, code="perf_archive_failed")
        return data

    def download_file(self, remote_path: str, dst_path: Path) -> None:
        raise NotImplementedError


def validate_loopback_host(host: str) -> str:
    cleaned = host.strip().lower()
    if cleaned == "localhost":
        return cleaned
    try:
        address = ipaddress.ip_address(cleaned)
        if address.is_loopback:
            return str(address)
    except ValueError:
        pass
    raise ValueError("守护进程地址须为回环地址（127.0.0.1、::1 或 localhost）")


class LocalDaemonClient(ManagedComfyClient):
    """Loopback-only HTTP transport for the Colab daemon."""

    def __init__(
        self, *, host: str | None = None, port: int | None = None, base_url: str | None = None,
        timeout: float = 120.0, config: EngineConfig | None = None,
        runtime_tokens: tuple[str, ...] = (),
    ) -> None:
        self.config = config or load_config()
        self.runtime_tokens = runtime_tokens
        if base_url is not None:
            try:
                parsed = urllib.parse.urlsplit(base_url.strip())
                bind_port = parsed.port
            except ValueError:
                raise ValueError("守护进程 URL 无效") from None
            if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise ValueError("守护进程 URL 须为 http://<loopback>:<port>")
            host = parsed.hostname
            port = DEFAULT_PORT if bind_port is None else bind_port
        self.host = validate_loopback_host(host if host is not None else self.config.get("YC_H3_HOST", DEFAULT_HOST))
        try:
            self.port = int(port if port is not None else self.config.get("YC_H3_PORT", str(DEFAULT_PORT)))
        except (ValueError, TypeError):
            raise ValueError("守护进程端口无效") from None
        if not 1 <= self.port <= 65535:
            raise ValueError("守护进程端口超出范围")
        self.timeout = float(timeout)
        display_host = f"[{self.host}]" if ":" in self.host else self.host
        self.origin = f"http://{display_host}:{self.port}"

    def _request(
        self, method: str, path: str, *, body: bytes | None = None,
        headers: dict[str, str] | None = None, timeout: float | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def download_file(self, remote_path: str, dst_path: Path) -> None:
        q = urllib.parse.urlencode({"path": remote_path})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=300.0)
        try:
            conn.request("GET", f"/api/local/files?{q}", headers={"Connection": "close"})
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read()[:400].decode("utf-8", errors="replace")
                raise self._error(resp.status, detail, code="download_failed")
            expected_len = parse_content_length(resp.getheader("Content-Length"))
            written = 0
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_writer(dst_path) as stream:
                while chunk := resp.read(256 * 1024):
                    stream.write(chunk)
                    written += len(chunk)
                if expected_len is not None and written != expected_len:
                    raise ArtifactContractError(
                        f"ComfyUI 成品下载不完整：预期 {expected_len} 字节，实收 {written} 字节",
                        code="incomplete_download",
                        details={"expected_bytes": expected_len, "actual_bytes": written},
                    )
        finally:
            conn.close()
