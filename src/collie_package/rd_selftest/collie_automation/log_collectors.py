"""可复用的采集器模块，用于按需组合不同日志/数据抓取任务。"""

import os
import subprocess
import time
import threading
from typing import Iterable, List

from .. import log_class, state
from ..memory_models import dump_mem
try:
    from memcat import MemcatTask
except Exception:  # noqa: BLE001
    MemcatTask = None


class BaseCollector:
    """统一接口，子类实现 start/stop。"""

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class LogcatCollector(BaseCollector):
    def __init__(self, device_id: str = ""):
        self.recorder = None
        self.device_id = device_id

    def start(self):
        self.recorder = log_class.LogcatRecorder(device_id=self.device_id)
        self.recorder.start()
        time.sleep(1)  # 等待 logcat 稳定

    def stop(self):
        if self.recorder:
            self.recorder.stop()


class MemcatCollector(BaseCollector):
    def __init__(self, timestamp: str, device_id: str = ''):
        self.timestamp = timestamp
        self.memcat_task = None
        self.device_id = device_id

    def start(self):
        if MemcatTask is None:
            raise RuntimeError('memcat 未安装，无法启用 memcat 采集')
        output_file = os.path.join(state.FILE_DIR, "memcat.txt")
        self.memcat_task = MemcatTask(sample_period=[1, 1000], outfile=output_file)
        self.memcat_task.start_capture()
        time.sleep(1)

    def stop(self):
        if self.memcat_task:
            self.memcat_task.stop_capture()
            print("🔴 Memcat记录已停止")


class MeminfoCollector(BaseCollector):
    def __init__(self, timestamp: str, device_id: str = ''):
        self.timestamp = timestamp
        self.meminfo_file = os.path.join(state.FILE_DIR, f"meminfo{self.timestamp}.txt")
        self.device_id = device_id

    def start(self):
        meminfo_output = dump_mem.get_meminfo(device_id=self.device_id)
            with open(self.meminfo_file, "a", encoding="utf-8") as f:
            f.write(f"测试前 - \n{'='*50}\n")
            f.write(meminfo_output + "\n")

    def stop(self):
        meminfo_output = dump_mem.get_meminfo(device_id=self.device_id)
            with open(self.meminfo_file, "a", encoding="utf-8") as f:
            f.write(f"\n测试后 - \n{'='*50}\n")
            f.write(meminfo_output + "\n")
        print("🔴 Meminfo记录已停止")


class VmstatCollector(BaseCollector):
    def __init__(self, timestamp: str, device_id: str = ''):
        self.timestamp = timestamp
        self.vmstat_file = os.path.join(state.FILE_DIR, f"vmstat{self.timestamp}.txt")
        self.device_id = device_id

    def start(self):
        vmstat_output = dump_mem.get_vmstat(device_id=self.device_id)
            with open(self.vmstat_file, "a", encoding="utf-8") as f:
            f.write(f"测试前 - \n{'='*50}\n")
            f.write(vmstat_output + "\n")

    def stop(self):
        vmstat_output = dump_mem.get_vmstat(device_id=self.device_id)
            with open(self.vmstat_file, "a", encoding="utf-8") as f:
            f.write(f"\n测试后 - \n{'='*50}\n")
            f.write(vmstat_output + "\n")
        print("🔴 Vmstat记录已停止")


class GreclaimParmCollector(BaseCollector):
    """记录 greclaim 参数节点（测试前后各一次）。"""

    def __init__(self, timestamp: str, device_id: str = ''):
        self.timestamp = timestamp
        self.output_file = os.path.join(state.FILE_DIR, f"greclaim_parm{self.timestamp}.txt")
        self.device_id = device_id

    def start(self):
        output = _capture_adb_shell('cat /sys/kernel/mi_reclaim/greclaim_parm', device_id=self.device_id)
            with open(self.output_file, "a", encoding="utf-8") as f:
            f.write(f"测试前 - \n{'='*50}\n")
            f.write(output + "\n")

    def stop(self):
        output = _capture_adb_shell('cat /sys/kernel/mi_reclaim/greclaim_parm', device_id=self.device_id)
            with open(self.output_file, "a", encoding="utf-8") as f:
            f.write(f"\n测试后 - \n{'='*50}\n")
            f.write(output + "\n")
        print("🔴 Greclaim参数记录已停止")


class ProcessUseCountCollector(BaseCollector):
    """记录 process_use_count 节点（测试前后各一次）。"""

    def __init__(self, timestamp: str, device_id: str = ''):
        self.timestamp = timestamp
        self.output_file = os.path.join(state.FILE_DIR, f"process_use_count{self.timestamp}.txt")
        self.device_id = device_id

    def start(self):
        output = _capture_adb_shell('cat /sys/kernel/mi_mempool/process_use_count', device_id=self.device_id)
            with open(self.output_file, "a", encoding="utf-8") as f:
            f.write(f"测试前 - \n{'='*50}\n")
            f.write(output + "\n")

    def stop(self):
        output = _capture_adb_shell('cat /sys/kernel/mi_mempool/process_use_count', device_id=self.device_id)
            with open(self.output_file, "a", encoding="utf-8") as f:
            f.write(f"\n测试后 - \n{'='*50}\n")
            f.write(output + "\n")
        print("🔴 process_use_count记录已停止")


