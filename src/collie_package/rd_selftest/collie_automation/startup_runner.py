import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class LaunchResidencyRecord:
    package: str
    round_no: int
    order_in_round: int
    global_order: int
    pid: Optional[int]
    success: bool
    alive_before: List[str]
    alive_after_count: int
    prev_alive_stats: Dict[int, Dict[str, object]]


class AppLaunchRunner:
    """封装应用启动与 PID 采集逻辑，便于在不同模式中复用。"""

    def __init__(self, packages: Iterable[str], device_id: str = "", app_wait: int = 4):
        self.packages = list(packages)
        self.device_id = device_id or ""
        self.app_wait = app_wait
        self.alive: Dict[str, int] = {}
        self.alive_counts: List[int] = []
        self.launch_records: List[LaunchResidencyRecord] = []
        self.launch_sequence: List[str] = []
        self._global_order = 0

    def _adb_prefix(self) -> List[str]:
        return ["adb", "-s", self.device_id] if self.device_id else ["adb"]

    def get_pid(self, package_name: str) -> Optional[int]:
        """使用 awk 获取应用主进程 PID。"""
        try:
            result = subprocess.run(
                self._adb_prefix() + ["shell", "ps", "-A", "-o", "PID,NAME"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, "adb shell ps")

            for line in (result.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue
                pid, name = parts[0], parts[1]
                if name == package_name and pid.isdigit():
                    return int(pid)
            return None
        except Exception as e:
            print(f"PID获取失败 {package_name}: {str(e)}")
            return None

    def _alive_package_names(self) -> List[str]:
        """返回当前存活的已启动包名列表。"""
        try:
            result = subprocess.run(
                self._adb_prefix() + ["shell", "ps"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, "adb shell ps")

            ps_output = result.stdout
            alive_names: List[str] = []
            for package in self.packages:
                pid = self.alive.get(package)
                if pid is None or pid <= 0:
                    continue
                if str(pid) in ps_output:
                    alive_names.append(package)
            return alive_names
        except subprocess.CalledProcessError:
            return []

    def _count_alive_pids(self) -> int:
        """遍历 alive，判断对应的 pid 是否存活，并返回存活的个数。"""
        return len(self._alive_package_names())

    def _format_prev_inline(self, prev_stats: Dict[int, Dict[str, object]]) -> str:
        """压缩展示前1~5驻留信息。"""
        chunks: List[str] = []
        for n in range(1, 6):
            detail = prev_stats.get(n, {})
            checked = detail.get("checked", []) or []
            alive = detail.get("alive", []) or []
            if not checked:
                continue
            names = ", ".join(alive[:5]) if alive else "-"
            chunks.append(f"前{n}:{len(alive)}/{len(checked)}[{names}]")
        return " ".join(chunks) if chunks else "前序: -"

    def _launch_app(self, package_name: str, app_exit: bool = True) -> bool:
        """带桌面返回的应用启动流程。"""
        try:
            subprocess.run(
                self._adb_prefix()
                + [
                    "shell",
                    "monkey",
                    "-p",
                    package_name,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            start_time = datetime.now()
            print(f"[{start_time.strftime('%H:%M:%S')}] 启动 {package_name}")

            if package_name in (
                "com.tencent.tmgp.pubgmhd",
                "com.tencent.tmgp.sgame",
            ):
                time.sleep(15)
            time.sleep(self.app_wait)

            if app_exit:
                subprocess.run(
                    self._adb_prefix() + ["shell", "input", "keyevent", "KEYCODE_HOME"]
                )
            time.sleep(1)

            return True
        except subprocess.CalledProcessError as e:
            print(f"启动失败 {package_name}: {str(e)}")
            return False

    def _poll_pid(self, package_name: str, retry: int = 3, sleep_time: float = 1.0):
        pid = None
        remaining = retry
        while remaining > 0 and not pid:
            pid = self.get_pid(package_name)
            remaining -= 1
            if not pid:
                time.sleep(sleep_time)
        return pid

    def _compute_prev_residency(self, alive_before: List[str]) -> Dict[int, Dict[str, object]]:
        """计算前1~5个应用的驻留情况。"""
        stats: Dict[int, Dict[str, object]] = {}
        for offset in range(1, 6):
            if not self.launch_sequence:
                stats[offset] = {"checked": [], "alive": [], "rate": None}
                continue
            checked = self.launch_sequence[-offset:]
            alive = [pkg for pkg in checked if pkg in alive_before]
            rate = (len(alive) / len(checked)) if checked else None
            stats[offset] = {"checked": checked, "alive": alive, "rate": rate}
        return stats

    def summarize_prev_residency(self) -> Dict[int, Dict[str, object]]:
        """汇总前1~5驻留率。"""
        totals: Dict[int, Dict[str, float]] = {
            n: {"alive": 0.0, "total": 0.0} for n in range(1, 6)
        }
        for record in self.launch_records:
            for n, detail in record.prev_alive_stats.items():
                checked = detail.get("checked", [])
                alive = detail.get("alive", [])
                totals[n]["alive"] += float(len(alive))
                totals[n]["total"] += float(len(checked))

        summary: Dict[int, Dict[str, object]] = {}
        for n, data in totals.items():
            rate = (data["alive"] / data["total"]) if data["total"] else None
            summary[n] = {"alive": int(data["alive"]), "total": int(data["total"]), "rate": rate}
        return summary

    def collect_round(self, round_num: int) -> Dict[str, Optional[int]]:
        """执行一轮启动，返回 package->pid 映射。"""
        round_pids: Dict[str, Optional[int]] = {}
        success_count = 0

        current_packages = self.packages
        # current_packages = (
        #     list(reversed(self.packages)) if round_num == 2 else list(self.packages)
        # )

        for idx, package in enumerate(current_packages, 1):
            alive_before = self._alive_package_names()
            prev_stats = self._compute_prev_residency(alive_before)
            alive_after_names = alive_before

            if self._launch_app(package):
                pid = self._poll_pid(package)
                round_pids[package] = pid
                self.alive[package] = pid if pid is not None else -1
                alive_after_names = self._alive_package_names()
                alive_count = len(alive_after_names)
                self.alive_counts.append(alive_count)
                status = "成功" if pid else "失败"
                prev_inline = self._format_prev_inline(prev_stats)
                print(
                    f"应用 {idx}/{len(current_packages)}: {package.ljust(25)} "
                    f"PID: {str(pid).ljust(8)} {status}  {prev_inline}"
                )
                success_count += 1 if pid else 0
            else:
                round_pids[package] = None

            record = LaunchResidencyRecord(
                package=package,
                round_no=round_num,
                order_in_round=idx,
                global_order=self._global_order + 1,
                pid=round_pids[package],
                success=bool(round_pids[package]),
                alive_before=alive_before,
                alive_after_count=len(alive_after_names),
                prev_alive_stats=prev_stats,
            )
            self.launch_records.append(record)
            self.launch_sequence.append(package)
            self._global_order += 1

        print(
            f"\n第 {round_num} 轮完成: 成功获取 {success_count}/{len(self.packages)} 个应用的PID"
        )
        return round_pids

    def run_rounds(self) -> Tuple[Dict[str, Optional[int]], Dict[str, Optional[int]]]:
        print("\n=== 开始第一轮应用启动 ===")
        round1 = self.collect_round(1)

        print("\n=== 开始第二轮应用启动 ===")
        round2 = self.collect_round(2)

        return round1, round2

    def run_multiple_rounds(self, rounds: int) -> List[Dict[str, Optional[int]]]:
        """执行指定轮次的启动测试，返回每轮的 PID 结果列表。"""
        if rounds < 1:
            raise ValueError("rounds 必须大于等于 1")

        results: List[Dict[str, Optional[int]]] = []
        for idx in range(1, rounds + 1):
            print(f"\n=== 开始第{idx}轮应用启动 ===")
            results.append(self.collect_round(idx))

        return results

    @property
    def background_average(self) -> float:
        """最近一次执行的平均后台驻留数量。"""
        if not self.packages or not self.alive_counts:
            return 0.0
        sample = self.alive_counts[-len(self.packages) :]
        return sum(sample) / len(self.packages)


def _adb_prefix(device_id: str) -> List[str]:
    return ["adb", "-s", device_id] if device_id else ["adb"]


def get_pid(package_name: str, device_id: str = "") -> Optional[int]:
    """便捷方法：单次获取应用主进程 PID。"""
    try:
        result = subprocess.run(
            _adb_prefix(device_id) + ["shell", "ps", "-A", "-o", "PID,NAME"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, "adb shell ps")

        for line in (result.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1]
            if name == package_name and pid.isdigit():
                return int(pid)
        return None
    except Exception as e:
        print(f"PID获取失败 {package_name}: {str(e)}")
        return None


def launch_app(
    package_name: str,
    device_id: str = "",
    app_exit: bool = True,
    app_wait: int = 4,
) -> bool:
    """便捷方法：启动单个应用并可选择返回桌面。"""
    try:
        subprocess.run(
            _adb_prefix(device_id)
            + [
                "shell",
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        start_time = datetime.now()
        print(f"[{start_time.strftime('%H:%M:%S')}] 启动 {package_name}")

        if package_name in ("com.tencent.tmgp.pubgmhd", "com.tencent.tmgp.sgame"):
            time.sleep(15)
        time.sleep(app_wait)

        if app_exit:
            subprocess.run(
                _adb_prefix(device_id)
                + ["shell", "input", "keyevent", "KEYCODE_HOME"]
            )
        time.sleep(1)
        return True
    except subprocess.CalledProcessError as e:
        print(f"启动失败 {package_name}: {str(e)}")
        return False
