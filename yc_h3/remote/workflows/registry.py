"""Public workflow catalog used by the daemon and standalone Colab workers."""
from .minimax_h3 import registry as h3
from .minimax_h3.registry import (
    H3_FAMILY, WORKFLOWS, for_model, get_workflow, load_workflows,
)
