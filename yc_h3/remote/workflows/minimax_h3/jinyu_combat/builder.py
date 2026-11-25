"""Jinyu hybrid with the combat LoRA chain."""
from ..jinyu_hybrid.builder import build_jinyu


def build(params, profile, job_id):
    return build_jinyu("jinyu-combat", params, profile, job_id, combat=True)
