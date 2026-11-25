import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from yc_h3 import ledger
from yc_h3.config import credential_fingerprint, load_config


BEFORE = "2026-01-01T00:00:00+00:00"
SUBMITTED = "2026-01-02T00:00:00+00:00"
AFTER = "2026-01-03T00:00:00+00:00"


@pytest.fixture
def config(tmp_path):
    return load_config(data_dir=tmp_path / "state", cwd=tmp_path, environ={})


def usage(config, provider, record_type, quantity, key, **fields):
    return ledger.record_usage(
        config=config, provider=provider, record_type=record_type, action="test",
        quantity=quantity, unit=fields.pop("unit", "CNY"), estimated=False,
        idempotency_key=key, ts=AFTER, **fields,
    )


def test_ledger_read_paths_are_strictly_readonly(config):
    assert ledger.load_rates(config=config) == {}
    assert ledger.lookup_cny_rate("runninghub", "point", config=config) is None
    assert ledger.list_balances(config=config) == []
    assert ledger.reconcile_balances(config=config) == []
    assert ledger.summarize_usage(config=config) == {"jobs": [], "sessions": []}
    assert not config.data_dir.exists()


def test_shared_wallet_and_account_isolation(config):
    first = credential_fingerprint("first-test-account")
    second = credential_fingerprint("second-test-account")
    for account, cash, card in ((first, 9, 11), (second, 30, 15)):
        ledger.set_balance(
            config=config, provider="chenyu", account_fp=account, unit="CNY",
            source="api", ts=BEFORE, **ledger.parse_chenyu_balance({"balance": cash, "card_balance": card}),
        )
    assert usage(config, "chenyu", "job", 2, "same-id", account_fp=first, submitted_at=SUBMITTED)["recorded"]
    assert usage(config, "chenyu", "job", 7, "same-id", account_fp=second, submitted_at=SUBMITTED)["recorded"]
    gpu = usage(config, "chenyu-comfy", "session", 3, "gpu", account_fp=first, submitted_at=SUBMITTED)["entry"]
    assert gpu["provider"] == "chenyu-comfy" and gpu["wallet_owner"] == "chenyu"
    usage(config, "chenyu", "job", 1, "possibly charged before snapshot", account_fp=first, submitted_at=BEFORE)
    usage(config, "chenyu", "job", 10, "wrong-unit", account_fp=first, unit="point", submitted_at=SUBMITTED)
    usage(config, "chenyu", "job", 10, "unknown-account", submitted_at=SUBMITTED)
    rows = {row["account_fp"]: row for row in ledger.reconcile_balances(config=config, provider="chenyu-comfy")}
    assert len(rows) == 2
    assert rows[first]["consumed_since_snapshot"] == 5
    assert rows[first]["projected_balance"] == 15
    assert rows[first]["cash_balance"] == 9 and rows[first]["card_balance"] == 11
    assert rows[first]["unreconciled_quantity"] == 1 and rows[first]["unknown_or_other_unit_count"] == 1
    assert rows[second]["consumed_since_snapshot"] == 7 and rows[second]["projected_balance"] == 38


def test_rates_env_and_package_fallback(config, tmp_path):
    directory = config.data_dir / "ledger"
    directory.mkdir(parents=True)
    (directory / "rates.json").write_text(json.dumps({
        "runninghub:point": {"package_cny": 20, "package_units": 100},
        "colab-comfy:CU": {"cny_per_unit": 0.6},
    }))
    assert ledger.lookup_cny_rate("runninghub", "point", config=config) == 0.2
    (tmp_path / ".env").write_text("RUNNINGHUB_CNY_PER_POINT=0.3\nCOLAB_CNY_PER_CU=0.4\n")
    merged = load_config(data_dir=config.data_dir, cwd=tmp_path, environ={})
    assert usage(merged, "runninghub", "job", 10, "rated", unit="point")["entry"]["est_cny"] == 3.0
    assert usage(merged, "colab-comfy", "session", 4, "gpu", unit="CU")["entry"]["est_cny"] == 1.6


def test_runninghub_usage_is_idempotent_under_concurrent_writers(config):
    def write(_):
        return usage(config, "runninghub", "job", 2, "same", unit="point")["recorded"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(write, range(8))) == 1
    with pytest.raises(ledger.LedgerConflictError):
        usage(config, "runninghub", "job", 3, "same", unit="point")
    assert ledger.summarize_usage(config=config)["jobs"][0]["quantity"] == 2


