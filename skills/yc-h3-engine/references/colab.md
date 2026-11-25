# Colab 通道参考（`--provider colab-comfy`）

## 1. 平台支持与固有边界

- **支持平台**：**macOS / Linux / Windows WSL2**。在原生 Windows（PowerShell / cmd）下调用 `daemon`、`gpu --backend colab` 或 `--provider colab-comfy` 会被主动拦截（退出码 `3`），请改用 WSL2 或原生 Windows 通道（`runninghub` / `chenyu` / `chenyu-comfy`）。
- **前置条件**：已安装 `google-colab-cli` 并完成 Google OAuth 登录（`colab` 与 `ssh` 可用），账号具备可用 Colab CU 额度。
- **隔离命名空间**：本地守护进程默认监听 `127.0.0.1:7798`（loopback-only），Colab 会话前缀强制 `yh3-`，远端工作目录固定 `/content/yc_h3_engine`。
- **断电/强杀固有边界**：Google Colab CLI 无独立于本机的服务端硬截止 API。本地守护进程存活期间会执行空闲自动关机与硬时限回收，但若本机关机或强杀 WSL2，兜底只能依赖 Google Colab 服务端自身空闲回收。收工务必运行 `yc-h3 gpu stop --backend colab` 确认释放。

## 2. 守护进程与开机流程

金鱼工作流（`jinyu-hybrid` / `jinyu-combat`）依赖 `ComfyUI-KJNodes` 与 `comfyui-SelfLift` 插件，冷启时需在 `gpu plan` 与 `gpu up` 中成对传入 `--setup` 和 `--setup-params`：

```bash
# 1) 启动本地守护进程（默认绑定当前解析的 data_dir 与配置身份，同端口异配会被阻断）
yc-h3 daemon start --json
yc-h3 daemon status --json

# 2) 只读规划选卡（--gpu AUTO 默认按 G4 → H100 → A100 依次回退，A100 自动附带 --high-mem）
yc-h3 gpu plan --backend colab --session h3 --gpu AUTO --setup \
  --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' \
  --max-runtime-minutes 360 --idle-minutes 120 --json

# 3) 取得用户确认后开机（从 gpu plan 返回的 digest 字段取值；CLI 会同步轮询至开机与冷部署终态）
yc-h3 gpu up --backend colab --session h3 --gpu AUTO --setup \
  --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' \
  --max-runtime-minutes 360 --idle-minutes 120 \
  --approve-digest "<gpu plan 返回的 digest>" --json
```

## 3. 预检、生成与释放

- **槽位**：9 图 + 3 音频 + 3 视频（合计 `<= 12`；有伴音参考视频同时占用音频槽与总量槽，并与独立音频共用累计 `<= 15s` 上限；支持 `--first-frame` 走 `i2v`、`--mute-reference-video`、`--trim-to-requested-seconds`）。
- **审批要求**：每次 `gen` 须先 `preflight` 获取实时 `approve_digest`（涵盖 `pass` 与 `warn`），且两步参数（含 `--tag`）须完全一致。
- **本地实跑自校准预估**：成片交付后自动将脱敏观测记录到 `<data_dir>/perf/observations.jsonl`；单镜 `preflight` 优先使用精确 `workload_key` 样本（`estimate_source: "local_history_exact"`），跨时长等无精确样本时自动回退至同卡型、同工作流、同参考视频类别的暖跑归一化估算（`estimate_source: "local_history_normalized"`），并按当前会话实时 `CU/hr`（`billing_rate_per_hour`）及可选 `COLAB_CNY_PER_CU` 估算单镜耗时与费用（`estimate_seconds`、`estimated_wall_seconds`、`estimated_quantity`、`estimated_cny`、`estimate_samples`）；均无样本时为 `estimate_source: "uncalibrated"`。
- **释放保护**：存在活跃受管会话时，`yc-h3 daemon stop` 默认拒绝退出，须先 `yc-h3 gpu stop --backend colab`，或显式加 `--stop-colab` / `--detach-colab`。

```bash
yc-h3 preflight --provider colab-comfy --workflow jinyu-hybrid \
  --image ./p1.png --image ./p2.png --seconds 5 --aspect 16:9 --no-music \
  --out ./out/s01.mp4 --tag ep01 --prompt-file ./s01.txt --json

yc-h3 gen --provider colab-comfy --workflow jinyu-hybrid \
  --image ./p1.png --image ./p2.png --seconds 5 --aspect 16:9 --no-music \
  --out ./out/s01.mp4 --tag ep01 --prompt-file ./s01.txt \
  --approve-digest "<preflight 返回的 approve_digest>" --json

# 查看状态、对账与释放实例（自动记录会话 CU 实耗到台账）
yc-h3 gpu status --backend colab --json
yc-h3 gpu reconcile --backend colab --session yh3-h3 --json
yc-h3 gpu stop --backend colab --session yh3-h3 --json
yc-h3 daemon stop --json
```
