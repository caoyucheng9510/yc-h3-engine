"""Standalone command-line interface for yc-h3-engine."""
from __future__ import annotations

import argparse
import getpass
import http.client
import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import quote

from . import ledger
from .config import global_env_path, load_config, read_env_file, update_env_file
from .providers import registry, video_common
from .providers._http import validate_api_key
from .providers.base import GenerationRequest, PreflightResult, PreflightBlockedError, ProviderConflictError, ProviderError
from .tasks_store import TaskConflictError, TaskStore, TaskStoreError
from .remote.workflows.minimax_h3.spec import JINYU_WORKFLOWS

_INIT_KEYS = (
    "RUNNINGHUB_API_KEY", "RUNNINGHUB_WORKFLOW_ID", "RUNNINGHUB_WORKFLOW_ID_HYBRID", "RUNNINGHUB_WORKFLOW_ID_COMBAT",
    "CHENYU_API_KEY", "YC_H3_DATA_DIR", "YC_H3_PROVIDER_ORDER", "RUNNINGHUB_CNY_PER_POINT", "COLAB_CNY_PER_CU",
)
_SUMMARY_FIELDS = ("task_key", "provider", "state", "workflow", "tag", "remote_job_id", "remote_task_id",
                   "artifact_path", "artifact_sha256", "created_at", "updated_at", "delivered_at", "perf_archived")
_HISTORY_FIELDS = ("remote_job_id", "status", "delivered_path", "artifact_sha256")
_COLAB_TASK_POLL_INTERVAL = 1.0
_COLAB_TASK_TIMEOUTS = {"stop": 300, "up": 1800, "setup": 3600}
_COLAB_TASK_EXIT_CODES = {
    "approval_context_required": 2,
    "approval_required": 2,
    "approval_stale": 2,
    "active_managed_session_exists": 3,
    "foreign_app": 3,
    "instance_id_changed": 3,
    "local_task_conflict": 3,
    "needs_reconcile": 3,
    "session_deadline_or_cancelled": 3,
    "session_host_changed": 3,
    "session_host_mismatch": 3,
    "session_vanished": 3,
    "setup_target_mismatch": 3,
    "unmanaged_session": 3,
    "unmanaged_session_collision": 3,
}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _json_flag(parser):
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def _provider_flag(parser):
    parser.add_argument("--provider", choices=registry.PROVIDER_ORDER, help="指定通道")


def _daemon_address(parser):
    parser.add_argument("--host", help="本机回环地址")
    parser.add_argument("--port", type=int, help="守护进程端口")


