"""自动化解析连续启动采集到的日志，并生成差异报告。"""

import glob
import os
import re
import shutil
from typing import Dict, Iterable, List, Optional, Sequence

from .. import parse_direct_reclaim, parse_kswapd, state
from ..log_tools import log_analyzer


def _find_latest_logcat(output_dir: str, timestamp: str) -> Optional[str]:
    """优先匹配带时间戳的 logcat，缺失时回落到目录下最新的 logcat 文件。"""
    prefer = os.path.join(output_dir, f"logcat_{timestamp}.txt")
    if os.path.exists(prefer):
        return prefer
    candidates = glob.glob(os.path.join(output_dir, "logcat_*.txt"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _split_sections(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """提取“测试前/测试后”两个区块。"""
    before: list[str] = []
    after: list[str] = []
    target: Optional[list[str]] = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("测试前"):
            target = before
            continue
        if stripped.startswith("测试后"):
            target = after
            continue
        if target is not None:
            target.append(line.rstrip("\n"))
    return before, after


def _parse_metrics(lines: Sequence[str]) -> Dict[str, List[object]]:
    """将类似 vmstat 的 key/value 行解析为 {key: [values...]}。"""
    data: Dict[str, List[object]] = {}
    for raw in lines:
        line = raw.strip()
        if not line or set(line) <= {"=", "-"}:
            continue

        key: Optional[str] = None
        rest: Optional[str] = None
        if ":" in line:
            key, rest = line.split(":", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                key, rest = parts[0], parts[1]
        if not key or rest is None:
            continue

        tokens = re.split(r"[\s,]+", rest.strip())
        values: List[object] = []
        for token in tokens:
            if not token:
                continue
            if re.fullmatch(r"-?\d+", token):
                values.append(int(token))
            else:
                values.append(token)
        if values:
            data[key] = values
    return data


def _format_values(values: Optional[List[object]]) -> str:
    if values is None:
        return "-"
    if not values:
        return "-"
    return " ".join(str(v) for v in values)


def _is_numeric_list(values: Optional[List[object]]) -> bool:
    return bool(values) and all(isinstance(v, int) for v in values)


def _compute_diff(before: Optional[List[object]], after: Optional[List[object]]) -> str:
    if before is None and after is None:
        return "-"
    if before is None:
        return "新增"
    if after is None:
        return "缺失"
    if _is_numeric_list(before) and _is_numeric_list(after):
        if len(before) == len(after):
            diffs = [a - b for a, b in zip(after, before)]
            return " ".join(str(d) for d in diffs)
        return "长度不一致"
    if before == after:
        return "无变化"
    return "已更新"


def diff_before_after_file(file_path: str, output_path: str, title: str) -> str:
    """对测试前/后数据做差异化渲染，并写入 output_path。"""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()
    before_lines, after_lines = _split_sections(lines)
    before = _parse_metrics(before_lines)
    after = _parse_metrics(after_lines)

    rows: List[str] = []
    rows.append(title)
    rows.append("=" * max(20, len(title)))
    rows.append(f"源文件: {file_path}")
    rows.append("")
    if not before_lines or not after_lines:
        rows.append("未找到“测试前/测试后”两个区块，无法计算差异。")
    else:
        rows.append(f"{'Key':<40}{'Before':>18}{'After':>18}{'Diff':>18}")
        rows.append("-" * 94)
        for key in sorted(set(before) | set(after)):
            bval = before.get(key)
            aval = after.get(key)
            rows.append(
                f"{key:<40}{_format_values(bval):>18}{_format_values(aval):>18}{_compute_diff(bval, aval):>18}"
            )

    with open(output_path, "w", encoding="utf-8") as out:
        out.write("\n".join(rows))
    return output_path


def _move_if_exists(src_path: str, target_dir: str) -> Optional[str]:
    """存在则移动到目标目录，返回目标路径。"""
    if not os.path.exists(src_path):
        return None
    os.makedirs(target_dir, exist_ok=True)
    dst_path = os.path.join(target_dir, os.path.basename(src_path))
    try:
        if os.path.abspath(src_path) != os.path.abspath(dst_path):
            shutil.move(src_path, dst_path)
        return dst_path
    except Exception as exc:  # noqa: BLE001
        print(f"[Auto][WARN] 移动文件失败 {src_path} -> {dst_path}: {exc}")
        return None


def _move_with_prefix(base_dir: str, prefix: str, target_dir: str) -> List[str]:
    """按前缀匹配文件（忽略后缀），批量移动到目标目录。"""
    moved: List[str] = []
    pattern = os.path.join(base_dir, f"{prefix}*")
    for path in glob.glob(pattern):
        if not os.path.isfile(path):
            continue
        os.makedirs(target_dir, exist_ok=True)
        dst_path = os.path.join(target_dir, os.path.basename(path))
        try:
            if os.path.abspath(path) != os.path.abspath(dst_path):
                shutil.move(path, dst_path)
            moved.append(dst_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto][WARN] 移动文件失败 {path} -> {dst_path}: {exc}")
    return moved


def run_auto_parsers(timestamp: str) -> None:
    """连续启动后自动解析采集到的日志。"""
    base_dir = state.FILE_DIR or os.getcwd()
    os.makedirs(base_dir, exist_ok=True)

    # 分类目录
    dirs = {
        "residency": os.path.join(base_dir, "residency_results"),
        "ftrace": os.path.join(base_dir, "ftrace_logs"),
        "nodes": os.path.join(base_dir, "node_logs"),
        "memory": os.path.join(base_dir, "memory_info"),
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    print("\n=== 自动解析连续启动日志 ===")

    # ftrace + 解析结果
    ftrace_path = os.path.join(base_dir, f"ftrace_{timestamp}.txt")
    if os.path.exists(ftrace_path):
        print(f"[Auto] 解析 direct reclaim: {ftrace_path}")
        try:
            parse_direct_reclaim.parse_ftrace_file(ftrace_path, dirs["ftrace"], quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto][WARN] direct reclaim 解析失败: {exc}")

        print(f"[Auto] 解析 kswapd: {ftrace_path}")
        try:
            parse_kswapd.parse_ftrace_file(ftrace_path, dirs["ftrace"], quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto][WARN] kswapd 解析失败: {exc}")

        _move_if_exists(ftrace_path, dirs["ftrace"])
        # 兜底移动解析结果（兼容旧路径）
        _move_if_exists(os.path.join(base_dir, "direct_reclaim_report.txt"), dirs["ftrace"])
        _move_if_exists(os.path.join(base_dir, "direct_reclaim_records.csv"), dirs["ftrace"])
        _move_if_exists(os.path.join(base_dir, "kswapd_report.txt"), dirs["ftrace"])
        _move_if_exists(os.path.join(base_dir, "kswapd_cycles.csv"), dirs["ftrace"])
    else:
        print(f"[Auto] 未找到 ftrace 文件，跳过: {ftrace_path}")

    # logcat + 驻留结果
    logcat_path = _find_latest_logcat(base_dir, timestamp)
    if logcat_path:
        print(f"[Auto] 解析 logcat: {logcat_path}")
        try:
            log_analyzer.analyze_log_file(logcat_path, dirs["residency"])
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto][WARN] logcat 解析失败: {exc}")
        _move_if_exists(logcat_path, dirs["residency"])
    else:
        print("[Auto] 未找到 logcat 文件，跳过解析。")

    # 方案节点日志 + diff
    diff_targets = [
        ("vmstat", f"vmstat{timestamp}.txt", dirs["memory"]),
        ("process_use_count", f"process_use_count{timestamp}.txt", dirs["nodes"]),
        ("greclaim_parm", f"greclaim_parm{timestamp}.txt", dirs["nodes"]),
    ]
    for label, filename, target_dir in diff_targets:
        source_path = os.path.join(base_dir, filename)
        if not os.path.exists(source_path):
            print(f"[Auto] 未找到 {label} 文件，跳过: {source_path}")
            continue
        diff_output = os.path.join(target_dir, f"{label}_diff_{timestamp}.txt")
        try:
            diff_before_after_file(
                source_path, diff_output, title=f"{label} 差异 ({timestamp})"
            )
            print(f"[Auto] {label} 差异已保存: {diff_output}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto][WARN] 生成 {label} 差异失败: {exc}")

        _move_if_exists(source_path, target_dir)

    # 驻留结果：冷启动/oomadj/memcat
    residency_targets = [
        "process_report.txt",
        "冷启动分析报告.html",
        f"oomadj_{timestamp}.csv",
        f"oomadj_summary_report_{timestamp}.txt",
        f"oomadj_analysis_plots_{timestamp}.png",
    ]
    for name in residency_targets:
        _move_if_exists(os.path.join(base_dir, name), dirs["residency"])

    # 内存信息
    _move_if_exists(os.path.join(base_dir, f"meminfo{timestamp}.txt"), dirs["memory"])
    # memcat 输出前缀，忽略后缀（可能带 .csv/.html 等）
    _move_with_prefix(base_dir, "memcat", dirs["memory"])
