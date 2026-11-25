import builtins
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from yc_h3 import chenyu_gpu, fsutil
from yc_h3.chenyu_gpu import ChenyuGpuManager
from yc_h3.config import credential_fingerprint, load_config
from yc_h3.ledger import list_balances, record_usage, set_balance
from yc_h3.providers.base import ProviderError
from yc_h3.tasks_store import TaskStore


class OfflinePlatform:
    origin = "https://www.chenyu.cn"
    instance_id = "vm-test-123"

    def __init__(self, manager, *, create_failure=None):
        self.key = manager.config.get("CHENYU_API_KEY")
        self.fingerprint = credential_fingerprint(self.key)
        self.manager = manager
        self.create_failure = create_failure
        self.created = None
        self.stopped = False
        self.calls = []
        self.list_attempts = 0
        self.first_guard_rejected = False
        self.images = []
        self.skip_lookup = 0

    def instances(self):
        self.list_attempts += 1
        if self.created and not self.stopped:
            if self.skip_lookup:
                self.skip_lookup -= 1
                return []
            return [dict(self.created)]
        return []

    def instance(self, instance_uuid):
        assert instance_uuid == self.instance_id
        self.calls.append(("/instance/info", instance_uuid))
        return dict(self.created or {}, status=22 if self.stopped else 2, save_image_status=2 if self.images else None)

    def balance(self):
        return {"balance": 10, "card_balance": 44}

    def request(self, route, *, payload=None, query=None):
        self.calls.append((route, payload))
        if route == "/gpu/list":
            return []
        if route == "/instance/create_by_image":
            placement = self.manager.state.read()
            assert placement["allocation_pending"] and not placement["guards_ready"]
            assert placement["deadline_at"] == int(placement["allocated_at"]) + placement["max_runtime_minutes"] * 60
            assert "instance_uuid" not in placement
            self.created = {
                "instance_uuid": self.instance_id, "status": 2,
                "create_time": int(placement["allocated_at"]),
                "gpu_uuid": payload["gpu_uuid"], "image_uuid": payload["image_uuid"], "title": "platform-default",
            }
            if self.create_failure is not None:
                self.create_failure()
                raise ProviderError("synthetic timeout", code="platform_transport_error")
            return {"instance_uuid": self.instance_id}
        if route == "/instance/shutdown_timer" and self.first_guard_rejected:
            self.first_guard_rejected = False
            raise ProviderError("tool unavailable while starting")
        if route in ("/instance/destroy", "/instance/shutdown"):
            assert payload["instance_uuid"] == self.instance_id
            self.stopped = True
        if route == "/bill/list":
            return {"order_list": [
                {"remark": self.instance_id, "charge_channel": 1, "amount": -1.25},
                {"remark": self.instance_id, "charge_channel": 2, "amount": -0.75},
            ]}
        if route == "/image/private/list":
            return self.images
        if route == "/instance/save_image":
            assert payload["instance_uuid"] == self.instance_id
            self.images = [{"image_uuid": "image-001", "save_image_status": 2}]
            self.stopped = True
            return self.images[0]
        return {}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    engine = load_config(data_dir=tmp_path / "engine", cwd=tmp_path, environ={"CHENYU_API_KEY": "offline-api-key"})
    manager = ChenyuGpuManager(config=engine, sleep_fn=lambda seconds: None)
    platform = OfflinePlatform(manager)
    monkeypatch.setattr(manager, "_platform", lambda: platform)
    return engine, manager, platform


