---
name: yc-h3-engine
description: 执行 MiniMax-H3（金鱼工作流 jinyu-hybrid / jinyu-combat）多通道视频生成与算力运维，支持 RunningHub、晨羽按次 API、晨羽独占 GPU 与 Google Colab。当用户要安装或配置 yc-h3、体检环境与余额、把规范 H3 提示词与参考图/音/视频生成视频、做预检报价与确认提交、开关晨羽/Colab 显卡或存私有镜像、续拉/补领/取消断线任务、或查看项目费用台账与钱包余额时使用。不用于写剧本、分镜、生图、剪辑或代写 H3 提示词（写词交由官方 h3-prompt-writing）。
compatibility: 跨平台 CLI（Python >= 3.12、FFmpeg/FFprobe）；RunningHub、晨羽按次与晨羽独占原生支持 Windows / macOS / Linux；Colab 通道支持 macOS / Linux / Windows WSL2。
---

# `yc-h3-engine`：MiniMax-H3 视频生成执行入口

## 职责边界与输入契约

- **专注执行，上下游解耦**：本 Skill 负责算力管理、只读预检与审批报价、提交生成、断点恢复与费用台账；**不负责**剧本、分镜、生图、提示词创作或后期剪辑。
- **必需输入**：
  - **规范化的 MiniMax-H3 提示词**（`--prompt` 或 `--prompt-file`，推荐写 UTF-8 文件或用 `--prompt-file -` 走标准输入，避免多行英文引号在不同 shell 下被转义）；
  - **输出路径与规格**（`--out` 须以 `.mp4` 结尾，按需指定 `--provider`、`--workflow`、`--seconds`、`--aspect` 等）。
- **输出路径（`--out`）约定**：
  - 用户指定了路径或项目已有目录约定（如 `assets/clips/`）时直接沿用；
  - 未指定且无项目约定时，**不要写入 `/tmp` 或工具仓库目录**，先询问保存位置；若用户交由 Agent 自定，则落到当前工作目录 `./outputs/<语义文件名>.mp4`（重名时 CLI 自动递增 `_v2`，不覆盖旧文件）。
- **条件输入（按 `--task` 与通道能力提供）**：
  - `t2v`（文生视频）：零参考素材（省略全部素材参数时自动推断）；
  - `r2v`（多模态参考）：至少 1 项本地参考图 / 音频 / 视频（`--image` / `--audio` / `--video`）；
  - `i2v`（仅受管 ComfyUI `colab-comfy` / `chenyu-comfy`）：单独首帧图（`--first-frame`）。
- **官方写词 Skill 协作**：本 Skill 不内置非官方写词模板。若用户仅提供口语想法、尚未生成规范 H3 提示词：
  - 优先交由已安装的官方 **`h3-prompt-writing`**（或用户指定的写词 Skill）写好规范提示词后，再调用 `yc-h3`；
  - 若未安装，引导先安装官方 `h3-prompt-writing`（`https://github.com/MiniMax-AI/MiniMax-H3/tree/main/.agents/skills/h3-prompt-writing`），生成提示词后再执行。

## 1. 任务路由与按需查阅

按当前用户意图定位步骤，并只在对应场景读取 `references/`，不必全量加载：

