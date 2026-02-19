"""预处理步骤的通用封装（root、节点设置等）。"""

from typing import Optional

from .. import tools
from ..config_loader import load_rules_config
import time

# 在这里填入测试前需要执行的命令字符串，例如 "adb shell setprop foo bar"
_RULES = load_rules_config()
_PRE_RULES = _RULES.get('pre_start', {}) if isinstance(_RULES, dict) else {}

PRE_START_COMMANDS = list(_PRE_RULES.get(
    'commands',
    [
        # 先清空日志缓冲区，避免历史 logcat/dmesg 干扰驻留解析
        "adb logcat -b all -c",
        "adb shell dmesg -C",
        "adb root",
        "adb shell 'echo 7 > /sys/kernel/mi_mempool/config'",
        "adb shell 'echo 2 > /sys/kernel/mem_limit/debug'",
        "adb shell 'echo 63 > /sys/kernel/mi_reclaim/greclaim_enable'",
        "adb shell setprop persist.sys.miui.integrated.memory.debug.enable true",
        "adb shell setprop debug.sys.spc true",
        "adb shell stop",
        "adb shell start",
    ],
))
POST_START_COMMANDS = list(_PRE_RULES.get(
    'post_commands',
    [
        "adb shell settings put system mmperf stat",
        "adb shell settings put system mmperf trace",
    ],
))
POST_START_DELAY_SEC = int(_PRE_RULES.get('post_delay_sec', 10))


def run_pre_start(device_id: Optional[str] = None) -> None:
    """执行预处理命令。"""
    tools.run_pre_start_commands(PRE_START_COMMANDS, device_id=device_id or "")
    # adb shell stop/start 后，延迟执行设置和日志开关，避免框架启动阶段失败
    time.sleep(POST_START_DELAY_SEC)
    tools.run_pre_start_commands(POST_START_COMMANDS, device_id=device_id or "")
    tools.oomadj_log_enable(device_id=device_id or "")
