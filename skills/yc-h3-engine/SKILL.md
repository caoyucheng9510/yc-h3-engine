---
name: yc-h3-engine
description: MiniMax-H3 多通道生视频执行入口（RunningHub / 晨羽按次 / 晨羽独占 GPU / Colab）；负责初始化配置、环境体检、通道与工作流选型、只读预检与费用审批门禁、提交生成、断点恢复与费用台账。
compatibility: 跨平台 CLI（Python >= 3.12、FFmpeg/FFprobe）；RunningHub、晨羽按次与晨羽独占原生支持 Windows / macOS / Linux；Colab 通道支持 macOS / Linux / Windows WSL2。
---

# `yc-h3-engine`：MiniMax-H3 视频生成执行入口

本 Skill 只负责**生视频执行、算力管理与费用台账**，不替代用户现有项目中的编剧、分镜、生图或剪辑流程。

## 1. CLI 调用方式

- 优先调用全局安装命令：`yc-h3`（别名 `yc-h3-engine`）。
- 若用户未全局安装、仅克隆了仓库，则使用 `uv run --project <yc-h3-engine仓库路径> yc-h3`。
- 通用全局参数放在子命令前：`yc-h3 [--env-file <path>] [--data-dir <path>] <subcommand> ...`。

## 2. 首次初始化与环境体检

当用户说“初始化生视频工具”“配置 Key”“检查环境”时：

1. **严禁通过命令行参数传递明文 API Key**，严禁打印、回显或提交 `.env` 文件。
2. 引导用户自己运行 `yc-h3 init`（交互式输入写入 `~/.yc-h3-engine/.env`，加 `--local` 写当前项目 `./.env`），或在确认安全的前提下通过标准输入管道 `yc-h3 init --from-stdin-json --update` 增量写入；
3. 运行只读体检确认环境与余额：
   ```bash
   yc-h3 doctor --json
   yc-h3 doctor --remote --json
   ```
4. 若缺少 `ffmpeg`/`ffprobe`，直接给出对应系统安装命令（macOS `brew install ffmpeg`，Windows `winget install Gyan.FFmpeg`）；若 RunningHub 缺少 `RUNNINGHUB_WORKFLOW_ID`，提示用户打开原帖复制并网页运行一次后填入 ID（见 [references/runninghub.md](references/runninghub.md)）。

## 3. 通道与工作流选型

1.0 对外四个通道统一只用两个逻辑工作流名：**`jinyu-hybrid`**（默认，文戏/对白/情绪/常规参考）与 **`jinyu-combat`**（武打/高强度动作）。

| 场景 | 推荐通道 (`--provider`) | 素材槽位上限 | 关键约束与说明 |
| :--- | :--- | :--- | :--- |
| 零运维日常主力 / 需参考视频 | `runninghub` | 6 图 + 3 音 + 1 视频 | 48GB Plus，需 `.env` 配 `RUNNINGHUB_API_KEY` + `RUNNINGHUB_WORKFLOW_ID` |
| 不赶时间的低成本零星补镜 | `chenyu` | 9 图 + 3 音（**无视频**） | 5090 按次，需双钱包有余额，实测内容审查排队约 20–30+ 分钟 |
| 持续批跑 / 参考视频（全平台含 Win） | `chenyu-comfy` | 9 图 + 3 音 + 3 视频 | 晨羽独占 GPU，纯 HTTPS 无需 SSH，与 `chenyu` 共用 Key 与钱包 |
| 已有 Colab CU 额度大批量跑片 | `colab-comfy` | 9 图 + 3 音 + 3 视频 | 仅支持 macOS / Linux / Windows WSL2，原生 Windows 自动拦截退出码 `3` |

- 省略 `--provider` 时，CLI 会按可用性与**本次请求能力**自动选路（例如传了 `--video`、小数 `--seconds`、`--seed`、`--steps`、`--trim-to-requested-seconds` 时自动跳过 `chenyu` 按次 API）。

## 4. 费用门禁、断点恢复与退出码（铁律）

1. **先预检，再确认，后提交**：
   - 所有付费生成（`runninghub` / `chenyu` / `colab-comfy` / `chenyu-comfy`）与受管 GPU 开机/改时限/存镜像，必须先执行只读 `preflight` / `gpu plan`；
   - 向用户清楚汇报选中通道、工作流、素材顺序、规格帧数、估算消耗与当前余额；
   - **必须取得用户当轮明确批准**后，再将同一组参数附加实时 `--approve-digest <digest>` 执行 `gen` / `gpu up` / `gpu save-image`；
   - 严禁篡改审批后素材、严禁静默绕过预检或私自切换付费通道。
2. **断点恢复与单次 `--force-new`**：
   - 建单断线或超时后，先执行 `yc-h3 jobs list --json` 查看状态，按 `yc-h3 jobs resume <task_key> --json` 或 `yc-h3 jobs fetch <task_key> --json` 恢复领取（可加 `--out <new_path>` 换落盘路径、加 `--remote-job-id <id>` 补领历史作业）；
   - 只有在确认要重做时才使用 `--force-new` 重新预检审批；同一次 `--force-new` 审批只可消费一次，防止断线重跑重复扣费。
3. **统一退出码**：
   - `0`：成功 / 预检 `pass`
   - `2`：需人工审批 / 预检 `warn`（须带实时 `--approve-digest`）
   - `3`：预检 `block` / 会话或断点冲突 / `ambiguous` / 原生 Windows 调用 Colab 拦截
   - `1`：运行期异常或参数错误

## 5. 最短命令模板与 H3 提示词避坑

```bash
# 1) 只读预检（提示词推荐写入 UTF-8 文本文件用 --prompt-file 传入）
yc-h3 preflight --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./clips/s01.mp4 --prompt-file ./s01.txt --json

# 2) 用户确认报价后，原样参数追加 --approve-digest 提交
yc-h3 gen --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./clips/s01.mp4 --tag my-video \
  --prompt-file ./s01.txt --approve-digest "<DIGEST>" --json
```

**MiniMax-H3 提示词基本规范（送审前自查）**：
- **段式结构**：有参考素材默认 `--task r2v`（遵循 `ref2va` 英文六段式）；零参考素材用 `--task t2v`（遵循 `t2va` 英文三段式）；单图参考在 RunningHub / 晨羽按次请用 `--task r2v`。
- **引用编号对齐**：重复传入的 `--image` 按顺序对应 `<Picture 1>`, `<Picture 2>`...；`--video` 对应 `<Video 1>`；当同时传入有伴音参考视频与独立音频时，视频伴音排在 `<Audio 1>`，独立音频顺延为 `<Audio 2>`（静音参考视频请加 `--mute-reference-video`）。
- **无负向词与去配乐**：用可见/可听的正向描述，避免写“不要/禁止/no xxx”；需要纯干声无 BGM 时加 `--no-music`（且提示词中不要再写非 `N/A` 的 `non_diegetic_music`）。

## 6. 按需查阅通道细则

- RunningHub 槽位、工作流 ID 与参考视频规范化：[references/runninghub.md](references/runninghub.md)
- 晨羽按次双钱包、审查等待与独占 GPU / 存镜像：[references/chenyu.md](references/chenyu.md)
- Colab 守护进程、选卡开机、停机与 WSL2 边界：[references/colab.md](references/colab.md)