| 用户意图 | 执行入口 | 必读参考（按需） |
| :--- | :--- | :--- |
| 首次安装 / 配置 Key / 体检环境 | 第 2 节：`init`、`doctor` | 配 RunningHub 副本 ID 时读 [references/runninghub.md](references/runninghub.md) |
| 单镜或批跑生视频 | 第 3–5 节：`preflight` → 确认 → `gen` | 用哪个通道就读对应参考：[runninghub.md](references/runninghub.md) / [chenyu.md](references/chenyu.md) / [colab.md](references/colab.md) |
| 独占 GPU 开机 / 存镜像 / 对账 / 停机 | 第 4 节摘要与收尾规则 + `gpu` / `daemon` | 晨羽读 [references/chenyu.md](references/chenyu.md)；Colab 读 [references/colab.md](references/colab.md) |
| 断线续拉 / 补领成片 / 取消任务 | 第 4 节第 3 点：`jobs list/resume/fetch/cancel` | `runninghub` 认领与取消见 [references/runninghub.md](references/runninghub.md)；晨羽超时见 [references/chenyu.md](references/chenyu.md) |
| 查余额 / 统计项目花费 / 手动记账 | 第 5 节台账命令：`ledger` | — |
| `preflight` 提示词报错或警告排查 | 第 5 节避坑要点 | 完整段落、时间戳与引用规则见 [references/prompt-validation.md](references/prompt-validation.md) |

## 2. CLI 调用、安装与环境体检

- **调用方式**：优先直接运行全局命令 `yc-h3`（别名 `yc-h3-engine`）；若未全局安装，可先在克隆仓内运行 `uv tool install .` 装为全局命令，或用 `uv run --project <yc-h3-engine仓库路径> yc-h3`。通用全局参数放在子命令前：`yc-h3 [--env-file <path>] [--data-dir <path>] <subcommand> ...`。
- **凭据安全配置**：
  - **禁止通过命令行参数传递明文 API Key**，禁止打印、回显或提交 `.env` 文件；
  - 配置 API Key 时，优先让用户在自己终端交互运行 `yc-h3 init`（内置隐藏输入，首次写 `~/.yc-h3-engine/.env`，加 `--local` 写当前目录 `./.env`，更新加 `--update`），避免把明文密钥发进聊天上下文；
  - 修改非敏感配置（如 `YC_H3_PROVIDER_ORDER`、`RUNNINGHUB_WORKFLOW_ID`）或用户明确要求代写时，再通过标准输入管道增量写入：`yc-h3 init --from-stdin-json --update`。
- **只读体检**：
  ```bash
  yc-h3 doctor --json
  yc-h3 doctor --remote --json
  ```
  缺 `ffmpeg`/`ffprobe` 时直接给出安装命令（macOS `brew install ffmpeg`，Windows `winget install Gyan.FFmpeg`，Linux 用系统包管理器）；缺 `RUNNINGHUB_WORKFLOW_ID` 时按 [references/runninghub.md](references/runninghub.md) 引导用户在网页端跑通一次后填入数字 ID。

## 3. 通道与工作流选型

1.0 对外统一暴露两个逻辑工作流名：**`jinyu-hybrid`**（默认，文戏/对白/情绪/常规参考）与 **`jinyu-combat`**（武打/高强度动作）。

