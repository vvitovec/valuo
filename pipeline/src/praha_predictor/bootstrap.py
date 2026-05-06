from __future__ import annotations

import os
import sys
import types
from typing import Any


def prepare_runtime() -> None:
    if os.environ.get("HOUSESPREDICT_DISABLE_INTERPRET_VISUAL") != "1":
        return
    if "interpret.visual._interactive" in sys.modules:
        return

    module = types.ModuleType("interpret.visual._interactive")

    def _no_op(*args: Any, **kwargs: Any) -> None:
        return None

    module.get_show_addr = _no_op
    module.get_visualize_provider = _no_op
    module.init_show_server = _no_op
    module.preserve = _no_op
    module.set_show_addr = _no_op
    module.set_visualize_provider = _no_op
    module.show = _no_op
    module.show_link = _no_op
    module.shutdown_show_server = lambda: True
    module.status_show_server = lambda: {"app_runner_exists": False}
    sys.modules[module.__name__] = module