class OomadjCollector(BaseCollector):
    def __init__(self, package_list: List[str], timestamp: str):
        self.package_list = package_list
        self.timestamp = timestamp
        self.monitor = None
        self.oomadj_file = os.path.join(state.FILE_DIR, f"oomadj_{self.timestamp}.csv")

    def start(self):
        self.monitor = log_class.OOMAdjLogger(self.package_list, self.oomadj_file)
        self.monitor.start()

    def stop(self):
        if not self.monitor:
            return
        self.monitor.stop()
        oomadj_summary_file = os.path.join(
            state.FILE_DIR, f"oomadj_summary_report_{self.timestamp}.txt"
        )
        oomadj_analysis_file = os.path.join(
            state.FILE_DIR, f"oomadj_analysis_plots_{self.timestamp}.png"
        )
        log_class.analyze_oomadj_csv(
            self.oomadj_file, oomadj_summary_file, oomadj_analysis_file
        )
        print("🔴 Oomadj记录已停止")


class FtraceCollector(BaseCollector):
    """抓取特定 mm_vmscan 事件的 ftrace，可选是否按 direct reclaim 活跃计数开关 sched_switch，减少日志量。需要 root 权限。"""

    EVENTS = [
        "mm_vmscan_direct_reclaim_begin",
        "mm_vmscan_direct_reclaim_end",
        "mm_vmscan_kswapd_sleep",
        "mm_vmscan_kswapd_wake",
        "mm_vmscan_wakeup_kswapd",
    ]

    def __init__(self, timestamp: str, include_sched_switch: bool = False):
        self.timestamp = timestamp
        self.output_file = os.path.join(state.FILE_DIR, f"ftrace_{self.timestamp}.txt")
        self.process = None
        self._enabled = False
        self._outfile = None
        self._thread = None
        self._stop_event = threading.Event()
        self._reclaim_depth = 0
        # When False, only vmscan events are traced; sched_switch toggling is skipped
        self._include_sched_switch = include_sched_switch

    def _run_shell(self, cmd: str):
        subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )

    def _enable_sched_switch(self):
        if self._include_sched_switch:
            self._run_shell("adb shell 'echo 1 > /sys/kernel/tracing/events/sched/sched_switch/enable'")

    def _disable_sched_switch(self):
        # Always attempt to turn it off to avoid遗留开启状态
        self._run_shell("adb shell 'echo 0 > /sys/kernel/tracing/events/sched/sched_switch/enable'")

    def _enable_events(self):
        # 保证 sched_switch 初始为关闭状态，避免全量记录
        self._disable_sched_switch()

        for event in self.EVENTS:
            self._run_shell(
                f"adb shell 'echo 1 > /sys/kernel/tracing/events/vmscan/{event}/enable'"
            )
        self._run_shell("adb shell 'echo 1 > /sys/kernel/tracing/tracing_on'")
        self._enabled = True

    def _disable_events(self):
        self._run_shell("adb shell 'echo 0 > /sys/kernel/tracing/tracing_on'")
        for event in self.EVENTS:
            self._run_shell(
                f"adb shell 'echo 0 > /sys/kernel/tracing/events/vmscan/{event}/enable'"
            )
        # 关闭 sched_switch
        self._disable_sched_switch()
        self._enabled = False

    def start(self):
        self._enable_events()
        # 通过 trace_pipe 持续读取，同时监测 direct reclaim 深度以切换 sched_switch
        self._outfile = open(self.output_file, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["adb", "shell", "cat", "/sys/kernel/tracing/trace_pipe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._stop_event.clear()

        def _loop():
            for line in self.process.stdout:
                if self._stop_event.is_set():
                    break
                self._outfile.write(line)
                if self._include_sched_switch and "mm_vmscan_direct_reclaim_begin" in line:
                    self._reclaim_depth += 1
                    if self._reclaim_depth == 1:
                        self._enable_sched_switch()
                elif self._include_sched_switch and "mm_vmscan_direct_reclaim_end" in line:
                    if self._reclaim_depth > 0:
                        self._reclaim_depth -= 1
                    if self._reclaim_depth == 0:
                        self._disable_sched_switch()
            try:
                self.process.stdout.close()
            except Exception:
                pass

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        time.sleep(0.5)

    def stop(self):
        self._stop_event.set()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._outfile and not self._outfile.closed:
            self._outfile.close()
        self._reclaim_depth = 0
        if self._enabled:
            self._disable_events()


def start_collectors(collectors: Iterable[BaseCollector]):
    """批量启动采集器，若中途失败则回滚已启动项。"""
    started: list[BaseCollector] = []
    for collector in collectors:
        try:
            collector.start()
            started.append(collector)
        except Exception as exc:  # noqa: BLE001
            if started:
                stop_collectors(started)
            raise RuntimeError(f'启动采集器失败: {collector.__class__.__name__}: {exc}') from exc


def stop_collectors(collectors: Iterable[BaseCollector]):
    """逆序停止采集器，确保先启动的后停止。

    加入超时保护，防止个别采集器停止卡住主流程。
    """
    def _stop_with_timeout(collector: BaseCollector, timeout: float = 20.0):
        name = collector.__class__.__name__
        print(f"正在停止 {name} ...")

        result: dict = {"err": None}

        def runner():
            try:
                collector.stop()
            except Exception as exc:  # noqa: BLE001
                result["err"] = exc

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive():
            print(f"⚠️ 停止 {name} 超过 {timeout:.0f}s，跳过等待继续处理后续任务。")
        elif result["err"]:
            print(f"⚠️ 停止 {name} 出错: {result['err']}")
        else:
            print(f"{name} 已停止。")

    for collector in reversed(list(collectors)):
        _stop_with_timeout(collector)


def _capture_adb_shell(shell_cmd: str, timeout: float = 10.0, device_id: str = '') -> str:
    """执行 adb shell 命令并返回 stdout；失败时返回错误描述。"""
    adb_cmd = ['adb']
    if device_id:
        adb_cmd.extend(['-s', device_id])
    adb_cmd.extend(['shell', shell_cmd])
    try:
        result = subprocess.run(
            adb_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return f"命令失败({result.returncode}): {result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"命令超时({timeout}s)"
