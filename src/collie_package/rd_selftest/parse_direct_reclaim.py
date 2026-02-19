#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pyright: reportMissingTypeArgument=false, reportUnboundVariable=false, reportAttributeAccessIssue=false, reportArgumentType=false

"""Parse ftrace logs for mm_vmscan_direct_reclaim_begin/end and compute durations."""

import csv
import math
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

try:
    from . import state  # type: ignore
except Exception:  # 兼容直接运行脚本的场景
    import importlib
    state = importlib.import_module("collie_package.state")
from ..config_loader import load_rules_config

# 示例 ftrace 行：
# Jit thread pool-4435    [003] ..... 11640.869472: mm_vmscan_direct_reclaim_begin: order=0 gfp_flags=GFP_HIGHUSER_MOVABLE|__GFP_COMP|__GFP_ZERO|__GFP_CMA
# Jit thread pool-4435    [003] .N... 11640.870354: mm_vmscan_direct_reclaim_end: nr_reclaimed=54

_RULES = load_rules_config()
_DR_RULES = _RULES.get('parse_direct_reclaim', {}) if isinstance(_RULES, dict) else {}

LINE_RE = re.compile(
    _DR_RULES.get(
        'line_re',
        r'^(?P<comm>.+?)-(?P<pid>\d+)\s+'
        r'\[(?P<cpu>\d+)\]\s+'
        r'(?P<flags>[\.A-Z]+)\s+'
        r'(?P<ts>\d+\.\d+):\s+'
        r'(?P<event>\S+):\s+'
        r'(?P<args>.*)$',
    )
)

ORDER_RE = re.compile(_DR_RULES.get('order_re', r'order=(\d+)'))
GFP_RE = re.compile(_DR_RULES.get('gfp_re', r'gfp_flags=([^\s]+)'))
NR_RECLAIMED_RE = re.compile(_DR_RULES.get('nr_reclaimed_re', r'nr_reclaimed=(\d+)'))
PREV_PID_RE = re.compile(_DR_RULES.get('prev_pid_re', r'prev_pid=(\d+)'))
NEXT_PID_RE = re.compile(_DR_RULES.get('next_pid_re', r'next_pid=(\d+)'))
SUPPORTED_EVENTS = set(_DR_RULES.get(
    'events',
    [
        'mm_vmscan_direct_reclaim_begin',
        'mm_vmscan_direct_reclaim_end',
    ],
))


def parse_line(line):
    """
    Parse a single ftrace line.
    Return dict with fields or None if not a direct reclaim line.
    """
    m = LINE_RE.match(line)
    if not m:
        return None

    event = m.group('event')
    if event not in SUPPORTED_EVENTS:
        return None

    comm = m.group('comm').strip()
    pid = int(m.group('pid'))
    cpu = int(m.group('cpu'))
    ts = float(m.group('ts'))
    args = m.group('args')

    rec = {
        'comm': comm,
        'pid': pid,
        'cpu': cpu,
        'ts': ts,
        'event': event,
        'order': None,
        'gfp_flags': None,
        'nr_reclaimed': None,
    }

    if event == 'mm_vmscan_direct_reclaim_begin':
        m_order = ORDER_RE.search(args)
        if m_order:
            rec['order'] = int(m_order.group(1))
        m_gfp = GFP_RE.search(args)
        if m_gfp:
            rec['gfp_flags'] = m_gfp.group(1)
    else:
        # end
        m_nr = NR_RECLAIMED_RE.search(args)
        if m_nr:
            rec['nr_reclaimed'] = int(m_nr.group(1))

    return rec


def percentile(data, p):
    """
    Compute percentile p (0-100) of a list of numbers.
    """
    if not data:
        return None
    data = sorted(data)
    if len(data) == 1:
        return data[0]
    k = (len(data) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[f] + (data[c] - data[f]) * (k - f)


def merge_intervals(records: List[dict]) -> Tuple[float, List[Tuple[float, float]]]:
    """将所有 direct reclaim 区间合并，返回覆盖时间（ms）与合并后的区间列表。"""
    if not records:
        return 0.0, []

    intervals = sorted([(r["begin_ts"], r["end_ts"]) for r in records], key=lambda x: x[0])
    merged: List[Tuple[float, float]] = []
    cur_start, cur_end = intervals[0]

    for start, end in intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))

    coverage_ms = sum((end - start) * 1000.0 for start, end in merged)
    return coverage_ms, merged


