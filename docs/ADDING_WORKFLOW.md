# 扩展自定义工作流开发指南

`yc-h3-engine` 1.0 内置了 `jinyu-hybrid`（文戏）与 `jinyu-combat`（武打）两套工作流，但三个工作流目录均采用声明式注册表设计，方便有开发能力的群友接入自己的 RunningHub、晨羽或 ComfyUI 工作流。

---

## 一、新增 RunningHub 工作流

目录：`yc_h3/providers/runninghub_workflows/`

1. **导出并清洗节点图**：
   - 在 RunningHub 工作流页面导出 ComfyUI API 格式 JSON，放入 `yc_h3/providers/runninghub_workflows/<name>.json`；
   - 移除纯 UI 展示/预览节点，清空示例占位文件名（`LoadImage`/`LoadAudio` 改为 `"None"`、`VHS_LoadVideo` 改为 `""`）与示例提示词。
2. **在 `registry.json` 登记条目**：
   - 将 `workflow_id` 设为 `null`（避免把个人账号副本 ID 写死进仓库，运行时从 `.env` 读取 `RUNNINGHUB_WORKFLOW_ID` 或按工作流扩展对应环境变量）；
   - 完整填写 `graph_file`、`prompt_mode`（`ref2va`/`t2va`/`i2va`/`fl2va`/`l2va`）、`tasks`、`instance_type`、`sizes`、`max_edge`、`max_pixels`、`min_seconds`、`max_seconds`、`default_steps`、`min_steps` 以及 `nodes` 节点映射：

```json
{
  "my-custom-rh": {
    "workflow_id": null,
    "graph_file": "my-custom-rh.json",
    "prompt_mode": "ref2va",
    "tasks": ["r2v", "t2v"],
    "instance_type": "plus",
    "description": "自定义 MiniMax H3 工作流",
    "default_aspect": "16:9",
    "sizes": {
      "16:9": [1664, 928],
      "9:16": [928, 1664],
      "1:1": [1248, 1248]
    },
    "max_edge": 1664,
    "max_pixels": 1572864,
    "min_seconds": 0.2,
    "max_seconds": 15.0,
    "default_steps": 12,
    "min_steps": 7,
    "default_seed": -1,
    "nodes": {
      "prompt": {"id": "263", "class_type": "Text", "input": "text"},
      "length": {"id": "259", "class_type": "PrimitiveFloat", "input": "value"},
      "steps": {"id": "261", "class_type": "BasicScheduler", "input": "steps"},
      "seed": {"id": "397", "class_type": "SelfLiftH3Sampler", "input": "seed"},
      "selflift": {"id": "397", "class_type": "SelfLiftH3Sampler", "transition_step_input": "transition_step"},
      "size": {"id": "265", "class_type": "MiniMaxH3ReferenceToVideo", "width_input": "width", "height_input": "height"},
      "target_node": "265",
      "images": {"ids": ["51", "49"], "class_type": "LoadImage", "input": "image"},
      "audios": {"ids": ["48"], "class_type": "LoadAudio", "input": "audio"},
      "videos": {"ids": ["27"], "class_type": "VHS_LoadVideo", "input": "video", "audio_output_index": 2}
    }
  }
}
```

3. **校验与测试**：
   - 运行 `yc-h3 workflows --provider runninghub --json` 检查目录与槽位解析；
   - 运行 `yc-h3 preflight --provider runninghub --workflow my-custom-rh --out ./tmp.mp4 --prompt "..." --json` 验证节点 `class_type` 与字段签名校验通过。

---

## 二、新增晨羽按次工作流

目录：`yc_h3/providers/chenyu_workflows/`

晨羽 `catalog.py` 采用**按条目整体验证**：版本、节点映射、输出键与计费费率必须作为完整条目一并登记在 `registry.json` 中（禁止仅通过 `.env` 单独替换 `workflow_id` 而沿用旧节点映射）。

在 `yc_h3/providers/chenyu_workflows/registry.json` 的 `"workflows"` 对象下新增一条完整记录即可生效：

```json
{
  "my-chenyu-wf": {
    "workflow_id": "wf_xxxxxxxxxxxxxxxxxxxx",
    "revision_id": "wfr_xxxxxxxxxxxxxxxxxxxx",
    "gpu_model": "5090",
    "tasks": ["r2v", "t2v"],
    "prompt_mode": "ref2va",
    "compute_cny_per_minute": 0.12,
    "creator_fee_cny": 0.03,
    "default_aspect": "16:9",
    "default_megapixels": 1.5,
    "min_megapixels": 0.2,
    "max_megapixels": 2.0,
    "min_seconds": 1,
    "max_seconds": 15,
    "aspect_options": {
      "16:9": "16:9 (Widescreen)",
      "9:16": "9:16 (Portrait Widescreen)",
      "1:1": "1:1 (Square)"
    },
    "known_16_9_sizes": {
      "1.5": [1664, 928]
    },
    "nodes": {
      "combat": "node_417_value",
      "upscale_2x": "node_460_value",
      "aspect_ratio": "node_252_aspect_ratio",
      "megapixels": "node_252_megapixels",
      "seconds": "node_464_value",
      "prompt": "node_263_text",
      "images": ["node_465_image", "node_466_image"],
      "audios": ["node_474_audio"]
    },
    "output_key": "result_364_gifs_1",
    "combat": false
  }
}
```

校验命令：
```bash
yc-h3 workflows --provider chenyu --json
yc-h3 preflight --provider chenyu --workflow my-chenyu-wf --image ./ref.png --out ./tmp.mp4 --prompt-file ./prompt.txt --json
```

---

## 三、新增受管 ComfyUI 工作流（Colab / 晨羽独占共用）

目录：`yc_h3/remote/workflows/minimax_h3/`

1. 在 `yc_h3/remote/workflows/minimax_h3/<wf_pkg>/` 下新建两个文件：
   - `manifest.json`：声明 `id`、`steps`、`required_nodes`、`plugins` 与 `plugin_nodes`（须保持一致且插件在 `yc_h3/remote/setup_env.py` 的 `CUSTOM_NODES` 白名单内）、`profile_id`、`comfyui_revision`、`output_kind`、`tasks`、`verification`（新工作流初始填 `unverified`）以及 `weights`（固定 HuggingFace `repo`、`revision`、`repo_path`、`comfy_rel_path`、`size_bytes`、`sha256`）。
   - `builder.py`：实现 `build_graph(...)` 返回标准 ComfyUI API 格式节点字典。
2. 在 `yc_h3/remote/workflows/minimax_h3/spec.py` 注册新工作流逻辑名，在 `yc_h3/remote/workflows/minimax_h3/registry.py` 注册对应 manifest 与 builder。
   - **跨平台红线**：`spec.py` 必须保持纯 Python、零 `fcntl` 导入，确保原生 Windows 下可正常执行本地工作流浏览与 `chenyu-comfy` 预检。
3. 更新 `pyproject.toml` 确认新工作流目录下的 `manifest.json` 等非 `.py` 文件已包含在构建包数据内。
4. 运行离线回归与打包核验：
   ```bash
   uv run pytest
   yc-h3 workflows --provider chenyu-comfy --json
   ```