def _parser():
    parser = ArgumentParser(description="MiniMax H3 多通道视频生成与交付")
    parser.add_argument("--env-file", type=Path, help="显式指定 .env 配置")
    parser.add_argument("--data-dir", type=Path, help="断点、受管会话和台账目录")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="配置向导")
    init.add_argument("--local", action="store_true", help="写入当前目录的 .env")
    init.add_argument("--from-stdin-json", action="store_true", help="从标准输入读取键值 JSON 对象")
    init.add_argument("--update", "--overwrite", action="store_true", help="更新本次给出的键并保留其他键")
    _json_flag(init)

    doctor = commands.add_parser("doctor", help="只读检查环境、配置和通道")
    _provider_flag(doctor)
    doctor.add_argument("--workflow", help="检查指定工作流的配置")
    doctor.add_argument("--remote", action="store_true", help="查询已配置通道的实时余额")
    _json_flag(doctor)

    providers = commands.add_parser("providers", help="列出通道及可用性")
    providers.add_argument("--probe", action="store_true", help="探测通道连接及本地配置")
    providers.add_argument("--remote", action="store_true", help="查询通道实时余额")
    _json_flag(providers)

    workflows = commands.add_parser("workflows", help="列出内置工作流、分辨率规格与素材槽位")
    _provider_flag(workflows)
    workflows.add_argument("--remote", action="store_true", help="同时查询已连接受管 GPU 的工作流目录")
    _json_flag(workflows)

    for name, help_text in (("preflight", "只读预检与审批报价"), ("gen", "使用实时审批摘要生成视频")):
        gen = commands.add_parser(name, help=help_text)
        _provider_flag(gen)
        gen.add_argument("--workflow", help="工作流，默认 jinyu-hybrid")
        gen.add_argument("--task", choices=("t2v", "i2v", "r2v"), help="默认按素材推断")
        prompt = gen.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt", help="完整 MiniMax H3 提示词")
        prompt.add_argument("--prompt-file", type=Path, help="UTF-8 提示词文件，使用 - 从标准输入读取")
        gen.add_argument("--image", type=Path, action="append",
                         help="参考图，可重复；按传入顺序对应提示词 <Picture 1>, <Picture 2>...")
        gen.add_argument("--audio", type=Path, action="append",
                         help="参考音频，可重复；对应 <Audio N>（有伴音参考视频排在最前，独立音频依次顺延）")
        gen.add_argument("--video", type=Path, action="append",
                         help="参考视频，可重复；按传入顺序对应 <Video 1>, <Video 2>...（默认保留伴音并占用前置 <Audio N>）")
        gen.add_argument("--first-frame", type=Path,
                         help="ComfyUI i2v 单独首帧，对应 <Picture 1>（仅 colab-comfy / chenyu-comfy）")
        gen.add_argument("--mute-reference-video", action="store_true",
                         help="忽略全部参考视频伴音（避免参考视频占用 <Audio N>）")
        gen.add_argument("--seconds", type=float,
                         help="目标时长（秒），默认 5（runninghub/comfy 支持 0.2–15 小数，chenyu 仅 1–15 整数）")
        gen.add_argument("--aspect",
                         help="预设画幅，默认 16:9（1664×928）；runninghub/comfy 支持 16:9|9:16|1:1，chenyu 支持 8 种画幅；与 --width/--height 互斥")
        gen.add_argument("--width", type=int,
                         help="自定义宽度（32 的正整数倍，须与 --height 成对使用且与 --aspect 互斥；不支持 chenyu）")
        gen.add_argument("--height", type=int,
                         help="自定义高度（32 的正整数倍，须与 --width 成对使用且与 --aspect 互斥；不支持 chenyu）")
        gen.add_argument("--seed", type=int, help="随机种子（-1 随机，或 64 位无符号整数；不支持 chenyu）")
        gen.add_argument("--steps", type=int, help="采样步数（runninghub 支持 7–100 默认 12；comfy 固定 12；不支持 chenyu）")
        gen.add_argument("--megapixels", type=float,
                         help="晨羽按次 API 像素档位（0.2–2.0，默认 1.5；16:9 下 1.5 对应 1664×928）")
        gen.add_argument("--upscale-2x", action="store_true", default=None, help="晨羽按次 API 开启 2 倍超分")
        gen.add_argument("--save-last-frame", action="store_true", default=None,
                         help="生成后额外导出末帧图片（runninghub / chenyu 支持）")
        gen.add_argument("--no-music", action="store_true", default=None,
                         help="无背景配乐（缺段时自动补 non_diegetic_music: N/A，已有非 N/A 配乐则阻断）")
        gen.add_argument("--trim-to-requested-seconds", action="store_true", default=None,
                         help="ComfyUI 通道按目标秒数裁切 17k+5 网格多余尾帧")
        gen.add_argument("--out", type=Path, required=True, help="目标 MP4 路径，重名时递增版本")
        gen.add_argument("--job-key", help="指定断点键，默认从 --out 生成")
        gen.add_argument("--tag", help="费用台账分组")
        gen.add_argument("--force-new", action="store_true", help="通过新审批替换现有断点")
        if name == "gen":
            gen.add_argument("--approve-digest", help="相同请求的最新 preflight 审批摘要")
        else:
            gen.add_argument("--simulated-gpu", help="用于规划的 ComfyUI GPU 模拟")
        _json_flag(gen)

    jobs = commands.add_parser("jobs", help="查询、恢复、领取或取消任务")
    job_commands = jobs.add_subparsers(dest="job_action", required=True)
    ls = job_commands.add_parser("list")
    _provider_flag(ls)
    _json_flag(ls)
    for name in ("resume", "fetch", "cancel"):
        job = job_commands.add_parser(name)
        job.add_argument("task_key")
        if name != "cancel":
            job.add_argument("--out", type=Path, help="改用目标 MP4 路径")
            job.add_argument("--remote-job-id", "--remote-task-id", help="指定已有远端作业或订单 ID")
        _json_flag(job)

    gpu = commands.add_parser("gpu", help="管理受管 GPU 会话")
    gpu_commands = gpu.add_subparsers(dest="gpu_action", required=True)
    for name in ("plan", "up", "status", "reconcile", "stop", "save-image"):
        action = gpu_commands.add_parser(name)
        action.add_argument("--backend", choices=("colab", "chenyu"), default="colab")
        _daemon_address(action)
        if name != "status":
            action.add_argument("--session")
        if name in ("plan", "up"):
            action.add_argument("--gpu")
            action.add_argument("--max-runtime-minutes", type=int)
            action.add_argument("--idle-minutes", type=int)
            action.add_argument("--high-mem", action=argparse.BooleanOptionalAction, default=None)
            action.add_argument("--setup", action="store_true")
            action.add_argument("--setup-params", type=json.loads, help="setup 参数 JSON 对象")
            action.add_argument("--image-uuid")
            action.add_argument("--pod-uuid")
            action.add_argument("--pod-tag")
        if name in ("up", "save-image"):
            action.add_argument("--approve-digest")
        if name == "up":
            action.add_argument("--no-connect", action="store_true")
        if name == "reconcile":
            action.add_argument("--clear-missing", action="store_true", help="晨羽确认无实例后清理分配占位")
        if name == "stop":
            action.add_argument("--shutdown-only", action="store_true", help="晨羽仅关机，保留实例")
        _json_flag(action)

    daemon = commands.add_parser("daemon", help="管理本机 Colab 守护进程")
    daemon_commands = daemon.add_subparsers(dest="daemon_action", required=True)
    for name in ("start", "status", "stop"):
        action = daemon_commands.add_parser(name)
        _daemon_address(action)
        if name == "start":
            action.add_argument("--foreground", action="store_true")
            action.add_argument("--no-auto-tunnel", action="store_true")
        if name == "stop":
            mode = action.add_mutually_exclusive_group()
            mode.add_argument("--stop-colab", action="store_true")
            mode.add_argument("--detach-colab", action="store_true")
        _json_flag(action)

    costs = commands.add_parser("ledger", help="记录费用、核对钱包余额和汇总")
    cost_commands = costs.add_subparsers(dest="ledger_action", required=True)
    record = cost_commands.add_parser("record")
    record.add_argument("--provider", choices=registry.PROVIDER_ORDER, required=True)
    record.add_argument("--quantity", type=float, required=True)
    record.add_argument("--unit", choices=ledger.UNITS, required=True)
    record.add_argument("--idempotency-key", required=True)
    record.add_argument("--record-type", choices=ledger.RECORD_TYPES, default="external")
    record.add_argument("--action", default="manual")
    record.add_argument("--estimated", action="store_true")
    record.add_argument("--est-cny", type=float)
    record.add_argument("--tag")
    record.add_argument("--note")
    record.add_argument("--account-fp")
    _json_flag(record)
    summary = cost_commands.add_parser("summary")
    _provider_flag(summary)
    summary.add_argument("--tag")
    summary.add_argument("--since")
    summary.add_argument("--account-fp")
    _json_flag(summary)
    balance = cost_commands.add_parser("balance")
    balance_commands = balance.add_subparsers(dest="balance_action", required=True)
    sync = balance_commands.add_parser("sync", description="仅支持 runninghub / chenyu 实时余额同步")
    sync.add_argument("--provider", choices=("runninghub", "chenyu"), required=True)
    _json_flag(sync)
    set_balance = balance_commands.add_parser("set")
    set_balance.add_argument("--provider", choices=registry.PROVIDER_ORDER, required=True)
    set_balance.add_argument("--unit", choices=ledger.UNITS, required=True)
    set_balance.add_argument("--balance", type=float)
    set_balance.add_argument("--cash-balance", type=float)
    set_balance.add_argument("--card-balance", type=float)
    set_balance.add_argument("--note", default="")
    set_balance.add_argument("--account-fp")
    _json_flag(set_balance)
    show = balance_commands.add_parser("show")
    _provider_flag(show)
    show.add_argument("--account-fp")
    _json_flag(show)
    return parser