| 场景 | 通道 (`--provider`) | 素材槽位上限 | 关键参数边界与平台约束 |
| :--- | :--- | :--- | :--- |
| 零运维日常主力 / 需参考视频 | `runninghub` | 6 图 + 3 音 + 1 视频（视频伴音占音频槽，音/视频各限 15s） | 48GB Plus；默认 `16:9`（`1664×928`），支持 `9:16`/`1:1` 或 32 倍数 `--width`/`--height`（最长边 `<=1664`、总像素 `<=1,572,864`）；支持小数秒、`--seed`/`--steps`（7–100）/`--save-last-frame`；不支持 `--first-frame`/`--megapixels`/`--upscale-2x`/`--trim-to-requested-seconds`；需配 Key + 副本 ID |
| 低单价按次补镜（不赶时间） | `chenyu` | 9 图 + 3 音（**无视频**，音频累计 ≤ 15s） | 5090 按次，需双钱包有余额；仅整数秒（1–15），默认 `16:9` + `1.5 MP`（`1664×928`），支持 8 种 `--aspect` / `--megapixels 0.2–2.0` / `--upscale-2x` / `--save-last-frame`（非 `16:9`、非已知 `16:9` 像素档位或 2x 超分时预检像素为 `null` 并附 `size_note`）；不支持视频/首帧/seed/steps/宽高；审查排队约 20–30+ 分钟 |
| 持续批跑 / 参考视频（全平台含 Win） | `chenyu-comfy` | 9 图 + 3 音 + 3 视频（合计 ≤ 12；视频累计 ≤ 15s，音频含视频伴音占音频与总量槽且累计 ≤ 15s） | 晨羽独占 GPU，纯 HTTPS 无需 SSH；默认 `16:9`（`1664×928`），支持 `9:16`/`1:1` 或 32 倍数 `--width`/`--height`（高显存最长边 `<=1664`、总像素 `<=1,572,864`）、`--first-frame` (`i2v`) 与 `--trim-to-requested-seconds`；固定 12 步，不支持 `--save-last-frame`/`--megapixels`/`--upscale-2x`；`<36 GiB` 卡（Tier 3）限最长边 `<=1024`、总像素 `<=557,056`（如 `1024×544` 或 `544×1024`）、最长 `8s` |
| 已有 Colab CU 额度大批量跑片 | `colab-comfy` | 同 `chenyu-comfy`（9 图 + 3 音 + 3 视频，合计 ≤ 12，音/视频各限 15s） | 仅 macOS / Linux / Windows WSL2（原生 Win 自动拦截退出码 `3`）；分辨率、参数与显存档位约束同 `chenyu-comfy` |

> 各通道完整的画幅预设、像素上限与时长约束可随时运行只读命令 `yc-h3 workflows --json` 查询。

- **通道顺位与自动选路检查**：
  - 选路优先级：**当轮用户明确指定 > 当前项目规则（如 `AGENTS.md`）> `.env` 的 `YC_H3_PROVIDER_ORDER`（`yc-h3 doctor --json` 的 `provider_order` 或 `yc-h3 providers --json`）> 默认顺位（`colab-comfy → chenyu-comfy → runninghub → chenyu`）**。
  - 若省略 `--provider` 跑 `preflight --json`，CLI 会按顺位自动跳过未开机或能力不匹配的通道并记录在 `routing_skipped` 中。**拿到结果后必须同时核对 `selected_provider` 与 `routing_skipped`**：只要顺位靠前的通道（尤其是未开机的独占 GPU）被跳过，必须向用户说明跳过原因，由用户决定先开机还是切到下一顺位，绝不静默切换付费通道。若用户确认沿用 `selected_provider`，在 `gen` 时显式带上 `--provider <selected_provider>` 即可直接复用本次摘要并防止两步之间选路漂移；若用户改为先开机或改选其他通道，则须针对最终通道重跑 `preflight`。
  - 若用户想长期固定顺序，引导在 `~/.yc-h3-engine/.env` 或项目 `./.env` 写入 `YC_H3_PROVIDER_ORDER=colab-comfy,runninghub,chenyu-comfy,chenyu`（只写部分通道时仅在所列通道中自动选路，显式 `--provider` 不受限）。

## 4. 费用门禁、报价汇报、断点恢复与收尾关机

1. **两步确认与摘要来源（为什么必须原样复传）**：
   `approve_digest` 会把命令参数、输入素材 SHA-256、输出路径、`--tag` 与报价事实一起计算摘要，审批后改动任意一项（或重存素材）都会触发摘要失配并返回退出码 `2`。三类操作的摘要字段来源须准确区分：
   - **视频生成**：先 `yc-h3 preflight ... --json` 取顶层 **`approve_digest`** → 用户批准后原样参数追加 `--approve-digest <approve_digest>` 执行 `yc-h3 gen`。
   - **受管 GPU 开机 / 改时限**：先 `yc-h3 gpu plan --backend colab|chenyu ... --json` 取顶层 **`digest`** → 用户批准后原样参数追加 `--approve-digest <digest>` 执行 `yc-h3 gpu up`。
   - **晨羽保存私有镜像**：存在可保存的运行中实例时，先不带摘要跑 `yc-h3 gpu save-image --backend chenyu --json`（计划生成成功后返回退出码 `2` 并向 stderr 打错误 JSON；若实例已停或有待核对状态会先以退出码 `3` 退出），取错误 JSON 中的 **`preflight.approve_digest`** → 用户批准后原样追加 `--approve-digest` 执行（会在锁内核验是否存在未取回成片，防止丢片）。
