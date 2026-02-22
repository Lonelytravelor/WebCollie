#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
收集 Android 设备的内存参数和 system properties，支持两种来源：
1. 解析本地 bugreport 文本文件
2. 从当前连接设备通过 adb 抓取（要求 root）

使用方式：
    python collect_device_meminfo.py
    python collect_device_meminfo.py --serial <device-serial> --output my_dump.txt

运行后会先询问你是否输入 bugreport 文件路径：
    - 输入现有文件路径：解析 bugreport
    - 直接回车：从当前设备 adb 抓取
    - 其它情况：提示重新输入
"""

import argparse
import datetime
import subprocess
import sys
import os
import re
from typing import List, Dict, Optional

# 采集的内存相关命令（live 模式下会真实执行；bugreport 模式下只尝试还原部分）
MEMORY_COMMANDS: Dict[str, str] = {
    "proc/meminfo": "cat /proc/meminfo",
    "proc/vmstat": "cat /proc/vmstat",
    "proc/zoneinfo": "cat /proc/zoneinfo",
    "proc/buddyinfo": "cat /proc/buddyinfo",
    "proc/pagetypeinfo": "cat /proc/pagetypeinfo 2>/dev/null || echo 'pagetypeinfo not available'",
    "proc/slabinfo": "cat /proc/slabinfo 2>/dev/null || echo 'slabinfo not available'",
    "dumpsys_meminfo": "dumpsys meminfo",
    "dumpsys_procstats": "dumpsys procstats",
    "proc/swaps": "cat /proc/swaps 2>/dev/null || echo 'no /proc/swaps'",
    # ZRAM
    "zram_conf": (
        "for d in /sys/block/zram*; do "
        "  echo \"# $d\"; "
        "  cat \"$d/disksize\" 2>/dev/null; "
        "  cat \"$d/mem_used_total\" 2>/dev/null; "
        "done 2>/dev/null || echo 'no zram'"
    ),

    # ===== vm sysctl：内存强相关参数 =====
    # 输出格式统一为：vm.<name> = <value>
    "vm_sysctl": (
        "echo '## /proc/sys/vm core settings'; "
        "for f in "
        "  min_free_kbytes "
        "  extra_free_kbytes "
        "  watermark_scale_factor "
        "  watermark_boost_factor "
        "  lowmem_reserve_ratio "
        "  swappiness "
        "  vfs_cache_pressure "
        "  dirty_background_ratio "
        "  dirty_ratio "
        "  dirty_background_bytes "
        "  dirty_bytes "
        "  overcommit_memory "
        "  overcommit_ratio "
        "  zone_reclaim_mode "
        "  min_slab_ratio "
        "  min_unmapped_ratio "
        "  percpu_pagelist_fraction "
        "  compact_unevictable_allowed "
        "  compact_defer_shift "
        "; do "
        "  if [ -f /proc/sys/vm/$f ]; then "
        "    echo \"vm.$f = $(cat /proc/sys/vm/$f)\"; "
        "  fi; "
        "done"
    ),

    # ===== 透明大页 THP 相关 =====
    "thp_enabled": "cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'no THP enabled file'",
    "thp_defrag": "cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || echo 'no THP defrag file'",
    "thp_khugepaged": (
        "if [ -d /sys/kernel/mm/transparent_hugepage/khugepaged ]; then "
        "  for f in /sys/kernel/mm/transparent_hugepage/khugepaged/*; do "
        "    echo \"# $f\"; cat \"$f\"; echo; "
        "  done; "
        "else "
        "  echo 'no khugepaged config dir'; "
        "fi"
    ),

    # ===== KO / 模块列表 =====
    "lsmod": "lsmod 2>/dev/null || cat /proc/modules 2>/dev/null || echo 'no lsmod or /proc/modules'",

    # ===== 内存相关节点（连续启动基线） =====
    "greclaim_parm": "cat /sys/kernel/mi_reclaim/greclaim_parm 2>/dev/null || echo 'no greclaim_parm'",
    "process_use_count": "cat /sys/kernel/mi_mempool/process_use_count 2>/dev/null || echo 'no process_use_count'",
}


# ------------- 通用本地命令 & adb -------------

def run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120
        )
        return result
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", f"{e}")


def adb_shell(shell_cmd: str, serial: Optional[str] = None) -> subprocess.CompletedProcess:
    base_cmd = ["adb"]
    if serial:
        base_cmd += ["-s", serial]
    base_cmd += ["shell", shell_cmd]
    return run_cmd(base_cmd)


def adb_getprop_raw(serial: Optional[str] = None) -> str:
    """一次性抓取 getprop 原始输出。"""
    result = adb_shell("getprop", serial)
    if result.returncode != 0:
        return f"<ERROR: {result.stderr.strip()}>"
    return result.stdout

def normalize_path(path_str: str) -> str:
    """
    处理用户输入的路径字符串：
    - 去掉首尾空格
    - 如果整个字符串被一对 '...' 或 "..." 包住，则去掉最外层引号
    """
    s = path_str.strip()
    if (len(s) >= 2) and ((s[0] == s[-1]) and s[0] in ("'", '"')):
        s = s[1:-1]
    return s


# ------------- live 模式：properties & memory -------------

def collect_properties_from_device(serial: Optional[str]) -> str:
    """
    收集全部 property 信息：
    - adb shell getprop
    - 把 [key]: [value] 转成 key = value 形式
    """
    lines: List[str] = []
    lines.append("===== PROPERTIES START =====")

    raw = adb_getprop_raw(serial)
    if raw.startswith("<ERROR:"):
        lines.append(raw)
    else:
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            # [key]: [value]
            if "]: [" in s and s.startswith("[") and s.endswith("]"):
                inner = s[1:-1]
                key, val = inner.split("]: [", 1)
                lines.append(f"{key} = {val}")
            else:
                lines.append(s)

    lines.append("===== PROPERTIES END =====")
    lines.append("")
    return "\n".join(lines)


def collect_memory_info_from_device(serial: Optional[str]) -> str:
    sections: List[str] = []
    sections.append("===== MEMORY INFO START =====")
    for name, shell_cmd in MEMORY_COMMANDS.items():
        sections.append(f"----- SECTION: {name} -----")
        sections.append(f"# CMD: {shell_cmd}")
        result = adb_shell(shell_cmd, serial)
        if result.returncode != 0:
            sections.append(f"<ERROR: cmd failed: {result.stderr.strip()}>")
        else:
            sections.append(result.stdout.rstrip("\n"))
        sections.append("")
    sections.append("===== MEMORY INFO END =====")
    sections.append("")
    return "\n".join(sections)


# ------------- bugreport 解析 -------------

def load_bugreport_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def parse_bugreport_properties(lines: List[str]) -> str:
    """
    从 bugreport 文本中解析全部 property：
    逻辑：扫描全文件，匹配类似 [key]: [value] 的行。
    """
    out: List[str] = []
    out.append("===== PROPERTIES START =====")

    pattern = re.compile(r"^\[(.+?)\]: \[(.*)\]$")
    found_any = False

    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = pattern.match(s)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2)
        out.append(f"{key} = {val}")
        found_any = True

    if not found_any:
        out.append("<WARN: no [key]: [value] style properties found in bugreport>")

    out.append("===== PROPERTIES END =====")
    out.append("")
    return "\n".join(out)


def extract_proc_block_from_bugreport(lines: List[str], proc_path: str) -> str:
    """
    从 bugreport 中提取形如 "/proc/meminfo" 的内容。

    寻找类似：
      ------ /proc/meminfo ------
      <内容...>
      ------ 其它 section ------
    """
    start_idx = None
    for i, line in enumerate(lines):
        if "------" in line and proc_path in line:
            start_idx = i + 1
            break

    if start_idx is None:
        return f"<not found in bugreport: {proc_path}>"

    collected: List[str] = []
    for j in range(start_idx, len(lines)):
        l = lines[j]
        if l.startswith("------ ") and " ------" in l:
            break
        collected.append(l.rstrip("\n"))

    if not collected:
        return f"<empty section in bugreport: {proc_path}>"

    return "\n".join(collected)


def collect_memory_info_from_bugreport(lines: List[str], bugreport_path: str) -> str:
    """
    根据 MEMORY_COMMANDS 的 key 生成各 section：
    - 对于 "proc/xxx" 这类，尝试从 bugreport 中提取 "/proc/xxx" 部分。
    - 其它（vm_sysctl / THP / zram_conf / lsmod / dumpsys），bugreport 未必有，
      统一给出说明。
    """
    sections: List[str] = []
    sections.append("===== MEMORY INFO START =====")

    for name, shell_cmd in MEMORY_COMMANDS.items():
        sections.append(f"----- SECTION: {name} -----")
        sections.append(f"# SRC: bugreport {os.path.basename(bugreport_path)}")

        if name.startswith("proc/"):
            # 例如 name = "proc/meminfo" -> "/proc/meminfo"
            proc_path = "/" + name
            content = extract_proc_block_from_bugreport(lines, proc_path)
            sections.append(content)
        else:
            # 目前 bugreport 很少系统性 dump /sys 或 sysctl，
            # 为了不误解析，这里直接说明不可用。
            sections.append(f"<not parsed from bugreport for section {name}>")

        sections.append("")

    sections.append("===== MEMORY INFO END =====")
    sections.append("")
    return "\n".join(sections)


# ------------- adb 设备检查 & root -------------

def check_adb_device(serial: Optional[str]) -> None:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += ["get-state"]
    result = run_cmd(cmd)
    if result.returncode != 0 or result.stdout.strip() not in ("device", "recovery", "rescue"):
        sys.stderr.write(
            f"[ERROR] adb get-state failed or device not ready. "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}\n"
        )
        sys.exit(1)


def ensure_root(serial: Optional[str]) -> None:
    """
    确保 adb shell 是 root：
    1. adb root
    2. 校验 id -u == 0
    """
    root_cmd = ["adb"]
    if serial:
        root_cmd += ["-s", serial]
    root_cmd += ["root"]

    res_root = run_cmd(root_cmd)
    if res_root.stderr:
        sys.stderr.write(f"[INFO] adb root stderr: {res_root.stderr.strip()}\n")

    res_id = adb_shell("id -u", serial)
    if res_id.returncode != 0:
        sys.stderr.write(f"[ERROR] 检查 root 权限失败：{res_id.stderr.strip()}\n")
        sys.exit(1)

    uid = res_id.stdout.strip()
    if uid != "0":
        sys.stderr.write(
            "[ERROR] 当前 adb shell 非 root (id -u != 0)。\n"
            "请确认设备为 userdebug/eng 且允许 adb root，然后重试。\n"
        )
        sys.exit(1)


# ------------- 交互：选择 bugreport 或 live -------------

def ask_bugreport_or_live() -> (str, Optional[str]):
    """
    交互式选择数据来源：
    - 输入 bugreport 文件路径 => ("bugreport", path)
    - 回车 => ("live", None)
    - 其它无效 => 循环重新输入
    """
    while True:
        print("请输入 bugreport 文本文件路径(不建议用bugreport解析)（回车则直接从当前设备抓取）：")
        inp = input("> ").strip()
        tmp = normalize_path(inp)
        if tmp == "":
            return "live", None
        if os.path.isfile(tmp):
            return "bugreport", tmp
        print("[WARN] 输入既不是空行也不是有效文件路径，请重新输入。")


# ------------- 其它 -------------

def build_default_output_filename(source: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if source == "bugreport":
        return f"android_mem_dump_from_bugreport_{ts}.txt"
    else:
        return f"android_mem_dump_{ts}.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="收集 Android 设备的内存参数和 system properties，支持 bugreport 或 adb live 抓取。"
    )
    parser.add_argument(
        "--serial", "-s",
        help="adb 设备序列号（多设备时指定），等价于 adb -s SERIAL ...（bugreport 模式下忽略）",
        default=None,
    )
    parser.add_argument(
        "--output", "-o",
        help="输出文件名（默认自动带时间戳生成）",
        default=None,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    mode, bugreport_path = ask_bugreport_or_live()

    if mode == "bugreport":
        # 从 bugreport 解析
        output_file = args.output or build_default_output_filename("bugreport")
        print(f"[INFO] 使用 bugreport 模式，文件: {bugreport_path}")
        lines = load_bugreport_lines(bugreport_path)

        prop_text = parse_bugreport_properties(lines)
        mem_text = collect_memory_info_from_bugreport(lines, bugreport_path)

        header: List[str] = []
        header.append("# Android memory + properties snapshot (from bugreport)")
        header.append(f"# Generated at: {datetime.datetime.now().isoformat(timespec='seconds')}")
        header.append(f"# bugreport file: {bugreport_path}")
        header.append("# NOTE: 部分 /sys & sysctl 参数无法从 bugreport 中还原，仅在 live adb 模式下可用。")
        header.append("")
        full_text = "\n".join(header) + prop_text + mem_text

    else:
        # live adb 抓取
        serial = args.serial
        output_file = args.output or build_default_output_filename("live")

        print(f"[INFO] 使用 live 模式，通过 adb 抓取（serial={serial!r}）")
        print("[INFO] Checking adb device ...")
        check_adb_device(serial)
        print("[INFO] Device is ready.")

        print("[INFO] Ensuring adb root ...")
        ensure_root(serial)
        print("[INFO] adb shell 已具备 root 权限。")

        print("[INFO] Collecting properties (all getprop) ...")
        prop_text = collect_properties_from_device(serial)

        print("[INFO] Collecting memory info (vm_sysctl / THP / lsmod / etc) ...")
        mem_text = collect_memory_info_from_device(serial)

        header: List[str] = []
        header.append("# Android memory + properties snapshot (from live device)")
        header.append(f"# Generated at: {datetime.datetime.now().isoformat(timespec='seconds')}")
        header.append(f"# adb serial: {serial if serial else '<default>'}")
        header.append("# NOTE: adb shell verified as root (id -u == 0)")
        header.append("")
        full_text = "\n".join(header) + prop_text + mem_text

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"[INFO] Done. Output saved to: {output_file}")
    except Exception as e:
        sys.stderr.write(f"[ERROR] Failed to write output file: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
