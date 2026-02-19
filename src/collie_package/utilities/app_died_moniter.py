import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from .. import state, log_class, tools
from ..config_loader import load_rules_config
from . import meminfo_summary

# 触发时执行的默认命令列表，按顺序执行，方便后续扩展（支持 rules.yaml）
_RULES = load_rules_config()
_MON_RULES = _RULES.get('app_died_monitor', {}) if isinstance(_RULES, dict) else {}

DEFAULT_TRIGGER_COMMANDS = list(_MON_RULES.get(
    'trigger_commands',
    [
        "adb shell dumpsys activity",
        "adb shell dumpsys meminfo",
        "adb shell cmd greezer getUids 9999",
    ],
))
DEFAULT_PERIODIC_DUMPSYS_COMMAND = _MON_RULES.get(
    'periodic_dumpsys_command',
    "adb shell dumpsys activity",
)
DEFAULT_PERIODIC_DUMPSYS_COUNT = int(_MON_RULES.get('periodic_dumpsys_count', 3))
DEFAULT_PERIODIC_DUMPSYS_INTERVAL = int(_MON_RULES.get('periodic_dumpsys_interval', 10))
DEFAULT_PERIODIC_COMMANDS = list(
    _MON_RULES.get('periodic_commands', DEFAULT_TRIGGER_COMMANDS)
)

# 预制监控配置，便于后续添加新的常用组合
FALLBACK_PRESET_MONITOR_TARGETS = list(_MON_RULES.get(
    'fallback_monitor_targets',
    [{"label": "Demo App", "packages": ["com.ss.android.ugc.aweme"]}],
))


def _load_preset_monitor_targets():
    """从 app_config.json 加载预设包名列表，缺失时回退默认。"""
    presets = []
    config = tools.load_default_config()
    if isinstance(config, dict):
        raw_presets = config.get("app_died_monitor_presets") or config.get("APP_DIED_MONITOR_PRESETS")
        if isinstance(raw_presets, dict):
            for label, pkgs in raw_presets.items():
                if isinstance(pkgs, list) and pkgs:
                    presets.append({"label": str(label), "packages": pkgs})

    if not presets:
        presets = FALLBACK_PRESET_MONITOR_TARGETS

    return presets

def _run_adb_command(command, timeout=10):
    """执行ADB命令并返回输出，支持超时"""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=True
        )
        return result.stdout, True
    except subprocess.TimeoutExpired:
        return "Command timed out", False


def _save_command_output(command, filename, max_retries=3, timeout=10):
    """执行命令并将输出保存到文件，支持超时重试"""
    retries = 0
    while retries < max_retries:
        output, success = _run_adb_command(command, timeout)
        if success:
            with open(filename, 'w') as f:
                f.write(output)
            return True
        retries += 1
        time.sleep(1)
    return False


def _run_and_save_command_output(command, filename, max_retries=3, timeout=10):
    """执行命令并将输出保存到文件，成功时返回输出内容"""
    retries = 0
    while retries < max_retries:
        output, success = _run_adb_command(command, timeout)
        if success:
            with open(filename, 'w') as f:
                f.write(output)
            return output, True
        retries += 1
        time.sleep(1)
    return "", False


def _command_suffix(command, index):
    """根据命令生成易读的文件后缀"""
    tail = command.strip().split()[-1] if command.strip() else ""
    tail = tail.replace('/', '_') or f"cmd{index + 1}"
    return tail


class ProcessMonitor:
    def __init__(self, package, command, output_dir="logs", interval=1, max_retries=3, timeout=60, duration=0):
        self.package = package
        # 兼容旧调用方式: command 可以是字符串或可迭代对象
        if isinstance(command, (list, tuple)):
            self.commands = list(command)
        else:
            self.commands = [command]
        self.output_dir = output_dir
        self.interval = interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.duration = duration

        self._monitor_thread = None
        self._stop_event = threading.Event()
        self._last_state = None

        os.makedirs(self.output_dir, exist_ok=True)

    def run_adb_command(self, command, timeout=10):
        return _run_adb_command(command, timeout)

    def is_process_alive(self, package_name, max_retries=3):
        """检查指定包名的进程是否存活，支持重试"""
        retries = 0
        while retries < max_retries:
            output, success = self.run_adb_command(f"adb shell pidof {package_name}")
            if success and output.strip():
                return True
            retries += 1
            time.sleep(1)
        return False

    def save_command_output(self, command, filename, max_retries=3, timeout=10):
        return _save_command_output(command, filename, max_retries, timeout)

    def _command_suffix(self, command, index):
        return _command_suffix(command, index)


def _prompt_interval_seconds():
    interval_input = input(f"请输入 dumpsys 间隔(秒) (默认: {DEFAULT_PERIODIC_DUMPSYS_INTERVAL}): ").strip()
    try:
        interval = float(interval_input) if interval_input else DEFAULT_PERIODIC_DUMPSYS_INTERVAL
        if interval <= 0:
            raise ValueError
    except ValueError:
        print(f"输入非法，使用默认 {DEFAULT_PERIODIC_DUMPSYS_INTERVAL}s 间隔")
        interval = DEFAULT_PERIODIC_DUMPSYS_INTERVAL
    return interval


def _prompt_dumpsys_count():
    count_input = input(f"请输入 dumpsys 次数 (默认: {DEFAULT_PERIODIC_DUMPSYS_COUNT}): ").strip()
    try:
        count = int(count_input) if count_input else DEFAULT_PERIODIC_DUMPSYS_COUNT
        if count <= 0:
            raise ValueError
    except ValueError:
        print(f"输入非法，使用默认 {DEFAULT_PERIODIC_DUMPSYS_COUNT} 次")
        count = DEFAULT_PERIODIC_DUMPSYS_COUNT
    return count