2. **报价汇报口径**：
   请用户审批时，固定汇报六项：**实际通道**（若跳过了首选通道须说明 `routing_skipped` 原因）、**最终输出路径（`--out`）**、**目标分辨率与时长**（按 `facts.width × facts.height`、`facts.seconds` / `facts.frames` 汇报；晨羽按次若 `width`/`height` 为 `null`，改报 `facts.aspect` + `facts.megapixels` + 是否 `upscale_2x` 及 `facts.size_note` 说明）、**素材标签绑定对照**（有参考素材时，按 `facts.prompt_bindings` 逐项列出 `<Picture N>` / `<Video N>` / `<Audio N>` 对应的文件名及视频伴音来源，便于用户在扣费前核对顺序）、**预估来源**（按 `facts.estimate_source` 与 `facts.estimate_samples` 译为「本地实跑 N 单 / 初始基线 / 暂无估算」）、以及 **`facts` 中实际非空的预计耗时与费用**（耗时：`estimated_wall_seconds` / `estimated_run_seconds` / `estimate_seconds`；费用：`estimated_reference_points` / `estimated_quantity` / `estimated_cny`）。缺失或为 `null` 的单项如实报「暂无估算」，不臆造数字。各通道估算拟合细节见对应通道参考文档。
3. **断点恢复与防重复扣费**：
   建单前 CLI 会落盘断点，断线或超时后远端可能仍在计费执行，直接重提会导致重复扣费：
   - 先查 `yc-h3 jobs list --json`，再按状态恢复：对 `chenyu` / `colab-comfy` / `chenyu-comfy` 的 `ambiguous` 断点或待续拉任务优先跑 `yc-h3 jobs resume <task_key> --json`（可按本地幂等键安全核对/重放并持续轮询）；`yc-h3 jobs fetch <task_key> --json` 仅单次查取，不做幂等重放（二者均支持 `--out <new_path>` 换路径、`--remote-job-id <id>` 补领历史作业）。`runninghub` 无远端幂等键，`ambiguous` 时须先核实远端任务再带 `--remote-task-id` 认领，确认未扣费才可重提；
   - `chenyu` 按次轮询超时上限 60 分钟，若返回 `task_not_completed` 表示远端仍在处理，应稍后用 `jobs resume` / `jobs fetch` 续拉，不可擅用 `--force-new`；
   - 仅当用户明确要重做时才加 `--force-new` 重新预检审批（每次 `--force-new` 审批仅能消费一次；若返回 `replacement_approval_stale` 说明本次重提已落盘，须直接跑 `yc-h3 jobs resume <task_key>`，严禁再次 `--force-new`）；同 `--out` 需要独立断点时传 `--job-key`。
4. **独占 GPU 收尾关机提醒（务必显式指定 `--backend`）**：
   CLI 的 `gpu` 子命令默认 `--backend colab`；操作晨羽独占 GPU 时**必须显式加 `--backend chenyu`**（否则可能误操作 Colab 返回退出码 `0` 而让晨羽实例继续计费）。本批生成结束或用户表示收工时，主动提醒并协助执行 `yc-h3 gpu stop --backend colab|chenyu`；晨羽若需固化系统盘环境，先走 `yc-h3 gpu save-image --backend chenyu`（保存完仅**关机**停 GPU 计费，仍保留已关机实例记录），随后须补跑 `yc-h3 gpu stop --backend chenyu` 彻底销毁实例，否则下次 `gpu plan` / `gpu up` 会报 `stopped_instance_requires_cleanup`（退出码 `3`）。遇本机断电或进程强杀，恢复后均须先跑 `yc-h3 gpu reconcile --backend colab|chenyu` 核对远端状态。
