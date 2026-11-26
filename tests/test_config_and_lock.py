import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yc_h3 import fsutil
from yc_h3.config import load_config, read_env_file, update_env_file
from yc_h3.providers._http import allowed_hosts, normalize_api_base


@pytest.fixture
def config_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    anchor = home / ".yc-h3-engine"
    anchor.mkdir(parents=True)
    work.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return anchor, work


def test_env_merge_fixed_anchor_precedence_and_zero_write(config_tree):
    anchor, work = config_tree
    (anchor / ".env").write_bytes(
        b"\xef\xbb\xbfGLOBAL='with # and spaces'\r\n"
        b"CHENYU_API_KEY=global-key-for-test\r\nYC_H3_DATA_DIR=state\r\n"
    )
    (work / ".env").write_text('COLAB_CNY_PER_CU=0.25\nGLOBAL= # fallback\nSHARED="local # value"\n', encoding="utf-8")
    (work / "explicit.env").write_text(
        'export SHARED="explicit \\"quote\\"" # comment\nCHENYU_API_KEY=\nYC_H3_TRUSTED_API_HOSTS=example.test\n',
        encoding="utf-8",
    )
    environ = {"FALLBACK": "process", "CHENYU_API_KEY": "process-key-for-test", "OTHER_API_TOKEN": "secondary-token"}
    cfg = load_config(cwd=work, env_file="explicit.env", environ=environ)
    assert cfg.data_dir == work / "state"
    assert cfg.env_files == (anchor / ".env", work / ".env", work / "explicit.env")
    assert (cfg.get("GLOBAL"), cfg.get("SHARED"), cfg.get("FALLBACK")) == (
        "with # and spaces", 'explicit "quote"', "process",
    )
    assert cfg.get("CHENYU_API_KEY") == "global-key-for-test"
    assert allowed_hosts(["Base.TEST"], config=cfg) == {"base.test", "example.test"}
    assert cfg.secrets == {"global-key-for-test", "process-key-for-test", "secondary-token"}
    assert "global-key-for-test" not in repr(cfg)
    assert cfg.redact({"detail": "global-key-for-test and Bearer opaque", "runtime_auth_token": "unknown"}) == {
        "detail": "[REDACTED] and [REDACTED]", "runtime_auth_token": "[REDACTED]",
    }
    assert cfg.redact("runtime-value", "runtime-value") == "[REDACTED]"
    assert not cfg.data_dir.exists()

    other = load_config(cwd=work, env_file="explicit.env", data_dir="override", environ=environ)
    assert other.data_dir == work / "override"
    assert other.get("GLOBAL") == cfg.get("GLOBAL")
    assert other.config_identity != cfg.config_identity
    assert cfg.data_dir_hash != other.data_dir_hash
    assert cfg.config_identity == load_config(cwd=work, env_file="explicit.env", environ=environ).config_identity
    (work / ".env").write_text("COLAB_CNY_PER_CU=0.5\n", encoding="utf-8")
    assert cfg.config_identity != load_config(cwd=work, env_file="explicit.env", environ=environ).config_identity
    with pytest.raises(FileNotFoundError, match="--env-file"):
        load_config(cwd=work, env_file="missing.env")
    assert not other.data_dir.exists()


def test_redaction_collects_credentials_without_host_auth_settings(config_tree):
    _, work = config_tree
    (work / ".env").write_text("MONITORING_ACCESS_TOKEN=synthetic-monitoring-token\n", encoding="utf-8")
    settings = {
        "DISABLE_AUTH": "1",
        "NPM_CONFIG_ALWAYS_AUTH": "true",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "AUTH_MODE": "interactive",
        "AUTH_TOKEN_TYPE": "Bearer",
        "AUTH_ENABLED": "enabled",
        "AUTH_URL": "https://example.test",
        "AUTH_HOST": "localhost",
        "AUTH_PORT": "123456",
        "AUTH_DIR": "/tmp/agent",
        "AUTH_PATH": "/tmp/agent/auth",
        "AUTH_TIMEOUT": "120000",
        "AUTH_COUNT": "100000",
        "OTHER_AUTH": " abcd ",
        "EMPTY_AUTH": " ",
    }
    chenyu_key = "synthetic-chenyu-api-key"
    runninghub_key = "synthetic-runninghub-api-key"
    cfg = load_config(cwd=work, environ={
        **settings, "CHENYU_API_KEY": chenyu_key, "RUNNINGHUB_API_KEY": runninghub_key,
    })

    assert cfg.secrets == {chenyu_key, runninghub_key, "synthetic-monitoring-token"}
    assert cfg.redact(settings) == {
        **settings, "DISABLE_AUTH": "[REDACTED]", "NPM_CONFIG_ALWAYS_AUTH": "[REDACTED]",
        "OTHER_AUTH": "[REDACTED]", "EMPTY_AUTH": "[REDACTED]",
    }
    assert cfg.redact({
        "detail": f"job-1 true {chenyu_key} {runninghub_key} synthetic-monitoring-token",
        "accessToken": "other-credential",
        "credential_status": {"runninghub": "configured", "chenyu": "missing_api_key", "leaked": chenyu_key},
        "RUNNINGHUB_API_KEY": "configured",
    }) == {
        "detail": "job-1 true [REDACTED] [REDACTED] [REDACTED]",
        "accessToken": "[REDACTED]",
        "credential_status": {"runninghub": "configured", "chenyu": "missing_api_key", "leaked": "[REDACTED]"},
        "RUNNINGHUB_API_KEY": "[REDACTED]",
    }