def _print(value, *, file=None):
    file = file or sys.stdout
    encoding = getattr(file, "encoding", None) or "utf-8"
    print(value.encode(encoding, errors="backslashreplace").decode(encoding), file=file)


def _render_json(value, *, file=None):
    target = file or sys.stdout
    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    try:
        rendered.encode(getattr(target, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        rendered = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False)
    _print(rendered, file=target)


def _print_preflight_summary(value):
    facts = value.get("facts")
    if not isinstance(facts, dict):
        return
    width, height = facts.get("width"), facts.get("height")
    seconds = facts.get("seconds")
    frames = facts.get("frames") if facts.get("frames") is not None else facts.get("expected_frames")
    aspect = facts.get("aspect")
    has_seconds = type(seconds) in (int, float)
    has_frames = type(frames) is int
    timing = f"{seconds}s" + (f"（{frames} 帧 @ 24fps）" if has_frames else "") if has_seconds else ""
    if type(width) is int and type(height) is int:
        size_text = f"{width}x{height}" + (f"（{aspect}）" if isinstance(aspect, str) and aspect else "")
        _print(f"规格 {size_text}" + (f"，{timing}" if timing else ""))
    elif isinstance(aspect, str) and aspect:
        mp = facts.get("megapixels")
        up = " + 2x 超分" if facts.get("upscale_2x") is True else ""
        spec_text = (
            f"画幅 {aspect}"
            + (f" / {mp} MP" if type(mp) in (int, float) else "")
            + up + "（预检阶段像素待成片确认）"
        )
        _print(f"规格 {spec_text}" + (f"，{timing}" if timing else ""))
    bindings = facts.get("prompt_bindings")
    if isinstance(bindings, dict):
        paths_by_file = {}
        for info in bindings.values():
            if isinstance(info, dict) and info.get("file"):
                paths_by_file.setdefault(info["file"], set()).add(info.get("path") or info["file"])
        ambiguous_files = {name for name, paths in paths_by_file.items() if len(paths) > 1}
        for tag, info in bindings.items():
            if not isinstance(info, dict):
                continue
            file_name = info.get("file") or ""
            raw_path = info.get("path") or ""
            if file_name in ambiguous_files and raw_path:
                parent = Path(raw_path).parent.name
                label = f"{parent}/{file_name}" if parent else raw_path
                if sum(1 for p in paths_by_file[file_name] if Path(p).parent.name == parent) > 1:
                    label = raw_path
            else:
                label = file_name or raw_path
            source = info.get("source")
            if source == "video_audio" and info.get("from"):
                suffix = f"（来自 {info['from']} 伴音）"
            elif source == "video" and info.get("use_audio") is False:
                suffix = "（无伴音）"
            elif source == "first_frame":
                suffix = "（首帧）"
            else:
                suffix = ""
            _print(f"素材绑定 {tag} = {label}{suffix}")


def _emit(data, args, config=None):
    value = config.redact(data) if config else data
    if not args.json:
        if args.command in ("preflight", "gen") and isinstance(value, dict):
            provider = value.get("selected_provider") or value.get("provider_id")
            if provider:
                _print(f"通道 {provider}")
            if args.command == "preflight":
                _print_preflight_summary(value)
            if path := value.get("artifact_path"):
                _print(f"已交付 {path}")
        elif args.command == "init":
            _print(f".env {value['status']} {value['path']}")
            return
    _render_json(value)


def _init(args):
    target = args.env_file or (Path.cwd() / ".env" if args.local else global_env_path())
    if args.env_file and args.local:
        raise ValueError("--local 与 --env-file 互斥")
    target = target.expanduser().absolute()
    if target.is_symlink():
        raise ValueError(".env 不能是符号链接")
    exists = target.exists()
    if args.from_stdin_json:
        try:
            supplied = json.load(sys.stdin)
        except (ValueError, UnicodeError):
            raise ValueError("标准输入必须为有效 JSON 键值对象") from None
        if not isinstance(supplied, dict) or any(not isinstance(v, str) for v in supplied.values()):
            raise ValueError("标准输入必须为键值均为字符串的 JSON 对象")
    else:
        supplied = {}
    if exists and not args.update:
        existing = read_env_file(target)
        update_env_file(target, {})
        safe_path = load_config(env_file=target).redact(str(target))
        configured_keys = sorted(key for key in _INIT_KEYS if existing.get(key, "").strip())
        return {"status": "kept", "path": safe_path, "configured_keys": configured_keys}
    if not args.from_stdin_json and sys.stdin.isatty():
        for key in ("RUNNINGHUB_API_KEY", "RUNNINGHUB_WORKFLOW_ID", "CHENYU_API_KEY"):
            entered = getpass.getpass(f"{key}（留空跳过） ").strip()
            if entered:
                supplied[key] = entered
    if not exists:
        supplied = {**dict.fromkeys(_INIT_KEYS, ""), **supplied}
    update_env_file(target, supplied, overwrite=args.update)
    safe_path = load_config(env_file=target).redact(str(target))
    return {"status": "updated" if exists else "created", "path": safe_path}


def _doctor(args, config):
    ffmpeg, ffprobe = video_common.ffmpeg_bin(), video_common.ffprobe_bin()
    supported = sys.platform != "win32"
    colab = {"platform_supported": supported}
    if supported and args.provider in (None, "colab-comfy"):
        from .providers.colab_comfy import colab_cli_status

        colab.update(colab_cli_status(config))
    elif not supported:
        colab.update(reason=registry.WINDOWS_COLAB_REASON, detail="请使用 WSL2 或选择原生 Windows 通道。")
    workflow_name = registry.normalize_workflow(args.workflow, args.provider)
    effective_order = registry.configured_provider_order(config)
    names = (args.provider,) if args.provider else registry.display_provider_order(config)
    runninghub_workflows = []
    if "runninghub" in names and args.workflow is None:
        runninghub_workflows = [
            {"workflow": name, **registry.probe_provider("runninghub", config, workflow=name).to_dict()}
            for name in JINYU_WORKFLOWS
        ]
        workflow_name = next((row["workflow"] for row in runninghub_workflows if row["available"]), workflow_name)
    probes = [registry.probe_provider(name, config, remote=args.remote, workflow=workflow_name).to_dict() for name in names]
    check_tools = bool(ffmpeg and ffprobe and sys.version_info >= (3, 12))
    result = {
        "status": "pass" if check_tools else "block", "python": sys.version.split()[0],
        "ffmpeg": ffmpeg, "ffprobe": ffprobe, "env_files": list(map(str, config.env_files)),
        "env_priority": "显式 .env > 当前目录 .env > 全局 .env > 进程环境",
        "provider_order": list(effective_order),
        "credential_status": {
            "runninghub": validate_api_key(config.get("RUNNINGHUB_API_KEY"))[1] or "configured",
            "chenyu": validate_api_key(config.get("CHENYU_API_KEY"))[1] or "configured",
        },
        "data_dir": str(config.data_dir), "colab": colab, "providers": probes,
    }
    if not ffmpeg or not ffprobe:
        result["ffmpeg_install"] = {"macOS": "brew install ffmpeg", "Windows": "winget install Gyan.FFmpeg",
                                    "Linux": "从系统包管理器安装 ffmpeg"}
    if runninghub_workflows:
        result["runninghub_workflows"] = runninghub_workflows
    if args.provider and not probes[0]["available"]:
        result["status"] = "block"
    return result, 0 if result["status"] == "pass" else 3


def _workflows(args, config):
    providers = (args.provider,) if args.provider else registry.PROVIDER_ORDER
    workflows = [row for name in providers for row in registry.workflow_catalog(name)]
    if not args.remote:
        return workflows, 0
    remotes = {}
    for name in providers:
        if name not in ("colab-comfy", "chenyu-comfy"):
            continue
        probe = registry.probe_provider(name, config)
        if not probe.available:
            remotes[name] = probe.to_dict()
            continue
        try:
            remotes[name] = registry.get_provider(name, config).remote_workflows()
        except (OSError, http.client.HTTPException, ProviderError, ValueError) as exc:
            remotes[name] = {"available": False, "reason": getattr(exc, "code", "remote_unavailable")}
    return {"workflows": workflows, "remote": remotes}, 0


def _generation(args, config):
    prompt = args.prompt
    if args.prompt_file is not None:
        prompt = sys.stdin.read() if args.prompt_file == Path("-") else args.prompt_file.read_text(encoding="utf-8-sig")
    params = {key: getattr(args, key) for key in (
        "width", "height", "aspect", "seconds", "seed", "steps", "megapixels", "upscale_2x",
        "save_last_frame", "no_music", "trim_to_requested_seconds",
    ) if getattr(args, key) is not None}
    if any(not math.isfinite(value) for key, value in params.items() if key in ("seconds", "megapixels")):
        raise ValueError("--seconds 与 --megapixels 须为有限数")
    if "seconds" in params:
        seconds = params["seconds"]
        if not 0.2 <= seconds <= 15:
            raise ValueError("--seconds 须介于 0.2 和 15 之间")
        if seconds.is_integer():
            params["seconds"] = int(seconds)
    if "steps" in params and not 1 <= params["steps"] <= 100:
        raise ValueError("--steps 须介于 1 和 100 之间")
    if "seed" in params and not -1 <= params["seed"] < 2**64:
        raise ValueError("--seed 须为 -1 或 64 位无符号整数")
    inputs = {}
    for name in ("image", "audio", "video"):
        if paths := getattr(args, name):
            inputs[name + "s"] = paths
    if args.mute_reference_video:
        if not args.video:
            raise ValueError("--mute-reference-video 需要 --video")
        inputs["videos"] = [dict(path=path, use_audio=False) for path in args.video]
    if args.first_frame:
        inputs["image_path"] = args.first_frame
    request = GenerationRequest(
        "video", prompt=prompt, output_path=args.out, provider_id=args.provider, workflow=args.workflow,
        job_key=args.job_key, task=args.task, params=params, inputs=inputs, simulated_gpu=getattr(args, "simulated_gpu", None),
        approve_digest=getattr(args, "approve_digest", None), force_new=args.force_new, metadata={"tag": args.tag},
    )
    route = registry.resolve_provider(request, config)
    store = TaskStore(config=config)
    if route.provider is None:
        plan = PreflightResult("", "block", "", "", "", facts={"workflow": request.workflow})
        plan.signals.append({"severity": "block", "code": "no_eligible_provider", "detail": "没有通道同时满足配置和请求能力"})
        if args.command == "gen":
            raise PreflightBlockedError("没有符合请求能力且可用的通道", preflight=plan,
                                        details={"routing_skipped": route.skipped})
    elif args.command == "preflight":
        plan = route.provider.preflight(request, store=store)
    else:
        return route.provider.generate(request, store=store).to_dict(), 0
    data = plan.to_dict()
    data["approve_digest"] = plan.digest or None
    data["selected_provider"] = route.provider.provider_id if route.provider else None
    data["routing_skipped"] = route.skipped
    return data, plan.exit_code


def _job_summary(record):
    summary = {field: record[field] for field in _SUMMARY_FIELDS if field in record}
    if record.get("job_history"):
        summary["job_history"] = [
            {field: job.get(field) for field in _HISTORY_FIELDS} for job in record["job_history"]
        ]
    return summary


def _jobs(args, config):
    store = TaskStore(config=config)
    if args.job_action == "list":
        return [_job_summary(record) for record in store.list_tasks(provider=args.provider)], 0
    record = store.get(args.task_key)
    if record is None:
        raise ProviderConflictError("任务断点不存在", code="checkpoint_missing")
    provider = registry.get_provider(record["provider"], config)
    if args.job_action == "cancel":
        return _job_summary(provider.cancel(args.task_key, store=store)), 0
    method = provider.resume if args.job_action == "resume" else provider.fetch
    return method(args.task_key, store=store, output_path=args.out, remote_job_id=args.remote_job_id).to_dict(), 0


def _colab_client(args, config):
    from .providers.colab_comfy import require_colab
    from .providers.comfy_client import LocalDaemonClient

    require_colab()
    return LocalDaemonClient(host=args.host, port=args.port, config=config)


def _daemon(args, config):
    client = _colab_client(args, config)
    from .local import daemon
    from .providers.colab_comfy import check_daemon_identity

    endpoint = dict(host=client.host, port=client.port, engine_config=config)
    if args.daemon_action == "start":
        if args.foreground:
            daemon.run_daemon(**endpoint, no_auto_tunnel=args.no_auto_tunnel)
            return {"running": False}, 0
        return daemon.start_daemon_background(**endpoint, no_auto_tunnel=args.no_auto_tunnel), 0
    state = daemon.daemon_status(**endpoint)
    if args.daemon_action == "status":
        return state, 0
    stop = dict(stop_colab=args.stop_colab, detach_colab=args.detach_colab)
    if state["running"]:
        check_daemon_identity(client, config)
        status, reply = client._request_json("POST", "/api/local/daemon/stop", payload=stop, timeout=180)
        if status != 200 or not reply.get("ok"):
            return reply, 3 if status == 409 else 1
        daemon.wait_for_daemon_exit(client.host, client.port, engine_config=config)
    elif args.stop_colab or args.detach_colab:
        reply = daemon.stop_unreachable_daemon(expected_host=client.host, expected_port=client.port,
                                               engine_config=config, **stop)
    else:
        reply = {"ok": True, "running": False}
    return reply, 0 if reply.get("ok") else 3


def _colab_gpu_task_error(task_id, error):
    message = error if isinstance(error, str) and error else "Colab GPU 后台任务失败"
    # TaskRegistry preserves code prefixes and legacy guard messages as plain text.
    code = message.partition(":")[0]
    if code == "远端 setup 提交失败 HTTP 403":
        code = "approval_required"
    elif message.startswith("请先释放受管会话 "):
        code = "active_managed_session_exists"
    elif message.startswith(("Colab 当前账号配置与占位不符，", "Colab 本地会话归属已变化，")) or (
        message.startswith(("会话 ", "Colab 会话 ")) and message.endswith((
            " 已存在，请重新执行 gpu plan", " 不存在，请先启动会话", " 不存在，请先执行 gpu up",
            " 的占位已变化，请核对会话归属",
        ))
    ):
        code = "local_task_conflict"
    exit_code = _COLAB_TASK_EXIT_CODES.get(code, 1)
    return ProviderError(message, code=code if exit_code != 1 else "local_task_failed",
                         exit_code=exit_code, details={"task_id": task_id, "status": "error"})


def _wait_for_colab_gpu_task(client, task_id, *, timeout):
    if not isinstance(task_id, str) or not task_id:
        raise ProviderError("守护进程返回的 Colab GPU 任务 ID 无效", code="local_task_invalid_id")
    path = "/api/local/tasks/" + quote(task_id, safe="")
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            http_status, task = client._request_json("GET", path, timeout=min(30, remaining))
        except TimeoutError:
            continue
        if http_status != 200 or not isinstance(task, dict):
            raise client._error(http_status, task, code="local_task_poll_failed")
        status = task.get("status")
        if status == "done":
            return {**task, "task_id": task_id}
        if status == "error":
            raise _colab_gpu_task_error(task_id, task.get("error"))
        if status != "running":
            raise ProviderError(f"守护进程返回未知的 Colab GPU 任务状态 {status!r}",
                                code="local_task_invalid_status", details={"task_id": task_id})
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(_COLAB_TASK_POLL_INTERVAL, remaining))
    raise ProviderError(
        f"等待 Colab GPU 任务 {task_id} 超时。请运行 yc-h3 gpu status / yc-h3 gpu reconcile 核对会话状态。",
        code="local_task_timeout", exit_code=1, details={"task_id": task_id},
    )


