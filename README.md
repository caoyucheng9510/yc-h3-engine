# yc-h3-engine

开源、跨平台的 **MiniMax-H3 多通道视频生成执行引擎**（CLI `yc-h3` + 独立 Agent Skill `skills/yc-h3-engine/`）。

它只负责一件事：**接收素材与提示词 → 只读预检与费用确认 → 提交生成 → 严格解码校验并无覆盖落盘 → 断点恢复与费用台账**，与你现有的剧本、生图、剪辑或短剧项目彻底解耦。

- **原生跨平台（Windows 10/11 / macOS / Linux）**：RunningHub、晨羽按次工作流 API、晨羽独占 GPU、配置向导、体检、断点与台账均原生支持。
- **Colab 通道**：1.0 支持 **macOS / Linux / Windows WSL2**（在原生 Windows 终端下会自动拦截并提示改用原生通道或 WSL2）。
- **内置两套通用工作流**：四个后端对外统一使用 **`jinyu-hybrid`**（文戏，默认）与 **`jinyu-combat`**（武打），切换后端无需记忆多套工作流名，同时支持扩展自定义工作流（见 [docs/ADDING_WORKFLOW.md](docs/ADDING_WORKFLOW.md)）。

---

## 一、四通道怎么选（按上手门槛分层）

| 推荐层级 | 通道 (`--provider`) | 平台支持 | 素材槽位 | 特点与适用场景 |
| :--- | :--- | :--- | :--- | :--- |
| **新手首选（填 Key 即用）** | `runninghub` | Win / macOS / Linux | 6 图 + 3 音 + 1 视频 | 48GB Plus 云端托管，零 GPU 运维，速度快（5s 约 2.5 分钟），支持参考视频与自定义 seed/steps |
| **低成本零星补镜** | `chenyu` | Win / macOS / Linux | 9 图 + 3 音（无视频） | 5090 按次计费单价最低（5s 约 ¥0.29–¥0.34），但实测含约 20–30+ 分钟平台内容审查排队，适合不赶时间的补镜 |
| **进阶连续批跑（Win 友好）** | `chenyu-comfy` | Win / macOS / Linux | 9 图 + 3 音 + 3 视频 | 晨羽独占 GPU，与 `chenyu` 共用同一个 API Key 和钱包，纯 HTTPS 无需 SSH，支持私有镜像复用权重 |
| **进阶算力（Colab Pro+）** | `colab-comfy` | macOS / Linux / WSL2 | 9 图 + 3 音 + 3 视频 | 托管 Google Colab（优先 G4 96GB / H100 / A100），适合已有 Colab CU 额度的大批量生成 |

> 详细耗时、显存与单条成本实测对比见 [docs/BENCHMARKS.md](docs/BENCHMARKS.md)。

---

## 二、快速安装（两种用法二选一）

