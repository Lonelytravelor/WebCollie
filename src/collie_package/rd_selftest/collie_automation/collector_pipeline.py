from typing import Callable, Dict, List, Optional, Tuple

from .. import tools
from .log_collectors import (
    FtraceCollector,
    GreclaimParmCollector,
    LogcatCollector,
    MemcatCollector,
    MeminfoCollector,
    OomadjCollector,
    ProcessUseCountCollector,
    VmstatCollector,
    start_collectors,
    stop_collectors,
)


def build_collectors(package_list: List[str], timestamp: str, device_id: str = ""):
    """根据配置生成采集器列表。"""
    collectors = []
    if tools.get_log_setting("logcat"):
        collectors.append(LogcatCollector(device_id))
    if tools.get_log_setting("memcat"):
        collectors.append(MemcatCollector(timestamp, device_id=device_id))
    if tools.get_log_setting("meminfo"):
        collectors.append(MeminfoCollector(timestamp, device_id=device_id))
    if tools.get_log_setting("vmstat"):
        collectors.append(VmstatCollector(timestamp, device_id=device_id))
    if tools.get_log_setting("greclaim_parm"):
        collectors.append(GreclaimParmCollector(timestamp, device_id=device_id))
    if tools.get_log_setting("process_use_count"):
        collectors.append(ProcessUseCountCollector(timestamp, device_id=device_id))
    if tools.get_log_setting("oomadj"):
        collectors.append(OomadjCollector(package_list, timestamp))
    if tools.get_log_setting("ftrace"):
        collectors.append(FtraceCollector(timestamp))
    return collectors


def run_with_collectors(
    collectors,
    workload: Callable[
        [], Tuple[Dict[str, Optional[int]], Dict[str, Optional[int]]]
    ],
):
    """启动采集器并执行工作负载，保证停止采集器。"""
    started = False
    try:
        start_collectors(collectors)
        started = True
        return workload()
    finally:
        if started:
            stop_collectors(collectors)
