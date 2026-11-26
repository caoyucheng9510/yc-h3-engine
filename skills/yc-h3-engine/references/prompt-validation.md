# CLI 提示词确定性校验口径与报错排查

本参考用于排查 `yc-h3 preflight` 报出的提示词阻断（`block`）或告警（`warn`），以及核对 CLI 素材编号映射。**本参考不是写词教程**；从零撰写或润色 MiniMax-H3 提示词请使用官方 `h3-prompt-writing` Skill。

## 1. 三种任务模式与段落结构

CLI 会按 `--task`（或根据传入素材自动推断）映射到对应校验模式；提示词外层**不可包裹 Markdown 代码围栏**（`` ``` ``），且任何必填段落都不可留空：

| CLI `--task` | 触发条件 | 校验模式 | 必填段落顺序（英文键名 + 冒号） | 首行约束 |
| :--- | :--- | :--- | :--- | :--- |
| `t2v` | 零参考素材（无图/音/视频/首帧） | `t2va` | `integrated_multimodal_description:` → `overall_soundscape:` → `non_diegetic_music:` | 建议直接从首段开始，不要加多余前置文本（有前置文本会触发 `warn`） |
| `r2v` | 传了 `--image` / `--audio` / `--video`（单图在 RunningHub / 晨羽按次也走 `r2v`） | `ref2va` | `subject_definitions:` → `summary:` → `retention_analysis:` → `detailed_description:` → `overall_soundscape:` → `non_diegetic_music:` | **必须**直接以 `subject_definitions:` 开头，不可有任何前置文本（否则 `block`） |
| `i2v` | 仅受管 ComfyUI（`colab-comfy` / `chenyu-comfy`）传入 `--first-frame`（仅单首帧，不可与 `--image`/`--audio`/`--video` 或 `--task r2v` 混用） | `i2va` | `integrated_multimodal_description:` → `overall_soundscape:` → `non_diegetic_music:` | 开头**必须**包含官方首帧对齐指令，含 `0.00 seconds` 与 `<Picture 1>`（例如 `The video starts at 0.00 seconds with <Picture 1>.`），否则 `block` |

## 2. 镜头编号与切镜时间戳

在正文段（`ref2va` 的 `detailed_description:`，或 `t2va`/`i2va` 的 `integrated_multimodal_description:`）中，CLI 会先剔除 `<d>...</d>` 对白与双引号 `"..."` 画面文字后再校验镜头标记：

- 必须包含 `[Shot 1]`，且后续镜头编号必须从 `1` 开始连续递增（`[Shot 2]`, `[Shot 3]`...）。
- `[Shot 1]` **不可**带切镜时间戳。
- `[Shot 2]` 及之后**必须**紧跟切镜时间戳，格式为 `[Shot N] At MM:SS.mmm, ...`：
  - 秒数 `SS` 必须 `< 60`；
  - 每个切点必须严格大于前一个切点；
  - 切点必须严格小于目标总时长 `--seconds`（等于或超出都会阻断）。

## 3. 素材引用编号对齐（`<Picture N>` / `<Audio N>` / `<Video N>`）

- 编号均为 **1-based**（从 `1` 开始），不可出现 `<= 0`、超过通道槽位上限、或超过本次实际传入数量的引用，也不可引用当前通道不支持的媒体类型（例如在 `chenyu` 按次通道中写 `<Video 1>`）。
- 重复传入的 `--image` 按命令行顺序对应 `<Picture 1>`, `<Picture 2>`...（`i2v` 模式下 `--first-frame` 对应 `<Picture 1>`）。
- 重复传入的 `--video` 按命令行顺序对应 `<Video 1>`, `<Video 2>`...
- **视频伴音对 `<Audio N>` 的占位规则**：当同时传入有伴音的参考视频与独立 `--audio` 时，启用伴音的参考视频音轨排在最前（占 `<Audio 1>`...），独立 `--audio` 依次顺延。如果只想借参考视频画面、不要其原声，请加 `--mute-reference-video`（该开关会静音全部参考视频）。
- **提交前以 `facts.prompt_bindings` 复核顺序**：`yc-h3 preflight --json` 会在 `facts.prompt_bindings` 中返回每个 `<Picture N>` / `<Video N>` / `<Audio N>` 实际绑定的文件名、路径及视频伴音来源（非 `--json` 模式也会在顶部打印「素材绑定」行）。离线校验只能拦住编号越界，不能自动发现「两张图顺序传反」，提交前应逐条核对此表与提示词中的角色/场景/声音语义是否一致。

## 4. `--no-music` 行为与正/负向措辞

- **传入 `--no-music`**：
  - 提示词缺少 `non_diegetic_music:` 段时，CLI 自动在末尾补 `non_diegetic_music: N/A`；
  - 提示词已写 `non_diegetic_music:` 段时，其内容必须为 `N/A`（大小写不敏感）；写了非 `N/A` 配乐描述或留空会因策略冲突被阻断。
- **避免负向措辞**：H3 ComfyUI 工作流没有独立 negative prompt 分支，正文与 `overall_soundscape:` 中写 `no music`、`without subtitles`、`avoid text` 等负向描述容易适得其反并触发告警；直接描述画面和声音中实际存在的内容即可。

## 5. 阻断（`block`，退出码 `3`）与警告（`warn`，退出码 `2`）对照

| 级别 | 触发条件 | 处理方式 |
| :--- | :--- | :--- |
| **`block`（阻断）** | 提示词为空或外层带 `` ``` `` 围栏；段落缺失/顺序错/空段；`ref2va` 首行非 `subject_definitions:`；`i2va` 缺首帧对齐指令；缺 `[Shot 1]`、镜头编号不连续、`[Shot 1]` 带时间戳、后续镜头缺时间戳或时间戳越界/倒退；`<Picture/Audio/Video N>` 编号非法或越界；`--no-music` 与非 `N/A` 配乐冲突 | 必须修正提示词或修正素材/参数后重跑 `preflight` |
| **`warn`（警告）** | `t2va` 开头有多余前置文本；`ref2va` 传了参考图但提示词未引用任何 `<Picture N>`；正文/声景段出现疑似负向措辞；`non_diegetic_music` 非 `N/A`（将生成配乐）；正文含中/日/韩（CJK）文字或台词（提醒成片后检查有无野生烧字幕） | 向用户如实说明告警项；确认可接受后仍可凭本次 `approve_digest` 提交 |
