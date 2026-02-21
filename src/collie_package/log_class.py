import argparse
import csv
import os
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

from . import state
from . import tools

matplotlib.use("Agg")

_font_candidates = [
    "SimHei",
    "Noto Sans CJK SC",
    "Microsoft YaHei",
    "WenQuanYi Micro Hei",
]
for _name in _font_candidates:
    if any(f.name == _name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.sans-serif"] = [_name]
        break
plt.rcParams["axes.unicode_minus"] = False


class LogcatRecorder:
    def __init__(self, device_id: str = ""):
        now = datetime.now()
        timestamp = now.strftime("%d_%H_%M")
        self.output_file = os.path.join(state.FILE_DIR, f"logcat_{timestamp}.txt")
        self.device_id = device_id

        self.stop_event = threading.Event()
        self.process = None
        self.thread = None
        self.log_file = None
        print(f"日志文件: {self.output_file}")

    def _adb_prefix(self):
        return ["adb", "-s", self.device_id] if self.device_id else ["adb"]

    def start(self):
        self.thread = threading.Thread(target=self._record_logcat)
        self.thread.daemon = True
        self.thread.start()
        print("✅ Logcat记录已启动")

    def _record_logcat(self):
        try:
            self.log_file = open(self.output_file, "w", buffering=1, encoding="utf-8")
            subprocess.run(self._adb_prefix() + ["logcat", "-b", "all", "-c"])
            time.sleep(3)

            cmd = self._adb_prefix() + ["logcat", "-b", "all"]
            self.process = subprocess.Popen(
                cmd,
                stdout=self.log_file,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            while not self.stop_event.is_set():
                time.sleep(0.1)
        except Exception as exc:
            print(f"⚠️ Logcat记录出错: {str(exc)}")
        finally:
            self._cleanup()

    def _cleanup(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()

        if self.log_file and not self.log_file.closed:
            self.log_file.close()

        print("🔴 Logcat记录已停止")

    def stop(self):
        if not self.stop_event.is_set():
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=3)


class OOMAdjLogger:
    def __init__(self, package_list, output_file="oomadj_log.csv"):
        self.package_list = package_list
        self.output_file = output_file
        self.logging_active = False
        self.log_thread = None
        self.log_data = []
        self.last_valid_pids = {}

    def _run_logging(self):
        timestamp = 0
        while self.logging_active:
            start_time = time.time()
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            current_oomadj = []
            for package in self.package_list:
                pid = self._get_pid(package)
                if pid:
                    self.last_valid_pids[package] = pid
                    oomadj = self._get_oomadj(pid)
                    current_oomadj.append(oomadj)
                else:
                    if package in self.last_valid_pids:
                        oomadj = self._get_oomadj(self.last_valid_pids[package])
                        current_oomadj.append(oomadj if oomadj != "/" else "/")
                    else:
                        current_oomadj.append("/")

            self.log_data.append(current_oomadj)
            timestamp += 1

            elapsed = time.time() - start_time
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

    def _get_pid(self, package):
        try:
            result = subprocess.run(
                ["adb", "shell", "pidof", package],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2.5,
            )
            output = result.stdout.strip()

            if output:
                pids = output.split()
                return pids[0]
        except Exception as exc:
            print(f"获取{package} PID时出错: {str(exc)}")

        return None

    def _get_oomadj(self, pid):
        try:
            result = subprocess.run(
                ["adb", "shell", "cat", f"/proc/{pid}/oom_score_adj"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2.5,
            )
            output = result.stdout.strip()
            return output if output else "/"
        except Exception:
            return "/"

    def start(self):
        if self.logging_active:
            return

        print(f"开始监控 {len(self.package_list)} 个应用的oomadj...")
        self.logging_active = True
        self.log_data = []
        self.last_valid_pids = {}
        self.log_thread = threading.Thread(target=self._run_logging)
        self.log_thread.daemon = True
        self.log_thread.start()

    def stop(self):
        if not self.logging_active:
            return

        self.logging_active = False
        if self.log_thread:
            self.log_thread.join(timeout=2.0)

        with open(self.output_file, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            headers = ["Package"] + [f"T+{i}s" for i in range(len(self.log_data))]
            writer.writerow(headers)

            for i, package in enumerate(self.package_list):
                row = [package]
                for record in self.log_data:
                    row.append(record[i])
                writer.writerow(row)

        print(f"记录完成，共 {len(self.log_data)} 个时间点")
        print(f"结果已保存到 {self.output_file}")


def analyze_oomadj_csv(csv_file, report_file="oomadj_report.txt", plot_file="oomadj_analysis_plots.png"):
    df = pd.read_csv(csv_file)

    transposed = df.set_index("Package").T

    results = {
        "total_apps": len(df),
        "total_records": len(transposed),
        "app_details": {},
        "global_stats": {
            "always_alive": [],
            "never_alive": [],
            "adj_values": [],
        },
    }

    for app in df["Package"]:
        app_data = transposed[app]
        if isinstance(app_data, pd.DataFrame):
            print(
                f"[analyze_oomadj_csv] Duplicate package column detected for {app}, using the first column"
            )
            app_data = app_data.iloc[:, 0]
        if hasattr(app_data, "squeeze"):
            app_data = app_data.squeeze()

        numeric_data = pd.to_numeric(app_data.replace("/", pd.NA), errors="coerce")
        invalid_values = app_data[(app_data != "/") & numeric_data.isna()]

        alive_mask = numeric_data.notna()
        alive_count = int(alive_mask.sum())
        death_count = len(app_data) - alive_count
        survival_rate = alive_count / len(app_data) if len(app_data) else 0

        alive_durations = []
        current_alive = 0
        for is_alive in alive_mask:
            if is_alive:
                current_alive += 1
            else:
                if current_alive > 0:
                    alive_durations.append(current_alive)
                    current_alive = 0
        if current_alive > 0:
            alive_durations.append(current_alive)

        adj_values = numeric_data.dropna().astype(int).tolist()
        adj_stats = {
            "min": min(adj_values) if adj_values else None,
            "max": max(adj_values) if adj_values else None,
            "mean": np.mean(adj_values) if adj_values else None,
            "median": np.median(adj_values) if adj_values else None,
            "mode": Counter(adj_values).most_common(1)[0][0] if adj_values else None,
        }

        results["app_details"][app] = {
            "survival_rate": survival_rate,
            "alive_count": alive_count,
            "death_count": death_count,
            "alive_durations": alive_durations,
            "max_alive_duration": max(alive_durations) if alive_durations else 0,
            "adj_values": adj_values,
            "adj_stats": adj_stats,
            "invalid_count": len(invalid_values),
        }

        if len(invalid_values) > 0:
            print(f"[analyze_oomadj_csv] Ignored {len(invalid_values)} non-numeric entries for {app}")

        results["global_stats"]["adj_values"].extend(adj_values)

        if survival_rate == 1.0:
            results["global_stats"]["always_alive"].append(app)
        elif survival_rate == 0.0:
            results["global_stats"]["never_alive"].append(app)

    global_adj = results["global_stats"]["adj_values"]
    if global_adj:
        results["global_stats"]["adj_min"] = min(global_adj)
        results["global_stats"]["adj_max"] = max(global_adj)
        results["global_stats"]["adj_mean"] = np.mean(global_adj)
        results["global_stats"]["adj_median"] = np.median(global_adj)
        adj_counter = Counter(global_adj)
        results["global_stats"]["adj_mode"] = adj_counter.most_common(1)[0][0]
        results["global_stats"]["adj_mode_freq"] = adj_counter.most_common(1)[0][1]
    else:
        results["global_stats"]["adj_min"] = None
        results["global_stats"]["adj_max"] = None
        results["global_stats"]["adj_mean"] = None
        results["global_stats"]["adj_median"] = None
        results["global_stats"]["adj_mode"] = None
        results["global_stats"]["adj_mode_freq"] = None

    generate_report(results, report_file, plot_file)

    return results


def generate_report(results, report_file, plot_file):
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("OOMAdj 监控分析报告 (重点关注进程存活)\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"监控应用总数: {results['total_apps']}\n")
        f.write(f"监控总时长: {results['total_records']} 秒\n")
        f.write(f"始终存活的应用数: {len(results['global_stats']['always_alive'])}\n")
        f.write(f"从未存活的应用数: {len(results['global_stats']['never_alive'])}\n")

        survival_rates = [
            d["survival_rate"]
            for d in results["app_details"].values()
            if not np.isnan(d["survival_rate"])
        ]
        avg_survival_rate = np.mean(survival_rates) if survival_rates else 0
        f.write(f"平均存活率: {avg_survival_rate:.2%}\n")

        alive_durations = []
        for d in results["app_details"].values():
            alive_durations.extend(d["alive_durations"])

        if alive_durations:
            f.write(f"平均存活时长: {np.mean(alive_durations):.1f} 秒\n")
            f.write(f"最长存活时长: {max(alive_durations)} 秒\n")
            f.write(f"最短存活时长: {min(alive_durations)} 秒\n")
        else:
            f.write("无存活事件\n")

        f.write("全局OOMAdj统计: ")
        if results["global_stats"]["adj_min"] is not None:
            f.write(f"最小值={results['global_stats']['adj_min']}, ")
            f.write(f"最大值={results['global_stats']['adj_max']}, ")
            f.write(f"平均值={results['global_stats']['adj_mean']:.1f}, ")
            f.write(f"中位数={results['global_stats']['adj_median']}, ")
            f.write(
                f"众数={results['global_stats']['adj_mode']} (出现{results['global_stats']['adj_mode_freq']}次)\n\n"
            )
        else:
            f.write("无有效数据\n\n")

        if results["global_stats"]["always_alive"]:
            f.write("始终存活的应用:\n")
            for app in results["global_stats"]["always_alive"]:
                adj_stats = results["app_details"][app]["adj_stats"]
                f.write(
                    f"  {app}: OOMAdj范围[{adj_stats['min']}-{adj_stats['max']}], 平均={adj_stats['mean']:.1f}, 众数={adj_stats['mode']}\n"
                )
            f.write("\n")

        if results["global_stats"]["never_alive"]:
            f.write("从未存活的应用:\n")
            for app in results["global_stats"]["never_alive"]:
                f.write(f"  {app}\n")
            f.write("\n")

        f.write("=" * 60 + "\n")
        f.write("详细应用分析\n")
        f.write("=" * 60 + "\n")

        sorted_apps = sorted(
            results["app_details"].items(),
            key=lambda x: x[1]["survival_rate"],
            reverse=True,
        )

        for app, data in sorted_apps:
            f.write(f"\n应用: {app}\n")
            total_records = data["alive_count"] + data["death_count"]
            f.write(f"存活率: {data['survival_rate']:.2%} ({data['alive_count']}/{total_records})\n")

            if data["alive_durations"]:
                f.write(f"存活次数: {len(data['alive_durations'])}\n")
                f.write(f"最长存活时间: {data['max_alive_duration']} 秒\n")
                f.write(f"平均存活时间: {np.mean(data['alive_durations']):.1f} 秒\n")

            if data["adj_values"]:
                f.write("存活期间OOMAdj统计:\n")
                f.write(f"  最小值: {data['adj_stats']['min']}\n")
                f.write(f"  最大值: {data['adj_stats']['max']}\n")
                f.write(f"  平均值: {data['adj_stats']['mean']:.1f}\n")
                f.write(f"  中位数: {data['adj_stats']['median']}\n")
                mode_value = Counter(data["adj_values"]).most_common(1)
                if mode_value:
                    f.write(f"  众数: {mode_value[0][0]} (出现{mode_value[0][1]}次)\n")

            f.write("-" * 50 + "\n")

    create_plots(results, plot_file)

    print(f"分析报告已保存至: {report_file}")
    print(f"图表已保存至: {plot_file}")


def create_plots(results, plot_file):
    plt.figure(figsize=(15, 12))

    plt.subplot(2, 2, 1)
    survival_rates = [d["survival_rate"] for d in results["app_details"].values()]
    plt.hist(survival_rates, bins=20, color="skyblue", edgecolor="black")
    plt.title("APP ALIVE SCATTER")
    plt.xlabel("ALIVE RATE")
    plt.ylabel("APP COUNT")
    plt.grid(axis="y", alpha=0.75)

    plt.subplot(2, 2, 2)
    if results["global_stats"]["adj_values"]:
        adj_values = results["global_stats"]["adj_values"]
        filtered_adj = [x for x in adj_values if -1000 <= x <= 1000]
        plt.hist(filtered_adj, bins=50, color="salmon", edgecolor="black")
        plt.title("OOMAdj SCATTER")
        plt.xlabel("OOMAdj")
        plt.ylabel("COUNT")
        plt.grid(axis="y", alpha=0.75)
    else:
        plt.text(0.5, 0.5, "NULL OOMAdj DATA", horizontalalignment="center", verticalalignment="center")

    plt.subplot(2, 2, 3)
    alive_durations = []
    for d in results["app_details"].values():
        alive_durations.extend(d["alive_durations"])

    if alive_durations:
        cutoff = np.percentile(alive_durations, 95)
        filtered_durations = [d for d in alive_durations if d <= cutoff]
        plt.hist(filtered_durations, bins=50, color="lightgreen", edgecolor="black")
        plt.title("APP ALIVE TIME SCATTER")
        plt.xlabel("ALIVE TIME(S)")
        plt.ylabel("APPEAR COUNT")
        plt.grid(axis="y", alpha=0.75)
    else:
        plt.text(0.5, 0.5, "NULL ALIVE DATA", horizontalalignment="center", verticalalignment="center")

    plt.subplot(2, 2, 4)
    apps = [app for app in results["app_details"]]
    survival_rates = [results["app_details"][app]["survival_rate"] for app in apps]
    sorted_indices = np.argsort(survival_rates)[::-1]
    sorted_apps = [apps[i] for i in sorted_indices]
    sorted_rates = [survival_rates[i] for i in sorted_indices]

    plt.bar(sorted_apps, sorted_rates, color="violet")
    plt.title("PER APP ALIVE RATE")
    plt.xlabel("APP")
    plt.ylabel("ALIVE RATE")
    plt.xticks(rotation=90)
    plt.ylim(0, 1.0)
    plt.grid(axis="y", alpha=0.75)
    plt.tight_layout()

    plt.savefig(plot_file)
    plt.close()


class BugReportHandler:
    def __init__(self):
        self.skip_bugreport = False
        self.user_input = None

    def _get_user_input(self):
        prompt = "是否跳过抓取bugreport? (10秒内输入'0'跳过，直接回车或等待超时则抓取): "
        self.user_input = input(prompt)

    def capture_bugreport(self):
        print("开始抓取bugreport,生成bugreport时间较长,请等待...")
        tools.capture_bugreport(device_id=None, output_dir=state.FILE_DIR, timeout=1200)
        subprocess.run(["adb", "logcat", "-c"])

    def handle_bugreport(self):
        input_thread = threading.Thread(target=self._get_user_input)
        input_thread.daemon = True
        input_thread.start()

        print("等待用户决定是否跳过bugreport抓取...")
        for i in range(10, 0, -1):
            if not input_thread.is_alive():
                break
            print(f"\r剩余时间: {i}秒", end="")
            time.sleep(1)

        print("\n时间到，开始处理...")

        if self.user_input and self.user_input.strip().lower() == "0":
            print("用户选择跳过抓取bugreport")
        else:
            self.capture_bugreport()
