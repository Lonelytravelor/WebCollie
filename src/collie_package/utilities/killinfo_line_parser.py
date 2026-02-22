import re
from typing import Dict, List, Optional, Tuple

from collie_package.log_tools.parse_cont_startup import (
    AM_KILL_PATTERN,
    KILLINFO_PATTERN,
    LMK_PATTERN,
    KILL_TYPE_MAP,
    describe_min_score,
    _looks_like_spurious_killinfo,
    parse_am_kill_payload,
    parse_killinfo_payload,
)


_LMK_FALLBACK_PATTERN = re.compile(
    r"lowmemorykiller:\s*(?:Kill|Killing)\s*['\"]?(?P<process>[^\s'\"(]+)['\"]?"
    r"\s*(?:\((?:pid\s*)?(?P<pid>\d+)[^)]*\)|pid\s*(?P<pid_alt>\d+))?(?P<tail>.*)",
    re.IGNORECASE,
)

_KILLINFO_FALLBACK_PATTERN = re.compile(r"killinfo:\s*\[(?P<payload>[^\]]+)\]", re.IGNORECASE)
_AM_KILL_FALLBACK_PATTERN = re.compile(r"am_kill\s*:\s*\[(?P<payload>[^\]]+)\]", re.IGNORECASE)
_KILL_KI_PATTERN = re.compile(
    r"(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*?"
    r"\[(?P<part1>[Kk]ill[^\]]*|[Tt]rig[^\]]*|[Ss]kip[^\]]*)\]\s*"
    r"\[(?P<part2>[^\]]+)\]\s*\[(?P<part3>[^\]]+)\]"
)
_KILL_KI_FALLBACK_PATTERN = re.compile(
    r"\[(?P<part1>[Kk]ill[^\]]*|[Tt]rig[^\]]*|[Ss]kip[^\]]*)\]\s*"
    r"\[(?P<part2>[^\]]+)\]\s*\[(?P<part3>[^\]]+)\]"
)


_FIELDS_ORDER = [
    ("process_name", "process"),
    ("pid", "pid"),
    ("uid", "uid"),
    ("adj", "adj"),
    ("min_adj", "min_adj"),
    ("rss_kb", "rss_kb"),
    ("proc_swap_kb", "swap_kb"),
    ("kill_reason", "reason"),
    ("mem_total_kb", "mem_total"),
    ("mem_free_kb", "mem_free"),
    ("cached_kb", "cached"),
    ("swap_free_kb", "swap_free"),
    ("thrashing", "thrashing"),
    ("max_thrashing", "thrash_max"),
    ("psi_mem_some", "psi_mem_some"),
    ("psi_mem_full", "psi_mem_full"),
    ("psi_io_some", "psi_io_some"),
    ("psi_io_full", "psi_io_full"),
    ("psi_cpu_some", "psi_cpu_some"),
]


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _extract_payload_from_line(line: str, pattern: re.Pattern) -> Optional[str]:
    match = pattern.search(line)
    if match:
        payload = match.group("payload")
        return payload.strip()
    return None


def _parse_lmk_line(line: str) -> Optional[Dict[str, str]]:
    match = LMK_PATTERN.search(line)
    if match:
        ts = match.group("ts")
        process = match.group("process")
        pid = match.group("pid") or match.group("pid_alt") or ""
        tail = match.group("tail") or ""
    else:
        match = _LMK_FALLBACK_PATTERN.search(line)
        if not match:
            return None
        ts = ""
        process = match.group("process")
        pid = match.group("pid") or match.group("pid_alt") or ""
        tail = match.group("tail") or ""

    reason_match = re.search(r"(?:reason|kill_reason)\\s+([A-Za-z0-9_-]+)", tail)
    reason = reason_match.group(1) if reason_match else ""

    return {
        "ts": ts or "",
        "process": process or "",
        "pid": pid or "",
        "tail": tail.strip(),
        "reason": reason or "",
    }


def _parse_killinfo_line(line: str) -> Optional[Tuple[str, List[str], Dict[str, str]]]:
    match = KILLINFO_PATTERN.search(line)
    ts = ""
    payload = None
    if match:
        ts = match.group("ts")
        payload = match.group("payload")
    else:
        payload = _extract_payload_from_line(line, _KILLINFO_FALLBACK_PATTERN)

    if not payload:
        text = line.strip()
        if text.startswith("[") and text.endswith("]"):
            payload = text[1:-1].strip()
        elif "," in text:
            payload = text

    if not payload:
        return None

    fields, parsed = parse_killinfo_payload(payload)
    return ts or "", fields, parsed