5. **统一退出码**：
   - `0`：成功 / 预检 `pass`
   - `2`：需人工审批 / 预检 `warn`（注意：`runninghub` 与 `chenyu` 因后付费预估告警，正常无错的 `preflight` 也会返回退出码 `2`，核对告警并取得确认后带实时 `--approve-digest` 提交）
   - `3`：预检 `block` / 会话或断点冲突 / `ambiguous` / `replacement_approval_stale` / 原生 Windows 调 Colab 拦截（先解决阻断原因或查 `jobs list` / `gpu reconcile --backend colab|chenyu`，不可强行重提）
   - `1`：运行期异常或参数错误

## 5. 常用命令模板与核心避坑

```bash
# 1) 只读预检 → 用户确认后原样参数加 --approve-digest 提交（显式锁定 --provider）
yc-h3 preflight --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./outputs/s01.mp4 --tag my-video --prompt-file ./s01.txt --json

yc-h3 gen --provider runninghub --workflow jinyu-hybrid \
  --image ./char.png --image ./bg.png --audio ./voice.wav \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./outputs/s01.mp4 --tag my-video --prompt-file ./s01.txt \
  --approve-digest "<preflight 返回的 approve_digest>" --json

# 2) 钱包余额与费用台账（--unit 区分大小写：CU/point/credit/call/char/USD/CNY）
# sync 仅接受 runninghub 或 chenyu（chenyu-comfy 共用晨羽钱包，同样传 --provider chenyu）
yc-h3 ledger balance sync --provider runninghub --json
yc-h3 ledger balance show --json
# --since 须用带时区 ISO-8601（如 2026-11-24T00:00:00Z）；--tag 仅过滤 jobs，sessions 仍汇总全部 GPU 会话
yc-h3 ledger summary --tag my-video --json
# 手动设置余额（晨羽双钱包分项用 --cash-balance 与 --card-balance 成对传入）
yc-h3 ledger balance set --provider colab-comfy --unit CU --balance 100 --json
yc-h3 ledger balance set --provider chenyu --unit CNY --cash-balance 10 --card-balance 50 --json
# 手动补记外部消耗（四项必填：--provider / --quantity / --unit / --idempotency-key）
yc-h3 ledger record --provider runninghub --quantity 120 --unit point --idempotency-key manual-01 --tag my-video --json
```

**执行期高频提示词避坑（完整校验口径与 `preflight` 报错排查见 [references/prompt-validation.md](references/prompt-validation.md)）**：
- **素材编号对齐与顺序复核**：引擎会自动把按顺序传入的素材映射到各后端工作流节点，无需关心底层节点 ID；`--image` 对应 `<Picture 1>`, `<Picture 2>`...，`--video` 对应 `<Video 1>`, `<Video 2>`...，有伴音参考视频排在 `<Audio 1>`、独立 `--audio` 依次顺延（不需要视频原声时加 `--mute-reference-video`）。注意 `preflight` 只能拦截编号越界，无法自动识别「传参顺序与提示词语义写反」；**每次跑完 `preflight` 必须逐项核对 `facts.prompt_bindings` 与提示词中各标签所指代的对象一致后再请用户确认**。
- **`--no-music` 冲突规则**：传 `--no-music` 时，若提示词缺失 `non_diegetic_music:` 段会由 CLI 自动补为 `N/A`；若已写该段，其内容必须为 `N/A`，写了其他配乐描述或留空都会被拒。
- **单图任务别错用 `--first-frame`**：在 `runninghub` / `chenyu` 上单图参考应走 `--image`（`r2v`），不要传 `--first-frame`（仅 `colab-comfy` / `chenyu-comfy` 支持 `--first-frame` 走 `i2v`）。