def test_account_fingerprint_and_labels_survive_host_auth_settings(config, tmp_path):
    chenyu_key = "ab12cd34ef56"
    runninghub_key = "synthetic-runninghub-api-key"
    cfg = load_config(data_dir=config.data_dir, cwd=tmp_path, environ={
        "DISABLE_AUTH": "1",
        "NPM_CONFIG_ALWAYS_AUTH": "true",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "CHENYU_API_KEY": f" {chenyu_key} ",
        "RUNNINGHUB_API_KEY": runninghub_key,
    })
    # A fingerprint may contain a known key as a substring. Only a complete match is sensitive.
    fingerprint = f"1{chenyu_key}".ljust(64, "f")
    note = f"job-1 true ({chenyu_key}) {runninghub_key} Bearer runtime-token"
    expected_note = "job-1 true ([REDACTED]) [REDACTED] [REDACTED]"
    balance = ledger.set_balance(config=cfg, provider="chenyu-comfy", account_fp=f" {fingerprint.upper()} ",
                                 unit="CNY", balance=20, ts=BEFORE, note=note)
    assert balance["account_fp"] == fingerprint and balance["note"] == expected_note
    assert ledger.list_balances(config=cfg, provider="chenyu", account_fp=fingerprint) == [balance]

    fields = {"account_fp": fingerprint, "project": "project-1_true", "note": note}
    result = usage(cfg, "chenyu-comfy", "job", 2, "job-1_true", **fields)
    assert result["recorded"]
    assert result["entry"]["account_fp"] == fingerprint
    assert result["entry"]["provider"] == "chenyu-comfy"
    assert result["entry"]["project"] == fields["project"]
    assert result["entry"]["idempotency_key"] == "job-1_true"
    assert result["entry"]["note"] == expected_note
    assert usage(cfg, "chenyu-comfy", "job", 2, "job-1_true", **fields)["duplicate"]

    for raw in (chenyu_key, f" {chenyu_key.upper()} ", runninghub_key):
        with pytest.raises(ValueError, match="account_fp"):
            usage(cfg, "chenyu", "job", 2, "invalid", account_fp=raw)
        with pytest.raises(ValueError, match="account_fp"):
            ledger.set_balance(config=cfg, provider="chenyu", account_fp=raw, unit="CNY", balance=20)


def test_stop_records_cash_card_and_redacts_credentials(config, tmp_path):
    (tmp_path / ".env").write_text("CHENYU_API_KEY=synthetic-chenyu-credential\nCOLAB_CNY_PER_CU=0.5\n")
    cfg = load_config(data_dir=config.data_dir, cwd=tmp_path, environ={})
    charge = {
        "session_id": "test-instance", "session_name": "yh3-test synthetic-chenyu-credential",
        "ts": AFTER, "allocated_at": SUBMITTED, "est_cny": 3, "cash_cny": 1, "card_cny": 2,
        "balance_after": 12, "card_balance_after": 30,
    }
    for _ in range(2):
        assert ledger.record_chenyu_gpu_stop(charge, config=cfg) == {
            "ledger_recorded": True, "ledger_balance_recorded": True,
        }
    saved, = ledger.list_balances(config=cfg, provider="chenyu")
    assert saved["balance"] == 42 and saved["cash_balance"] == 12 and saved["card_balance"] == 30
    assert saved["account_fp"] == credential_fingerprint("synthetic-chenyu-credential")
    ledger.record_colab_stop(
        {"session_id": "colab-test", "ts": AFTER, "balance_before": 7, "balance_after": 5}, config=cfg,
    )
    rows = ledger.summarize_usage(config=cfg)["sessions"]
    assert [(row["provider"], row["est_cny"]) for row in rows] == [("chenyu-comfy", 3), ("colab-comfy", 1)]
    raw = (config.data_dir / "ledger" / "usage.jsonl").read_text(encoding="utf-8")
    assert "synthetic-chenyu-credential" not in raw
    assert "[REDACTED]" in raw


def test_legacy_rows_read_and_stay_in_unattributed_wallet(config):
    directory = config.data_dir / "ledger"
    directory.mkdir(parents=True)
    (directory / "balances.tsv").write_text(
        f"ts\tprovider\tunit\tbalance\tsource\tnote\n{BEFORE}\tchenyu\tCNY\t40\tmanual\tlegacy\n",
    )
    (directory / "usage.jsonl").write_text(json.dumps({
        "ts": AFTER, "provider": "chenyu-comfy", "record_type": "session",
        "quantity": 3, "unit": "CNY", "estimated": False, "idempotency_key": "legacy",
    }) + "\n")
    account = credential_fingerprint("test-account")
    ledger.set_balance(config=config, provider="chenyu", account_fp=account, unit="CNY", balance=20, ts=BEFORE)
    rows = {row["account_fp"]: row for row in ledger.reconcile_balances(config=config)}
    assert rows[""]["projected_balance"] == 37
    assert rows[account]["projected_balance"] == 20
