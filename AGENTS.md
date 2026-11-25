# yc-h3-engine 仓库规范

本仓为开源、跨平台的 MiniMax-H3 多通道生视频执行引擎（CLI `yc-h3` + 独立 Skill `skills/yc-h3-engine/`），负责接收素材与提示词、走费用预检与断点保护提交生成、校验落盘成片并记录费用台账，与用户的剧本/生图/剪辑项目完全解耦。

## 1. 平台与运行边界

- **原生跨平台（Windows / macOS / Linux）**：`runninghub`、`chenyu`（按次工作流 API）、`chenyu-comfy`（晨羽独占 GPU）、`init`、`doctor`、`jobs`、`ledger` 必须原生支持 Windows 与 POSIX；本地文件锁统一经跨平台锁模块实现，禁止在通用路径直接引入仅限 POSIX 的 `fcntl`。
- **Colab 通道（`colab-comfy`）**：1.0 明确支持 **macOS / Linux / Windows WSL2**；在原生 Windows（非 WSL2）调用 `colab-comfy` 或 `daemon` / `gpu --backend colab` 时必须主动拦截并给出清晰替代指引，不得抛出原始 `fcntl` 或 OpenSSH `ControlMaster` 异常。
- **1.0 内置工作流**：对外统一只暴露 **`jinyu-hybrid`**（文戏，默认）与 **`jinyu-combat`**（武打）两个逻辑名，由 `--provider` 路由到对应后端，同时保持各后端 `workflows/` 注册表的开放可扩展性。

## 2. 隔离命名空间与凭据安全

- **命名空间隔离**：默认数据目录 `~/.yc-h3-engine`（支持 `YC_H3_DATA_DIR` / `--data-dir`）；GPU 会话前缀固定 `yh3-`；远端工作根目录 `/content/yc_h3_engine`（Colab）与 `/root/yc_h3_engine`（晨羽独占）；本地 Colab 守护进程默认监听 `127.0.0.1:7798`（仅限 loopback）。
- **`.env` 配置与保密红线**：`RUNNINGHUB_API_KEY`、`RUNNINGHUB_WORKFLOW_ID`、`CHENYU_API_KEY` 等配置优先从 `.env` 读取（按 `--env-file` → `./.env` → `~/.yc-h3-engine/.env`，并允许进程环境变量兜底）。严禁打印、回显、提交 `.env`，严禁在错误消息、断点 JSON、台账流水或日志中泄露任何密钥或 Bearer Token。
- **开源脱敏**：仓内禁止出现个人账号 ID、私有工作流 ID、个人本机绝对路径、个人套餐实付价格或真实余额数据。

## 3. 费用门禁与数据完整性不变量

- **只读预检 + 审批摘要**：`preflight`、`doctor`、`providers`、`workflows` 必须严格只读不扣费；付费生成与受管 GPU 开机/改时限/保存镜像须核对当次实时 `--approve-digest`，并在提交前按 SHA-256 校验素材未被篡改。
- **防重复扣费与原子不覆盖**：建单前落盘 `submitting` 断点；提交结果不明时标为 `ambiguous` 并拦截原样重提；成品通过 24fps/分辨率/双声道/解码校验后以原子不覆盖方式落盘（重名递增 `_v2`），绝不覆盖或删除用户已有素材与成品。
- **统一退出码**：`0` 成功 / 预检 `pass`；`2` 需确认审批 / 预检 `warn`；`3` 预检 `block` / 会话冲突 / 断点冲突或 `ambiguous`；`1` 运行期或参数错误。

## 4. 开源许可与测试

- 原创代码、文档与自有 Skill 采用 **MIT License**；源自 WorldForge（MIT）与 YZ金鱼公开工作流的部分须在 `THIRD_PARTY_NOTICES.md` 与 README 保留清晰署名；不打包无再分发许可的第三方 Skill 或模型权重。
- 自动化测试全部离线可跑（`uv run pytest`），严禁在单测中发起真实付费 API 或真实 GPU 分配请求。
