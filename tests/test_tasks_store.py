from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier
from uuid import UUID

import pytest

from yc_h3 import tasks_store
from yc_h3.config import credential_fingerprint, load_config
from yc_h3.fsutil import atomic_write_json, sha256_file
from yc_h3.providers._concurrency import ProviderSlots, count_active_tasks
from yc_h3.providers.base import ProviderError, publish_atomic_no_clobber
from yc_h3.tasks_store import (
    AmbiguousTaskError, TaskConflictError, TaskStore, comfy_job_delivery_state, delivery_confirmed,
    make_task_key, submission_identity,
)


def external(store, **options):
    return store.begin_external_submit(
        "take", provider="runninghub", request_digest="request-digest",
        credential_fingerprint="fingerprint", endpoint_origin="https://example.test",
        logical_target="scene/take.mp4", **options,
    )


def comfy(store, instance="instance-a", **options):
    return store.begin_comfy_submit(
        "take", provider="chenyu-comfy", request_digest="request-digest",
        session_name="yh3-test", vm_host="host-a", instance_id=instance, instance_uuid=instance,
        credential_fingerprint="fingerprint", endpoint_origin="https://example.test",
        logical_target="scene/take.mp4", **options,
    )


@pytest.mark.parametrize("begin", [external, comfy])
def test_force_new_approval_is_consumed_before_remote_submit(tmp_path, begin):
    store = TaskStore(data_dir=tmp_path / "state")
    assert store.get("take") is None
    assert store.list_tasks() == []
    assert store.find_remote_job("missing") is None
    assert not store.data_dir.exists()

    action, first = begin(store, force_new=True, replaced_submission_id=None)
    assert action == "submit" and store.get("take")["state"] == "submitting"
    assert submission_identity(first) == first["submission_id"]
    store.mark_ambiguous("take", expected_submission_id=first["submission_id"])
    with pytest.raises(AmbiguousTaskError):
        begin(store)
    with pytest.raises(TaskConflictError) as exc:
        begin(store, force_new=True, replaced_submission_id=None)
    assert exc.value.code == "replacement_approval_stale"

    barrier = Barrier(2)

    def replace_stale():
        barrier.wait()
        try:
            return begin(store, force_new=True, replaced_submission_id=first["submission_id"])[1]["submission_id"]
        except TaskConflictError as exc:
            assert exc.code == "replacement_approval_stale"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: replace_stale(), range(2)))
    assert len([attempt for attempt in attempts if attempt]) == 1
    current = store.get("take")
    assert current["submission_id"] in attempts and current["submission_id"] != first["submission_id"]
    assert current["replaced_submission_id"] == first["submission_id"]
    with pytest.raises(TaskConflictError, match="归属已变更"):
        store.mark_failed("take", error="late response", expected_submission_id=first["submission_id"])


def test_comfy_replay_only_in_same_instance_and_credential(tmp_path):
    store = TaskStore(data_dir=tmp_path)
    _, first = comfy(store)
    action, retry = comfy(store)
    assert action == "retry_idempotent"
    assert retry["client_request_id"] == first["client_request_id"]
    with pytest.raises(TaskConflictError, match="实例、凭据或源"):
        comfy(store, instance="instance-b")


