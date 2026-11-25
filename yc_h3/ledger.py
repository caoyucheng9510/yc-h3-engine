"""Cross-process usage records and balance snapshots."""
from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import EngineConfig, credential_fingerprint, load_config
from .fsutil import atomic_write_bytes, ensure_real_subpath, file_lock, redact_secrets

UNITS = ("CU", "point", "credit", "call", "char", "USD", "CNY")
RECORD_TYPES = ("session", "job", "external")
_LEGACY_BALANCE_FIELDS = ("ts", "provider", "unit", "balance", "source", "note")
_BALANCE_FIELDS = (*_LEGACY_BALANCE_FIELDS, "account_fp", "wallet_owner", "cash_balance", "card_balance")
_RATE_ENV = {("runninghub", "point"): "RUNNINGHUB_CNY_PER_POINT", ("colab-comfy", "CU"): "COLAB_CNY_PER_CU"}
_WALLET_CREDENTIALS = {"chenyu": "CHENYU_API_KEY", "runninghub": "RUNNINGHUB_API_KEY"}
_CORE_FIELDS = ("provider", "record_type", "project", "action", "quantity", "unit", "estimated")
_UNMETERED = {"imagegen", "image-gen", "host-imagegen", "builtin-imagegen", "openai-imagegen"}


class LedgerConflictError(ValueError):
    code = "ledger_conflict"
    exit_code = 3


def _safe_text(value: str, config: EngineConfig) -> str:
    if not isinstance(value, str):
        raise ValueError("台账文本字段必须为字符串")
    return redact_secrets(value, *config.secrets)


def _label(value: str, name: str, config: EngineConfig) -> str:
    cleaned = _safe_text(value, config).strip()
    if not cleaned or any(ord(char) < 32 for char in cleaned):
        raise ValueError(f"{name} 必须是非空单行文本")
    return cleaned


def wallet_owner(provider: str) -> str:
    return "chenyu" if provider == "chenyu-comfy" else provider


def _account_fp(provider: str, provided: str | None, config: EngineConfig) -> str:
    if provided is None:
        env_key = _WALLET_CREDENTIALS.get(wallet_owner(provider))
        key = config.get(env_key) if env_key else None
        return credential_fingerprint(key) if key else ""
    if not isinstance(provided, str) or not re.fullmatch(r"[0-9a-fA-F]{12,64}|", provided):
        raise ValueError("account_fp 必须为非敏感的十六进制凭据指纹")
    if provided and redact_secrets(provided, *config.secrets) != provided:
        raise ValueError("account_fp 必须为非敏感的十六进制凭据指纹")
    return provided.lower()


def _provider(value: str, config: EngineConfig) -> str:
    cleaned = _label(value, "provider", config)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cleaned) or cleaned in _UNMETERED:
        raise ValueError("台账仅接受有明确计量单位的服务")
    return cleaned


def _unit(value: str) -> str:
    if value not in UNITS:
        raise ValueError("不支持此计量单位")
    return value


def _amount(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须为有限非负数")
    try:
        amount = float(value)
    except OverflowError:
        raise ValueError(f"{name} 必须为有限非负数") from None
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{name} 必须为有限非负数")
    return amount


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("台账时间必须使用带时区的 ISO-8601")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("台账时间必须使用带时区的 ISO-8601")
    return result


def _timestamp(value: str | None) -> str:
    return (_time(value) if value is not None else datetime.now(timezone.utc)).isoformat()


def _ledger_path(root: Path, filename: str) -> Path:
    return ensure_real_subpath(root, root / "ledger" / filename)


def load_rates(data_dir: Path | str | None = None, *, config: EngineConfig | None = None) -> dict:
    root = (config or load_config(data_dir=data_dir)).data_dir
    path = _ledger_path(root, "rates.json")
    if not path.exists():
        return {}
    rates = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rates, dict):
        raise ValueError("台账费率配置必须为 JSON 对象")
    return rates


def lookup_cny_rate(
    provider: str, unit: str, data_dir: Path | str | None = None, *, config: EngineConfig | None = None,
) -> float | None:
    config = config or load_config(data_dir=data_dir)
    env_name = _RATE_ENV.get((provider, unit))
    if env_name and (configured := config.get(env_name)) is not None:
        try:
            return _amount(float(configured), env_name)
        except ValueError:
            raise ValueError(f"{env_name} 必须为有限非负数") from None
    entry = load_rates(config=config).get(f"{provider}:{unit}")
    if entry is None:
        return None
    if isinstance(entry, dict):
        if "cny_per_unit" in entry:
            return _amount(entry["cny_per_unit"], "cny_per_unit")
        package_cny = _amount(entry.get("package_cny"), "package_cny")
        package_units = _amount(entry.get("package_units"), "package_units")
        if not package_units:
            raise ValueError("package_units 必须为正数")
        return _amount(package_cny / package_units, "套餐单位费率")
    return _amount(entry, "cny_per_unit")