### 前置依赖
1. **Python 3.12+** 与 **[uv](https://docs.astral.sh/uv/)**（推荐用 `uv` 一行安装与隔离依赖）。
2. **FFmpeg / FFprobe**（用于成片 24fps / 双声道 / `-xerror` 严格解码校验及 RunningHub 参考视频规范化）：
   - **macOS**：`brew install ffmpeg`
   - **Windows**：`winget install Gyan.FFmpeg`
   - **Linux**：使用系统包管理器安装 `ffmpeg`

### 方式 A：装成全局命令 + 安装 Skill（推荐，可接入任意已有项目）
```bash
# 1. 下载仓库并全局安装 yc-h3 / yc-h3-engine 命令
git clone <你的仓库地址> yc-h3-engine
cd yc-h3-engine
uv tool install .
```
然后将仓内 `skills/yc-h3-engine/` 复制到你常用的 Agent Skill 目录（如 `~/.agents/skills/yc-h3-engine` 或 `~/.claude/skills/yc-h3-engine`）。之后在电脑**任意项目目录**下，AI 和终端都可以直接调用 `yc-h3`。

### 方式 B：仓内直接运行（不装全局命令）
```bash
git clone <你的仓库地址> yc-h3-engine
cd yc-h3-engine
uv sync
uv run yc-h3 --help
```

---

## 三、平台准备与 `.env` 配置（开箱即用）

本工具统一使用 **`.env`** 管理配置（不要求手动修改系统环境变量）。加载顺序固定为：

`--env-file <显式文件>` > `当前目录 ./.env` > `全局默认 ~/.yc-h3-engine/.env` > `进程环境变量`（高优先级空值不覆盖低优先级有效值）。

### 1. 运行初始化向导与环境体检
可以直接让你的 AI Agent 执行（或自己在终端运行）：
```bash
yc-h3 init          # 默认生成/更新 ~/.yc-h3-engine/.env（加 --local 写当前目录 .env）
yc-h3 doctor        # 严格只读：检查 Python、FFmpeg、.env 配置与各通道状态
yc-h3 doctor --remote  # 同时查询已配置通道的实时余额
```
也可以手动复制 [.env.example](.env.example) 到 `~/.yc-h3-engine/.env` 后按需填写：

```dotenv
# RunningHub（需网页首跑一次获取自己的 workflowId）
RUNNINGHUB_API_KEY=
RUNNINGHUB_WORKFLOW_ID=

# 晨羽（按次工作流与独占 GPU 共用同一个 Key 和钱包）
CHENYU_API_KEY=

# 可选：自定义数据目录（默认 ~/.yc-h3-engine）
# YC_H3_DATA_DIR=

# 可选：用于台账折算人民币的单价（不填也能正常记原始积分/CU）
# RUNNINGHUB_CNY_PER_POINT=
# COLAB_CNY_PER_CU=
```

### 2. RunningHub 账号准备（必读）
RunningHub 开放平台要求：**必须先在网页端将工作流保存到自己账号并完整成功运行一次**，平台才会激活该副本的 API 模板。

1. 打开原作者公开工作流页面：[YZ金鱼-MiniMax H3 终极篇-文武双全](https://www.runninghub.cn/post/2124779546246475777)。
2. **[待手工补充：RunningHub 注册、充值购买积分/Plus 会员的入口与注意事项]**
3. **[待手工补充：点击复制/运行工作流（选 Plus 48GB 机型跑通一次）并从 API 面板复制 `workflowId` 与 `API Key` 的截图步骤]**
4. 将拿到的 Key 与工作流 ID 填入 `~/.yc-h3-engine/.env` 的 `RUNNINGHUB_API_KEY` 与 `RUNNINGHUB_WORKFLOW_ID`（如文戏与武打使用不同副本，可分别填 `RUNNINGHUB_WORKFLOW_ID_HYBRID` 与 `RUNNINGHUB_WORKFLOW_ID_COMBAT`）。

### 3. 晨羽账号准备（按次与独占 GPU 通用）
1. **[待手工补充：晨羽平台（https://www.chenyu.cn）注册、实名认证、获取 `CHENYU_API_KEY` 的入口与步骤]**
2. **[待手工补充：晨羽双钱包充值说明]**
   - **重要提示（双钱包机制）**：晨羽按次工作流由**算力卡**支付 5090 算力费（约 `0.12 CNY/运行分钟`），由**现金余额**支付创作者费（`0.03 CNY/次`），**两个钱包都需要有余额**才能正常下单。
   - 晨羽内置公开工作流（`wf_f8b8f8ee12000cebc6bf`），填好 `CHENYU_API_KEY` 后无需复制即可直接调用；但实测每单在平台内容审查阶段会排队 20–30+ 分钟（排队不扣算力费），且不支持参考视频，赶进度或需参考视频请选 `runninghub` 或 `chenyu-comfy`。

### 4. Colab 通道准备（仅 macOS / Linux / Windows WSL2）
1. 拥有可用 Google Colab Pro+ / 计算单元（CU）额度，并安装且完成 `google-colab-cli` 登录（确保 `colab` 与 `ssh` 在 `PATH` 中可用）。
2. 启动本地守护进程并规划开机（默认监听 `127.0.0.1:7798`，会话前缀 `yh3-`）：
   ```bash
   yc-h3 daemon start
   yc-h3 gpu plan --backend colab --session default --gpu AUTO --setup \
     --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' --json
   # 核对预检返回的 approve_digest 后开机：
   yc-h3 gpu up --backend colab --session default --gpu AUTO --setup \
     --setup-params '{"plugins":["ComfyUI-KJNodes","comfyui-SelfLift"]}' \
     --approve-digest "<DIGEST>" --json
   ```

---

## 四、最短出片示例（预检 → 确认 → 生成）

所有付费生成强制遵循**两步确认门禁**：先 `preflight` 核对素材顺序、分辨率、帧数与估算费用，拿到实时 `approve_digest` 后再执行 `gen`。

```bash
# 1. 只读预检（不扣费，返回 approve_digest 与报价事实）
yc-h3 preflight --provider runninghub --workflow jinyu-hybrid \
  --image ./hero.png --image ./scene.png \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./out/shot01.mp4 \
  --prompt-file ./shot01.txt --json

# 2. 确认无误后带实时 digest 提交生成（自动严格解码校验并无覆盖落盘）
yc-h3 gen --provider runninghub --workflow jinyu-hybrid \
  --image ./hero.png --image ./scene.png \
  --seconds 5 --aspect 16:9 --no-music \
  --out ./out/shot01.mp4 --tag demo \
  --prompt-file ./shot01.txt \
  --approve-digest "<上一步返回的 approve_digest>" --json
```

切换后端只需改 `--provider`（例如 `--provider chenyu`、`--provider chenyu-comfy`、`--provider colab-comfy`），或省略 `--provider` 由引擎按请求能力自动选择可用通道。

### 常用维护命令速查
```bash
yc-h3 providers --probe --json              # 查看各通道本地配置状态
yc-h3 workflows --json                      # 查看所有内置工作流与素材槽位上限
yc-h3 jobs list --json                      # 查看任务断点摘要（已脱敏）
yc-h3 jobs resume <task_key> --json         # 断网恢复并下载成片（可加 --out 改落盘路径）
yc-h3 jobs fetch <task_key> --json          # 单次查询并领取已完成成品
yc-h3 ledger balance sync --provider runninghub --json  # 同步 RunningHub / 晨羽实时余额
yc-h3 ledger balance show --json            # 按账号与钱包核对最新推算余额
yc-h3 ledger summary --tag demo --json      # 按分组统计费用汇总
```

### 统一退出码
- `0`：执行成功 / 预检 `pass`
- `2`：需人工确认审批 / 预检 `warn`（须携带匹配 `approve_digest`）
- `3`：预检 `block` / 会话或断点冲突 / `ambiguous` / 原生 Windows 调用 Colab 拦截
- `1`：运行期异常或参数值错误

---

## 五、实测性能与费用参考（脱敏汇总）

以下均基于 `jinyu-hybrid` / `jinyu-combat` `1664×928`（12 步 SelfLift）真实运行样本汇总，完整样本量、日期与计时口径见 [docs/BENCHMARKS.md](docs/BENCHMARKS.md)：

| 通道 / 机型 | 样本规格 | 实测交付耗时 | 原始计费单位参考 | 适用判断 |
| :--- | :--- | :--- | :--- | :--- |
| **RunningHub Plus 48GB** | `jinyu-hybrid` 5s / 11s（纯图/图音） | 5s 中位数约 `135–151s`；11s 中位数约 `490s` | Plus 约 `0.23 积分/运行秒`（按官方套餐折算） | 综合最省心，适合日常主力出片 |
| **晨羽按次 API（5090）** | `jinyu-hybrid` 5s–11s（4 单样本） | 推理 `2.15–4.81 min`，但含内容审查总墙钟 `37–44 min` | 公开费率 `0.12 CNY/推理分钟 + 0.03 CNY/次`（实扣 `¥0.29–¥0.61/单`） | 单条最便宜，适合不赶时间的补镜 |
| **Colab `A100-80`** | `jinyu-hybrid` 5.2s–15.1s（暖跑） | 5.2s 纯推理 `250.6s`；11.5s 纯推理 `607.8s` | Colab 实测 `10.99 CU/hr`（纯图约 `0.76–1.86 CU/条`） | 已有 CU 额度内适合批跑纯图镜头 |
| **Colab `G4-96` (Blackwell)** | `jinyu-hybrid` 10s 768p 历史配方（暖跑） | 纯图 `173–180s`；带 10s 参考视频 `809s` | Colab 实测 `10.05 CU/hr`（纯图约 `0.49 CU`，视频参考约 `2.26 CU`） | 有库存时优先选 G4，吞吐显著快于 A100 |

---

## 六、重要边界与已知限制

1. **防遗留计费 GPU 的平台固有边界**：
   - **Colab（`colab-comfy`）**：Google Colab CLI 无独立于本机的服务端硬截止 API。守护进程会在本机存活期间执行空闲关机与硬时限回收，但如果你直接关机或强杀 WSL2，只能依赖 Google Colab 服务端自身的空闲回收兜底，收工请务必运行 `yc-h3 gpu stop --backend colab` 确认释放。
   - **晨羽独占 GPU（`chenyu-comfy`）**：引擎会在建机前落盘绝对截止时间，并在建机响应丢失时自动在当前命令内认领并挂上平台定时停机与空闲销毁护栏；但因晨羽 `create_by_*` 接口不支持建机同时传入 `shutdown_time`，若本机在发出建机请求后立刻断电或被强杀，请重新运行 `yc-h3 gpu reconcile --backend chenyu` 或登录晨羽网页端确认实例状态。
2. **自定义扩展**：如需接入自己的 ComfyUI / RunningHub / 晨羽工作流，请阅读 [docs/ADDING_WORKFLOW.md](docs/ADDING_WORKFLOW.md)。

---

## 七、开源协议与第三方署名

- 本仓库作者原创代码、文档与自有 Skill 采用 **[MIT License](LICENSE)**。
- 第三方派生文件与署名清单详见 **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)**：
  - `yc_h3/local/` 与 `yc_h3/remote/` 衍生自 **WorldForge**（MIT License，`Copyright (c) 2026 WorldForge Contributors`）；
  - `yc_h3/providers/runninghub_workflows/jinyu-hybrid.json` 与 `jinyu-combat.json` 改编自 **YZ金鱼** 公开工作流《[YZ金鱼-MiniMax H3 终极篇-文武双全](https://www.runninghub.cn/post/2124779546246475777)》，排除在本仓 MIT 再授权之外并保留原作者署名；
  - 本仓库不托管任何模型权重，底层 MiniMax-H3 模型与社区 LoRA 遵守各自上游协议。
