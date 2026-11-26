# Colab 通道参考（`--provider colab-comfy`）

## 1. 平台支持与固有边界

- **支持平台**：**macOS / Linux / Windows WSL2**。在原生 Windows（PowerShell / cmd）下调用 `daemon`、`gpu --backend colab` 或 `--provider colab-comfy` 会被主动拦截（退出码 `3`），请改用 WSL2 或原生 Windows 通道（`runninghub` / `chenyu` / `chenyu-comfy`）。
- **前置条件**：已安装 `google-colab-cli` 并完成 Google OAuth 登录（`colab` 与 `ssh` 可用），账号具备可用 Colab CU 额度。
- **隔离命名空间**：本地守护进程默认监听 `127.0.0.1:7798`（loopback-only），Colab 会话前缀强制 `yh3-`，远端工作目录固定 `/content/yc_h3_engine`。
- **断电/强杀固有边界**：Google Colab CLI 无独立于本机的服务端硬截止 API。本地守护进程存活期间会执行空闲自动关机与硬时限回收，但若本机关机或强杀 WSL2，兜底只能依赖 Google Colab 服务端自身空闲回收。收工务必运行 `yc-h3 gpu stop --backend colab` 确认释放。

## 2. 守护进程与开机流程

金鱼工作流（`jinyu-hybrid` / `jinyu-combat`）依赖 `ComfyUI-KJNodes` 与 `comfyui-SelfLift` 插件，冷启时需在 `gpu plan` 与 `gpu up` 中成对传入 `--setup` 和 `--setup-params`。`gpu plan` / `gpu up` 会自动补 `yh3-` 前缀，而 `gpu reconcile` / `gpu stop` 显式传 `--session` 时严格要求带 `yh3-` 前缀（或省略以操作当前会话），建议全程统一写成 `yh3-` 前缀名。注意：Colab `gpu up --setup` 仅同步等待到远端服务就绪并**提交** `setup` 作业即返回（不轮询等插件装完）；若随即跑 `preflight` 报 `runtime_or_plugins_not_ready`（退出码 `3`），说明远端仍在安装 ComfyUI/插件，稍候重试 `preflight` 即可，勿重复执行 `--setup`：

```bash
# 1) 启动本地守护进程（默认绑定当前解析的 data_dir 与配置身份，同端口异配会被阻断）
yc-h3 daemon start --json
yc-h3 daemon status --json

# 2) 只读规划选卡（--gpu AUTO 默认按 G4 → H100 → A100 依次回退，A100 自动附带 --high-mem）
yc-h3 gpu plan --backend colab --session yh3-h3 --gpu AUTO --setup \
  --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' \
  --max-runtime-minutes 360 --idle-minutes 120 --json

# 3) 取得用户确认后开机（从 gpu plan 返回的 digest 字段取值；同步等到远端服务就绪并提交 setup 作业后返回）
yc-h3 gpu up --backend colab --session yh3-h3 --gpu AUTO --setup \
  --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' \
  --max-runtime-minutes 360 --idle-minutes 120 \
  --approve-digest "<gpu plan 返回的 digest>" --json
```

## 3. 预检、生成与释放

- **槽位与参数边界**：
  - 槽位：9 图 + 3 音频 + 3 视频（合计 `<= 12`；参考视频累计 `<= 15s` 且单段 `>= 5` 帧 @ 24fps；有伴音参考视频同时占用音频槽与总量槽，并与独立音频共用累计 `<= 15s` 上限；支持 `--first-frame` 走 `i2v`、`--mute-reference-video`、`--trim-to-requested-seconds`）。
  - 参数边界：默认 `--aspect 16:9`（`1664×928`），支持 `9:16`（`928×1664`）/`1:1`（`1248×1248`）或 32 倍数 `--width`/`--height`（与 `--aspect` 互斥；高显存最长边 `<= 1664`、总像素 `<= 1,572,864`）、`--seconds 0.2–15.0`、`--seed`；`--out` 须以 `.mp4` 结尾；`--steps` 固定为 `12`（传入其他步数会被拒绝）；**不支持 `--save-last-frame`、`--megapixels`、`--upscale-2x`**。
  - **显存档位上限（Tier 3）**：若手动指定 `<36 GiB` 显存卡（如 `L4` 24GB），硬上限为最长边 `<= 1024`、总像素 `<= 557,056`（如 `1024×544` 或竖屏 `544×1024`）、最长 `8s`；省略尺寸走默认 `16:9`（`1664×928`）会触发 `vram_insufficient` 阻断，须显式传合法 `--width`/`--height` 与 `<= 8s` 时长。
- **审批要求**：每次 `gen` 须先 `preflight` 获取实时 `approve_digest`（涵盖 `pass` 与 `warn`），且两步参数（含 `--tag`）须完全一致。
- **本地实跑自校准预估**：单镜 `preflight` 优先读取本地单作业 `perf.json` 归档，按 `workload_key` 精确匹配计算中位数（`estimate_source: "local_history_exact"`，暖跑优先、冷跑可用）；无有效精确耗时时，再回退读取 `<data_dir>/perf/observations.jsonl`（由成片交付后尽力写入；指定历史远端作业补领不会新写观测）按同卡型、同工作流、同参考视频类别做暖跑归一化估算（`estimate_source: "local_history_normalized"`）；均无可用样本时为 `estimate_source: "uncalibrated"`。单镜预估 CU（`estimated_quantity`）按估算耗时与当前会话实时 `billing_rate_per_hour`（`CU/hr`）折算，可选 `COLAB_CNY_PER_CU` 仅用于折算 `estimated_cny`（同时返回 `estimate_seconds`、`estimated_wall_seconds`、`estimate_samples`）。
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
