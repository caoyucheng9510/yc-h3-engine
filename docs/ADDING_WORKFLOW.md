# 扩展自定义工作流开发指南

`yc-h3-engine` 1.0 内置了 `jinyu-hybrid`（文戏）与 `jinyu-combat`（武打）两套工作流，三个工作流目录均采用声明式注册表设计，方便有开发能力的群友接入自己的 RunningHub、晨羽或受管 ComfyUI 工作流。

---

## 一、新增 RunningHub 工作流

目录：`yc_h3/providers/runninghub_workflows/`

1. **导出并清洗节点图**：
   - 在 RunningHub 工作流页面导出 ComfyUI API 格式 JSON，放入 `yc_h3/providers/runninghub_workflows/<name>.json`；
   - 移除纯 UI 展示/预览节点，清空示例占位文件名（`LoadImage`/`LoadAudio` 改为 `"None"`、`VHS_LoadVideo` 改为 `""`）与示例提示词。
2. **合并进 `registry.json` 的 `"workflows"` 对象**：
   - 将 `workflow_id` 设为 `null`（不把个人账号副本 ID 写死进仓库）。
   - **环境变量说明**：当前 `yc_h3/providers/runninghub.py` 的 `_workflow()` 仅对内置 `jinyu-hybrid` / `jinyu-combat` 识别独立变量 `RUNNINGHUB_WORKFLOW_ID_HYBRID` / `RUNNINGHUB_WORKFLOW_ID_COMBAT`；新增自定义条目默认读取通用 `RUNNINGHUB_WORKFLOW_ID`（若需为自定义条目配置专属变量，须同步扩展 `_workflow()` 中的映射）。
   - 未做单样本积分校准的新条目须显式设置 `"estimate_calibrated_for_point": false`（若设为 `true`，须一并提供 `run_seconds_per_output_second` 与 `reference_coins_per_run_second`）。

将下列条目**合并写入** `yc_h3/providers/runninghub_workflows/registry.json` 的 `"workflows"` 对象内：

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
    "estimate_calibrated_for_point": false,
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

3. **校验目录与实际构图**：
   先准备一份合规 T2VA 提示词文件 `t2va_test.txt`（使校验通过提示词检查并真正进入节点构图与字段签名验证）：
   ```text
   integrated_multimodal_description:
   [Shot 1] Wide shot of a quiet rain-soaked alley at dusk, warm lantern light reflecting on wet stone.

   overall_soundscape:
   Steady soft rainfall and distant muffled street ambience.

   non_diegetic_music:
   N/A
   ```
   然后运行校验：
   ```bash
   yc-h3 workflows --provider runninghub --json
   yc-h3 preflight --provider runninghub --workflow my-custom-rh --task t2v \
     --out ./tmp.mp4 --prompt-file ./t2va_test.txt --json
   ```

---

## 二、新增晨羽按次工作流

目录：`yc_h3/providers/chenyu_workflows/`

晨羽 `catalog.py` 采用**按条目整体验证**：版本、节点映射、输出键与计费费率必须作为完整条目一并合并写入 `registry.json` 的 `"workflows"` 对象中（禁止仅通过 `.env` 单独替换 `workflow_id` 而沿用旧节点映射）：

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

校验命令（使用合规 `t2va` 或带素材的 `ref2va` 提示词文件）：
```bash
yc-h3 workflows --provider chenyu --json
yc-h3 preflight --provider chenyu --workflow my-chenyu-wf --task t2v \
  --out ./tmp.mp4 --prompt-file ./t2va_test.txt --json
```

---

## 三、新增受管 ComfyUI 工作流（Colab / 晨羽独占共用）

目录：`yc_h3/remote/workflows/minimax_h3/`

远端 `registry.py` 会**自动扫描** `yc_h3/remote/workflows/minimax_h3/*/manifest.json`（包目录名使用下划线，如 `my_custom_wf/` 对应逻辑名 `my-custom-wf`），但 CLI 与本地路由仍需在 `spec.py` 登记逻辑名。

1. **创建包目录 `yc_h3/remote/workflows/minimax_h3/my_custom_wf/`**：
   - `manifest.json`（`verification` 必须是按卡型映射的字典；每条 `weights` 必须包含唯一 `id` 以及默认配方引用的权重别名，如 `hybrid_b25_49`、`heretic`、`video_vae_int8`、`audio_vae_fp32` 等）：
     ```json
     {
       "id": "my-custom-wf",
       "steps": 12,
       "required_nodes": ["UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3ReferenceToVideo", "CreateVideo", "SaveVideo"],
       "plugins": ["ComfyUI-KJNodes", "comfyui-SelfLift"],
       "plugin_nodes": {
         "ComfyUI-KJNodes": "MiniMaxChunkFeedForward",
         "comfyui-SelfLift": "SelfLiftH3Sampler"
       },
       "profile_id": "comfy-main",
       "comfyui_revision": "c59e67bd5a2757ddba1c6c6a993c0b6c811b54da",
       "output_kind": "video",
       "tasks": ["t2v", "i2v", "r2v"],
       "verification": {
         "G4-96": "unverified",
         "H100-80": "unverified",
         "A100-80": "unverified",
         "A100-40": "unverified",
         "L4-24": "unverified",
         "T4-16": "unverified"
       },
       "weights": [
         {
           "id": "hybrid_b25_49",
           "repo": "smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models",
           "revision": "6328c7dc34369783dd6a224b366aeb698e552e19",
           "repo_path": "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
           "comfy_rel_path": "diffusion_models/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
           "size_bytes": 20970379632,
           "sha256": "a629cfea8d89a071b140c6e1935dc9a23e72de6badc18975a2bb9e6d1423d76d"
         }
       ]
     }
     ```
   - `builder.py`（必须实现远端 `registry.py` 实际调用的 **`build(params, profile, prefix)`** 签名，可参考 `jinyu_hybrid/builder.py`）：
     ```python
     from ..registry import build_jinyu_graph

     def build(params, profile, prefix):
         return build_jinyu_graph(params, profile, prefix, combat=False)
     ```
2. **同步本地逻辑名与打包规则**：
   - 将 `"my-custom-wf"` 加入 `yc_h3/remote/workflows/minimax_h3/spec.py` 的 `JINYU_WORKFLOWS`（**跨平台红线**：`spec.py` 保持纯 Python、零 `fcntl` 导入，确保原生 Windows 可正常浏览工作流与跑 `chenyu-comfy`）；
   - 核对 `pyproject.toml` 确保新目录的 `manifest.json` 已覆盖在包数据匹配规则内。
3. **安装开发依赖并验证实际构图与全量回归**：
   ```bash
   uv sync --extra dev
   uv run pytest
   uv run python -c "from yc_h3.remote.workflows.minimax_h3 import registry, spec; wf = registry.get_workflow('my-custom-wf'); g = wf.build({'workflow':'my-custom-wf','task':'t2v','prompt':'test','seconds':5}, spec.resolve_hardware_profile(96, 176, 'G4'), 'smoke'); print('nodes:', len(g))"
   yc-h3 workflows --provider chenyu-comfy --json
   ```
