"""Built-in pure ComfyUI model catalog and MiniMax-H3 prompt helpers.

Adapted from WorldForge (MIT License); all non-ComfyUI models (Lyra 2.0,
Diffusers, Qwen-Image, Breeze-TTS) and Colab image/upscale workflows have been removed.
"""

import re

DEFAULT_MODELS = [
    {
        "id": "minimax-h3",
        "repo": "Comfy-Org/MiniMax-H3",
        "name": "MiniMax-H3（音视频同步）",
        "kind": "t2v",
        "engine": "minimax_h3",
        "builtin": True,
        "size_gb": 0,
        "vram_gb": 24,
        "recommended_gpu": "G4 / H100 / A100 / L4",
        "storage_hint": "ComfyUI 实例盘",
        "description": "MiniMax-H3 工作流，24fps 音视频同生",
        "defaults": {"fps": 24, "seconds": 5},
    },
]

H3_CINEMA_OPTICS = {
    "iwai_85mm_f12": "85mm at f/1.2, shallow depth of field, soft diffusion, warm backlighting and creamy bokeh",
    "wong_kar_wai_anamorphic": "35mm anamorphic optics, oval bokeh, horizontal lens flares and soft halation",
    "deakins_imax_65mm": "65mm film format, 50mm at T1.9, volumetric light and soft highlights",
    "leica_noctilux_f095": "50mm at f/0.95, shallow focus, swirly bokeh and luminous catchlights",
    "shinkai_twilight_panavision": "24mm at T1.4, twilight backlighting, lens reflections and luminous clouds",
}


_MUSIC_SECTION = re.compile(r"^[ \t]*non_diegetic_music[ \t]*:", re.I | re.M)


def apply_no_music_policy(prompt: str, no_music: bool) -> str:
    if not no_music:
        return prompt
    sections = _MUSIC_SECTION.split(prompt)
    if len(sections) == 1:
        return prompt + "\nnon_diegetic_music: N/A"
    if any(content.strip().casefold() != "n/a" for content in sections[1:]):
        raise ValueError("no_music=true 与提示词中显式 non_diegetic_music 配乐描述冲突")
    return prompt


def h3_prompt(text, params):
    no_music = params.get("no_music", False)
    text = apply_no_music_policy(text, no_music)
    optics = H3_CINEMA_OPTICS.get(params.get("cinema_camera"))
    if optics:
        music = _MUSIC_SECTION.search(text) if no_music else None
        if music:
            text = text[:music.start()] + optics + "\n" + text[music.start():]
        else:
            text += "\n" + optics
    return text
