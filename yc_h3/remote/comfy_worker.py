"""Process and prompt lifecycle for repository-owned ComfyUI workflows.

Adapted from WorldForge (MIT License). ComfyUI always binds strictly to 127.0.0.1
and records its PGID under ``<remote_root>/run/worker_pgids/<pid>.pgid``.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from . import _util, comfy_assets, presets, setup_env
    from .comfy_assets import ensure_weights, weight_ready
    from .workflows import registry as workflows
else:
    import _util
    import comfy_assets
    import presets
    import setup_env
    from comfy_assets import ensure_weights, weight_ready
    from workflows import registry as workflows

registry = workflows.h3
ROOT = Path(_util.remote_root())
_, OUTPUTS, JOBS_DIR = _util.remote_job_paths(ROOT)
INPUTS = ROOT / "inputs"
COMFY_OUTPUTS = ROOT / "comfy_outputs"
WORKER_PGIDS = ROOT / "run" / "worker_pgids"
START_TIMEOUT = 120
GENERATION_TIMEOUT = 3600


class Cancelled(RuntimeError):
    pass


def _pgid_file(pid: int) -> Path:
    return WORKER_PGIDS / f"{pid}.pgid"


def _write_pgid_file(pid: int) -> None:
    try:
        WORKER_PGIDS.mkdir(parents=True, exist_ok=True)
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        _pgid_file(pid).write_text(f"{pgid}\n", encoding="utf-8")
    except OSError:
        pass


def _remove_pgid_file(pid: int) -> None:
    try:
        _pgid_file(pid).unlink(missing_ok=True)
    except OSError:
        pass


def check_cancelled(job_id):
    if job_id:
        try:
            job = json.loads((JOBS_DIR / job_id / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if job.get("status") == "cancelled" or job.get("cancel_requested") is True:
            raise Cancelled("任务已取消")


def emit(job_id, prompt_id=None, **fields):
    print("@@FORGE@@ " + json.dumps(dict(fields, job_id=job_id, prompt_id=prompt_id), ensure_ascii=False), flush=True)


def comfy_command(port, profile, plugins, workflow=None, params=None):
    workflow = workflow or workflows.get_workflow(registry.DEFAULT_WORKFLOW)
    runtime = setup_env.comfy_runtime(workflow.profile_id)
    args = [
        str(runtime.python),
        "-u",
        "main.py",
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--input-directory",
        str(INPUTS),
        "--output-directory",
        str(COMFY_OUTPUTS),
        "--disable-all-custom-nodes",
        "--use-pytorch-cross-attention",
        "--models-directory", str(setup_env.COMFY_MODELS_DIR),
    ]
    if workflow.highvram(profile, params or {}):
        args.append("--highvram")
    if plugins:
        args += ["--whitelist-custom-nodes", *plugins]
    return args


def _prompt_match(entry, client_id, graph):
    return (
        isinstance(entry, (list, tuple))
        and len(entry) > 3
        and entry[2] == graph
        and isinstance(entry[3], dict)
        and entry[3].get("client_id") == client_id
    )


def submit_prompt(api, graph, client_id, prompt_id, check_cancel):
    try:
        response = api("/prompt", {"prompt": graph, "client_id": client_id, "prompt_id": prompt_id}, timeout=30)
        if response.get("prompt_id") != prompt_id:
            raise RuntimeError(f"ComfyUI 未确认提交 {prompt_id}：{response}")
        return prompt_id
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise RuntimeError(f"ComfyUI 拒绝工作流：{error.read(8192).decode(errors='replace')}") from error
    except (OSError, ValueError):
        pass
    for _ in range(5):
        check_cancel()
        try:
            pending = api("/queue")
            history = api(f"/history/{prompt_id}")
            entries = pending.get("queue_running", []) + pending.get("queue_pending", [])
            entries += [item.get("prompt") for item in history.values()]
            matches = {entry[1] for entry in entries if _prompt_match(entry, client_id, graph)}
            if matches == {prompt_id}:
                return prompt_id
            if matches:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"ComfyUI 提交结果需要对账：{prompt_id}")


def _process_vram(pid, timeout=3):
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            encoding="utf-8", errors="replace",
            check=True,
            timeout=timeout,
        )
        return sum(
            int(mib)
            for row in result.stdout.splitlines()
            if "," in row
            for proc, mib in [row.split(",", 1)]
            if int(proc) == pid
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    _util.atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", temp=temp)


def history_output(history, save_id, kind, extensions=None):
    if extensions is None:
        extensions = (".mp4", ".webm", ".mov", ".mkv") if kind == "video" else (".png", ".jpg", ".jpeg", ".webp")
    results = history.get("outputs", {}).get(save_id, {})
    candidates = [
        item
        for value in results.values()
        if isinstance(value, list)
        for item in value
        if isinstance(item, dict)
        and item.get("type") == "output"
        and str(item.get("filename", "")).lower().endswith(extensions)
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"指定输出节点未返回唯一 {kind} 文件")
    entry = candidates[0]
    source_root = COMFY_OUTPUTS.resolve()
    source = (source_root / entry.get("subfolder", "") / entry["filename"]).resolve()
    if source_root not in source.parents or not source.is_file():
        raise RuntimeError("ComfyUI 返回的输出路径无效")
    return source


def history_video(history, save_id):
    return history_output(history, save_id, "video", extensions=(".mp4",))


def publish_output(workflow, history, save_id, directory, params, profile, check_cancel):
    source = history_video(history, save_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"minimax-h3{source.suffix.lower()}"
    temp = directory / f".{target.stem}.part{target.suffix}"
    try:
        workflow.prepare_output(source, temp, params)
        details = workflow.verify_output(temp, params, profile)
        check_cancel()
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "file": target.name,
        "path": str(target),
        "kind": workflow.output_kind,
        "seed": params.get("seed"),
        **(details or {}),
        "sha256": comfy_assets.file_sha256(target),
    }


class PromptEvents:
    def __init__(self, connected):
        self.connected = connected
        self.completed = False
        self.cached_nodes = []
        self.active_node = None
        self.node_started = None
        self.nodes = {}
        self.samplers = {}

    def receive(self, message, prompt_id, job_id):
        if not isinstance(message, str):
            return
        event = json.loads(message)
        data = event.get("data") or {}
        if data.get("prompt_id") != prompt_id:
            return
        now = time.monotonic()
        node = data.get("node")
        event_type = event.get("type")
        if event_type == "execution_cached":
            self.cached_nodes.extend(data.get("nodes", []))
        elif event_type == "executing":
            if self.active_node is not None:
                self.nodes[self.active_node] = self.nodes.get(self.active_node, 0) + now - self.node_started
            self.active_node, self.node_started = node, now
            self.completed = node is None
        elif event_type == "progress" and node is not None and data.get("max"):
            value, maximum = data["value"], data["max"]
            sampler = self.samplers.setdefault(
                node,
                {
                    "last_at": self.node_started if node == self.active_node else None,
                    "last_value": -1,
                    "steps": maximum,
                    "step_times_s": [],
                },
            )
            if value > sampler["last_value"]:
                if value > 0 and sampler["last_at"] is not None:
                    sampler["step_times_s"].append(now - sampler["last_at"])
                sampler.update(last_at=now, last_value=value)
            return min(0.92, 0.56 + 0.36 * value / maximum), f"采样 {value}/{maximum}"

    def snapshot(self):
        samplers = {
            node: {"steps": item["steps"], "step_times_s": item["step_times_s"]}
            for node, item in self.samplers.items()
        }
        return {
            "ws_connected": self.connected,
            "nodes_s": self.nodes if self.connected else None,
            "samplers": samplers if self.connected else None,
            "execution_cached": list(dict.fromkeys(self.cached_nodes)) if self.connected else None,
        }


class Comfy:
    def __init__(self):
        self.proc = None
        self.launch_key = None
        self.port = None
        self.nodes = {}
        self.baseline = None
        self.observations = PromptEvents(False)
        self.on_process = lambda pid, cold_start: None
        self.on_observations = lambda observations: None

    def api(self, route, body=None, timeout=5):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    def stop(self):
        if self.proc is not None:
            pid = getattr(self.proc, "pid", None)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            if isinstance(pid, int):
                _remove_pgid_file(pid)
            self.proc = None
        self.launch_key = None
        self.nodes = {}

    def start(self, workflow, profile, params, check_cancel):
        workflow = workflows.get_workflow(workflow)
        runtime = setup_env.comfy_runtime(workflow.profile_id)
        plugins = workflow.plugins(profile, params)
        key = (workflow.profile_id, workflow.highvram(profile, params), plugins)
        with setup_env.comfy_runtime_lock(workflow.profile_id, check_cancel=check_cancel):
            check_cancel()
            cold_start = key != self.launch_key or self.proc is None or self.proc.poll() is not None
            if cold_start:
                self.stop()
                missing_plugins = [p for p in plugins if not setup_env.custom_node_installed(p, workflow.profile_id)]
                if missing_plugins:
                    raise RuntimeError(
                        "runtime_or_plugins_not_ready: 缺少 ComfyUI 插件 "
                        + ", ".join(missing_plugins)
                        + "，请先通过经审批的 setup 流程安装"
                    )
                check_cancel()
                INPUTS.mkdir(parents=True, exist_ok=True)
                COMFY_OUTPUTS.mkdir(parents=True, exist_ok=True)
                with socket.socket() as reserved:
                    reserved.bind(("127.0.0.1", 0))
                    self.port = reserved.getsockname()[1]
                self.proc = subprocess.Popen(
                    comfy_command(self.port, profile, plugins, workflow, params),
                    cwd=runtime.checkout,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                )
                if isinstance(getattr(self.proc, "pid", None), int):
                    _write_pgid_file(self.proc.pid)
                self.on_process(self.proc.pid, True)
                deadline = time.monotonic() + START_TIMEOUT
                try:
                    while time.monotonic() < deadline:
                        check_cancel()
                        if self.proc.poll() is not None:
                            raise RuntimeError(f"ComfyUI 启动退出 ({self.proc.returncode})")
                        try:
                            self.nodes = self.api("/object_info", timeout=10)
                            break
                        except (OSError, ValueError):
                            time.sleep(0.5)
                    else:
                        raise TimeoutError("ComfyUI 启动探测超时")
                    self.baseline = _process_vram(self.proc.pid)
                    self.launch_key = key
                except BaseException:
                    self.stop()
                    raise
            else:
                self.on_process(self.proc.pid, False)
            missing = workflow.required_nodes(plugins) - self.nodes.keys()
            if missing:
                self.stop()
                raise RuntimeError("ComfyUI 缺少节点：" + ", ".join(sorted(missing)))

    def offload(self, check_cancel):
        if self.proc is None or self.proc.poll() is not None:
            self.stop()
            return
        pid = self.proc.pid
        try:
            deadline = time.monotonic() + 20
            before = _process_vram(pid)
            self.api("/free", {"unload_models": True, "free_memory": True}, timeout=2)
            while time.monotonic() < deadline:
                check_cancel()
                queue = self.api("/queue", timeout=min(2, max(0.01, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    break
                used = _process_vram(pid, timeout=min(3, max(0.01, deadline - time.monotonic())))
                floor = (self.baseline or 0) + 256
                if (
                    not queue.get("queue_running")
                    and not queue.get("queue_pending")
                    and used is not None
                    and used <= floor
                    and (before is None or used <= before)
                ):
                    return
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        except (OSError, ValueError):
            pass
        self.stop()

    def wait_history(self, prompt_id, job_id, check_cancel, ws, timeout=GENERATION_TIMEOUT):
        from socket import timeout as SocketTimeout

        if ws is not None:
            from websocket import WebSocketException, WebSocketTimeoutException

        deadline = time.monotonic() + timeout
        self.observations = PromptEvents(ws is not None)
        progress = 0.56
        last_report = None

        def receive(budget):
            nonlocal ws, progress, last_report
            if ws is None:
                return
            try:
                ws.settimeout(budget)
                update = self.observations.receive(ws.recv(), prompt_id, job_id)
                now = time.monotonic()
                if last_report is None or now - last_report >= 1 or self.observations.completed:
                    self.on_observations(self.observations.snapshot())
                    last_report = now
                if update:
                    fraction, stage = update
                    progress = max(progress, fraction)
                    emit(job_id, prompt_id, event="progress", p=progress, stage=stage)
            except (WebSocketTimeoutException, SocketTimeout):
                pass
            except (WebSocketException, OSError, ValueError):
                ws = None

        while time.monotonic() < deadline:
            check_cancel()
            if self.proc.poll() is not None:
                raise RuntimeError("ComfyUI 生成进程退出")
            if ws is not None and not self.observations.completed:
                receive(0.25)
            try:
                history = self.api(f"/history/{prompt_id}").get(prompt_id)
            except (OSError, ValueError):
                history = None
            if not history:
                if ws is None or self.observations.completed:
                    time.sleep(0.25)
                continue
            drain_until = time.monotonic() + 0.5
            while ws is not None and not self.observations.completed:
                check_cancel()
                left = drain_until - time.monotonic()
                if left <= 0:
                    break
                receive(left)
            if history.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"ComfyUI 生成失败：{history['status'].get('messages', [])}")
            return history
        raise TimeoutError(f"ComfyUI 生成超时：{prompt_id}")


def _request(command):
    params = command["params"]
    return workflows.for_model(command["model"], params), params


def _ensure_workflow_weights(workflow, params, profile, report, cancel):
    assets = workflow.assets(profile, params)
    custom = params.get("_frozen_custom_assets")
    paths = ensure_weights(
        assets,
        report=report,
        check_cancel=cancel,
        venv_py=setup_env.comfy_runtime(workflow.profile_id).python,
        custom_assets=custom,
    )
    if custom:
        params["_custom_model_paths"] = {key: paths[key] for key in custom}


def run_job(comfy, command, profile):
    job_id = command["job_id"]
    cancel = lambda: check_cancelled(job_id)

    def report(p, stage):
        emit(job_id, event="progress", p=p, stage=stage)

    @contextmanager
    def phase(name):
        emit(job_id, event="phase", name=name, at=time.time())
        try:
            yield
        finally:
            emit(job_id, event="phase", name=name, at=time.time(), finished=True)

    prompt_id, ws = None, None
    downloaded_weights = False

    def weight_report(p, stage):
        nonlocal downloaded_weights
        downloaded_weights = True
        report(p, stage)

    try:
        comfy.observations = PromptEvents(False)
        comfy.on_observations = lambda observations: emit(job_id, event="observations", observations=observations)
        comfy.on_process = lambda pid, cold_start: emit(
            job_id,
            event="comfy_process",
            comfy_pid=pid,
            cold_start=cold_start or downloaded_weights,
        )
        workflow, params = _request(command)
        with phase("ensure_weights"):
            cancel()
            _ensure_workflow_weights(workflow, params, profile, weight_report, cancel)
        warmup = command["task"] == "load"
        with phase("prepare_inputs"):
            cancel()
            staged = workflow.stage(params, profile, INPUTS, job_id, cancel, warmup)
            if not warmup:
                staged["prompt"] = presets.h3_prompt(staged["prompt"], staged)
            prefix = f"{job_id}_warmup" if warmup else job_id
            graph, save_id = workflow.build(staged, profile, prefix)
        report(0.45, "启动 ComfyUI")
        with phase("comfy_start"):
            cancel()
            comfy.start(workflow.id, profile, params if warmup else staged, cancel)
        missing = {node["class_type"] for node in graph.values()} - comfy.nodes.keys()
        if missing:
            raise RuntimeError("工作流节点不可用：" + ", ".join(sorted(missing)))
        cancel()
        directory = (OUTPUTS / job_id).resolve()
        if not warmup and (
            directory != Path(command["out_dir"]).resolve()
            or OUTPUTS.resolve() not in directory.parents
        ):
            raise ValueError("ComfyUI 输出目录无效")
        client_id, prompt_id = str(uuid.uuid4()), str(uuid.uuid4())
        with phase("submit"):
            try:
                from websocket import create_connection

                ws = create_connection(f"ws://127.0.0.1:{comfy.port}/ws?clientId={client_id}", timeout=2)
            except Exception as exc:
                print(f"WebSocket 不可用，通过 history 跟踪：{exc}", flush=True)
            comfy.on_observations(PromptEvents(ws is not None).snapshot())
            cancel()
            submit_prompt(comfy.api, graph, client_id, prompt_id, cancel)
        emit(job_id, prompt_id, event="progress", p=0.56, stage="预热模型" if warmup else "已提交 ComfyUI")
        with phase("inference_total"):
            try:
                history = comfy.wait_history(
                    prompt_id, job_id, cancel, ws, timeout=300 if warmup else GENERATION_TIMEOUT
                )
            finally:
                comfy.on_observations(comfy.observations.snapshot())
        cancel()
        used_mb = _process_vram(comfy.proc.pid)
        residency = {
            "workflow": workflow.id,
            "profile": workflow.profile_id,
            "comfy_pid": comfy.proc.pid,
            "baseline_mb": comfy.baseline,
            "vram_mb": used_mb,
        }
        with phase("publish"):
            if warmup:
                source = history_output(history, save_id, workflow.output_kind)
                if source.name.startswith(prefix + "_"):
                    source.unlink()
            else:
                output = publish_output(workflow, history, save_id, directory, params, profile, cancel)
                _atomic_json(directory / "workflow.executed.json", graph)
        if warmup:
            emit(job_id, prompt_id, event="prepared", **residency)
        else:
            emit(job_id, prompt_id, event="done", outputs=[output], **residency)
    except Exception as error:
        recycle = bool(prompt_id) or isinstance(error, (TimeoutError, Cancelled, subprocess.TimeoutExpired))
        if recycle:
            comfy.stop()
        emit(job_id, prompt_id, event="error", msg=str(error), recycle=recycle)
    finally:
        if ws:
            ws.close()


def _prepare(command, profile, report, cancel):
    workflow, params = _request(command)
    cancel()
    _ensure_workflow_weights(workflow, params, profile, report, cancel)
    return params


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", help="JSON object for an I/O-only download job")
    args = parser.parse_args()
    profile = registry.probe_hardware()
    own_pid = os.getpid()
    _write_pgid_file(own_pid)
    try:
        if args.ensure:
            command = json.loads(args.ensure)
            job_id = command["job_id"]
            report = lambda p, stage: emit(job_id, event="progress", p=p, stage=stage)
            _prepare(command, profile, report, lambda: check_cancelled(job_id))
            return
        comfy = Comfy()
        try:
            emit(None, event="ready")
            for line in sys.stdin:
                data = json.loads(line)
                if data["cmd"] in ("run", "load"):
                    run_job(comfy, data, profile)
                elif data["cmd"] == "offload":
                    job_id = data.get("job_id")
                    comfy.offload(lambda: check_cancelled(job_id))
                    emit(job_id, event="offloaded", vram_mb=0, comfy_pid=comfy.proc.pid if comfy.proc else None)
        finally:
            comfy.stop()
    finally:
        _remove_pgid_file(own_pid)


if __name__ == "__main__":
    main()
