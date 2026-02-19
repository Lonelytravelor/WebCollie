#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""Parse ftrace logs for kswapd wake/sleep cycles and summarize activity."""

import csv
import math
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Optional

try:
    from . import state  # type: ignore
except Exception:  # 兼容直接运行脚本的场景
    import importlib

    state = importlib.import_module("collie_package.state")
from ..config_loader import load_rules_config

# Example lines:
# kswapd0-90      [004] ..... 272707.073912: mm_vmscan_kswapd_wake: nid=0 order=0
# kswapd0-90      [006] ..... 272708.867702: mm_vmscan_kswapd_sleep: nid=0
#   binder:1138_3-1645    [006] ..... 272709.532484: mm_vmscan_wakeup_kswapd: nid=0 order=0 gfp_flags=GFP_HIGHUSER|__GFP_COMP|__GFP_ZERO

_RULES = load_rules_config()
_KS_RULES = _RULES.get('parse_kswapd', {}) if isinstance(_RULES, dict) else {}

LINE_RE = re.compile(
    _KS_RULES.get(
        'line_re',
        r"^(?P<comm>.+?)-(?P<pid>\d+)\s+"
        r"\[(?P<cpu>\d+)\]\s+"
        r"(?P<flags>[\.A-Z]+)\s+"
        r"(?P<ts>\d+\.\d+):\s+"
        r"(?P<event>\S+):\s+"
        r"(?P<args>.*)$",
    )
)

NID_RE = re.compile(_KS_RULES.get('nid_re', r"nid=(\d+)"))
ORDER_RE = re.compile(_KS_RULES.get('order_re', r"order=(\d+)"))
GFP_RE = re.compile(_KS_RULES.get('gfp_re', r"gfp_flags=([^\s]+)"))
SUPPORTED_EVENTS = set(_KS_RULES.get(
    'events',
    [
        "mm_vmscan_wakeup_kswapd",
        "mm_vmscan_kswapd_wake",
        "mm_vmscan_kswapd_sleep",
    ],
))


def parse_line(line: str) -> Optional[dict[str, Any]]:
    """Parse a single ftrace line and return a dict for supported events."""
    m = LINE_RE.match(line)
    if not m:
        return None

    event = m.group("event")
    if event not in SUPPORTED_EVENTS:
        return None

    comm = m.group("comm").strip()
    pid = int(m.group("pid"))
    cpu = int(m.group("cpu"))
    ts = float(m.group("ts"))
    args = m.group("args")

    rec = {
        "event": event,
        "comm": comm,
        "pid": pid,
        "cpu": cpu,
        "ts": ts,
        "nid": None,
        "order": None,
        "gfp_flags": None,
    }

    m_nid = NID_RE.search(args)
    if m_nid:
        rec["nid"] = int(m_nid.group(1))

    m_order = ORDER_RE.search(args)
    if m_order:
        rec["order"] = int(m_order.group(1))

    m_gfp = GFP_RE.search(args)
    if m_gfp:
        rec["gfp_flags"] = m_gfp.group(1)

    return rec


def percentile(data: list[float], p: float) -> float | None:
    """Compute percentile p (0-100) of a list of numbers."""
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


