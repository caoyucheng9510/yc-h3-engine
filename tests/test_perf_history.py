"""Offline validation, persistence and calibration for local observations."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from yc_h3.perf_history import estimate_from_history, load_observations, record_observation
from tests.test_runninghub_provider import make_config


def test_history_reads_do_not_create_local_state(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    estimate = dict(provider="runninghub", workflow="jinyu-hybrid", output_seconds=2)
    assert load_observations(config) == []
    assert estimate_from_history(config, **estimate) is None
    assert not config.data_dir.exists()

    config.data_dir.mkdir()
    assert load_observations(config) == []
    assert estimate_from_history(config, **estimate) is None
    assert not (config.data_dir / "perf").exists()


def test_observations_deduplicate_concurrent_writes_and_skip_bad_lines(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    fields = dict(provider="runninghub", workflow="jinyu-hybrid", output_seconds=2,
                  hardware=" plus ", width=1664, height=928, run_seconds=10, unit="point")

    def write(index):
        record_observation(config, idempotency_key=f"runninghub:job-{index % 3}", **fields)

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(write, range(12)))
    path = config.data_dir / "perf" / "observations.jsonl"
    original = path.read_bytes()
    assert original.endswith(b"\n") and len(original.splitlines()) == 3
    rows = load_observations(config, provider="runninghub")
    assert len(rows) == 3
    assert {row["idempotency_key"] for row in rows} == {f"runninghub:job-{index}" for index in range(3)}
    assert all(row["hardware"] == "plus" for row in rows)
    assert all(datetime.fromisoformat(row["ts"]).utcoffset() == timedelta(0) for row in rows)
    assert load_observations(config, provider="chenyu") == []
    write(0)
    assert path.read_bytes() == original

    bad_number = json.dumps({**rows[0], "run_seconds": float("inf")}).encode()
    path.write_bytes(original + b'{}\n[]\n' + bad_number + b'\n\xff\n{"truncated"')
    assert load_observations(config) == rows
    record_observation(config, idempotency_key="runninghub:after-corrupt", **fields)
    assert [row["idempotency_key"] for row in load_observations(config)] == [
        *(row["idempotency_key"] for row in rows), "runninghub:after-corrupt",
    ]


@pytest.mark.parametrize(("field", "value"), [
    ("idempotency_key", "runninghub:offline-test-credential"),
    ("hardware", "NVIDIA L4 Bearer ephemeral-secret"),
    ("workflow", "/local/reference.mp4"),
    ("hardware", "C:\\local\\reference.mp4"),
    ("output_seconds", True),
    ("output_seconds", float("nan")),
    ("run_seconds", 0),
    ("wall_seconds", float("inf")),
    ("billed_quantity", -1),
    ("width", 640.0),
])
def test_rejects_unsafe_labels_and_invalid_numbers(tmp_path, monkeypatch, field, value):
    config = make_config(tmp_path, monkeypatch, RUNNINGHUB_API_KEY="offline-test-credential")
    fields = dict(idempotency_key="runninghub:job", provider="runninghub", workflow="jinyu-hybrid", output_seconds=2)
    fields[field] = value
    with pytest.raises(ValueError):
        record_observation(config, **fields)
    assert not config.data_dir.exists()


def test_recent_warm_cohort_normalizes_medians_and_uses_actual_billing(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    common = dict(provider="runninghub", workflow="jinyu-hybrid", hardware="plus",
                  has_ref_video=True, unit="point", estimated_billing=False)

    def observe(name, output, run, wall, charge, **options):
        record_observation(config, idempotency_key=f"job-{name}", output_seconds=output,
                           run_seconds=run, wall_seconds=wall, billed_quantity=charge, **{**common, **options})

    observe("old", 2, 200, 400, 200)
    observe("a", 2, 40, 60, 8)
    observe("b", 4, 120, 160, 12)
    observe("c", 8, 160, 200, 1000, estimated_billing=True)
    observe("cold", 2, 1000, 1000, 1000, cold_start=True)
    observe("no-ref", 2, 1000, 1000, 1000, has_ref_video=False)
    observe("other-workflow", 2, 1000, 1000, 1000, workflow="jinyu-combat")
    observe("other-card", 2, 1000, 1000, 1000, hardware="pro")

    params = dict(provider="runninghub", workflow="jinyu-hybrid", output_seconds=6, has_ref_video=True, recent_limit=3)
    assert estimate_from_history(config, **params, hardware=" PLUS ") == {
        "samples": 3, "estimated_run_seconds": 120.0, "estimated_wall_seconds": 180.0,
        "estimated_quantity": 18.0, "billing_samples": 2,
    }
    assert estimate_from_history(config, **params, hardware="pro")["estimated_run_seconds"] == 3000.0
    assert estimate_from_history(config, **params, hardware="missing")["samples"] == 3


def test_chenyu_wall_median_and_billing_without_valid_run_time(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    common = dict(provider="chenyu", workflow="jinyu-hybrid", unit="CNY", estimated_billing=False)
    record_observation(config, idempotency_key="one", output_seconds=2, wall_seconds=90, billed_quantity=0, **common)
    record_observation(config, idempotency_key="two", output_seconds=4, wall_seconds=140, billed_quantity=8, **common)
    assert estimate_from_history(config, provider="chenyu", workflow="jinyu-hybrid", output_seconds=6) == {
        "samples": 2, "estimated_run_seconds": None, "estimated_wall_seconds": 115.0,
        "estimated_quantity": 6.0, "billing_samples": 2,
    }