def test_job_history_old_and_current_delivery_and_missing_file(tmp_path):
    store = TaskStore(data_dir=tmp_path / "data")
    _, first = comfy(store)
    store.mark_comfy_submitted("take", "job-1", expected_submission_id=first["submission_id"])
    file1 = tmp_path / "take.mp4"
    file1.write_bytes(b"version one")
    store.mark_delivered("take", artifact_path=file1, artifact_sha256=sha256_file(file1))

    _, second = comfy(store, force_new=True, replaced_submission_id=first["submission_id"])
    store.mark_comfy_submitted("take", "job-2", expected_submission_id=second["submission_id"])
    _, third = comfy(store, instance="instance-b", force_new=True, replaced_submission_id=second["submission_id"])
    store.mark_comfy_submitted("take", "job-3", expected_submission_id=third["submission_id"])
    history = store.get("take")["job_history"]
    assert [row["remote_job_id"] for row in history] == ["job-1", "job-2", "job-3"]
    assert history[0]["delivered_path"] == str(file1)
    assert history[0]["artifact_sha256"] == sha256_file(file1)
    assert [row["remote_job_id"] for row in store.list_pending_comfy_deliveries(
        provider="chenyu-comfy", session_name="yh3-test", instance_uuid="instance-a",
    )] == ["job-2"]

    file2 = tmp_path / "take_v2.mp4"
    file2.write_bytes(b"version two")
    with pytest.raises(RuntimeError, match="SHA-256"):
        store.mark_remote_job_delivered("take", "job-2", artifact_path=file2, artifact_sha256="a" * 64)
    found = store.find_remote_job("job-2", task_key="take", instance_uuid="instance-a")
    assert found["status"] == "submitted" and found["submission_id"] == second["submission_id"]
    recorded = store.mark_remote_job_delivered(
        "take", "job-2", instance_uuid="instance-a", expected_submission_id=found["submission_id"],
        artifact_path=file2, artifact_sha256=sha256_file(file2),
    )
    assert recorded["status"] == "delivered"
    assert store.get("take")["remote_job_id"] == "job-3"
    assert store.get("take")["state"] == "submitted"
    assert comfy_job_delivery_state("job-2", task_store=store, provider="chenyu-comfy") == "delivered"
    file1.write_bytes(b"corrupt local file")
    assert comfy_job_delivery_state("job-1", task_store=store, provider="chenyu-comfy") is None
    assert [row["remote_job_id"] for row in store.list_pending_comfy_deliveries(
        provider="chenyu-comfy", instance_uuid="instance-a",
    )] == ["job-1"]


def test_legacy_attempt_identity_and_remote_history_survive_replacement(tmp_path):
    store = TaskStore(data_dir=tmp_path)
    store.tasks_dir.mkdir()
    record = {
        "state": "submitted", "remote_job_id": "legacy-job", "provider": "chenyu-comfy",
        "logical_target": "scene/take.mp4", "task_key": "take", "instance_id": "instance-a",
    }
    atomic_write_json(store.task_path("take"), record)
    identity = submission_identity(record)
    with pytest.raises(TaskConflictError):
        comfy(store, force_new=True, replaced_submission_id=None)
    action, latest = comfy(store, force_new=True, replaced_submission_id=identity)
    assert action == "submit"
    assert latest["job_history"][0]["remote_job_id"] == "legacy-job"
    assert store.find_remote_job("legacy-job", instance_uuid="instance-a")["submission_id"] == identity


def test_reserved_delivery_is_recovered_after_publish_without_second_post(tmp_path):
    store = TaskStore(data_dir=tmp_path / "data")
    _, attempt = external(store)
    store.mark_external_submitted("take", "job-1")
    stage = tmp_path / ".stage"
    stage.write_bytes(b"verified")
    digest = sha256_file(stage)
    result = publish_atomic_no_clobber(
        stage, tmp_path / "take.mp4",
        before_publish=lambda path: store.reserve_delivery(
            "take", reserved_output_path=path, artifact_sha256=digest,
            expected_submission_id=attempt["submission_id"],
        ),
    )
    assert store.get("take")["state"] == "submitted"
    action, claimed = external(store)
    assert action == "delivered" and claimed["artifact_path"] == str(result)
    assert claimed["job_history"][0]["artifact_sha256"] == digest


def test_checkpoint_redacts_explicit_and_runtime_credentials(tmp_path):
    cfg = load_config(data_dir=tmp_path, environ={"CHENYU_API_KEY": "configured-key-for-test"})
    store = TaskStore(config=cfg, runtime_tokens=("runtime-test-token",))
    comfy(store, extra={"body": {"apiKey": "unknown-key", "note": "runtime-test-token"}})
    store.mark_ambiguous("take", reason="configured-key-for-test Bearer response-token")
    record = store.task_path("take").read_text(encoding="utf-8")
    for secret in ("configured-key-for-test", "response-token", "unknown-key", "runtime-test-token"):
        assert secret not in record


