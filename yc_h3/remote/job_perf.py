"""Raw GPU job observations and atomic performance records.

Adapted from WorldForge (MIT License).
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

if __package__:
    from . import _util
else:
    import _util


GPU_FIELDS = {
    "name": "name",
    "utilization.gpu": "util",
    "memory.used": "mem_used_mb",
    "memory.total": "mem_total_mb",
    "temperature.gpu": "temp_c",
    "power.draw": "power_w",
    "clocks.current.graphics": "clock_mhz",
    "clocks.current.memory": "mem_clock_mhz",
    "pcie.link.gen.current": "pcie_gen",
    "pcie.link.width.current": "pcie_width",
    "utilization.memory": "mem_util",
    "power.limit": "power_limit_w",
}
PHASES = ("ensure_weights", "comfy_start", "prepare_inputs", "submit", "inference_total", "publish", "total")


def _smi(*args):
    try:
        result = subprocess.run(["nvidia-smi", *args], capture_output=True, encoding="utf-8", errors="replace", check=True, timeout=2)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _number(raw):
    try:
        value = float(raw)
        return int(value) if value.is_integer() else value if abs(value) < float("inf") else None
    except (ValueError, OverflowError):
        return None


def _text(raw):
    return raw if raw and raw not in ("N/A", "[N/A]", "Not Supported", "[Not Supported]", "-") else None


class Sampler:
    def __init__(self, directory, process_pid):
        self.directory = directory
        self.process_pid = process_pid
        self.stop_signal = threading.Event()
        self.thread = None
        self.error = None
        self.count = 0
        self.coverage = dict.fromkeys((*GPU_FIELDS.values(), "process_vram_mb"), 0)
        self.stats = {}
        self.first_gpu = {}

    def _gpu(self):
        output = _smi(f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits").splitlines()
        if not output:
            return {}
        parts = output[0].split(",")
        if len(parts) != len(GPU_FIELDS):
            return {}
        return {key: (_text(raw.strip()) if key == "name" else _number(raw.strip()))
                for key, raw in zip(GPU_FIELDS.values(), parts)}

    def _process_vram(self, pid):
        if pid is None:
            return None
        output = _smi("--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits")
        try:
            readings = [
                _number(memory.strip())
                for line in output.splitlines()
                if "," in line
                for proc, memory in [line.split(",", 1)]
                if int(proc) == pid
            ]
            return sum(readings) if readings and None not in readings else None
        except ValueError:
            return None

    def sample(self):
        now = time.time()
        gpu = self._gpu()
        pid = self.process_pid()
        vram = self._process_vram(pid)
        if gpu and not self.first_gpu:
            self.first_gpu = gpu
        row = {key: gpu.get(key) for key in self.coverage}
        row.update(ts=now, process_pid=pid, process_vram_mb=vram)
        self.count += 1
        for key in self.coverage:
            value = row[key]
            if value is None:
                continue
            self.coverage[key] += 1
            if isinstance(value, (int, float)):
                stats = self.stats.setdefault(key, {"min": value, "max": value, "sum": 0})
                stats["min"] = min(stats["min"], value)
                stats["max"] = max(stats["max"], value)
                stats["sum"] += value
        return row

    def _run(self):
        try:
            with (self.directory / "telemetry.jsonl").open("w") as stream:
                while not self.stop_signal.is_set():
                    started = time.monotonic()
                    stream.write(json.dumps(self.sample(), ensure_ascii=False) + "\n")
                    stream.flush()
                    self.stop_signal.wait(max(0, 2 - (time.monotonic() - started)))
        except OSError as error:
            self.error = str(error)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_signal.set()
        if self.thread:
            self.thread.join()

    def summary(self):
        return {
            key: {"min": stats["min"], "max": stats["max"], "mean": stats["sum"] / self.coverage[key]}
            if (stats := self.stats.get(key))
            else None
            for key in self.coverage
            if key != "name"
        }


def _markers(path):
    markers = {"comfy_full_load": False, "comfy_partial_load": False}
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if re.search(r"loaded completely; .* MB loaded", line):
                    markers["comfy_full_load"] = True
                if re.search(r"loaded partially; .* MB loaded", line):
                    markers["comfy_partial_load"] = True
    except FileNotFoundError:
        pass
    return markers


def write_perf(directory, job, status, finished, sampler=None, gpu_hint=None):
    directory = Path(directory)
    if not (directory / "telemetry.jsonl").exists():
        (directory / "telemetry.jsonl").touch()
    observed = job.get("_perf", {})
    timing = observed.get("phases", {})
    started = job.get("started")
    phases = {
        name: (max(0, timing[name].get("end", finished) - timing[name]["start"]) if name in timing else None)
        for name in PHASES
    }
    phases["total"] = max(0, finished - started) if started else None
    phases["queue"] = max(0, (started or finished) - job["created"])
    params = job.get("params", {})
    hardware = {"gpu_name": gpu_hint, "vram_total_mb": None}
    sampler = sampler or Sampler(directory, lambda: None)
    if sampler.first_gpu:
        hardware.update(
            gpu_name=sampler.first_gpu.get("name") or gpu_hint,
            vram_total_mb=sampler.first_gpu.get("mem_total_mb"),
        )
    events = observed.get("observations", {})
    workload = job.get("workload_key")
    if workload is None:
        workload = {
            "model_id": job["model_id"],
            "task": job["task"],
            "workflow": params.get("workflow"),
            "runtime_profile": observed.get("profile"),
        }
    perf = {
        "job_id": job["id"],
        "status": status,
        "hardware": hardware,
        "workload_key": workload,
        "phases_s": phases,
        "nodes_s": events.get("nodes_s"),
        "samplers": events.get("samplers"),
        "ws_connected": events.get("ws_connected", False),
        "gpu_stats": sampler.summary(),
        "sample_count": sampler.count,
        "field_coverage": sampler.coverage,
        "cold_start": observed.get("cold_start"),
        "markers": {
            **_markers(directory / "log.txt"),
            "execution_cached": events.get("execution_cached"),
            "telemetry_error": sampler.error,
        },
    }
    _util.atomic_write_text(
        directory / "perf.json", json.dumps(perf, ensure_ascii=False, allow_nan=False) + "\n",
        temp=directory / ".perf.json.tmp",
    )
