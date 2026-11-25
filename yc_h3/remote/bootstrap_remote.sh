#!/usr/bin/env bash
# Self-contained remote cold-deployment bootstrap for yc-h3-engine.
# Validates all dependencies and version constraints in requirements.txt,
# installs missing/outdated packages via pip (respecting PIP_FIND_LINKS / PIP_NO_INDEX),
# enforces loopback binding by default, and starts forge_server.py with PID tracking.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

DAEMON_MODE=0
CHECK_ONLY=0
BIND_HOST="127.0.0.1"
BIND_PORT=7860
PYTHON_BIN="${YC_H3_REMOTE_PYTHON:-python3}"
REMOTE_ROOT="${YC_H3_REMOTE_ROOT:-}"
LOG_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --daemon)
      DAEMON_MODE=1
      shift
      ;;
    --check-only)
      CHECK_ONLY=1
      shift
      ;;
    --host)
      BIND_HOST="$2"
      shift 2
      ;;
    --port)
      BIND_PORT="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --root)
      REMOTE_ROOT="$2"
      shift 2
      ;;
    --log)
      LOG_FILE="$2"
      shift 2
      ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REMOTE_ROOT" ]]; then
  if [[ -d "/content" && -w "/content" ]]; then
    REMOTE_ROOT="/content/yc_h3_engine"
  elif [[ "$HOME" == "/root" ]]; then
    REMOTE_ROOT="/root/yc_h3_engine"
  else
    REMOTE_ROOT="$HOME/.yc-h3-engine/remote"
  fi
fi
export YC_H3_REMOTE_ROOT="$REMOTE_ROOT"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$REMOTE_ROOT/cache/pip}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REMOTE_ROOT/cache/uv}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$REMOTE_ROOT/cache/huggingface}"

if [[ -z "$LOG_FILE" ]]; then
  LOG_FILE="$REMOTE_ROOT/server.log"
fi

# Validate bind and auth before checking or installing dependencies.
"$PYTHON_BIN" - "$BIND_HOST" <<'PY'
import ipaddress, os, sys

host = (sys.argv[1] or "").strip()
ok = host in ("127.0.0.1", "localhost", "::1")
if not ok:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
if os.environ.get("YC_H3_ALLOW_NON_LOOPBACK") == "1":
    token = os.environ.get("YC_H3_REMOTE_AUTH_TOKEN") or ""
    path = os.environ.get("YC_H3_REMOTE_AUTH_TOKEN_FILE")
    if not token and path:
        try:
            with open(path, "rb") as stream:
                line = stream.readline()
                token = (line[:-1].removesuffix(b"\r") if line.endswith(b"\n") else line).decode("ascii")
        except (OSError, UnicodeError):
            token = ""
    if len(token) < 32 or not all(33 <= ord(char) <= 126 for char in token):
        sys.stderr.write("开放远端监听须提供至少 32 字符的可见 ASCII 认证令牌\n")
        sys.exit(2)
    if not ok:
        try:
            ipaddress.ip_address(host)
            ok = True
        except ValueError:
            pass
if not ok:
    sys.stderr.write(
        f"拒绝绑定非 loopback 地址：{host!r}。\n"
        "算力端仅允许监听本机 loopback（127.0.0.1 / ::1 / localhost）。\n"
    )
    sys.exit(2)
PY

check_requirements() {
  "$PYTHON_BIN" - "$REQ_FILE" <<'PY'
import importlib.metadata as md
import sys
from pathlib import Path

req_path = Path(sys.argv[1])
if not req_path.is_file():
    sys.stderr.write(f"缺少依赖清单：{req_path}\n")
    sys.exit(1)

try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

missing = []
for raw_line in req_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        continue
    req = Requirement(line)
    try:
        installed = md.version(req.name)
    except md.PackageNotFoundError:
        missing.append(f"{line} (not installed)")
        continue
    if not req.specifier.contains(installed, prereleases=True):
        missing.append(f"{line} (installed {installed})")

if missing:
    for item in missing:
        sys.stderr.write(f"[bootstrap] 缺少或不满足依赖: {item}\n")
    sys.exit(1)
PY
}

if ! check_requirements; then
  echo "[bootstrap] 安装远端最小调度依赖：$REQ_FILE" >&2
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$REQ_FILE"
  check_requirements
fi

mkdir -p "$REMOTE_ROOT/run/worker_pgids"
PID_FILE="$REMOTE_ROOT/run/server.pid"

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "[bootstrap] 依赖校验通过 (ROOT=$REMOTE_ROOT, HOST=$BIND_HOST, PORT=$BIND_PORT)"
  exit 0
fi

SERVER_ARGS=("$SCRIPT_DIR/forge_server.py" "--host" "$BIND_HOST" "--port" "$BIND_PORT")

if [[ "$DAEMON_MODE" -eq 1 ]]; then
  mkdir -p "$(dirname "$LOG_FILE")"
  nohup "$PYTHON_BIN" -u "${SERVER_ARGS[@]}" >"$LOG_FILE" 2>&1 </dev/null &
  CHILD_PID=$!
  printf '%s\n' "$CHILD_PID" >"$PID_FILE"
  echo "started pid=$CHILD_PID host=$BIND_HOST port=$BIND_PORT"
else
  exec "$PYTHON_BIN" -u "${SERVER_ARGS[@]}"
fi
