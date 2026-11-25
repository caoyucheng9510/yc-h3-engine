"""Read-only merged settings and explicit private .env updates."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .fsutil import atomic_write_bytes, file_lock, is_secret_key, redact_data, sha256_bytes

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7798
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*")
_DOUBLE_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SINGLE_QUOTED = re.compile(r"'([^']*)'")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "b": "\b", "f": "\f"}
_CONFIG_PREFIXES = ("YC_H3_", "COLAB_", "RUNNINGHUB_", "CHENYU_")


def default_data_dir() -> Path:
    return Path.home() / ".yc-h3-engine"


def global_env_path() -> Path:
    return default_data_dir() / ".env"


def _parse_line(line: str, line_number: int) -> tuple[str, str, str] | None:
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    error = ValueError(f".env 第 {line_number} 行格式无效")
    match = _ASSIGNMENT.match(line)
    if match is None:
        raise error
    key, raw = match.group(1), line[match.end():]
    if raw.startswith(("'", '"')):
        pattern = _SINGLE_QUOTED if raw[0] == "'" else _DOUBLE_QUOTED
        quoted = pattern.match(raw)
        if quoted is None:
            raise error
        value, tail = quoted.group(1), raw[quoted.end():]
        if tail.strip() and not tail.lstrip().startswith("#"):
            raise error
        if raw[0] == '"':
            value = re.sub(
                r"\\(?:u[0-9a-fA-F]{4}|.)",
                lambda m: chr(int(m[0][2:], 16)) if len(m[0]) == 6 else _ESCAPES.get(m[0][1], m[0]),
                value,
            )
        return key, value, tail
    value, comment, remainder = raw.partition("#")
    return key, value.strip(), value[len(value.rstrip()):] + comment + remainder


def _parse_env(text: str) -> dict[str, str]:
    values = {}
    for number, line in enumerate(text.split("\n"), 1):
        if parsed := _parse_line(line.rstrip("\r"), number):
            key, value, _ = parsed
            values[key] = value
    return values


def read_env_file(path: Path | str) -> dict[str, str]:
    """Read a UTF-8 .env file without modifying process settings or disk."""
    try:
        raw = Path(path).expanduser().read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(".env 必须为 UTF-8 编码") from None
    return _parse_env(raw)


def update_env_file(path: Path | str, updates: Mapping[str, str], *, overwrite: bool = False) -> Path:
    """Atomically create or update .env, preserving existing keys by default."""
    target = Path(path).expanduser().absolute()
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str):
            raise ValueError(".env 更新须使用有效键名与字符串值")
    with file_lock(target.with_name(f".{target.name}.lock")):
        if target.is_symlink():
            raise ValueError(".env 不能是符号链接")
        exists = target.exists()
        raw = target.read_bytes() if exists else b""
        try:
            original = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(".env 必须为 UTF-8 编码") from None
        lines = re.findall(r"[^\n]*\n|[^\n]+$", original)
        newline = "\r\n" if "\r\n" in original else "\n"
        seen = set()
        edited = []
        for number, line in enumerate(lines, 1):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            parsed = _parse_line(body, number)
            if parsed is not None:
                key, _, tail = parsed
                seen.add(key)
                if overwrite and key in updates:
                    prefix = _ASSIGNMENT.match(body)[0]
                    line = prefix + json.dumps(updates[key], ensure_ascii=False) + tail + ending
            edited.append(line)
        added = [f"{key}={json.dumps(value, ensure_ascii=False)}{newline}" for key, value in updates.items() if key not in seen]
        if added and edited and not edited[-1].endswith(("\r", "\n")):
            edited.append(newline)
        updated = "".join(edited + added).encode("utf-8")
        if raw.startswith(b"\xef\xbb\xbf"):
            updated = b"\xef\xbb\xbf" + updated
        if not exists or updated != raw:
            atomic_write_bytes(target, updated)
        if os.name != "nt":
            os.chmod(target, 0o600)
    return target


def credential_fingerprint(credential: str) -> str:
    return sha256_bytes(credential.strip().encode("utf-8"))


def _secret_values(layers: Iterable[Mapping[str, str]]) -> frozenset[str]:
    return frozenset(
        value for layer in layers for key, value in layer.items() if value.strip() and is_secret_key(key)
    )


@dataclass(frozen=True)
class EngineConfig:
    data_dir: Path
    cwd: Path
    env_files: tuple[Path, ...]  # Lowest to highest priority.
    config_identity: str
    _values: Mapping[str, str] = field(repr=False)
    secrets: frozenset[str] = field(repr=False)

    @classmethod
    def load(cls, **options) -> EngineConfig:
        return load_config(**options)

    @property
    def data_dir_hash(self) -> str:
        return sha256_bytes(os.path.normcase(str(self.data_dir)).encode("utf-8"))

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def redaction_secrets(self, *runtime_tokens: str) -> frozenset[str]:
        return self.secrets.union(token for token in runtime_tokens if token)

    def redact(self, value, *runtime_tokens: str):
        return redact_data(value, *self.redaction_secrets(*runtime_tokens))


def load_config(
    *, env_file: Path | str | None = None, data_dir: Path | str | None = None,
    cwd: Path | str | None = None, environ: Mapping[str, str] | None = None,
) -> EngineConfig:
    """Merge independent config sources by key without creating anything."""
    working_dir = Path(cwd or Path.cwd()).expanduser().resolve()
    environment = dict(os.environ if environ is None else environ)
    layers = [environment]
    env_files = []
    explicit = working_dir / Path(env_file).expanduser() if env_file is not None else None
    for candidate, required in (
        (global_env_path(), False), (working_dir / ".env", False), (explicit, True),
    ):
        if candidate is None:
            continue
        try:
            layer = read_env_file(candidate)
        except FileNotFoundError:
            if required:
                raise FileNotFoundError("未找到 --env-file 指定的 .env") from None
            continue
        absolute = candidate.resolve()
        if absolute in env_files:
            previous = env_files.index(absolute)
            env_files.pop(previous)
            layers.pop(previous + 1)
        env_files.append(absolute)
        layers.append(layer)

    values = {key: value for layer in layers for key, value in layer.items() if value.strip()}
    selected_dir = data_dir if data_dir is not None else values.get("YC_H3_DATA_DIR")
    root = (working_dir / Path(selected_dir).expanduser()).resolve() if selected_dir is not None else default_data_dir().resolve()
    identity_values = {
        key: credential_fingerprint(value) if is_secret_key(key) else value
        for key, value in values.items()
        if key.startswith(_CONFIG_PREFIXES) and key != "YC_H3_DATA_DIR"
    }
    identity = sha256_bytes(json.dumps(
        [os.path.normcase(str(root)), identity_values], sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8"))
    return EngineConfig(root, working_dir, tuple(env_files), identity, MappingProxyType(values), _secret_values(layers))