def _gpu(args, config):
    action = args.gpu_action
    if args.backend == "colab":
        client = _colab_client(args, config)
        from .providers.colab_comfy import check_daemon_identity

        if action == "save-image":
            raise ValueError("gpu save-image 仅支持 --backend chenyu")
        if any(getattr(args, name, None) for name in ("image_uuid", "pod_uuid", "pod_tag", "clear_missing", "shutdown_only")):
            raise ValueError("该 GPU 选项仅支持 --backend chenyu")
        check_daemon_identity(client, config)
        if action == "status":
            state = client.state()
            return {name: state.get(name) for name in ("colab", "tunnel", "remote", "idle")}, 0
        body = {key: getattr(args, key) for key in (
            "session", "gpu", "setup", "setup_params", "max_runtime_minutes", "idle_minutes", "high_mem", "approve_digest",
        ) if getattr(args, key, None) is not None}
        if action == "up":
            body["connect"] = not args.no_connect
        status, response = client._request_json("POST", "/api/local/gpu/" + action, payload=body, timeout=180)
        exit_code = response.get("exit_code")
        if status >= 400:
            return response, exit_code if exit_code in (1, 2, 3) else (3 if status == 409 else 1)
        if action in ("up", "stop"):
            timeout_key = "setup" if action == "up" and args.setup else action
            task = _wait_for_colab_gpu_task(client, response.get("task_id"), timeout=_COLAB_TASK_TIMEOUTS[timeout_key])
            return task, 0
        return response, 2 if response.get("status") == "warn" else 0
    from .chenyu_gpu import ChenyuGpuManager

    if args.host is not None or args.port is not None or getattr(args, "high_mem", None) is not None:
        raise ValueError("守护进程地址和 --high-mem 仅支持 --backend colab")
    manager = ChenyuGpuManager(config=config)
    if action == "status":
        return manager.gpu_status(), 0
    if action == "reconcile":
        result = manager.gpu_reconcile(args.session, clear_missing=args.clear_missing)
        return result, 3 if result.get("status") == "block" else 0
    if action == "stop":
        return manager.gpu_stop(args.session, shutdown_only=args.shutdown_only), 0
    if action == "save-image":
        return manager.gpu_save_image(args.session, approve_digest=args.approve_digest), 0
    options = {field: getattr(args, field) for field in (
        "session", "gpu", "setup", "setup_params", "image_uuid", "pod_uuid", "pod_tag",
        "max_runtime_minutes", "idle_minutes",
    ) if getattr(args, field) is not None}
    if action == "up":
        return manager.gpu_up(approve_digest=args.approve_digest, connect=not args.no_connect, **options), 0
    result = manager.gpu_plan(**options)
    return result, {"block": 3, "warn": 2}.get(result.get("status"), 0)