def _read_usage(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError(f"费用流水最后一行不完整：{path}")
    rows = []
    for line in raw.splitlines():
        row = json.loads(line)
        row.setdefault("wallet_owner", wallet_owner(row["provider"]))
        row.setdefault("account_fp", "")
        rows.append(row)
    return rows


def record_usage(
    *, provider: str, record_type: str, action: str, quantity: float,
    unit: str, estimated: bool, idempotency_key: str, data_dir: Path | str | None = None,
    account_fp: str | None = None, config: EngineConfig | None = None,
    project: str | None = None, ts: str | None = None, est_cny: float | None = None,
    note: str | None = None, submitted_at: str | None = None,
) -> dict:
    if record_type not in RECORD_TYPES or type(estimated) is not bool:
        raise ValueError("record_type 或 estimated 格式无效")
    config = config or load_config(data_dir=data_dir)
    provider = _provider(provider, config)
    entry = {
        "ts": _timestamp(ts), "provider": provider,
        "account_fp": _account_fp(provider, account_fp, config), "wallet_owner": wallet_owner(provider),
        "record_type": record_type, "project": None if project is None else _label(project, "project", config),
        "action": _label(action, "action", config), "quantity": _amount(quantity, "quantity"), "unit": _unit(unit),
        "estimated": estimated, "idempotency_key": _label(idempotency_key, "idempotency_key", config),
    }
    if est_cny is None and entry["unit"] == "CNY":
        entry["est_cny"] = _amount(round(entry["quantity"], 4), "est_cny")
    elif est_cny is not None:
        entry["est_cny"] = _amount(est_cny, "est_cny")
    if submitted_at is not None:
        entry["submitted_at"] = _time(submitted_at).isoformat()
    if note is not None:
        entry["note"] = _safe_text(note, config)
    root = config.data_dir
    with file_lock(_ledger_path(root, ".ledger.lock")):
        path = _ledger_path(root, "usage.jsonl")
        rows = _read_usage(path)
        for previous in rows:
            if (previous["provider"], previous["account_fp"], previous["idempotency_key"]) != (
                entry["provider"], entry["account_fp"], entry["idempotency_key"],
            ):
                continue
            if any(previous[key] != entry[key] for key in _CORE_FIELDS):
                raise LedgerConflictError("同一个 idempotency_key 已记录不同的计量事实")
            return {"recorded": False, "duplicate": True, "entry": previous}
        if "est_cny" not in entry and (rate := lookup_cny_rate(provider, unit, config=config)) is not None:
            entry["est_cny"] = _amount(round(entry["quantity"] * rate, 4), "est_cny")
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        previous_bytes = path.read_bytes() if rows else b""
        atomic_write_bytes(path, previous_bytes + line + b"\n")
    return {"recorded": True, "duplicate": False, "entry": entry}


def _read_balances(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None)
        if tuple(reader.fieldnames or ()) not in (_BALANCE_FIELDS, _LEGACY_BALANCE_FIELDS):
            raise ValueError("余额台账表头无效")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("余额台账字段数量无效")
            row["balance"] = _amount(float(row["balance"]), "balance")
            row.setdefault("wallet_owner", wallet_owner(row["provider"]))
            row.setdefault("account_fp", "")
            for field in ("cash_balance", "card_balance"):
                raw = row.get(field)
                row[field] = _amount(float(raw), field) if raw else None
            rows.append(row)
    return rows


def parse_chenyu_balance(data: dict) -> dict:
    """Parse both wallet balances returned by the Chenyu API in CNY."""
    result = {}
    for source, field in (("balance", "cash_balance"), ("card_balance", "card_balance")):
        value = data.get(source)
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise ValueError(f"晨羽未返回有效的 {source}") from None
        result[field] = _amount(value, field)
    result["balance"] = _amount(round(result["cash_balance"] + result["card_balance"], 8), "balance")
    return result


def set_balance(
    *, provider: str, unit: str, balance: float, source: str = "manual",
    ts: str | None = None, note: str = "", data_dir: Path | str | None = None,
    account_fp: str | None = None, config: EngineConfig | None = None,
    cash_balance: float | None = None, card_balance: float | None = None,
) -> dict:
    if source not in ("api", "manual"):
        raise ValueError("余额来源只允许 api 或 manual")
    if any(char in value for value in (provider, unit, note) for char in "\t\r\n"):
        raise ValueError("余额字段不得包含制表符或换行")
    config = config or load_config(data_dir=data_dir)
    provider = _provider(provider, config)
    row = {
        "ts": _timestamp(ts), "provider": provider, "unit": _unit(unit),
        "balance": _amount(balance, "balance"), "source": source, "note": _safe_text(note, config),
        "account_fp": _account_fp(provider, account_fp, config), "wallet_owner": wallet_owner(provider),
        "cash_balance": _amount(cash_balance, "cash_balance") if cash_balance is not None else None,
        "card_balance": _amount(card_balance, "card_balance") if card_balance is not None else None,
    }
    root = config.data_dir
    with file_lock(_ledger_path(root, ".ledger.lock")):
        path = _ledger_path(root, "balances.tsv")
        rows = _read_balances(path)
        if row in rows:
            return row
        rows.append(row)
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer, _BALANCE_FIELDS, delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None, lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        atomic_write_bytes(path, buffer.getvalue().encode("utf-8"))
    return row


def list_balances(
    *, data_dir: Path | str | None = None, provider: str | None = None,
    account_fp: str | None = None, config: EngineConfig | None = None,
) -> list[dict]:
    root = (config or load_config(data_dir=data_dir)).data_dir
    rows = _read_balances(_ledger_path(root, "balances.tsv"))
    return [row for row in rows if (provider is None or row["wallet_owner"] == wallet_owner(provider))
            and (account_fp is None or row["account_fp"] == account_fp)]


def reconcile_balances(
    *, data_dir: Path | str | None = None, provider: str | None = None,
    account_fp: str | None = None, config: EngineConfig | None = None,
) -> list[dict]:
    config = config or load_config(data_dir=data_dir)
    snapshots = list_balances(config=config, provider=provider, account_fp=account_fp)
    usage = _read_usage(_ledger_path(config.data_dir, "usage.jsonl"))
    latest = {}
    for row in sorted(snapshots, key=lambda row: _time(row["ts"])):
        latest[row["wallet_owner"], row["unit"], row["account_fp"]] = row
    result = []
    for (owner, unit, fingerprint), snap in sorted(latest.items()):
        if (owner, unit) == ("colab-comfy", "CU"):
            types = {"session"}
        elif (owner, unit) == ("chenyu", "CNY"):
            types = {"job", "external", "session"}
        else:
            types = {"job", "external"}
        since = _time(snap["ts"])
        recent = [
            row for row in usage
            if row["wallet_owner"] == owner and row["account_fp"] == fingerprint
            and row["record_type"] in types and _time(row["ts"]) > since
        ]
        matching = [row for row in recent if row["unit"] == unit]
        unknown = len(recent) - len(matching)
        unreconciled = []
        if owner in ("runninghub", "chenyu") and snap["source"] == "api":
            deducted = []
            for row in matching:
                try:
                    submitted_after_snapshot = _time(row.get("submitted_at")) > since
                except ValueError:
                    submitted_after_snapshot = False
                (deducted if submitted_after_snapshot else unreconciled).append(row)
            matching = deducted
        consumed = math.fsum(row["quantity"] for row in matching)
        result.append({
            **snap, "consumed_since_snapshot": consumed, "projected_balance": snap["balance"] - consumed,
            "estimated_count": sum(row["estimated"] for row in matching), "unknown_or_other_unit_count": unknown,
            "unreconciled_count": len(unreconciled),
            "unreconciled_quantity": math.fsum(row["quantity"] for row in unreconciled),
        })
    return result


def summarize_usage(
    *, data_dir: Path | str | None = None, project: str | None = None,
    provider: str | None = None, since: str | None = None,
    account_fp: str | None = None, config: EngineConfig | None = None,
) -> dict:
    start = _time(since) if since is not None else None
    root = (config or load_config(data_dir=data_dir)).data_dir
    rows = _read_usage(_ledger_path(root, "usage.jsonl"))
    jobs: dict[tuple, list] = {}
    sessions: dict[tuple, list] = {}
    for row in rows:
        if (provider is not None and row["provider"] != provider
                or account_fp is not None and row["account_fp"] != account_fp
                or start is not None and _time(row["ts"]) < start):
            continue
        if row["record_type"] == "session":
            sessions.setdefault((row["provider"], row["unit"]), []).append(row)
        elif project is None or row["project"] == project:
            jobs.setdefault((row["project"], row["provider"], row["unit"]), []).append(row)

    def totals(entries: list[dict]) -> dict:
        cny = [entry["est_cny"] for entry in entries if entry.get("est_cny") is not None]
        return {
            "quantity": math.fsum(entry["quantity"] for entry in entries), "count": len(entries),
            "actual_quantity": math.fsum(entry["quantity"] for entry in entries if not entry["estimated"]),
            "estimated_count": sum(entry["estimated"] for entry in entries),
            "est_cny": round(math.fsum(cny), 4) if cny else None, "est_cny_count": len(cny),
        }

    return {
        "jobs": [
            {"project": slug, "provider": pid, "unit": unit, **totals(entries)}
            for (slug, pid, unit), entries in sorted(jobs.items(), key=lambda item: (item[0][0] or "", *item[0][1:]))
        ],
        "sessions": [
            {"provider": pid, "unit": unit, **totals(entries)}
            for (pid, unit), entries in sorted(sessions.items())
        ],
    }


def record_colab_stop(
    billing: dict, *, data_dir: Path | str | None = None, config: EngineConfig | None = None,
    account_fp: str | None = None,
) -> dict:
    """Record one released VM and anchor its next balance reconciliation."""
    result = {"ledger_recorded": False, "ledger_balance_recorded": False}
    session_id = billing.get("session_id")
    if not session_id:
        return result
    config = config or load_config(data_dir=data_dir)
    account_fp = account_fp if account_fp is not None else billing.get("account_fp")
    ts = billing.get("ts") or _timestamp(None)
    before = billing.get("balance_before")
    after = billing.get("balance_after")
    uptime = billing.get("uptime_seconds")
    rate = billing.get("cu_rate")
    if before is not None and after is not None and before >= after:
        quantity, estimated, note = before - after, False, "Colab 独占会话期间的余额差"
    elif uptime is not None and rate is not None:
        quantity, estimated, note = uptime * rate / 3600, True, "按会话秒数与 CU/hr 费率估算"
    else:
        quantity, estimated, note = None, True, ""
    if quantity is not None:
        usage = record_usage(
            config=config, account_fp=account_fp, provider="colab-comfy", record_type="session",
            action="gpu_stop", quantity=quantity, unit="CU", estimated=estimated,
            idempotency_key=f"colab-comfy:session:{session_id}", ts=ts,
            note=f"{billing.get('session_name') or session_id} · {note}",
        )
        result["ledger_recorded"] = True
        if billing.get("ts") is None:
            ts = usage["entry"]["ts"]
    if after is not None:
        set_balance(
            config=config, account_fp=account_fp, provider="colab-comfy", unit="CU",
            balance=after, source="api", ts=ts, note="停机释放后查询",
        )
        result["ledger_balance_recorded"] = True
    return result


def record_chenyu_gpu_stop(
    billing: dict, *, data_dir: Path | str | None = None, config: EngineConfig | None = None,
    account_fp: str | None = None,
) -> dict:
    """Record a Chenyu GPU session once and refresh the account's dual-wallet snapshot."""
    result = {"ledger_recorded": False, "ledger_balance_recorded": False}
    session_id = billing.get("session_id")
    if not session_id:
        return result
    config = config or load_config(data_dir=data_dir)
    account_fp = account_fp if account_fp is not None else billing.get("account_fp")
    ts = billing.get("ts") or _timestamp(None)
    card, cash = billing.get("card_cny"), billing.get("cash_cny")
    quantity = billing.get("est_cny")
    if quantity is not None:
        estimate = bool(billing.get("estimated"))
        note = (
            "按会话秒数与小时价估算，双钱包实扣待落账" if estimate
            else f"算力卡 {card or 0} CNY，现金 {cash or 0} CNY"
        )
        usage = record_usage(
            config=config, account_fp=account_fp, provider="chenyu-comfy", record_type="session", action="gpu_stop",
            quantity=quantity, unit="CNY", estimated=estimate, est_cny=quantity,
            idempotency_key=f"chenyu-comfy:session:{session_id}", ts=ts,
            note=f"{billing.get('session_name') or session_id} · {note}",
            submitted_at=billing.get("allocated_at"),
        )
        result["ledger_recorded"] = True
        if billing.get("ts") is None:
            ts = usage["entry"]["ts"]
    cash_balance, card_balance = billing.get("balance_after"), billing.get("card_balance_after")
    if cash_balance is not None and card_balance is not None:
        balances = parse_chenyu_balance({"balance": cash_balance, "card_balance": card_balance})
        set_balance(
            config=config, account_fp=account_fp, provider="chenyu-comfy", unit="CNY",
            source="api", ts=ts, note="停机后双钱包", **balances,
        )
        result["ledger_balance_recorded"] = True
    return result
