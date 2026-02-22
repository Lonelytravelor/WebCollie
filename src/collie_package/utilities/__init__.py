"""常用实用工具分组，对应 CLI 实用工具菜单。"""

from importlib import import_module
from typing import Any

_MODULES = {
    'app_died_moniter',
    'app_install',
    'check_app',
    'check_app_alive',
    'collect_device_meminfo',
    'compare_android_mem_design',
    'complie_and_prepare_app',
    'killinfo_line_parser',
    'meminfo_summary',
    'simpleperf',
}

__all__ = sorted(_MODULES)


def __getattr__(name: str) -> Any:
    if name in _MODULES:
        module = import_module(f'.{name}', __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