def build_report(records, unmatched_begin, unmatched_end, coverage_ms):
    """
    根据所有 direct reclaim 记录构建一个文本报告（字符串）。
    records: list of dicts, each with:
        comm, pid, cpu_begin, cpu_end, begin_ts, end_ts,
        duration_ms, on_cpu_ms, order, gfp_flags, nr_reclaimed
    """
    lines = []

    if not records:
        lines.append("No direct reclaim records found.")
        return "\n".join(lines)

    durations_wall = [r['duration_ms'] for r in records]
    durations_cpu = [r['on_cpu_ms'] for r in records]
    total_nr = sum(r['nr_reclaimed'] for r in records if r['nr_reclaimed'] is not None)
    total_cpu_ms = sum(durations_cpu)
    total_wall_ms = sum(durations_wall)
    time_span_s = records[-1]['end_ts'] - records[0]['begin_ts'] if len(records) > 1 else durations_wall[0] / 1000.0
    time_span_ms = time_span_s * 1000.0

    lines.append("============================================")
    lines.append(" Direct Reclaim Analysis Report")
    lines.append("============================================")
    lines.append("")
    lines.append("==== 0. Data Quality Check ====")
    lines.append(
        f"Matched pairs: {len(records)}, unmatched begin: {len(unmatched_begin)}, unmatched end: {len(unmatched_end)}"
    )
    if unmatched_begin:
        lines.append("Unmatched begin examples (first 5):")
        for rec in unmatched_begin[:5]:
            lines.append(
                f"  ts={rec['ts']:.6f} pid={rec['pid']} cpu={rec['cpu']} comm={rec['comm']} line={rec.get('raw', '')}"
            )
    if unmatched_end:
        lines.append("Unmatched end examples (first 5):")
        for rec in unmatched_end[:5]:
            lines.append(
                f"  ts={rec['ts']:.6f} pid={rec['pid']} cpu={rec['cpu']} comm={rec['comm']} line={rec.get('raw', '')}"
            )
    lines.append("")

    lines.append("==== 1. Global Stats ====")
    lines.append(f"Total direct reclaim count : {len(records)}")
    lines.append(f"Total nr_reclaimed         : {total_nr}")
    lines.append(f"Total on-CPU time in direct reclaim (ms) : {total_cpu_ms:.3f}")
    lines.append(f"Total wall time (begin->end) (ms)        : {total_wall_ms:.3f}")
    lines.append(f"Wall-clock covered by direct reclaim (ms): {coverage_ms:.3f}")
    lines.append(f"Trace time span (ms)                    : {time_span_ms:.3f}")
    if time_span_ms > 0:
        lines.append(f"CPU time / span ratio                 : {100.0 * total_cpu_ms / time_span_ms:.3f}%")
        lines.append(f"Wall-clock cover / span ratio         : {100.0 * coverage_ms / time_span_ms:.3f}%")
    lines.append(
        "On-CPU duration per direct reclaim (ms) min/avg/p95/max : "
        f"{min(durations_cpu):.3f} / "
        f"{(sum(durations_cpu)/len(durations_cpu)):.3f} / "
        f"{percentile(durations_cpu, 95):.3f} / "
        f"{max(durations_cpu):.3f}"
    )
    lines.append(
        "Wall duration per direct reclaim (ms) min/avg/p95/max  : "
        f"{min(durations_wall):.3f} / "
        f"{(sum(durations_wall)/len(durations_wall)):.3f} / "
        f"{percentile(durations_wall, 95):.3f} / "
        f"{max(durations_wall):.3f}"
    )
    lines.append("")

    # 2. Per comm
    lines.append("==== 2. Per comm (thread name) Stats ====")
    by_comm = defaultdict(list)
    for r in records:
        by_comm[r['comm']].append(r)

    # 按总耗时排序
    def comm_key(item):
        comm, recs = item
        return sum(x['on_cpu_ms'] for x in recs)

    for comm, recs in sorted(by_comm.items(), key=comm_key, reverse=True):
        durs_cpu = [x['on_cpu_ms'] for x in recs]
        nr_sum = sum(x['nr_reclaimed'] for x in recs if x['nr_reclaimed'] is not None)
        lines.append(
            f"[comm={comm}] count={len(recs)}, "
            f"nr_reclaimed={nr_sum}, "
            f"avg_oncpu={sum(durs_cpu)/len(durs_cpu):.3f} ms, "
            f"total_oncpu={sum(durs_cpu):.3f} ms, "
            f"min/p95/max_oncpu={min(durs_cpu):.3f}/{percentile(durs_cpu, 95):.3f}/{max(durs_cpu):.3f} ms"
        )
    lines.append("")

    # 3. Per PID (进程维度)
    lines.append("==== 3. Per PID (process) Stats ====")
    by_pid = defaultdict(list)
    for r in records:
        by_pid[r['pid']].append(r)

    # 按总耗时排序
    def pid_key(item):
        pid, recs = item
        return sum(x['on_cpu_ms'] for x in recs)

    # 为避免太长，只展示前 N 个进程
    TOP_PID = 30
    for idx, (pid, recs) in enumerate(sorted(by_pid.items(), key=pid_key, reverse=True), 1):
        durs_cpu = [x['on_cpu_ms'] for x in recs]
        nr_sum = sum(x['nr_reclaimed'] for x in recs if x['nr_reclaimed'] is not None)
        comm_counter = Counter(x['comm'] for x in recs)
        main_comm, main_comm_cnt = comm_counter.most_common(1)[0]
        lines.append(
            f"[#{idx:02d}] pid={pid}, main_comm={main_comm} (seen {main_comm_cnt} / {len(recs)}), "
            f"count={len(recs)}, nr_reclaimed={nr_sum}, "
            f"avg_oncpu={sum(durs_cpu)/len(durs_cpu):.3f} ms, total_oncpu={sum(durs_cpu):.3f} ms"
        )
        if idx >= TOP_PID:
            lines.append(f"... (only top {TOP_PID} PIDs shown)")
            break
    lines.append("")

    # 4. Per gfp_flags
    lines.append("==== 4. Per gfp_flags Stats ====")
    by_gfp = defaultdict(list)
    for r in records:
        key = r['gfp_flags'] or "(unknown)"
        by_gfp[key].append(r)

    def gfp_key(item):
        gfp, recs = item
        return sum(x['on_cpu_ms'] for x in recs)

    for gfp, recs in sorted(by_gfp.items(), key=gfp_key, reverse=True):
        durs_cpu = [x['on_cpu_ms'] for x in recs]
        nr_sum = sum(x['nr_reclaimed'] for x in recs if x['nr_reclaimed'] is not None)
        lines.append(
            f"[gfp_flags={gfp}] count={len(recs)}, nr_reclaimed={nr_sum}, "
            f"avg_oncpu={sum(durs_cpu)/len(durs_cpu):.3f} ms, total_oncpu={sum(durs_cpu):.3f} ms, "
            f"min/p95/max_oncpu={min(durs_cpu):.3f}/{percentile(durs_cpu, 95):.3f}/{max(durs_cpu):.3f} ms"
        )
    lines.append("")

    # 5. Top N slow direct reclaim
    lines.append("==== 5. Top 20 Slowest Direct Reclaim Events ====")
    TOP_SLOW = 20
    for idx, r in enumerate(sorted(records, key=lambda x: x['on_cpu_ms'], reverse=True)[:TOP_SLOW], 1):
        lines.append(
            f"[#{idx:02d}] oncpu_dur={r['on_cpu_ms']:.3f} ms, wall_dur={r['duration_ms']:.3f} ms, pid={r['pid']}, comm={r['comm']}, "
            f"cpu_begin={r['cpu_begin']}, cpu_end={r['cpu_end']}, "
            f"begin_ts={r['begin_ts']:.6f}, end_ts={r['end_ts']:.6f}, "
            f"order={r['order']}, gfp_flags={r['gfp_flags']}, nr_reclaimed={r['nr_reclaimed']}"
        )

    lines.append("")
    lines.append("==== End of Report ====")

    return "\n".join(lines)


