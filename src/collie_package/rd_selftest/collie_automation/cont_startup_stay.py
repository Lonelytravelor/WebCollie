import argparse
import os
import sys
from datetime import datetime
from typing import Dict, Optional

from .. import log_class, tools, state
from .pre_start import run_pre_start
from .collector_pipeline import build_collectors, run_with_collectors
from .startup_reporting import (
    analyze_results,
    generate_html_report,
    generate_report,
)
from .post_run_parser import run_auto_parsers
from .startup_runner import AppLaunchRunner

device = ""
nogame = False


class ConsoleLogger:
    """将终端输出同时写入文件。"""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.file = None
        self._stdout = sys.stdout
        self._stderr = sys.stderr

    def __enter__(self):
        os.makedirs(os.path.dirname(self.file_path) or ".", exist_ok=True)
        self.file = open(self.file_path, "w", encoding="utf-8")
        sys.stdout = self
        sys.stderr = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.flush()
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        if self.file:
            self.file.close()

    def write(self, data):
        self._stdout.write(data)
        if self.file:
            self.file.write(data)

    def flush(self):
        self._stdout.flush()
        if self.file:
            self.file.flush()

    def isatty(self):
        return hasattr(self._stdout, "isatty") and self._stdout.isatty()


def _find_residency_dir(base_dir: str) -> str:
    candidate = os.path.join(base_dir, "residency_results")
    return candidate if os.path.isdir(candidate) else base_dir


def _find_memcat_html(base_dir: str) -> Optional[str]:
    """查找 memcat 生成的 html（返回第一个匹配路径），用于内嵌到报告。"""
    for root, _, files in os.walk(base_dir):
        for name in files:
            if name.startswith("memcat") and name.endswith(".html"):
                return os.path.join(root, name)
    return None


