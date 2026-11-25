# 晨羽通道参考（`--provider chenyu` 与 `--provider chenyu-comfy`）

晨羽按次工作流 API（`chenyu`）与晨羽独占 GPU（`chenyu-comfy`）共用同一个 `CHENYU_API_KEY` 与同账号 CNY 双钱包（算力卡 + 现金），全平台原生支持 Windows / macOS / Linux。

## 1. 晨羽按次工作流 API（`--provider chenyu`）

- **适用**：不赶时间的低成本零星补镜（5s 实测约 ¥0.29–¥0.34）。
- **双钱包要求**：算力卡支付 5090 算力费（`0.12 CNY/运行分钟`），现金余额支付创作者费（`0.03 CNY/次`），两者均须有足够余额。
- **审查排队预期**：实测平台内容审查等待约 20–30+ 分钟（总墙钟 37–44 分钟），等待期不扣算力费，但平台无远端取消接口（`jobs cancel` 不可用）。
- **规格与约束**：
  - 工作流：`jinyu-hybrid`（默认）与 `jinyu-combat`（内置公开工作流 `wf_f8b8f8ee12000cebc6bf`，无需复制）。
  - 任务：`r2v`（默认，至少 1 图或 1 音）或 `t2v`；**不支持 `--video`、不支持小数 `--seconds`、不支持 `--seed` / `--steps` / `--width` / `--height` / `--task i2v`**。
  - 槽位：**9 图 + 3 音频**（单文件 `<= 100 MB`，音频累计 `<= 15s`）。
  - 参数：`--seconds 1..15`（整数）、`--aspect`（支持 `16:9`/`9:16`/`1:1`/`4:3`/`3:4`/`3:2`/`2:3`/`21:9`）、`--megapixels 0.2..2.0`（默认 `1.5`）、`--upscale-2x`、`--no-music`、`--save-last-frame`。

```bash
yc-h3 ledger balance sync --provider chenyu --json

yc-h3 preflight --provider chenyu --workflow jinyu-hybrid \
  --image ./char.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --megapixels 1.5 --no-music \
  --out ./out/s01.mp4 --prompt-file ./s01.txt --json

yc-h3 gen --provider chenyu --workflow jinyu-hybrid \
  --image ./char.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --megapixels 1.5 --no-music \
  --out ./out/s01.mp4 --prompt-file ./s01.txt \
  --approve-digest "<DIGEST>" --json
```

## 2. 晨羽独占 GPU（`--provider chenyu-comfy`）

- **适用**：持续批跑、需要参考视频 / 小数秒 / 自定义 seed / 私有镜像复用权重，且无需本地守护进程与 SSH（Windows 原生可用）。
- **槽位**：9 图 + 3 音频 + 3 视频（合计 `<= 12`，可选 `--first-frame` 走 `i2v`）；支持 `--trim-to-requested-seconds`。
- **生命周期与计费护栏**：
  - 建机前冻结绝对截止时间，建机回执丢失时在当前命令内自动认领并挂平台定时停机与空闲销毁护栏；若进程在挂护栏前被强杀/断电，务必运行 `yc-h3 gpu reconcile --backend chenyu` 或登录晨羽网页端核查。
  - **存镜像防丢片**：`gpu save-image` 会在锁内核对远端作业列表与本地多版本交付证明；若存在未取回成片会直接阻断并给出 `yc-h3 jobs fetch <task_key> --remote-job-id <id>` 领取命令。

```bash
# 1) 规划并经批准开机（默认 --gpu AUTO 按 6000D-84G → nGPU-48GB → 5090 回退；--setup 默认装齐金鱼插件）
yc-h3 gpu plan --backend chenyu --session h3 --gpu AUTO --setup \
  --max-runtime-minutes 120 --idle-minutes 30 --json

yc-h3 gpu up --backend chenyu --session h3 --gpu AUTO --setup \
  --max-runtime-minutes 120 --idle-minutes 30 \
  --approve-digest "<GPU_PLAN_DIGEST>" --json

# 2) 生成（每次付费提交均需先 preflight 取实时 approve_digest）
yc-h3 preflight --provider chenyu-comfy --workflow jinyu-hybrid \
  --image ./char.png --video ./ref.mp4 --seconds 5.5 --seed 42 \
  --out ./out/s02.mp4 --prompt-file ./s02.txt --json

yc-h3 gen --provider chenyu-comfy --workflow jinyu-hybrid \
  --image ./char.png --video ./ref.mp4 --seconds 5.5 --seed 42 \
  --out ./out/s02.mp4 --prompt-file ./s02.txt \
  --approve-digest "<GEN_DIGEST>" --json

# 3) 状态、对账、保存私有镜像与销毁释放
yc-h3 gpu status --backend chenyu --json
yc-h3 gpu reconcile --backend chenyu --session h3 --json
yc-h3 gpu save-image --backend chenyu --session h3 --json
yc-h3 gpu stop --backend chenyu --session h3 --json
```
