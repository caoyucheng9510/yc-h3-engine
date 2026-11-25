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
2. 引导用户自己运行 `yc-h3 init`（首次生成 `~/.yc-h3-engine/.env`，加 `--local` 写当前项目 `./.env`，更新已有配置加 `--update`），或在确认安全的前提下通过标准输入管道 `yc-h3 init --from-stdin-json --update` 增量写入；
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
| 零运维日常主力 / 需参考视频 | `runninghub` | 6 图 + 3 音 + 1 视频（视频伴音占音频槽，音/视频各限累计 15s） | 48GB Plus，需 `.env` 配 `RUNNINGHUB_API_KEY` + `RUNNINGHUB_WORKFLOW_ID` |
| 不赶时间的按次补镜 | `chenyu` | 9 图 + 3 音（**无视频**，音频累计 ≤ 15s） | 5090 按次，需双钱包有余额，实测内容审查排队约 20–30+ 分钟 |
| 持续批跑 / 参考视频（全平台含 Win） | `chenyu-comfy` | 9 图 + 3 音 + 3 视频（合计 ≤ 12，视频伴音占音频与总量槽且共用 15s 音频上限） | 晨羽独占 GPU，纯 HTTPS 无需 SSH，与 `chenyu` 共用 Key 与钱包 |
| 已有 Colab CU 额度大批量跑片 | `colab-comfy` | 9 图 + 3 音 + 3 视频（合计 ≤ 12，视频伴音占音频与总量槽且共用 15s 音频上限） | 仅支持 macOS / Linux / Windows WSL2，原生 Windows 自动拦截退出码 `3` |

- 省略 `--provider` 时，CLI 会按可用性与**本次请求能力**自动选路（例如传了 `--video`、小数 `--seconds`、`--seed`、`--steps`、`--trim-to-requested-seconds` 时自动跳过 `chenyu` 按次 API）。

## 4. 费用门禁、摘要来源与退出码（铁律）

1. **先预检，再确认，后提交（三类摘要字段来源须准确区分）**：
   - **视频生成**：先执行 `yc-h3 preflight ... --json`，读取返回顶层的 **`approve_digest`**；用户批准后，**原样保持全部参数（含 `--tag`）不变**，追加 `--approve-digest <approve_digest>` 执行 `yc-h3 gen`。
   - **受管 GPU 开机 / 改时限**：先执行 `yc-h3 gpu plan --backend colab|chenyu ... --json`，读取返回顶层的 **`digest`**；用户批准后，原样参数追加 `--approve-digest <digest>` 执行 `yc-h3 gpu up`。
   - **晨羽保存私有镜像**：在存在可保存的受管运行实例时，先不带摘要执行 `yc-h3 gpu save-image --backend chenyu --json`（计划生成成功后返回退出码 `2` 并向 stderr 输出错误 JSON；若实例已停或有待核对状态会先以退出码 `3` 退出），读取错误 JSON 中的 **`preflight.approve_digest`**；用户批准后，原样追加 `--approve-digest <approve_digest>` 执行（此时在生命周期锁内核验远端与本地交付证明，若存在未交付作业将以退出码 `3` 阻断并给出定向 `jobs fetch` 命令）。
   - 严禁篡改审批后素材、严禁静默绕过预检或私自切换付费通道。
2. **断点恢复与单次 `--force-new`**：
   - 建单断线或超时后，先执行 `yc-h3 jobs list --json` 查看状态，按 `yc-h3 jobs resume <task_key> --json` 或 `yc-h3 jobs fetch <task_key> --json` 恢复领取（可加 `--out <new_path>` 换落盘路径、加 `--remote-job-id <id>` 补领历史作业）；
   - 只有在确认要重做时才使用 `--force-new` 重新预检审批；同一次 `--force-new` 审批只可消费一次，防止断线重跑重复扣费。
3. **统一退出码**：
   - `0`：成功 / 预检 `pass`
   - `2`：需人工审批 / 预检 `warn`（须带实时 `--approve-digest`）
   - `3`：预检 `block` / 会话或断点冲突 / `ambiguous` / 原生 Windows 调用 Colab 拦截
   - `1`：运行期异常或参数错误

## 5. 最短命令模板与 H3 提示词规范

```bash
# 1) 只读预检（提示词推荐写入 UTF-8 文本文件用 --prompt-file 传入；--tag 须与 gen 保持一致）
yc-h3 preflight --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./clips/s01.mp4 --tag my-video --prompt-file ./s01.txt --json

# 2) 用户确认报价后，原样参数追加 --approve-digest 提交
yc-h3 gen --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./clips/s01.mp4 --tag my-video \
  --prompt-file ./s01.txt --approve-digest "<preflight 返回的 approve_digest>" --json
```

**MiniMax-H3 提示词结构规范（CLI 代码按以下确定性规则校验，不合规会被预检阻断）**：
- **零参考素材（`--task t2v` → `t2va` 英文三段式）**：直接以 `integrated_multimodal_description:`（内含 `[Shot 1]`）→ `overall_soundscape:` → `non_diegetic_music:` 顺序编写，首行不要加多余前置文本。
- **常规参考素材（传 `--image` / `--audio` / `--video` → `--task r2v` → `ref2va` 英文六段式）**：首行必须直接以 `subject_definitions:` 开头，依次写齐 `subject_definitions:` → `summary:` → `retention_analysis:` → `detailed_description:`（内含 `[Shot 1]`）→ `overall_soundscape:` → `non_diegetic_music:`；单图参考在 RunningHub / 晨羽按次请用 `--task r2v`（不要传 `--first-frame`）。
- **受管 ComfyUI 单独首帧（传 `--first-frame` → 自动推断 `--task i2v` → `i2va` 英文三段式）**：仅 `colab-comfy` / `chenyu-comfy` 支持 `--first-frame`（不要同时传 `--task r2v`）；首行必须包含官方首帧对齐指令（含 `0.00 seconds` 与 `<Picture 1>`，如 `The video starts at 0.00 seconds with <Picture 1>.`），随后依次写齐 `integrated_multimodal_description:`（含 `[Shot 1]`）→ `overall_soundscape:` → `non_diegetic_music:`。
- **引用编号对齐**：重复传入的 `--image` 按顺序对应 `<Picture 1>`, `<Picture 2>`...；重复传入的 `--video` 按顺序对应 `<Video 1>`, `<Video 2>`...；当同时传入有伴音参考视频与独立音频时，视频伴音排在 `<Audio 1>`，独立音频依次顺延（静音参考视频请加 `--mute-reference-video`）。
- **无负向词与去配乐**：用可见/可听的正向描述，避免在正文写 `no music` / `without text` 等负面措辞；需要纯干声无 BGM 时加 `--no-music`（且 `non_diegetic_music:` 写 `N/A`）。

## 6. 按需查阅通道细则

- RunningHub 槽位、工作流 ID 与参考视频规范化：[references/runninghub.md](references/runninghub.md)
- 晨羽按次双钱包、审查等待与独占 GPU / 存镜像：[references/chenyu.md](references/chenyu.md)
- Colab 守护进程、选卡开机、停机与 WSL2 边界：[references/colab.md](references/colab.md)