def _read_process_summary(base_dir: str) -> str:
    """提取 process_report.txt 的分析总结部分。"""
    residency_dir = _find_residency_dir(base_dir)
    candidates = [
        os.path.join(residency_dir, "process_report.txt"),
        os.path.join(base_dir, "process_report.txt"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        start = None
        end = None
        for idx, line in enumerate(lines):
            if "分析总结" in line:
                start = idx
                continue
            if start is not None and line.startswith("="):
                # 下一条全等号视为结束
                end = idx
                break
        if start is None:
            return ""
        chunk = lines[start:end] if end else lines[start:]
        return "".join(chunk).strip()
    return ""


def _read_oomadj_summary(base_dir: str, timestamp: str) -> str:
    """提取 oomadj_summary_report 的全局摘要（详细应用分析之前）。"""
    residency_dir = _find_residency_dir(base_dir)
    candidates = [
        os.path.join(residency_dir, f"oomadj_summary_report_{timestamp}.txt"),
        os.path.join(base_dir, f"oomadj_summary_report_{timestamp}.txt"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        end = None
        for idx, line in enumerate(lines):
            if "详细应用分析" in line:
                end = idx
                break
        chunk = lines[:end] if end else lines
        return "".join(chunk).strip()
    return ""


def _find_ftrace_dir(base_dir: str) -> str:
    candidate = os.path.join(base_dir, "ftrace_logs")
    return candidate if os.path.isdir(candidate) else base_dir


def _extract_global_section(path: str) -> str:
    """提取 ftrace 报告中的 Global Stats 部分。"""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()

    start = None
    end = None
    for idx, line in enumerate(lines):
        if "Global Stats" in line:
            start = idx
            continue
        if start is not None and line.startswith("==== 2"):
            end = idx
            break
    if start is None:
        return ""
    chunk = lines[start:end] if end else lines[start:]
    return "".join(chunk).strip()


def _read_ftrace_global_stats(base_dir: str) -> str:
    """读取 direct reclaim / kswapd 报告的 Global Stats 段落。"""
    ftrace_dir = _find_ftrace_dir(base_dir)
    targets = [
        ("Direct Reclaim", os.path.join(ftrace_dir, "direct_reclaim_report.txt")),
        ("Kswapd", os.path.join(ftrace_dir, "kswapd_report.txt")),
    ]
    sections = []
    for label, path in targets:
        section = _extract_global_section(path)
        if section:
            sections.append(f"[{label}]\\n{section}")
    return "\n\n".join(sections)


def parse_args():
    parser = argparse.ArgumentParser(description="连续冷启动+日志采集模式")

    # -s 选项，接收一个字符串，默认为 None
    parser.add_argument("-s", "--string", type=str, help="输入的字符串", default=None)

    # -nogame 选项，作为布尔值（不出现时为 False）
    parser.add_argument("-nogame", action="store_true", help="是否禁用游戏模式")

    return parser.parse_args()


def main():
    args = parse_args()
    global device
    device = args.string

    package_list = tools.load_config_status(
        include_keys=None, exclude_keys=["国内整机驻留测试"]
    )

    if package_list == -1:
        return
    
    now = datetime.now()
    timestamp = now.strftime("%d_%H_%M")  # 格式: 日期_小时_分钟
    log_dir = state.FILE_DIR or os.getcwd()
    os.makedirs(log_dir, exist_ok=True)
    console_log_path = os.path.join(log_dir, f"console_{timestamp}.log")

    with ConsoleLogger(console_log_path):
        print(f"[Info] 终端输出已同步至: {console_log_path}")
        choice = input(
            "\n是否执行预处理命令（root/节点设置等）? 按 Enter 跳过直接开始，输入 y 执行预处理后开始: "
        ).strip().lower()
        if choice == "y":
            run_pre_start(device_id=device)
            print("预处理完成，准备开始测试。")
        else:
            print("已跳过预处理，准备开始测试。")

        input("请确保手机处于初始状态,按 Enter 开始测试...")
        test_start = datetime.now()
        collectors = build_collectors(package_list, timestamp, device)
        round1: Dict[str, Optional[int]] = {}
        round2: Dict[str, Optional[int]] = {}
        runner = AppLaunchRunner(package_list, device_id=device)
        try:
            round1, round2 = run_with_collectors(collectors, runner.run_rounds)
        except Exception as e:
            print(f"⚠️ 主程序出错: {str(e)}")
        
        # 自动解析采集到的日志与节点
        run_auto_parsers(timestamp)

        # 读取驻留/查杀解析摘要
        kill_summary = _read_process_summary(state.FILE_DIR)
        oomadj_summary = _read_oomadj_summary(state.FILE_DIR, timestamp)
        ftrace_summary = _read_ftrace_global_stats(state.FILE_DIR)
        memcat_html = _find_memcat_html(state.FILE_DIR)
        memcat_rel = (
            os.path.relpath(memcat_html, state.FILE_DIR) if memcat_html else None
        )
        test_end = datetime.now()

        # 生成报告
        analysis = analyze_results(round1, round2)
        background = runner.background_average
        residency_records = runner.launch_records
        residency_summary = runner.summarize_prev_residency()
        generate_report(
            analysis,
            len(package_list),
            background,
            residency_records,
            residency_summary,
            oomadj_summary=oomadj_summary,
            kill_summary=kill_summary,
            ftrace_summary=ftrace_summary,
            start_time=test_start,
            end_time=test_end,
        )
        generate_html_report(
            analysis,
            len(package_list),
            background,
            runner.alive_counts,
            residency_records,
            residency_summary,
            oomadj_summary=oomadj_summary,
            kill_summary=kill_summary,
            ftrace_summary=ftrace_summary,
            memcat_html=memcat_rel,
            start_time=test_start,
            end_time=test_end,
        )

        bugreport_handler = log_class.BugReportHandler()
        bugreport_handler.handle_bugreport()


if __name__ == "__main__":
    main()
