"""Jinyu FL2VA/REF2VA hybrid with native reference loaders."""
from ..registry import get_workflow, Graph, assets_for, output_frames


def build(params, profile, job_id):
    return build_jinyu("jinyu-hybrid", params, profile, job_id)


def build_jinyu(workflow_id, params, profile, job_id, *, combat=False):
    assets = assets_for(workflow_id, profile, params)
    workflow = get_workflow(workflow_id)
    graph = Graph(assets)
    text_key = "heretic" if "heretic" in assets else "text_enc_nvfp4"
    model, clip, vae, audio_vae = graph.loaders("hybrid_b25_49", text_key)
    loras = [("lora_dmad", 0.8), ("lora_mystic_v1", 0.35)]
    if combat:
        loras.extend((("combat_v2", 0.65), ("fight_flow_v1", 0.35), ("motion_repair_v2", 0.35)))
    if params.get("speed_slider"):
        loras.append(("speed_slider", 3.0))
    for key, strength in loras:
        model = graph.lora(model, key, strength)
    model = graph.custom_loras(model, params)
    if params.get("sparse_attention", True):
        model = graph.sparse(model)
    model = graph.add("MiniMaxH3SigmaShift", model=model, shift_video=12.0, shift_audio=3.0)
    model = graph.add("MiniMaxChunkFeedForward", model=model, chunks=2, seq_threshold=4096)

    images = params["images"]
    prompt = params["prompt"]
    if params.get("image_path"):
        images = [{"path": params["image_path"]}]
        if "<Picture 1>" not in prompt:
            prompt = "<Picture 1>\n" + prompt if params.get("no_music") else prompt + "\n<Picture 1>"
    references = {}
    for index, item in enumerate(images):
        references[f"ref_images.ref_image_{index}"] = graph.add("LoadImage", image=item["path"])
    for index, item in enumerate(params["videos"]):
        loader = graph.add("LoadVideo", file=item["path"])
        split = graph.add("GetVideoComponents", video=loader)
        references[f"ref_videos.ref_video_{index}"] = split
        if item["use_audio"]:
            references[f"ref_video_audios.ref_video_audio_{index}"] = [split[0], 1]
    for index, item in enumerate(params["audios"]):
        references[f"ref_audios.ref_audio_{index}"] = graph.add("LoadAudio", audio=item["path"])
    positive = graph.add("MiniMaxH3ReferenceToVideo", clip=clip, vae=vae, audio_vae=audio_vae, prompt=prompt,
                         width=params["width"], height=params["height"], length=output_frames(params),
                         ref_image_size="match", **references)
    latent = [positive[0], 1]
    sampler = graph.add("KSamplerSelect", sampler_name="euler")
    sigmas = graph.add("BasicScheduler", model=model, scheduler=params.get("scheduler", "simple"), steps=workflow.steps(params, assets), denoise=1.0)
    seed = int(params.get("seed", 0))
    if params.get("use_selflift", True):
        negative = graph.add("ConditioningZeroOut", conditioning=positive)
        samples = graph.add("SelfLiftH3Sampler", model=model, positive=positive, negative=negative, vae=vae,
                            latent_image=latent, sampler=sampler, sigmas=sigmas, seed=seed, cfg=1.0,
                            transition_step=6, lowres_scale=0.6, rho=0.0, w_min=0.5, w_max=1.0,
                            upscaler_model=graph.model_file("selflift_lms"),
                            highres_tiling=False, upscaler_unload=True)
    else:
        samples = graph.sample(model, positive, latent, sampler, sigmas, seed)
    return graph.save(samples, vae, audio_vae, job_id, tiled_vae=False)