def test_checkpoint_preserves_core_fields_and_redacts_external_metadata(tmp_path, monkeypatch):
    chenyu_key = "synthetic-chenyu-api-key"
    runninghub_key = "synthetic-runninghub-api-key"
    cfg = load_config(cwd=tmp_path, data_dir=tmp_path / "data", environ={
        "DISABLE_AUTH": "1",
        "NPM_CONFIG_ALWAYS_AUTH": "true",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "CHENYU_API_KEY": chenyu_key,
        "RUNNINGHUB_API_KEY": runninghub_key,
    })
    artifact = tmp_path / "shot-1_true.mp4"
    artifact.write_bytes(b"verified artifact")
    digest = sha256_file(artifact)
    # Exercise an actual substring collision with a structured SHA-256 and UUID.
    runtime_token = digest[:8]
    submission_id = UUID(f"{runtime_token}-1111-4111-8111-111111111111")
    monkeypatch.setattr(tasks_store.uuid, "uuid4", lambda: submission_id)
    store = TaskStore(config=cfg, runtime_tokens=(runtime_token,))
    checkpoint = {
        "task_key": "shot-1_true",
        "provider": "runninghub",
        "request_digest": digest,
        "credential_fingerprint": credential_fingerprint(runninghub_key),
        "endpoint_origin": "https://example.test",
        "logical_target": "out/shot-1_true.mp4",
    }
    task_key = checkpoint["task_key"]
    action, attempt = store.begin_external_submit(**checkpoint, extra={"external": {"apiKey": chenyu_key}})
    assert action == "submit"
    assert attempt["submission_id"] == str(submission_id)
    store.cache_comfy_submit(task_key, {"detail": runtime_token}, expected_submission_id=str(submission_id))
    store.mark_external_submitted(task_key, "remote-1_true", expected_submission_id=str(submission_id))
    store.record_cancel_error(task_key, error=f"failed {runninghub_key}")
    store.mark_perf_archived(task_key, perf_summary={"detail": chenyu_key})
    store.reserve_delivery(task_key, reserved_output_path=artifact, artifact_sha256=digest,
                           expected_submission_id=str(submission_id))

    action, delivered = store.begin_external_submit(**checkpoint)
    assert action == "delivered"
    assert delivered == store.get(task_key)
    assert {key: delivered[key] for key in checkpoint} == checkpoint
    assert delivered["submission_id"] == str(submission_id)
    assert delivered["remote_task_id"] == "remote-1_true"
    assert delivered["artifact_path"] == delivered["reserved_output_path"] == str(artifact.resolve())
    assert delivered["artifact_sha256"] == digest
    assert delivered["created_at"] == attempt["created_at"]
    proof, = delivered["job_history"]
    assert proof["submission_id"] == str(submission_id)
    assert delivery_confirmed(proof)
    assert delivered["external"] == {"apiKey": "[REDACTED]"}
    assert delivered["cached_submit_payload"] == delivered["perf_summary"] == {"detail": "[REDACTED]"}
    assert delivered["cancel_error"] == "failed [REDACTED]"
    raw = store.task_path(task_key).read_text(encoding="utf-8")
    assert chenyu_key not in raw and runninghub_key not in raw


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
def test_external_submit_requires_force_new_by_default_after_terminal_state(tmp_path, terminal_state):
    store = TaskStore(data_dir=tmp_path)
    _, first = external(store)
    if terminal_state == "failed":
        store.mark_failed("take", error="failure")
    else:
        store.mark_cancelled("take")
    with pytest.raises(TaskConflictError, match="--force-new"):
        external(store)
    action, replacement = external(store, force_new=True, replaced_submission_id=first["submission_id"])
    assert action == "submit" and replacement["replaced_submission_id"] == first["submission_id"]


def test_provider_slots_and_active_count_are_scoped(tmp_path):
    store = TaskStore(data_dir=tmp_path)
    slots = ProviderSlots(
        "runninghub", "https://example.test", "fingerprint", max_slots=1,
        sleep_fn=lambda _: None, poll_interval_sec=0.01, wait_timeout_sec=0, data_dir=tmp_path,
    )
    assert list(tmp_path.iterdir()) == []
    with ExitStack() as first_slot, ExitStack() as second_slot:
        with slots.lock_for_submission(store, "take", "op-1", logical_target="scene/take.mp4",
                                       force_new=False, held_slot=first_slot) as decision:
            assert decision == (None, True)
            with slots.lock_admission():
                external(store)
        with pytest.raises(ProviderError, match="并发槽位超时"):
            with slots.lock_for_submission(store, "other", "op-2", logical_target="other.mp4",
                                           force_new=False, held_slot=second_slot):
                pytest.fail("unexpected second slot")
    assert count_active_tasks(store, provider="runninghub", endpoint_origin="https://example.test",
                              credential_fingerprint="fingerprint") == 1
    assert count_active_tasks(store, provider="runninghub", endpoint_origin="https://example.test",
                              credential_fingerprint="other") == 0


@pytest.mark.parametrize("target", ["a..b.mp4", "片段.mp4", "a/path.mp4", "CON.mp4"])
def test_generated_task_key_works_in_store(target, tmp_path):
    store = TaskStore(data_dir=tmp_path)
    assert store.get(make_task_key(target)) is None