def _ledger(args, config):
    if args.ledger_action == "summary":
        return ledger.summarize_usage(config=config, provider=args.provider, project=args.tag,
                                     since=args.since, account_fp=args.account_fp), 0
    if args.ledger_action == "record":
        return ledger.record_usage(
            config=config, provider=args.provider, record_type=args.record_type, action=args.action,
            quantity=args.quantity, unit=args.unit, estimated=args.estimated, idempotency_key=args.idempotency_key,
            project=args.tag, note=args.note, account_fp=args.account_fp, est_cny=args.est_cny,
        ), 0
    if args.balance_action == "show":
        return ledger.reconcile_balances(config=config, provider=args.provider, account_fp=args.account_fp), 0
    if args.balance_action == "set":
        amount = args.balance
        parts = {}
        if args.cash_balance is not None or args.card_balance is not None:
            if ledger.wallet_owner(args.provider) != "chenyu" or args.unit != "CNY":
                raise ValueError("双钱包分项仅适用于晨羽 CNY")
            parts = ledger.parse_chenyu_balance({"balance": args.cash_balance, "card_balance": args.card_balance})
            if amount is not None and not math.isclose(parts["balance"], amount):
                raise ValueError("--balance 须等于两个钱包余额的合计")
            amount = parts.pop("balance")
        if amount is None:
            raise ValueError("请提供 --balance 或两个晨羽钱包余额")
        return ledger.set_balance(config=config, provider=args.provider, unit=args.unit, balance=amount,
                                  note=args.note, account_fp=args.account_fp, **parts), 0
    probe = registry.probe_provider(args.provider, config, remote=True)
    if not probe.available:
        raise ProviderError(probe.details.get("detail") or "余额查询失败", code=probe.reason, exit_code=3)
    details = dict(probe.details)
    if args.provider == "chenyu":
        balance = {key: details[key] for key in ("balance", "cash_balance", "card_balance")}
        unit = "CNY"
    else:
        points = details.get("remainPoint", details.get("remainCoins"))
        if points is None:
            raise ProviderError("RunningHub 未返回积分余额", code="account_status_failed")
        balance, unit = {"balance": points}, "point"
    return ledger.set_balance(config=config, provider=args.provider, unit=unit, source="api",
                              account_fp=details["account_fp"], **balance), 0