def _parse_am_kill_line(line: str) -> Optional[Tuple[str, List[str], Dict[str, str]]]:
    match = AM_KILL_PATTERN.search(line)
    ts = ""
    payload = None
    if match:
        ts = match.group("ts")
        payload = match.group("payload")
    else:
        payload = _extract_payload_from_line(line, _AM_KILL_FALLBACK_PATTERN)

    if not payload:
        return None

    fields, parsed = parse_am_kill_payload(payload)
    return ts or "", fields, parsed


def parse_kill_line_text(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        raise ValueError("输入内容为空")

    kill_ki = _parse_kill_ki_line(text)
    if kill_ki:
        return _format_kill_ki_result(kill_ki)

    lowered = text.lower()
    lmk_info = None
    if "lowmemorykiller" in lowered:
        lmk_info = _parse_lmk_line(text)
    if lmk_info:
        return _format_lmk_result(lmk_info)

    if "am_kill" in lowered:
        am_kill_result = _parse_am_kill_line(text)
        if am_kill_result:
            ts, fields, parsed = am_kill_result
            return _format_am_kill_result(ts, fields, parsed)

    killinfo_result = _parse_killinfo_line(text)
    if killinfo_result:
        ts, fields, parsed = killinfo_result
        return _format_killinfo_result(ts, fields, parsed)

    am_kill_result = _parse_am_kill_line(text)
    if am_kill_result:
        ts, fields, parsed = am_kill_result
        return _format_am_kill_result(ts, fields, parsed)

    if text.startswith("[") and text.endswith("]"):
        raw_payload = text[1:-1].strip()
        if raw_payload:
            fields = [f.strip() for f in raw_payload.split(",")]
            if len(fields) <= 6:
                _, parsed = parse_am_kill_payload(raw_payload)
                return _format_am_kill_result("", fields, parsed)
            fields, parsed = parse_killinfo_payload(raw_payload)
            return _format_killinfo_result("", fields, parsed)

    if "," in text and "am_kill" not in lowered:
        fields = [f.strip() for f in text.split(",")]
        if len(fields) <= 6:
            _, parsed = parse_am_kill_payload(text)
            return _format_am_kill_result("", fields, parsed)
        fields, parsed = parse_killinfo_payload(text)
        return _format_killinfo_result("", fields, parsed)

    raise ValueError("无法识别输入内容，请输入包含 killinfo/am_kill/lowmemorykiller/kill ki 的单行日志")


def _parse_kill_ki_line(line: str) -> Optional[Dict[str, str]]:
    match = _KILL_KI_PATTERN.search(line)
    if match:
        ts = match.group("ts")
        part1 = match.group("part1")
        part2 = match.group("part2")
        part3 = match.group("part3")
    else:
        match = _KILL_KI_FALLBACK_PATTERN.search(line)
        if not match:
            return None
        ts = ""
        part1 = match.group("part1")
        part2 = match.group("part2")
        part3 = match.group("part3")

    part1_list = [p.strip() for p in part1.split("|")]
    part2_list = [p.strip() for p in part2.split("|")]
    part3_list = [p.strip() for p in part3.split("|")]

    if len(part1_list) < 11 or len(part2_list) < 10 or len(part3_list) < 6:
        return None

    return {
        "ts": ts,
        "part1": part1_list,
        "part2": part2_list,
        "part3": part3_list,
    }


def _format_killinfo_result(ts: str, fields: List[str], parsed: Dict[str, str]) -> str:
    lines = ["解析类型: killinfo"]
    lines.append(f"时间: {ts or '未提供'}")
    if _looks_like_spurious_killinfo(fields):
        lines.append("提示: 该 killinfo 仅包含数字字段，可能为无效/噪声记录")

    lines.append("基本信息:")
    lines.append(f"  进程: {parsed.get('process_name', '') or '-'}")
    lines.append(f"  pid: {parsed.get('pid', '') or '-'}")
    lines.append(f"  uid: {parsed.get('uid', '') or '-'}")
    lines.append(f"  adj: {parsed.get('adj', '') or '-'}")
    lines.append(f"  min_adj: {parsed.get('min_adj', '') or '-'}")
    lines.append(f"  rss_kb: {parsed.get('rss_kb', '') or '-'}")
    lines.append(f"  kill_reason: {parsed.get('kill_reason', '') or '-'}")

    detail_lines = []
    for key, label in _FIELDS_ORDER:
        value = parsed.get(key, "")
        if value == "" or key in {"process_name", "pid", "uid", "adj", "min_adj", "rss_kb", "kill_reason"}:
            continue
        detail_lines.append(f"  {label:<12}: {value}")

    af = _safe_int(parsed.get("active_file_kb"))
    inf = _safe_int(parsed.get("inactive_file_kb"))
    if af is not None and inf is not None:
        detail_lines.append(f"  file_pages  : {af + inf} (inactive {inf} active {af})")
    aa = _safe_int(parsed.get("active_anon_kb"))
    ina = _safe_int(parsed.get("inactive_anon_kb"))
    if aa is not None and ina is not None:
        detail_lines.append(f"  anon_pages  : {aa + ina} (inactive {ina} active {aa})")

    if detail_lines:
        lines.append("内存/压力信息:")
        lines.extend(detail_lines)

    lines.append("原始字段:")
    lines.append("  [" + ", ".join(fields) + "]")
    return "\n".join(lines)


def _format_am_kill_result(ts: str, fields: List[str], parsed: Dict[str, str]) -> str:
    lines = ["解析类型: am_kill"]
    lines.append(f"时间: {ts or '未提供'}")
    lines.append("基本信息:")
    lines.append(f"  进程: {parsed.get('process_name', '') or '-'}")
    lines.append(f"  pid: {parsed.get('pid', '') or '-'}")
    lines.append(f"  uid: {parsed.get('uid', '') or '-'}")
    lines.append(f"  adj: {parsed.get('adj', '') or '-'}")
    lines.append(f"  reason: {parsed.get('reason', '') or '-'}")
    lines.append(f"  pss_kb: {parsed.get('pss_kb', '') or '-'}")
    lines.append("原始字段:")
    lines.append("  [" + ", ".join(fields) + "]")
    return "\n".join(lines)


def _format_lmk_result(info: Dict[str, str]) -> str:
    lines = ["解析类型: lowmemorykiller"]
    lines.append(f"时间: {info.get('ts') or '未提供'}")
    lines.append("基本信息:")
    lines.append(f"  进程: {info.get('process') or '-'}")
    lines.append(f"  pid: {info.get('pid') or '-'}")
    if info.get("reason"):
        lines.append(f"  reason: {info.get('reason')}")
    if info.get("tail"):
        lines.append("附加信息:")
        lines.append(f"  {info.get('tail')}")
    return "\n".join(lines)


def _format_kill_ki_result(info: Dict[str, List[str]]) -> str:
    part1 = info["part1"]
    part2 = info["part2"]
    part3 = info["part3"]

    kill_type = part1[1]
    kill_type_desc = KILL_TYPE_MAP.get(kill_type, f"未知({kill_type})")
    min_score = part1[2]
    min_score_desc = describe_min_score(min_score)

    lines = ["解析类型: kill ki"]
    lines.append(f"时间: {info.get('ts') or '未提供'}")
    lines.append("查杀信息:")
    lines.append(f"  查杀类型: {kill_type_desc} ({kill_type})")
    lines.append(f"  可查杀最低分值: {min_score} ({min_score_desc})")
    lines.append(f"  可查杀进程数: {part1[3]}")
    lines.append(f"  重要应用数量: {part1[4]}")
    lines.append(f"  本次已清理进程数: {part1[5]}")
    lines.append(f"  已清理重要进程数: {part1[6]}")
    lines.append(f"  跳过计数: {part1[7]}")
    lines.append(f"  目标内存: {part1[8]} KB")
    lines.append(f"  需要释放内存: {part1[9]} KB")
    lines.append(f"  已释放内存: {part1[10]} KB")

    lines.append("进程信息:")
    lines.append(f"  进程: {part2[0]}")
    lines.append(f"  uid: {part2[1]}")
    lines.append(f"  pid: {part2[2]}")
    lines.append(f"  adj: {part2[3]}")
    lines.append(f"  score: {part2[4]}")
    lines.append(f"  pss: {part2[5]} KB")
    lines.append(f"  swapUsed: {part2[6]} KB")
    lines.append(f"  ret: {part2[7]}")
    lines.append(f"  isMain: {part2[8]}")
    lines.append(f"  isImp: {part2[9]}")

    lines.append("内存信息:")
    lines.append(f"  memFree: {part3[0]} KB")
    lines.append(f"  memAvail: {part3[1]} KB")
    lines.append(f"  memFile: {part3[2]} KB")
    lines.append(f"  memAnon: {part3[3]} KB")
    lines.append(f"  memSwapFree: {part3[4]} KB")
    lines.append(f"  cmaFree: {part3[5]} KB")
    if len(part3) > 6:
        lines.append(f"  extra: {' | '.join(part3[6:])}")

    lines.append("原始字段:")
    lines.append("  [" + " | ".join(part1) + "]")
    lines.append("  [" + " | ".join(part2) + "]")
    lines.append("  [" + " | ".join(part3) + "]")
    return "\n".join(lines)


def main():
    print("单行 killinfo/am_kill/lowmemorykiller 解析")
    while True:
        raw = input("请输入单行日志（q 退出）: ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return
        try:
            result = parse_kill_line_text(raw)
            print("\n" + result + "\n")
        except Exception as exc:  # noqa: BLE001
            print(f"解析失败: {exc}")


if __name__ == "__main__":
    main()
