#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
交互式解析 dumpsys meminfo：
- 直接回车：通过 adb 从当前设备抓取 dumpsys meminfo 并解析
- 输入 bugreport txt 路径：从文件中提取 dumpsys meminfo 内容后解析

输出：分析结果会保存到 txt 文件，默认写入 state.FILE_DIR（无则写入当前目录）。

分析要点：
1. Total PSS by process：统计总进程数、总 PSS，并列出前 20 占用
2. PSS by OOM adjustment：每类的进程数量、总占用、Top3 重点进程
3. 全局内存状态：Total/Free/Used/Lost、DMA-BUF 等
4. ZRAM：物理占用、换出量、swap 总量及使用率
"""

import datetime
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from .. import state
from ..config_loader import load_rules_config

_RULES = load_rules_config()
_MEM_RULES = _RULES.get('meminfo_summary', {}) if isinstance(_RULES, dict) else {}
KB_IN_MB = int(_MEM_RULES.get('kb_in_mb', 1024))


# ------------------- 公共辅助 ------------------- #

def _parse_kb(value: str) -> int:
    return int(value.replace(",", "").strip())


def _kb_to_mb(kb: int) -> float:
    return kb / KB_IN_MB


def _format_kb_mb(kb: int) -> str:
    return f"{kb:,}K ({_kb_to_mb(kb):.1f} MB)"


def _format_percent(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # pragma: no cover - 防御性处理
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _detect_output_dir() -> str:
    base_dir = state.FILE_DIR or "."
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


# ------------------- 数据抓取 ------------------- #

def _ask_source() -> Tuple[str, Optional[str]]:
    """
    返回 ("device", None) 或 ("file", path)
    """
    while True:
        user_input = input("回车直接抓取当前设备 dumpsys meminfo；或输入 bugreport txt 路径：").strip()
        normalized = user_input.strip("'\"")
        if normalized == "":
            return "device", None
        if os.path.isfile(normalized):
            return "file", normalized
        print("[WARN] 路径不存在，请重新输入。")


def _fetch_meminfo_from_device() -> str:
    serial = input("如需指定序列号请输入（直接回车默认设备）：").strip() or None
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", "dumpsys", "meminfo"]
    result = _run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"adb 执行失败: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _load_meminfo_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    # bugreport 可能包含多个 section，这里截取 meminfo 段落，若未匹配则返回全文
    start = content.find("Total PSS by process")
    if start == -1:
        return content
    end_marker = "duration of dumpsys meminfo"
    end = content.find(end_marker, start)
    if end != -1:
        # 包含 end_marker 行
        end = content.find("\n", end)
        return content[start:end] if end != -1 else content[start:]
    return content[start:]


# ------------------- 解析器 ------------------- #

def _parse_total_pss_by_process(text: str) -> Dict:
    start = text.find("Total PSS by process")
    if start == -1:
        return {"processes": [], "total_pss_kb": 0, "count": 0}

    lines = text[start:].splitlines()[1:]
    items = []
    line_re = re.compile(r"^\s*([\d,]+)K:\s*(.+)$")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if items:
                break
            continue
        # 遇到下一个顶级 section 结束
        if not line.startswith(" "):
            break
        if stripped.startswith("Total PSS by OOM"):
            break
        m = line_re.match(line)
        if not m:
            continue
        pss_kb = _parse_kb(m.group(1))
        remainder = m.group(2)
        proc_name = remainder.split("(pid")[0].strip()
        swap_match = re.search(r"([\d,]+)K in swap", line)
        swap_kb = _parse_kb(swap_match.group(1)) if swap_match else None
        items.append({"name": proc_name, "pss_kb": pss_kb, "swap_kb": swap_kb})

    total_kb = sum(i["pss_kb"] for i in items)
    items_sorted = sorted(items, key=lambda x: x["pss_kb"], reverse=True)
    return {
        "processes": items_sorted[:20],
        "total_pss_kb": total_kb,
        "count": len(items),
    }


def _parse_pss_by_oom(text: str) -> List[Dict]:
    start = text.find("Total PSS by OOM adjustment")
    if start == -1:
        return []

    lines = text[start:].splitlines()[1:]
    cat_line_re = re.compile(r"^\s*([\d,]+)K:\s*([^(]+)")
    proc_line_re = re.compile(r"^\s*([\d,]+)K:\s*(.+)$")

    categories: List[Dict] = []
    current = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                # 遇到空行且已进入 section，认为结束
                break
            continue
        if not line.startswith(" "):
            break
        if stripped.startswith("Total PSS by category"):
            break

        if "(pid" in line:
            m_proc = proc_line_re.match(line)
            if not m_proc or current is None:
                continue
            pss_kb = _parse_kb(m_proc.group(1))
            proc_name = m_proc.group(2).split("(pid")[0].strip()
            swap_match = re.search(r"([\d,]+)K in swap", line)
            swap_kb = _parse_kb(swap_match.group(1)) if swap_match else None
            current["processes"].append({"name": proc_name, "pss_kb": pss_kb, "swap_kb": swap_kb})
            continue

        m_cat = cat_line_re.match(line)
        if not m_cat:
            continue
        pss_kb = _parse_kb(m_cat.group(1))
        label = m_cat.group(2).strip()
        swap_match = re.search(r"([\d,]+)K in swap", line)
        swap_kb = _parse_kb(swap_match.group(1)) if swap_match else None
        current = {
            "name": label,
            "total_pss_kb": pss_kb,
            "swap_kb": swap_kb,
            "processes": [],
        }
        categories.append(current)

    for cat in categories:
        cat["process_count"] = len(cat["processes"])
        cat["top_processes"] = sorted(cat["processes"], key=lambda x: x["pss_kb"], reverse=True)[:5]

    return categories


def _parse_global_status(text: str) -> Dict[str, str]:
    stats: Dict[str, str] = {}
    line_re = re.compile(r"^\s*([A-Za-z0-9 /]+):\s*([\d,]+)K(.*)$")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        stats[key] = f"{m.group(2)}K{m.group(3)}"
    return stats


def _parse_zram(text: str) -> Dict[str, Optional[int]]:
    zram_line = None
    for line in text.splitlines():
        if line.strip().startswith("ZRAM:"):
            zram_line = line.strip()
            break
    if not zram_line:
        return {}

    # ZRAM: 1,014,332K physical used for 2,962,248K in swap (8,388,604K total swap)
    phys = re.search(r":\s*([\d,]+)K", zram_line)
    swap_used = re.search(r"used for\s*([\d,]+)K in swap", zram_line)
    total_swap = re.search(r"\(([\d,]+)K total swap", zram_line)

    data = {}
    if phys:
        data["physical_kb"] = _parse_kb(phys.group(1))
    if swap_used:
        data["swap_used_kb"] = _parse_kb(swap_used.group(1))
    if total_swap:
        data["total_swap_kb"] = _parse_kb(total_swap.group(1))

    # 解析“total swap pss”用于估算整体换出
    swap_pss_match = re.search(r"total swap pss \+?\s*([\d,]+)K", text)
    if swap_pss_match:
        data["swap_pss_kb"] = _parse_kb(swap_pss_match.group(1))
    return data


# ------------------- 报告生成 ------------------- #

def _render_total_process_section(info: Dict) -> List[str]:
    lines = []
    lines.append("## Total PSS by process (Top 20)")
    top20_sum = sum(p["pss_kb"] for p in info["processes"])
    ratio = (top20_sum / info["total_pss_kb"]) if info["total_pss_kb"] else 0
    lines.append(f"- 总进程数：{info['count']}, 总 PSS：{_format_kb_mb(info['total_pss_kb'])}")
    lines.append(f"- Top20 合计：{_format_kb_mb(top20_sum)}，占全部进程 {_format_percent(ratio)}")
    for idx, proc in enumerate(info["processes"], 1):
        swap_text = f" | swap {_format_kb_mb(proc['swap_kb'])}" if proc.get("swap_kb") is not None else ""
        lines.append(f"- #{idx:02d} {_format_kb_mb(proc['pss_kb'])}{swap_text} : {proc['name']}")
    lines.append("")
    return lines


def _render_oom_section(categories: List[Dict]) -> List[str]:
    lines = []
    lines.append("## PSS by OOM adjustment")
    if not categories:
        lines.append("- 未找到该段落")
        lines.append("")
        return lines

    for cat in categories:
        swap_part = f"，swap：{_format_kb_mb(cat['swap_kb'])}" if cat.get("swap_kb") is not None else ""
        lines.append(
            f"- {cat['name']}: 进程 {cat['process_count']} 个，合计 {_format_kb_mb(cat['total_pss_kb'])}{swap_part}"
        )
        if cat["top_processes"]:
            for p in cat["top_processes"]:
                swap_text = f"，swap {_format_kb_mb(p['swap_kb'])}" if p.get("swap_kb") is not None else ""
                lines.append(f"  - {p['name']}: PSS {_format_kb_mb(p['pss_kb'])}{swap_text}")
    lines.append("")
    return lines


def _classify_priority(name: str) -> str:
    """根据 OOM 类别名称粗分优先级."""
    n = name.lower()
    necessary = ["native", "system", "persistent", "persistent service"]
    high = ["foreground", "visible", "perceptible", "home", "heavy weight", "previous"]
    low = ["perceptible low", "cached", "b services", "backup", "cached services", "empty"]
    if any(k in n for k in necessary):
        return "necessary"
    if any(k in n for k in high):
        return "high"
    if any(k in n for k in low):
        return "low"
    return "other"


def _render_priority_summary(categories: List[Dict]) -> List[str]:
    lines: List[str] = []
    lines.append("## 优先级视角 (基于 OOM 类别)")
    if not categories:
        lines.append("- 无分类数据")
        lines.append("")
        return lines

    summary = {
        "necessary": {"pss": 0, "count": 0, "procs": []},
        "high": {"pss": 0, "count": 0, "procs": []},
        "low": {"pss": 0, "count": 0, "procs": []},
        "other": {"pss": 0, "count": 0, "procs": []},
    }

    for cat in categories:
        group = _classify_priority(cat["name"])
        summary[group]["pss"] += cat["total_pss_kb"]
        summary[group]["count"] += cat["process_count"]
        summary[group]["procs"].extend(cat.get("top_processes", []))

    for key, label in (
        ("necessary", "必要 (system/native/persistent)"),
        ("high", "高优先级 (前台/可见/播放等)"),
        ("low", "低优先级 (缓存等)"),
        ("other", "其它"),
    ):
        data = summary[key]
        lines.append(f"- {label}: 进程 {data['count']} 个，合计 {_format_kb_mb(data['pss'])}")
        if data["procs"]:
            top_combined = sorted(data["procs"], key=lambda x: x["pss_kb"], reverse=True)[:5]
            lines.append("  代表进程：")
            for p in top_combined:
                swap_text = f"，swap {_format_kb_mb(p['swap_kb'])}" if p.get("swap_kb") is not None else ""
                lines.append(f"  - {p['name']}: {_format_kb_mb(p['pss_kb'])}{swap_text}")
    lines.append("")
    return lines


def _render_global_status(stats: Dict[str, str]) -> List[str]:
    lines = []
    lines.append("## 全局内存状态")
    if not stats:
        lines.append("- 未解析到 Total/Free/Used RAM 信息")
        lines.append("")
        return lines

    for key in ("Total RAM", "Free RAM", "Used RAM", "Lost RAM", "DMA-BUF", "DMA-BUF Heaps"):
        if key in stats:
            lines.append(f"- {key}: {stats[key]}")
    lines.append("")
    return lines


def _render_zram(zram: Dict[str, int]) -> List[str]:
    lines = []
    lines.append("## ZRAM / Swap")
    if not zram:
        lines.append("- 未找到 ZRAM 行")
        lines.append("")
        return lines

    phys = zram.get("physical_kb")
    swap_used = zram.get("swap_used_kb")
    total_swap = zram.get("total_swap_kb")
    if phys is not None:
        lines.append(f"- 物理占用: {_format_kb_mb(phys)}")
    if swap_used is not None:
        lines.append(f"- 已换出: {_format_kb_mb(swap_used)}")
    if total_swap is not None:
        lines.append(f"- Swap 总量: {_format_kb_mb(total_swap)}")
    if swap_used is not None and total_swap:
        ratio = swap_used / total_swap if total_swap else 0
        lines.append(f"- Swap 使用率: {ratio * 100:.1f}%")
    if zram.get("swap_pss_kb") is not None:
        lines.append(f"- total swap pss: {_format_kb_mb(zram['swap_pss_kb'])} (整体换出量)")
    lines.append("")
    return lines


def generate_report(raw_text: str, source_desc: str) -> str:
    total_proc = _parse_total_pss_by_process(raw_text)
    oom_categories = _parse_pss_by_oom(raw_text)
    global_stats = _parse_global_status(raw_text)
    zram_stats = _parse_zram(raw_text)

    sections: List[str] = []
    sections.append("# dumpsys meminfo 汇总")
    sections.append(f"# 生成时间: {datetime.datetime.now().isoformat(timespec='seconds')}")
    sections.append(f"# 数据来源: {source_desc}")
    sections.append("")

    sections += _render_total_process_section(total_proc)
    sections += _render_oom_section(oom_categories)
    sections += _render_priority_summary(oom_categories)
    sections += _render_global_status(global_stats)
    sections += _render_zram(zram_stats)

    # 额外建议
    if total_proc["processes"]:
        top1 = total_proc["processes"][0]
        sections.append(
            f"- 观察：最高占用进程 {top1['name']} 使用 {_format_kb_mb(top1['pss_kb'])}，"
            f"可结合实际场景确认是否异常。"
        )
    if oom_categories:
        heavy_cat = max(oom_categories, key=lambda c: c["total_pss_kb"])
        sections.append(
            f"- 观察：OOM 分类占用最大的是 {heavy_cat['name']} ({_format_kb_mb(heavy_cat['total_pss_kb'])})，"
            "建议重点检查该类中的前几名进程。"
        )
    sections.append("")

    return "\n".join(sections)


# ------------------- CLI 入口 ------------------- #

def main() -> None:
    source, path = _ask_source()
    try:
        if source == "device":
            raw_text = _fetch_meminfo_from_device()
            source_desc = "adb shell dumpsys meminfo (当前设备)"
        else:
            raw_text = _load_meminfo_from_file(path)  # type: ignore[arg-type]
            source_desc = f"bugreport 文件: {path}"
    except Exception as exc:
        print(f"[ERROR] 抓取/读取 meminfo 失败：{exc}")
        print("请检查设备连接/adb root，或改用 bugreport txt 解析后重试。")
        return

    report = generate_report(raw_text, source_desc)
    out_dir = _detect_output_dir()
    filename = f"meminfo_summary_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"[INFO] 解析完成，结果已保存：{out_path}")


if __name__ == "__main__":
    main()
