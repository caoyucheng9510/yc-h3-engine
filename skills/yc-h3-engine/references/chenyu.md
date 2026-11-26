# 晨羽通道参考（`--provider chenyu` 与 `--provider chenyu-comfy`）

晨羽按次工作流 API（`chenyu`）与晨羽独占 GPU（`chenyu-comfy`）共用同一个 `CHENYU_API_KEY` 与同账号 CNY 双钱包（算力卡 + 现金），全平台原生支持 Windows / macOS / Linux。

## 1. 晨羽按次工作流 API（`--provider chenyu`）

- **适用**：不赶时间的低成本零星补镜（按公开双钱包费率折算，5s 推理样本约 ¥0.29–¥0.34）。
- **双钱包要求**：算力卡支付 5090 算力费（`0.12 CNY/运行分钟`），现金余额支付创作者费（`0.03 CNY/次`），两者均须有足够余额。
- **审查排队预期与超时续拉**：平台内容审查排队等待属于平台端波动，等待期不扣算力费，但平台无远端取消接口（`jobs cancel` 不可用）。CLI 默认轮询超时上限已放宽至 **60 分钟（`3600s`）**；若超时返回 `task_not_completed`，说明原订单已受理且远端尚未完成，CLI 已停止本次等待并保留本地断点，后续使用 `yc-h3 jobs resume <task_key>` 继续轮询或 `yc-h3 jobs fetch <task_key>` 单次查取可沿用同一订单拉取（不重复下单，禁止擅自 `--force-new`，最终状态仍以远端返回为准）。若提交断线落入 `ambiguous` 状态，直接跑 `yc-h3 jobs resume <task_key>` 即可按本地幂等键安全核对/重放（或在确认远端订单后用 `--remote-job-id <wfrun_...>` 认领）。
- **本地实跑自校准预估**：
  - 无本地样本时，预检仅展示公开费率（`estimate_source: "uncalibrated"`，`estimate_samples: 0`，`estimated_run_seconds` / `estimated_wall_seconds` / `estimated_cny` 均为 `null`）。
  - 成片交付后，CLI 会尽力将脱敏观测写入 `<data_dir>/perf/observations.jsonl`；后续 `preflight` 优先按同工作流、同卡型（`gpu_model`）的近期本地样本拆分估算（同卡型无匹配时可回退至同工作流、同无参考视频类别的近期样本）：
    - `estimated_run_seconds`：按成片秒数归一化估算纯推理耗时，用于按公开费率（算力卡 + 创作者费）估算 `estimated_cny`；若运行耗时样本缺失，亦可用带实扣记录的历史样本按成片秒数拟合 `estimated_cny`；
    - `estimated_wall_seconds`：取近期端到端墙钟耗时中位数（记至首次查得远端成功，不含本地下载与校验），随近期实测波动自动更新；
    - `facts` 中同时标明 `estimate_source: "local_history"` 与 `estimate_samples`（单项缺样本时对应字段为 `null`）。
- **规格与约束**：
  - 工作流：`jinyu-hybrid`（默认）与 `jinyu-combat`（内置公开工作流 `wf_f8b8f8ee12000cebc6bf`，无需复制）。
  - 任务：`r2v`（默认，至少 1 图或 1 音）或 `t2v`；**不支持 `--video`、`--first-frame`、小数 `--seconds`、`--seed`、`--steps`、`--width`、`--height`、`--task i2v`、`--trim-to-requested-seconds`**。
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
  --approve-digest "<preflight 返回的 approve_digest>" --json
