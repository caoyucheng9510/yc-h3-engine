"""Offline Chenyu settlement, dual-wallet probing and recovery."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from yc_h3.ledger import summarize_usage
from yc_h3.perf_history import load_observations
from yc_h3.providers.base import ApprovalRequiredError, GenerationRequest, ProviderConflictError
from yc_h3.providers.chenyu import ChenyuProvider
from yc_h3.providers.chenyu_workflows.catalog import load_registry
from yc_h3.tasks_store import TaskStore
from tests.test_runninghub_provider import MEDIA_URL, TEXT_PROMPT, Transport, make_config, render_clip


def reply(**data):
    return {"code": 0, "data": data}


def test_catalog_and_two_wallets(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="fake-chenyu-credential")
    transport = Transport(info=[reply(balance="0.42", card_balance=1.33)])
    provider = ChenyuProvider(config=config, auth_opener=transport)
    assert provider._timeout == 3600.0
    assert ChenyuProvider(config=config, timeout_sec=25.0)._timeout == 25.0
    assert load_registry()["default"] == "jinyu-hybrid"
    probe = provider.probe(check_remote=True)
    assert probe.available
    assert {key: probe.details[key] for key in ("cash_balance", "card_balance", "balance")} == {
        "cash_balance": 0.42, "card_balance": 1.33, "balance": 1.75,
    }
    assert not config.data_dir.exists()


@pytest.mark.parametrize(("params", "inputs", "reason"), [
    ({"seconds": 1.5}, {}, "整数"),
    ({"seed": 7}, {}, "seed"),
    ({"steps": 12}, {}, "steps"),
    ({}, {"videos": ["unread.mp4"]}, "videos"),
])
def test_unsupported_parameters_block(tmp_path, monkeypatch, params, inputs, reason):
    config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="fake-chenyu-credential")
    provider = ChenyuProvider(config=config, auth_opener=Transport())
    result = provider.preflight(GenerationRequest("video", prompt=TEXT_PROMPT, output_path=tmp_path / "out.mp4",
                                                  params=params, inputs=inputs))
    assert result.status == "block"
    assert reason in result.signals[0]["detail"]
    assert not config.data_dir.exists()


def test_same_account_replays_exact_order_and_writes_ledger_once(tmp_path, monkeypatch, render_clip):
    config = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="fake-chenyu-credential")
    transport = Transport(
        submit=[ConnectionResetError(), reply(run_order_id="wfrun_offline01")],
        info=[reply(run_status="succeeded", settlement_status="settled", final_amount="0.13",
                    run_duration_seconds="30",
                    platform_final_amount="0.10", creator_final_amount="0.03", platform_card_amount="0.09",
                    outputs={"result_364_gifs_1": [MEDIA_URL]})],
    )
    provider = ChenyuProvider(config=config, auth_opener=transport,
                              download_opener=Transport(**{"output.mp4": [render_clip(608, 352, 39)]}))
    store = TaskStore(config=config)
    path = tmp_path / "out.mp4"
    request = GenerationRequest("video", prompt=TEXT_PROMPT, output_path=path, force_new=True,
                                params={"seconds": 1.0, "megapixels": 0.2}, metadata={"tag": "paid-run"})
    approval = provider.preflight(request, store=store)
    assert approval.status == "warn", approval.signals
    assert approval.facts["estimate_source"] == "uncalibrated" and approval.facts["estimate_samples"] == 0
    assert all(approval.facts[key] is None for key in ("estimated_run_seconds", "estimated_wall_seconds", "estimated_cny"))
    assert "0.12 CNY/分钟" in approval.signals[0]["detail"]
    assert not config.data_dir.exists()
    with pytest.raises(ApprovalRequiredError):
        provider.generate(request, store=store)
    request.approve_digest = approval.digest
    with pytest.raises(ProviderConflictError, match="建单结果不明"):
        provider.generate(request, store=store)
    task_key = approval.facts["task_key"]
    assert store.get(task_key)["state"] == "ambiguous"
    with pytest.raises(ProviderConflictError, match="审批已消费"):
        provider.generate(request, store=store)
    another = make_config(tmp_path, monkeypatch, CHENYU_API_KEY="another-fake-chenyu-credential")
    with pytest.raises(ProviderConflictError, match="凭据或源"):
        ChenyuProvider(config=another, auth_opener=Transport()).resume(task_key, store=store)

    submitted_at = datetime.fromisoformat(store.get(task_key)["submit_requested_at"]).timestamp()
    clock = SimpleNamespace(time=lambda: submitted_at + 120, monotonic=time.monotonic)
    monkeypatch.setattr("yc_h3.providers.chenyu.time", clock)
    result = provider.resume(task_key, store=store)
    assert result.artifact_path == str(path)
    assert result.billing["platform_card_amount"] == 0.09
    calls = transport.calls("submit")
    assert len(calls) == 2
    first, second = (json.loads(call.data) for call in calls)
    assert first == second
    assert first["idempotency_key"] == store.get(task_key)["chenyu_idempotency_key"]
    assert provider.fetch(task_key, store=store, remote_job_id="wfrun_offline01").artifact_path == str(path)
    fp = approval.target_identity["credential_fingerprint"]
    usage = summarize_usage(config=config, project="paid-run", account_fp=fp)["jobs"]
    assert len(usage) == 1 and usage[0]["quantity"] == 0.13 and usage[0]["count"] == 1
    ledger_row = json.loads((config.data_dir / "ledger" / "usage.jsonl").read_bytes())
    assert ledger_row["wallet_owner"] == "chenyu" and ledger_row["account_fp"] == fp
    assert config.get("CHENYU_API_KEY") not in store.task_path(task_key).read_text()
    observation, = load_observations(config, provider="chenyu")
    assert observation["hardware"] == approval.target_identity["gpu_model"]
    assert observation["run_seconds"] == 30.0 and observation["wall_seconds"] == 120.0
    assert observation["billed_quantity"] == 0.13 and observation["estimated_billing"] is False
    assert observation["unit"] == "CNY"

    longer = replace(request, output_path=tmp_path / "longer.mp4", force_new=False,
                     params={**request.params, "seconds": 3})
    quote = provider.preflight(longer, store=store)
    assert quote.facts["estimate_source"] == "local_history" and quote.facts["estimate_samples"] == 1
    assert quote.facts["estimated_run_seconds"] == 90.0 and quote.facts["estimated_wall_seconds"] == 120.0
    assert quote.facts["estimated_cny"] == 0.21
    assert "预计纯推理耗时 90.0 秒" in quote.signals[0]["detail"]
    assert "近期端到端耗时中位数 120.0 秒" in quote.signals[0]["detail"]