def test_known_api_keys_allow_short_credentials_but_skip_switch_literals(config_tree):
    _, work = config_tree
    cfg = load_config(cwd=work, environ={"CHENYU_API_KEY": "abc12", "RUNNINGHUB_API_KEY": " TrUe "})
    assert cfg.secrets == {"abc12"}
    assert cfg.redact("credential abc12 TrUe") == "credential [REDACTED] TrUe"


def test_env_updates_preserve_unedited_content_and_private_permissions(tmp_path):
    path = tmp_path / "new" / ".env"
    update_env_file(path, {"CHENYU_API_KEY": "first # key", "OTHER": '中文\t\r\n"\\'})
    assert read_env_file(path) == {"CHENYU_API_KEY": "first # key", "OTHER": '中文\t\r\n"\\'}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    original = b"\xef\xbb\xbf# keep\r\nCHENYU_API_KEY=first # note\r\nUNCHANGED='x # y'\r\nEMPTY=\r\n"
    path.write_bytes(original)
    update_env_file(path, {"CHENYU_API_KEY": "new", "EMPTY": "filled"})
    assert path.read_bytes() == original
    update_env_file(path, {"CHENYU_API_KEY": "new", "EXTRA": "value"}, overwrite=True)
    assert path.read_bytes() == original.replace(b"first", b'"new"') + b'EXTRA="value"\r\n'
    assert read_env_file(path)["CHENYU_API_KEY"] == "new"


def test_malformed_env_and_url_errors_do_not_echo_credentials(tmp_path):
    path = tmp_path / ".env"
    path.write_text('CHENYU_API_KEY="invalid-secret\n')
    with pytest.raises(ValueError, match="第 1 行") as exc:
        read_env_file(path)
    assert "invalid-secret" not in str(exc.value)
    with pytest.raises(ValueError) as exc:
        normalize_api_base("https://user:invalid-secret@example.test")
    assert "invalid-secret" not in str(exc.value)


def test_atomic_state_write_failure_preserves_original(tmp_path):
    path = tmp_path / "state.json"
    fsutil.atomic_write_text(path, "before")
    with pytest.raises(RuntimeError):
        with fsutil.atomic_writer(path) as stream:
            stream.write(b"incomplete")
            raise RuntimeError("cancel")
    assert path.read_bytes() == b"before"
    assert list(tmp_path.iterdir()) == [path]


def test_real_process_lock_timeout_and_release(tmp_path):
    lock = tmp_path / "lock"
    code = """
import sys
from pathlib import Path
from yc_h3.fsutil import file_lock
try:
    with file_lock(Path(sys.argv[1]), timeout=0.15):
        print("acquired")
except TimeoutError:
    print("busy")
"""
    def child():
        return subprocess.check_output([sys.executable, "-c", code, str(lock)], encoding="utf-8").strip()

    with fsutil.file_lock(lock):
        assert child() == "busy"
    assert child() == "acquired"


def test_simulated_windows_lock_eof_timeout_unlock_and_atomic_write(tmp_path, monkeypatch):
    fake_os = Mock(wraps=os)
    fake_os.name = "nt"
    for flag in ("O_RDWR", "O_CREAT", "O_RDONLY", "O_WRONLY", "O_EXCL", "SEEK_SET"):
        setattr(fake_os, flag, getattr(os, flag))
    fake_os.O_BINARY = getattr(os, "O_BINARY", 0)
    fake_os.chmod.side_effect = AssertionError("POSIX chmod called")
    fake_os.fchmod = Mock(side_effect=AssertionError("fchmod called"))
    del fake_os.O_NOFOLLOW
    events = []
    lock = tmp_path / "win.lock"
    mod = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)

    def locking(fd, mode, count):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        events.append((mode, count))
        if len(events) in {1, 4}:
            raise OSError(errno.EACCES, "busy")

    mod.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", mod)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setattr(fsutil, "os", fake_os)
    with pytest.raises(RuntimeError):
        with fsutil.file_lock(lock, timeout=1):
            raise RuntimeError("verify finally")
    with pytest.raises(TimeoutError):
        with fsutil.file_lock(lock, timeout=0):
            pytest.fail("contended lock succeeded")
    assert events == [(1, 1), (1, 1), (2, 1), (1, 1)]

    target = tmp_path / "state"
    staging = tmp_path / ".state.tmp"
    fake_os.O_BINARY = getattr(os, "O_BINARY", 1 << 29)

    def windows_open(path, flags, *args):
        if Path(path).is_dir():
            raise OSError(errno.EACCES, "directory handles unsupported")
        if path == staging:
            assert flags & fake_os.O_BINARY
            if not hasattr(os, "O_BINARY"):
                flags &= ~fake_os.O_BINARY
        return os.open(path, flags, *args)

    fake_os.open.side_effect = windows_open
    content = b"first\nsecond\x1a\x00"
    with fsutil.atomic_writer(target, temp=staging) as stream:
        stream.write(content)
    assert target.read_bytes() == content
    assert not staging.exists()