def _dispatch(args, config):
    if args.command == "providers":
        effective_order = registry.configured_provider_order(config)
        ordered = registry.display_provider_order(config)
        priority = {name: idx for idx, name in enumerate(effective_order, 1)}
        if args.probe or args.remote:
            return [{**registry.probe_provider(name, config, remote=args.remote).to_dict(),
                     "auto_route_priority": priority.get(name)}
                    for name in ordered], 0
        return [{"provider_id": name, "capabilities": ["video"],
                 "platform_supported": name != "colab-comfy" or sys.platform != "win32",
                 "auto_route_priority": priority.get(name)}
                for name in ordered], 0
    handlers = {"doctor": _doctor, "workflows": _workflows, "preflight": _generation, "gen": _generation,
                "jobs": _jobs, "gpu": _gpu, "daemon": _daemon, "ledger": _ledger}
    return handlers[args.command](args, config)


def main(argv=None):
    args = None
    config = None
    try:
        args = _parser().parse_args(argv)
        if args.command == "init":
            result, code = _init(args), 0
        else:
            config = load_config(env_file=args.env_file, data_dir=args.data_dir)
            result, code = _dispatch(args, config)
        _emit(result, args, config)
        return code
    except KeyboardInterrupt:
        _print("操作已中断", file=sys.stderr)
        return 1
    except (ProviderError, TaskStoreError, ValueError, OSError, http.client.HTTPException, RuntimeError) as exc:
        code = getattr(exc, "exit_code", 3 if isinstance(exc, TaskConflictError) else 1)
        message = str(exc)
        error = {"error": getattr(exc, "code", "command_error"), "message": message, "exit_code": code}
        if isinstance(exc, ProviderError):
            error.update(exc.details)
            if preflight := error.get("preflight"):
                preflight["approve_digest"] = preflight.get("digest") or None
        if config:
            error = config.redact(error)
        if (args is not None and getattr(args, "json", False)) or (args is None and "--json" in (argv or sys.argv[1:])):
            _print(json.dumps(error, ensure_ascii=True, allow_nan=False), file=sys.stderr)
        else:
            _print(error["message"], file=sys.stderr)
            if "preflight" in error:
                _render_json(error["preflight"], file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