def build_report(records: list[dict[str, Any]]) -> str:
    """Build a human-readable report string from kswapd cycle records."""
    if not records:
        return "No kswapd wake/sleep cycles found."

    lines = []
    durations = [r["duration_ms"] for r in records]
    total_time_ms = sum(durations)
    time_span_s = (
        records[-1]["end_ts"] - records[0]["begin_ts"]
        if len(records) > 1
        else durations[0] / 1000.0
    )
    time_span_ms = time_span_s * 1000.0

    lines.append("============================================")
    lines.append(" Kswapd Activity Analysis Report")
    lines.append("============================================")
    lines.append("")
    lines.append("==== 1. Global Stats ====")
    lines.append(f"Total cycles                  : {len(records)}")
    lines.append(f"Total time in kswapd (ms)     : {total_time_ms:.3f}")
    lines.append(f"Trace time span (ms)          : {time_span_ms:.3f}")
    if time_span_ms > 0:
        lines.append(
            f"Kswapd time ratio             : {100.0 * total_time_ms / time_span_ms:.3f}%"
        )
    lines.append(
        "Cycle duration (ms) min/avg/p95/max : "
        f"{min(durations):.3f} / "
        f"{(sum(durations)/len(durations)):.3f} / "
        f"{percentile(durations, 95):.3f} / "
        f"{max(durations):.3f}"
    )
    lines.append("")

    lines.append("==== 2. Per NUMA node (nid) ====")
    by_nid = defaultdict(list)
    for r in records:
        by_nid[r["nid"]].append(r)
    for nid, recs in sorted(
        by_nid.items(),
        key=lambda item: sum(x["duration_ms"] for x in item[1]),
        reverse=True,
    ):
        durs = [x["duration_ms"] for x in recs]
        lines.append(
            f"[nid={nid}] count={len(recs)}, total_dur={sum(durs):.3f} ms, "
            f"avg_dur={sum(durs)/len(durs):.3f} ms, min/p95/max={min(durs):.3f}/"
            f"{percentile(durs, 95):.3f}/{max(durs):.3f} ms"
        )
    lines.append("")

    lines.append("==== 3. Per waker (thread that called mm_vmscan_wakeup_kswapd) ====")
    by_waker = defaultdict(list)
    for r in records:
        key = (r["waker_comm"], r["waker_pid"])
        by_waker[key].append(r)
    for (comm, pid), recs in sorted(
        by_waker.items(),
        key=lambda item: sum(x["duration_ms"] for x in item[1]),
        reverse=True,
    ):
        durs = [x["duration_ms"] for x in recs]
        orders = Counter(x["order"] for x in recs if x["order"] is not None)
        lines.append(
            f"[waker={comm}-{pid}] count={len(recs)}, total_dur={sum(durs):.3f} ms, "
            f"avg_dur={sum(durs)/len(durs):.3f} ms, orders={dict(orders)}"
        )
    lines.append("")

    lines.append("==== 4. Top 20 Longest Cycles ====")
    TOP = 20
    for idx, r in enumerate(
        sorted(records, key=lambda x: x["duration_ms"], reverse=True)[:TOP], 1
    ):
        lines.append(
            f"[#{idx:02d}] dur={r['duration_ms']:.3f} ms, nid={r['nid']}, order={r['order']}, "
            f"gfp_flags={r['gfp_flags']}, kswapd={r['comm']}-{r['pid']} (cpu {r['cpu_begin']}->{r['cpu_end']}), "
            f"begin_ts={r['begin_ts']:.6f}, end_ts={r['end_ts']:.6f}, "
            f"waker={r['waker_comm']}-{r['waker_pid']} at {r['waker_ts']:.6f}"
        )
    lines.append("")
    lines.append("==== End of Report ====")

    return "\n".join(lines)


def remove_quotes(path: str) -> str:
    """Strip wrapping quotes for consistent CLI input handling."""
    if (path.startswith('"') and path.endswith('"')) or (
        path.startswith("'") and path.endswith("'")
    ):
        return path[1:-1]
    return path


def collect_input_files(path_str: str) -> list[str]:
    """Accept file or directory path and return a sorted list of files to parse."""
    path = remove_quotes(path_str.strip())
    if os.path.isdir(path):
        files = [os.path.join(path, name) for name in sorted(os.listdir(path))]
        return [f for f in files if os.path.isfile(f)]
    return [path]