def _prompt_start_immediately():
    start_now = input("是否立即抓取第一次 dumpsys? (y/N): ").strip().lower()
    return start_now in {"y", "yes", "1", "是"}


def periodic_dumpsys_main():
    commands = list(DEFAULT_PERIODIC_COMMANDS)
    interval = _prompt_interval_seconds()
    count = _prompt_dumpsys_count()
    start_immediately = _prompt_start_immediately()

    os.makedirs(state.FILE_DIR, exist_ok=True)
    logcat_recorder = log_class.LogcatRecorder()
    logcat_recorder.start()

    print("即将抓取:")
    for cmd in commands:
        print(f"- {cmd}")
    print(f"次数: {count}, 间隔: {interval}s, {'立即抓取' if start_immediately else '首次等待间隔后抓取'}")

    try:
        for idx in range(count):
            if idx == 0:
                if not start_immediately:
                    print(f"等待 {interval}s 后抓取第 1 次...")
                    time.sleep(interval)
                else:
                    print("立即抓取第 1 次...")
            else:
                print(f"等待 {interval}s 后抓取第 {idx + 1} 次...")
                time.sleep(interval)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            for cmd_index, cmd in enumerate(commands):
                suffix = _command_suffix(cmd, cmd_index)
                filename = os.path.join(
                    state.FILE_DIR,
                    f"dumpsys_{suffix}_{timestamp}_{idx + 1}.txt"
                )

                output, ok = _run_and_save_command_output(cmd, filename)
                if ok:
                    print(f"抓取成功，已保存: {filename}")
                    if "dumpsys meminfo" in cmd:
                        report = meminfo_summary.generate_report(
                            output,
                            source_desc=f"{cmd} (定时抓取第 {idx + 1} 次)"
                        )
                        report_name = os.path.join(
                            state.FILE_DIR,
                            f"meminfo_summary_{timestamp}_{idx + 1}.txt"
                        )
                        with open(report_name, "w", encoding="utf-8") as f:
                            f.write(report)
                        print(f"meminfo 解析已保存: {report_name}")
                else:
                    print(f"抓取失败（已重试）：{cmd}")
    except KeyboardInterrupt:
        print("\n已取消 dumpsys 定时抓取。")
    finally:
        logcat_recorder.stop()

    def _monitor_loop(self):
        end_time = time.time() + (self.duration * 60) if self.duration > 0 else float('inf')

        print(f"Starting monitoring for {self.package}, interval={self.interval}s, duration={'∞' if self.duration==0 else f'{self.duration}min'}")

        while time.time() < end_time and not self._stop_event.is_set():
            current_state = self.is_process_alive(self.package, self.max_retries)
            status = "alive ✅" if current_state else "dead ❌"
            now = datetime.now().strftime('%H:%M:%S')

            # 单行刷新
            sys.stdout.write(f"\r[{now}] Package {self.package} status: {status}")
            sys.stdout.flush()

            # 状态变化为死亡时才触发命令
            if self._last_state is True and current_state is False:
                print(f"\nProcess {self.package} just died! Executing command...")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                for idx, cmd in enumerate(self.commands):
                    suffix = self._command_suffix(cmd, idx)
                    filename = os.path.join(self.output_dir, f"{self.package}_{timestamp}_{suffix}.txt")

                    if self.save_command_output(cmd, filename, self.max_retries, self.timeout):
                        print(f"Command executed successfully: '{cmd}'. Output saved to: {filename}")
                    else:
                        print(f"Failed to execute command after {self.max_retries} retries: '{cmd}'")

            self._last_state = current_state
            time.sleep(self.interval)

        print("\nMonitoring stopped.")

    def start(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            print("Monitoring is already running.")
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join()
        print("Monitoring fully stopped.")


def main():
    # 选择预制项或自定义
    presets = _load_preset_monitor_targets()

    print("可选预制监控目标:")
    for idx, preset in enumerate(presets, start=1):
        print(f"[{idx}] {preset['label']} -> {', '.join(preset['packages'])}")
    print("[0] 自定义输入包名（可用逗号分隔，最多2个）")

    preset_choice = input("请选择预制项编号 (默认: 0): ").strip() or "0"

    packages = []
    if preset_choice.isdigit() and 1 <= int(preset_choice) <= len(presets):
        packages = presets[int(preset_choice) - 1]["packages"]
    else:
        raw_input_packages = input("请输入 Android 包名（可用逗号分隔，最多2个，默认: com.example.app）: ").strip()
        if raw_input_packages:
            packages = [p.strip() for p in raw_input_packages.split(",") if p.strip()]
        if not packages:
            packages = ["com.example.app"]

    # 最多监控2个
    packages = packages[:2]

    interval_input = input("请输入轮询间隔(秒) (默认: 1): ").strip()
    try:
        interval = float(interval_input) if interval_input else 1
    except ValueError:
        print("输入非法，使用默认 1s 间隔")
        interval = 1

    # 启动logcat记录
    logcat_recorder = log_class.LogcatRecorder()
    logcat_recorder.start()

    # 启动监控（最多2个）
    monitors = []
    for pkg in packages:
        monitor = ProcessMonitor(
            pkg,
            command=DEFAULT_TRIGGER_COMMANDS,
            output_dir=state.FILE_DIR,
            interval=interval,
            duration=0,
        )
        monitor.start()
        monitors.append(monitor)

    input("按任意键结束监控;")

    for monitor in monitors:
        monitor.stop()
    logcat_recorder.stop()


if __name__ == "__main__":
    main()