```

## 2. 晨羽独占 GPU（`--provider chenyu-comfy`）

- **适用**：持续批跑、需要参考视频 / 小数秒 / 自定义 seed / 私有镜像复用环境 + 持久盘复用权重，且无需本地守护进程与 SSH（Windows 原生可用）。
- **槽位与参数边界**：
  - 槽位：9 图 + 3 音频 + 3 视频（合计 `<= 12`；参考视频累计 `<= 15s` 且单段 `>= 5` 帧 @ 24fps；有伴音参考视频同时占用音频槽与总量槽，并与独立音频共用累计 `<= 15s` 上限；支持 `--first-frame` 走 `i2v`、`--mute-reference-video`、`--trim-to-requested-seconds`）。
  - 参数边界：支持 `--aspect`（`16:9`/`9:16`/`1:1`）或 32 倍数 `--width`/`--height`（与 `--aspect` 互斥）、`--seconds 0.2–15.0`、`--seed`；`--out` 须以 `.mp4` 结尾；`--steps` 固定为 `12`（传入其他步数会被拒绝）；**不支持 `--save-last-frame`、`--megapixels`、`--upscale-2x`**。
  - **显存档位上限（Tier 3）**：`<36 GiB` 显存卡（含 `--gpu AUTO` 第三顺位 `5090` 32GB、`nGPU-32GB`、`4090D` 24GB）硬上限为 `1024×544`、最长 `8s`；省略尺寸走默认 `16:9`（`1664×928`）会触发 `vram_insufficient` 阻断，须显式传合法 `--width`/`--height`（如 `1024×544`）与 `<= 8s` 时长。
- **生命周期、查卡、私有镜像与计费护栏**：
  - **实时查卡、回退顺位与时限参数**：`gpu plan --backend chenyu --gpu AUTO --json` 严格只读，会实时拉取晨羽 `/gpu/list` 返回各候选卡的 `available` / `stock` 与小时价；`--gpu AUTO` 默认按 **`6000D-84G → nGPU-48GB → 5090`** 自动回退（亦支持单卡或逗号顺位，另预设 `nGPU-32GB`、`4090D`）。`--max-runtime-minutes` 默认 `120`（范围 `1–1440`）；**`--idle-minutes` 默认 `30`，仅允许离散值 `0 / 10 / 30 / 60 / 120`**（传其他整数会报参数错误）。
  - 建机前冻结绝对截止时间，建机回执丢失时在当前命令内自动认领并挂平台定时停机与空闲销毁护栏；若进程在挂护栏前被强杀/断电，务必运行 `yc-h3 gpu reconcile --backend chenyu` 或登录晨羽网页端核查。
  - **权重持久盘与 50 GB 免费额度**：权重默认缓存在持久盘 `/usrdata/yc_h3_engine/models`。平台「我的镜像」与 `/usrdata` 存储各有 **50 GB 免费额度**，超额按 `¥0.00036/GB/小时` 计费；环境升级时避免在旧私有镜像上反复叠层保存，建议从官方基础镜像重建并清理旧镜像。
  - **存私有镜像两步审批、防丢片与后续销毁**：存在可保存的受管运行实例且计划生成成功时，首次不带摘要执行 `gpu save-image` 返回退出码 `2` 并向 stderr 输出错误 JSON（摘要在 `preflight.approve_digest`；若实例已停或有待核对状态会先阻断退出）；执行时会在生命周期锁内核对远端作业列表与本地多版本交付证明，若存在未取回成片直接阻断并给出 `yc-h3 jobs fetch <task_key> --remote-job-id <id>` 领取命令。保存成功后返回新的 `image_uuid` 并自动**关机**（停止 GPU 计费，但本地与远端仍保留已关机实例记录）——**随后须补跑 `yc-h3 gpu stop --backend chenyu` 彻底销毁实例并核销账单**，否则下次 `gpu plan` / `gpu up` 会报 `stopped_instance_requires_cleanup`（退出码 `3`）。
  - **复用私有镜像或已有 Pod**：`gpu plan` / `gpu up` 不会自动改用上次保存的镜像，须显式传 `--image-uuid <image_uuid>` 复用；若使用晨羽网页端已建好的 Pod，可传 `--pod-uuid <pod_uuid>`（可选 `--pod-tag <tag>`，与 `--image-uuid` 互斥；CLI 不支持直接发布新 Pod）。
- **本地实跑自校准预估**：单镜 `preflight` 优先读取本地单作业 `perf.json` 归档，按 `workload_key` 精确匹配计算中位数（`estimate_source: "local_history_exact"`，暖跑优先、冷跑可用）；无有效精确耗时时，再回退读取 `<data_dir>/perf/observations.jsonl`（由成片交付后尽力写入；指定历史远端作业补领不会新写观测）按同卡型、同工作流、同参考视频类别做暖跑归一化估算（`estimate_source: "local_history_normalized"`，含 `estimate_samples`、`estimate_seconds`、`estimated_wall_seconds`、`estimated_quantity`、`estimated_cny`）；均无可用样本时返回 `estimate_source: "uncalibrated"`。

```bash
# 1) 规划并经批准开机（gpu plan 会实时查询库存并返回 digest；--setup 默认装齐金鱼插件）
# 下次复用已保存私有镜像时，在 plan 和 up 两步同时追加 --image-uuid "<image_uuid>"
# 从网页端已有 Pod 启动时，追加 --pod-uuid "<pod_uuid>" [--pod-tag "<tag>"]（与 --image-uuid 互斥）
yc-h3 gpu plan --backend chenyu --session h3 --gpu AUTO --setup \
  --max-runtime-minutes 120 --idle-minutes 30 --json

yc-h3 gpu up --backend chenyu --session h3 --gpu AUTO --setup \
  --max-runtime-minutes 120 --idle-minutes 30 \
  --approve-digest "<gpu plan 返回的 digest>" --json

# 2) 生成（每次付费提交均需先 preflight 取 approve_digest，且两步参数完全一致）
yc-h3 preflight --provider chenyu-comfy --workflow jinyu-hybrid \
  --image ./char.png --video ./ref.mp4 --seconds 5.5 --seed 42 \
  --out ./out/s02.mp4 --prompt-file ./s02.txt --json

yc-h3 gen --provider chenyu-comfy --workflow jinyu-hybrid \
  --image ./char.png --video ./ref.mp4 --seconds 5.5 --seed 42 \
  --out ./out/s02.mp4 --prompt-file ./s02.txt \
  --approve-digest "<preflight 返回的 approve_digest>" --json

# 3) 状态、对账、两步保存私有镜像与销毁释放
yc-h3 gpu status --backend chenyu --json
yc-h3 gpu reconcile --backend chenyu --session h3 --json
yc-h3 gpu save-image --backend chenyu --session h3 --json
yc-h3 gpu save-image --backend chenyu --session h3 \
  --approve-digest "<上一步错误 JSON 中的 preflight.approve_digest>" --json
yc-h3 gpu stop --backend chenyu --session h3 --json
```