def remove_quotes(path):
    """去除路径两端的引号，保持与其他工具一致的输入体验。"""
    if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
        return path[1:-1]
    return path


def collect_input_files(path_str):
    """接受文件或目录路径，返回需要解析的文件列表（按名称排序）。"""
    path = remove_quotes(path_str.strip())
    if os.path.isdir(path):
        files = [os.path.join(path, name) for name in sorted(os.listdir(path))]
        return [f for f in files if os.path.isfile(f)]
    return [path]


def parse_ftrace_file(
    input_path: str, output_dir: Optional[str] = None, quiet: bool = False
) -> Optional[Tuple[str, str]]:
    """
    解析 direct reclaim 相关 ftrace，返回报告/CSV 路径。
    input_path 可为文件或目录；output_dir 为空时使用 state.FILE_DIR 或当前目录。
    """
    input_files = collect_input_files(input_path)
    if not input_files:
        print(f"未找到可解析的文件: {input_path}")
        return None

    output_dir = output_dir or state.FILE_DIR or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "direct_reclaim_report.txt")
    csv_path = os.path.join(output_dir, "direct_reclaim_records.csv")

    events: List[dict] = []
    sched_events: List[dict] = []

    for file_path in input_files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, 1):
                    # 先尝试解析 sched_switch 以支持 on-CPU 时长累加
                    m = LINE_RE.match(line)
                    if m and m.group("event") == "sched_switch":
                        args = m.group("args")
                        prev_pid = PREV_PID_RE.search(args)
                        next_pid = NEXT_PID_RE.search(args)
                        if prev_pid and next_pid:
                            sched_events.append(
                                {
                                    "type": "sched_switch",
                                    "ts": float(m.group("ts")),
                                    "cpu": int(m.group("cpu")),
                                    "prev_pid": int(prev_pid.group(1)),
                                    "next_pid": int(next_pid.group(1)),
                                }
                            )
                        continue

                    parsed = parse_line(line)
                    if parsed:
                        parsed["raw"] = line.strip()
                        events.append(parsed)
        except FileNotFoundError:
            print(f"[WARN] 文件不存在，跳过: {file_path}")
        except Exception as e:
            print(f"[WARN] 解析文件时出错 {file_path}: {e}")

    # 合并事件并按时间排序
    events.extend(sched_events)
    events.sort(key=lambda r: r["ts"])

    active: Dict[int, List[dict]] = defaultdict(list)  # pid -> stack of begin events
    records: List[dict] = []
    unmatched_end: List[dict] = []
    unmatched_begin: List[dict] = []
    running: Dict[int, Dict[str, float]] = {}  # cpu -> {"pid": pid, "ts": last_ts}
    have_sched = False

    for parsed in events:
        if parsed.get("type") == "sched_switch":
            have_sched = True
            cpu = parsed["cpu"]
            ts = parsed["ts"]
            prev_state = running.get(cpu)
            if prev_state:
                delta = ts - prev_state["ts"]
                prev_pid = prev_state["pid"]
                if delta > 0 and active.get(prev_pid):
                    active[prev_pid][-1]["on_cpu_ms"] += delta * 1000.0
            running[cpu] = {"pid": parsed["next_pid"], "ts": ts}
            continue

        pid = parsed["pid"]
        event = parsed["event"]

        if event == "mm_vmscan_direct_reclaim_begin":
            begin_rec = dict(parsed)
            begin_rec["on_cpu_ms"] = 0.0
            active[pid].append(begin_rec)
            # 该事件发生时任务正在当前 CPU 上运行，记录 last_ts 便于后续累积
            running[parsed["cpu"]] = {"pid": pid, "ts": parsed["ts"]}
            continue

        # end 事件
        begin_stack = active.get(pid)
        begin = begin_stack.pop() if begin_stack else None
        if begin is None:
            unmatched_end.append(parsed)
            continue
        if begin_stack == []:
            active.pop(pid, None)

        # 如果当前 pid 正在某个 CPU 运行，补齐这一段 on-CPU 时间
        for cpu_id, state in running.items():
            if state["pid"] == pid:
                delta = parsed["ts"] - state["ts"]
                if delta > 0:
                    begin["on_cpu_ms"] += delta * 1000.0
                state["ts"] = parsed["ts"]
                break

        duration_ms = (parsed["ts"] - begin["ts"]) * 1000.0
        on_cpu_ms = begin["on_cpu_ms"] if have_sched else duration_ms
        if have_sched and on_cpu_ms == 0.0:
            on_cpu_ms = duration_ms

        record = {
            "comm": begin["comm"],
            "pid": pid,
            "cpu_begin": begin["cpu"],
            "cpu_end": parsed["cpu"],
            "begin_ts": begin["ts"],
            "end_ts": parsed["ts"],
            "duration_ms": duration_ms,
            "on_cpu_ms": on_cpu_ms,
            "order": begin["order"],
            "gfp_flags": begin["gfp_flags"],
            "nr_reclaimed": parsed["nr_reclaimed"],
        }
        records.append(record)

    # 收集未配对的 begin（剩余 active）
    for stack in active.values():
        unmatched_begin.extend(stack)

    # sort by begin_ts 方便看时间线
    records.sort(key=lambda r: r["begin_ts"])

    # 构建报告
    coverage_ms, _ = merge_intervals(records)
    report = build_report(records, unmatched_begin, unmatched_end, coverage_ms)

    if not quiet:
        print(report)

    try:
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(report)
        if not quiet:
            print(f"\nText report saved to: {report_path}")
    except Exception as e:
        print(f"\n[WARN] Failed to write report file {report_path}: {e}", file=sys.stderr)

    # 导出 CSV（每次 direct reclaim 一条）
    fieldnames = [
        "comm",
        "pid",
        "cpu_begin",
        "cpu_end",
        "begin_ts",
        "end_ts",
        "duration_ms",
        "on_cpu_ms",
        "order",
        "gfp_flags",
        "nr_reclaimed",
    ]
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                writer.writerow(r)
        if not quiet:
            print(f"Per-direct-reclaim records written to {csv_path}")
    except Exception as e:
        print(f"[WARN] Failed to write CSV {csv_path}: {e}", file=sys.stderr)

    return report_path, csv_path


def main():
    print("=" * 50)
    print("Direct Reclaim ftrace 解析工具")
    print("=" * 50)

    user_path = input("请输入ftrace文件或目录路径: ").strip()
    parse_ftrace_file(user_path)


if __name__ == "__main__":
    main()
