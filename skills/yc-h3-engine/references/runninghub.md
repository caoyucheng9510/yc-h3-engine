# RunningHub 通道参考（`--provider runninghub`）

## 1. 必备配置（`.env`）

- `RUNNINGHUB_API_KEY`：RunningHub 开放平台 API Key。
- `RUNNINGHUB_WORKFLOW_ID`：用户自己在网页端打开 [YZ金鱼原帖](https://www.runninghub.cn/post/2109907990638850050) 并**成功运行一次（Plus 48GB）**后获取的工作流 ID。
  - 若文戏与武打使用不同副本，可分别配置 `RUNNINGHUB_WORKFLOW_ID_HYBRID` 与 `RUNNINGHUB_WORKFLOW_ID_COMBAT`。
- 可选：`RUNNINGHUB_CNY_PER_POINT`（单积分人民币折算价，仅影响新预检与新入账流水）、`YC_H3_RUNNINGHUB_MAX_CONCURRENCY`（本地并发槽位）。

## 2. 规格与槽位约束

- **工作流**：`jinyu-hybrid`（默认，文戏）与 `jinyu-combat`（武打）。
- **任务**：`r2v`（默认，至少 1 项参考素材，单图也走 `r2v`）或 `t2v`（零参考素材）；传入 `--task i2v` 或 `--first-frame` 会被预检阻断。
- **素材上限**：**6 图 + 3 音频 + 1 视频**。
  - 独立音频数 + 启用伴音的参考视频数 `<= 3`，参考音频与参考视频累计时长各自 `<= 15.0s`。
  - 参考视频提交时由本地 FFmpeg 自动规范化（24fps CFR、32 对齐缩放、`17k+5` 帧窗口、48kHz 双声道 AAC 或静音），规范化后须 `<= 30 MB`。
  - 需要丢弃参考视频伴音时传 `--mute-reference-video`。
- **画幅与时长**：
  - 默认 `--aspect 16:9`（`1664×928`），支持 `9:16`（`928×1664`）、`1:1`（`1248×1248`），或同时传 `--width` 与 `--height`（32 倍数、最长边 `<= 1664`、总像素 `<= 1,572,864`，与 `--aspect` 互斥）。
  - `--seconds` 支持 `0.2–15.0`（小数或整数，默认 `5`），输出帧数按 24fps 向上对齐 `17k + 5` 网格。
  - `--steps` 默认 `12`（范围 `7–100`）；`--seed` 默认 `-1`（随机）；支持 `--no-music` 与 `--save-last-frame`；`--out` 须以 `.mp4` 结尾；**不支持 `--megapixels`、`--upscale-2x`、`--trim-to-requested-seconds`**。
- **本地实跑自校准预估**：
  - 成片交付成功后，CLI 会尽力将脱敏运行观测写入 `<data_dir>/perf/observations.jsonl`（旧 `r2v` 断点缺失是否含参考视频标记时不入库）。
  - 后续 `preflight` 优先按本地同工作流（`workflow`）、同机型（`instance_type`）、同是否含参考视频（`has_ref_video`）的近期样本拟合；同机型无匹配时可回退至同工作流、同参考视频类别的近期样本。仅当历史能拟合出运行耗时（`estimated_run_seconds`）或参考积分（`estimated_reference_points`）时才采用 `estimate_source: "local_history"`（附 `estimate_samples`、`estimated_wall_seconds` 及配置 `RUNNINGHUB_CNY_PER_POINT` 后的 `estimated_cny`）。
  - 无可用历史样本（含仅有墙钟样本）时，符合默认 `16:9`（`1664×928`）/ `12` 步 / Plus 48GB / 无参考视频则沿用单样本初始基线（`estimate_source: "initial_baseline"`，`estimate_samples: 0`，`estimated_wall_seconds: null`），否则返回 `estimate_source: "uncalibrated"`。
  - 包含参考视频或非默认画幅在积累本地样本后也能给出估算；所有预估仅供参考，最终结算以平台实扣为准（优先读取 `chargedPoint`，亦兼容 `consumePoint` / `consumePoints`）。

## 3. 常用命令

```bash
# 检查配置与实时余额
yc-h3 doctor --provider runninghub --remote --json
yc-h3 ledger balance sync --provider runninghub --json

# 预检与提交（注意：--tag 等全部参数在两步中必须完全一致）
yc-h3 preflight --provider runninghub --workflow jinyu-hybrid \
  --image ./p1.png --video ./ref.mp4 --mute-reference-video \
  --seconds 6 --aspect 16:9 --no-music --save-last-frame \
  --out ./out/s01.mp4 --tag ep01 --prompt-file ./s01.txt --json

yc-h3 gen --provider runninghub --workflow jinyu-hybrid \
  --image ./p1.png --video ./ref.mp4 --mute-reference-video \
  --seconds 6 --aspect 16:9 --no-music --save-last-frame \
  --out ./out/s01.mp4 --tag ep01 --prompt-file ./s01.txt \
  --approve-digest "<preflight 返回的 approve_digest>" --json

# 断线恢复（ambiguous 核实远端任务后可传 --remote-task-id 认领）
yc-h3 jobs list --provider runninghub --json
yc-h3 jobs resume <task_key> --remote-task-id <REMOTE_TASK_ID> --json
yc-h3 jobs fetch <task_key> --out ./out/s01_alt.mp4 --json
yc-h3 jobs cancel <task_key> --json
```