def test_windows_import_and_state_read_write_without_posix(tmp_path, monkeypatch):
    script = """
import sys, builtins
original = builtins.__import__
def reject(name, *args, **kwargs):
    if name == 'fcntl' or name.startswith(('yc_h3.local', 'yc_h3.remote.setup_env')):
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = reject
from yc_h3.chenyu_gpu import ChenyuGpuManager, ChenyuRemoteClient
assert not any(name.startswith(('yc_h3.local', 'yc_h3.remote.setup_env')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True)

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            if name in {"O_NOFOLLOW", "fchmod", "chmod"}:
                pytest.fail(f"POSIX-only access: {name}")
            return getattr(os, name)

    calls = []
    msvcrt = SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2,
        locking=lambda fd, operation, size: calls.append((operation, size)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    original_import = builtins.__import__

    def reject_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            pytest.fail("Windows state accessed fcntl")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_fcntl)
    monkeypatch.setattr(chenyu_gpu, "os", WindowsOS())
    monkeypatch.setattr(fsutil, "os", WindowsOS())
    state = chenyu_gpu.ChenyuGpuState(tmp_path / "engine" / "state.json")
    assert state.read() == {} and not state.path.parent.exists()
    record = {"instance_uuid": "offline-id", "runtime_auth_token": "x" * 40}
    with state.lock():
        state.write(record)
        assert state.read() == record
        assert "runtime_auth_token" not in state.path.read_text(encoding="utf-8")
        state.write({})
    assert state.read() == {}
    assert calls == [(1, 1), (2, 1)]


@pytest.mark.parametrize("platform_clock_lag", [0, 2])
def test_create_reply_loss_recovers_with_original_deadline_in_same_command(managed, monkeypatch, platform_clock_lag):
    _, manager, platform = managed
    now = [1000.5]
    monkeypatch.setattr(chenyu_gpu, "time", SimpleNamespace(time=lambda: now[0]))

    def lose_reply():
        now[0] += 22
        platform.skip_lookup = 2
        platform.created["create_time"] -= platform_clock_lag

    platform.create_failure = lose_reply
    options = {"gpu": "6000D-84G", "max_runtime_minutes": 1, "idle_minutes": 0}
    plan = manager.gpu_plan(**options)
    result = manager.gpu_up(**options, approve_digest=plan["digest"], connect=False)
    assert result["instance"]["deadline_at"] == 1060
    assert result["instance"]["guards_ready"]
    timeline = [route for route, _ in platform.calls]
    assert timeline.index("/instance/shutdown_timer") < timeline.index("/instance/info")
    assert timeline.count("/instance/create_by_image") == 1
    assert dict(platform.calls)["/instance/shutdown_timer"]["shutdown_time"] == 1060
    assert dict(platform.calls)["/instance/set_idle_close"]["idle_period_minutes"] == 0


def test_create_reply_loss_after_deadline_destroys_and_rechecks(managed, monkeypatch):
    engine, manager, platform = managed
    now = [1000.5]
    monkeypatch.setattr(chenyu_gpu, "time", SimpleNamespace(time=lambda: now[0]))

    def lose_reply():
        now[0] += 61

    platform.create_failure = lose_reply
    options = {"max_runtime_minutes": 1}
    plan = manager.gpu_plan(**options)
    with pytest.raises(ProviderError) as error:
        manager.gpu_up(**options, approve_digest=plan["digest"], connect=False)
    assert error.value.code == "gpu_deadline_expired"
    assert platform.stopped and manager.state.read() == {}
    assert "/instance/shutdown_timer" not in dict(platform.calls)
    assert (engine.data_dir / "ledger/usage.jsonl").is_file()


@pytest.mark.parametrize("bound", [False, True])
def test_reconcile_restores_guards_and_retries_after_running(managed, monkeypatch, bound):
    _, manager, platform = managed
    monkeypatch.setattr(chenyu_gpu, "time", SimpleNamespace(time=lambda: 1022))
    record = {
        "session_name": "yh3-h3", "endpoint_origin": platform.origin, "credential_fingerprint": platform.fingerprint,
        "source_kind": "image", "source_id": chenyu_gpu.DEFAULT_BASE_IMAGE_UUID,
        "gpu": "6000D-84G", "allocated_at": 1000, "deadline_at": 1060, "max_runtime_minutes": 1,
        "hourly_cny": 3, "idle_minutes": 30, "guards_ready": False, "allocation_pending": not bound,
    }
    if bound:
        record["instance_uuid"] = platform.instance_id
    else:
        record["known_instance_ids"] = ["vm-previous"]
    with manager.state.lock():
        manager.state.write(record)
    platform.created = {"instance_uuid": platform.instance_id, "status": 2, "create_time": 1000,
                        "gpu_uuid": chenyu_gpu.GPU_PRESETS["6000D-84G"]["gpu_uuid"],
                        "image_uuid": chenyu_gpu.DEFAULT_BASE_IMAGE_UUID}
    platform.first_guard_rejected = True
    result = manager.gpu_reconcile()
    assert result["instance"]["guards_ready"]
    assert result["instance"]["deadline_at"] == 1060
    calls = [payload["shutdown_time"] for route, payload in platform.calls if route == "/instance/shutdown_timer"]
    assert calls == [1060, 1060]


@pytest.mark.parametrize("undelivered", [None, 1])
def test_save_image_checks_three_versions_under_lifecycle_lock(managed, tmp_path, monkeypatch, undelivered):
    engine, manager, platform = managed
    plan = manager.gpu_plan()
    manager.gpu_up(approve_digest=plan["digest"], connect=False)
    record = manager.state.read()
    record.update(connected=True, endpoint="https://fixture.example", runtime_auth_token="x" * 40)
    with manager.state.lock():
        manager.state.write(record)

    store = TaskStore(config=engine)
    logical = tmp_path / "same.mp4"
    job_ids = ["job-v1", "job-v2", "job-v3"]
    current = None
    for version, job_id in enumerate(job_ids, 1):
        _, current = store.begin_comfy_submit(
            "same-task", request_digest="digest", logical_target=str(logical),
            session_name=record["session_name"], vm_host=platform.instance_id, instance_id="scheduler-1",
            instance_uuid=platform.instance_id, provider="chenyu-comfy", credential_fingerprint=platform.fingerprint,
            force_new=current is not None, replaced_submission_id=(current or {}).get("submission_id"),
        )
        identity = current["submission_id"]
        current = store.mark_comfy_submitted("same-task", job_id, expected_submission_id=identity)
        if version != undelivered:
            artifact = logical if version == 1 else logical.with_name(f"same_v{version}.mp4")
            artifact.write_bytes(f"fixture version {version}".encode())
            current = store.mark_delivered("same-task", artifact_path=artifact,
                                           artifact_sha256=fsutil.sha256_file(artifact), expected_submission_id=identity)

    route_calls = []

    class Remote:
        def health(self):
            return {"instance_id": "scheduler-1"}

        def _json(self, method, path, *, timeout):
            assert path == "/api/maintenance/job-inventory"
            with pytest.raises(TimeoutError):
                with fsutil.file_lock(manager.state.path.with_name("." + manager.state.path.name + ".lock"), timeout=0):
                    pytest.fail("inventory fetched outside lifecycle lock")
            return {"complete": True, "session": record["session_name"], "instance_id": "scheduler-1",
                    "platform_instance_id": platform.instance_id,
                    "jobs": [{"id": job_id, "status": "done"} for job_id in job_ids]}

        def _request_json(self, method, path, **kwargs):
            route_calls.append(path)
            return 200, {"ok": True}

    monkeypatch.setattr(manager, "_client", lambda record: Remote())
    approved = manager._save_image_plan(platform, record)["digest"]
    if undelivered:
        with pytest.raises(ProviderError) as error:
            manager.gpu_save_image(approve_digest=approved)
        assert error.value.code == "pending_comfy_deliveries"
        assert "yc-h3 jobs fetch same-task --remote-job-id job-v1" in str(error.value)
        assert not route_calls and "/instance/save_image" not in dict(platform.calls)
    else:
        assert manager.gpu_save_image(approve_digest=approved)["image_uuid"] == "image-001"
        assert route_calls == ["/api/maintenance/cleanup-for-image"]


def test_save_image_fails_closed_if_remote_inventory_is_unavailable(managed, monkeypatch):
    _, manager, platform = managed
    plan = manager.gpu_plan()
    manager.gpu_up(approve_digest=plan["digest"], connect=False)

    class Remote:
        def health(self):
            return {"instance_id": "scheduler-1"}

        def _json(self, *args, **kwargs):
            raise ProviderError("synthetic unavailable inventory")

    monkeypatch.setattr(manager, "_client", lambda record: Remote())
    approved = manager._save_image_plan(platform, manager.state.read())["digest"]
    with pytest.raises(ProviderError) as error:
        manager.gpu_save_image(approve_digest=approved)
    assert error.value.code == "job_inventory_unverified"
    assert "/instance/save_image" not in dict(platform.calls)


def test_stop_uses_same_chenyu_account_and_dual_wallet_snapshot(managed):
    engine, manager, platform = managed
    set_balance(config=engine, provider="chenyu", unit="CNY", balance=57,
                cash_balance=11, card_balance=46, ts="2026-01-01T00:00:00+00:00", source="api")
    record_usage(config=engine, provider="chenyu", record_type="job", action="gen", quantity=1, unit="CNY",
                 estimated=False, idempotency_key="fixture-job", ts="2026-01-01T00:01:00+00:00")
    plan = manager.gpu_plan()
    manager.gpu_up(approve_digest=plan["digest"], connect=False)
    assert manager.gpu_stop()["settled"]
    assert manager.gpu_stop()["status"] == "absent"
    usage = [json.loads(row) for row in (engine.data_dir / "ledger/usage.jsonl").read_text().splitlines()]
    assert len(usage) == 2
    gpu = usage[-1]
    assert (gpu["provider"], gpu["wallet_owner"], gpu["account_fp"], gpu["quantity"]) == (
        "chenyu-comfy", "chenyu", platform.fingerprint, 2,
    )
    wallet = list_balances(config=engine)[-1]
    assert wallet["account_fp"] == platform.fingerprint
    assert (wallet["wallet_owner"], wallet["cash_balance"], wallet["card_balance"]) == ("chenyu", 10, 44)


def test_chenyu_instance_endpoint_accepts_platform_routing_suffix_and_rejects_foreign_domain():
    assert chenyu_gpu.validate_instance_endpoint(
        "https://fixture-id89.zone.chenyu.cn", "fixture-id",
    ) == "https://fixture-id89.zone.chenyu.cn"
    with pytest.raises(ValueError):
        chenyu_gpu.validate_instance_endpoint("https://fixture-id89.zone.chenyu.cn.example.com", "fixture-id")


@pytest.mark.parametrize("observation", ["list_error", "unmatched_instance"])
def test_unresolved_create_keeps_placeholder_and_blocks_duplicate_submission(managed, monkeypatch, observation):
    _, manager, platform = managed

    def lose_reply():
        platform.skip_lookup = 100

    platform.create_failure = lose_reply
    options = {"max_runtime_minutes": 1}
    plan = manager.gpu_plan(**options)
    with pytest.raises(ProviderError) as error:
        manager.gpu_up(**options, approve_digest=plan["digest"], connect=False)
    assert error.value.code == "needs_reconcile"
    placement = manager.state.read()
    assert placement["allocation_pending"] and not placement["guards_ready"]
    assert "instance_uuid" not in placement
    with pytest.raises(ProviderError) as error:
        manager.gpu_up(**options, approve_digest=plan["digest"], connect=False)
    assert error.value.exit_code == 3
    assert [route for route, _ in platform.calls].count("/instance/create_by_image") == 1

    observed = iter([False, True, False, False])

    def instances():
        if not next(observed):
            return []
        if observation == "list_error":
            raise ProviderError("synthetic lookup timeout", code="platform_transport_error")
        return [{"instance_uuid": "vm-new-unmatched", "status": 2}]

    monkeypatch.setattr(platform, "instances", instances)
    with pytest.raises(ProviderError) as error:
        manager.gpu_reconcile(clear_missing=True)
    assert error.value.code == "needs_reconcile"
    assert manager.state.read() == placement


@pytest.mark.parametrize("runtime, reconcile_at, clear_missing", [
    pytest.param(2, 1061, False, id="after-propagation-grace"),
    pytest.param(1, 1060, False, id="at-deadline"),
    pytest.param(2, 1001, True, id="explicit-clear"),
])
def test_reconcile_clears_unallocated_gpu_after_confirmed_absence(
    managed, monkeypatch, runtime, reconcile_at, clear_missing,
):
    engine, manager, platform = managed
    now = [1000.5]
    monkeypatch.setattr(chenyu_gpu, "time", SimpleNamespace(time=lambda: now[0]))

    def fail_to_allocate():
        platform.created = None

    platform.create_failure = fail_to_allocate
    options = {"max_runtime_minutes": runtime}
    plan = manager.gpu_plan(**options)
    with pytest.raises(ProviderError) as error:
        manager.gpu_up(**options, approve_digest=plan["digest"], connect=False)
    assert error.value.code == "needs_reconcile"
    placement = manager.state.read()
    assert placement["allocation_pending"] and placement["known_instance_ids"] == []
    with pytest.raises(ProviderError) as error:
        manager.gpu_reconcile()
    assert error.value.code == "needs_reconcile"
    assert manager.state.read() == placement

    now[0] = reconcile_at
    before = platform.list_attempts
    assert manager.gpu_reconcile(clear_missing=clear_missing) == {"status": "cleared", "session_name": "yh3-h3"}
    assert platform.list_attempts - before == 4
    assert manager.state.read() == {}
    assert manager.gpu_plan(**options)["action"] == "allocate"
    assert not (engine.data_dir / "ledger/usage.jsonl").exists()


def test_chenyu_warn_submission_accepts_matching_approve_digest_without_ack(managed, monkeypatch):
    _, manager, platform = managed
    monkeypatch.setattr(chenyu_gpu, "time", SimpleNamespace(time=lambda: 1000))
    record = {
        "status": 2, "guards_ready": True, "deadline_at": 1060, "hourly_cny": 3,
        "endpoint": f"https://{platform.instance_id}.zone.chenyu.cn", "endpoint_origin": platform.origin,
        "credential_fingerprint": platform.fingerprint, "runtime_auth_token": "synthetic-" + "x" * 32,
    }
    client = manager._client(record)
    context = {"billing_rate": 3}
    remote_digest = "a" * 64
    digest = client._submission_digest(remote_digest, context, record["deadline_at"])
    facts = {"status": "warn", "preflight_digest": digest, "remote_preflight_digest": remote_digest,
             "billing_context": context}
    monkeypatch.setattr(client, "_prepare", lambda _: (200, facts, []))
    submitted = []

    def submit(method, route, *, payload, timeout):
        submitted.append((method, route, payload))
        return 200, {"job_id": "fixture-job"}

    monkeypatch.setattr(client, "_request_json", submit)
    request = {"task": "t2v", "params": {"prompt": "Offline submission"}}
    for approval in ({}, {"approve_digest": "b" * 64}, {"preflight_digest": digest}):
        assert client.submit_job({**request, **approval})[0] == 409
    assert not submitted
    assert client.submit_job({**request, "approve_digest": digest}) == (200, {"job_id": "fixture-job"})
    method, route, payload = submitted[0]
    assert (method, route) == ("POST", "/api/jobs")
    assert payload["preflight_digest"] == remote_digest and payload["ack_warnings"] is True