def parse_ftrace_file(
    input_path: str, output_dir: Optional[str] = None, quiet: bool = False
) -> tuple[str, str] | None:
    """
    解析 kswapd 相关 ftrace，返回报告/CSV 路径。
    input_path 可为文件或目录；output_dir 为空时使用 state.FILE_DIR 或当前目录。
    """
    input_files = collect_input_files(input_path)
    if not input_files:
        print(f"未找到可解析的文件: {input_path}")
        return None

    output_dir = output_dir or state.FILE_DIR or os.getcwd()
    if not output_dir:
        output_dir = os.getcwd()
    assert output_dir is not None
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "kswapd_report.txt")
    csv_path = os.path.join(output_dir, "kswapd_cycles.csv")

    last_wakeup: dict[int, dict[str, Any]] = {}
    active: dict[int, list[dict[str, Any]]] = defaultdict(list)
    records: list[dict[str, Any]] = []

    for file_path in input_files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, 1):
                    _ = line_no
                    parsed = parse_line(line)
                    if not parsed:
                        continue

                    event = str(parsed.get("event") or "")

                    if event == "mm_vmscan_wakeup_kswapd":
                        nid = parsed.get("nid")
                        if not isinstance(nid, int):
                            continue
                        last_wakeup[nid] = {
                            "ts": parsed["ts"],
                            "comm": parsed["comm"],
                            "pid": parsed["pid"],
                            "cpu": parsed["cpu"],
                            "order": parsed["order"],
                            "gfp_flags": parsed["gfp_flags"],
                        }
                        continue

                    pid = parsed.get("pid")
                    nid = parsed.get("nid")
                    if not isinstance(pid, int):
                        continue

                    if event == "mm_vmscan_kswapd_wake":
                        if not isinstance(nid, int):
                            nid = -1
                        trigger = last_wakeup.get(nid, {})
                        parsed["waker_comm"] = trigger.get("comm")
                        parsed["waker_pid"] = trigger.get("pid")
                        parsed["waker_ts"] = trigger.get("ts")
                        parsed["gfp_flags"] = parsed["gfp_flags"] or trigger.get(
                            "gfp_flags"
                        )
                        parsed["order"] = (
                            parsed["order"]
                            if parsed["order"] is not None
                            else trigger.get("order")
                        )
                        active[pid].append(parsed)
                        continue

                    if event == "mm_vmscan_kswapd_sleep":
                        stack = active.get(pid, [])
                        if not stack:
                            continue
                        begin = stack.pop()
                        duration_ms = (parsed["ts"] - begin["ts"]) * 1000.0
                        records.append(
                            {
                                "comm": begin["comm"],
                                "pid": pid,
                                "cpu_begin": begin["cpu"],
                                "cpu_end": parsed["cpu"],
                                "begin_ts": begin["ts"],
                                "end_ts": parsed["ts"],
                                "duration_ms": duration_ms,
                                "nid": begin["nid"],
                                "order": begin["order"],
                                "gfp_flags": begin["gfp_flags"],
                                "waker_comm": begin.get("waker_comm"),
                                "waker_pid": begin.get("waker_pid"),
                                "waker_ts": begin.get("waker_ts"),
                            }
                        )
        except FileNotFoundError:
            print(f"[WARN] 文件不存在，跳过: {file_path}")
        except Exception as e:
            print(f"[WARN] 解析文件时出错 {file_path}: {e}")

    records.sort(key=lambda r: r["begin_ts"])
    report = build_report(records)
    if not quiet:
        print(report)

    try:
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(report)
        if not quiet:
            print(f"\nText report saved to: {report_path}")
    except Exception as e:
        print(
            f"\n[WARN] Failed to write report file {report_path}: {e}",
            file=sys.stderr,
        )

    fieldnames = [
        "comm",
        "pid",
        "cpu_begin",
        "cpu_end",
        "begin_ts",
        "end_ts",
        "duration_ms",
        "nid",
        "order",
        "gfp_flags",
        "waker_comm",
        "waker_pid",
        "waker_ts",
    ]
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                writer.writerow(r)
        if not quiet:
            print(f"Per-cycle records written to {csv_path}")
    except Exception as e:
        print(f"[WARN] Failed to write CSV {csv_path}: {e}", file=sys.stderr)

    return report_path, csv_path


def main():
    print("=" * 50)
    print("Kswapd ftrace 解析工具")
    print("=" * 50)

    user_path = input("请输入ftrace文件或目录路径: ").strip()
    parse_ftrace_file(user_path)


if __name__ == "__main__":
    main()
