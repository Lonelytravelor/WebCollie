#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
对比两个由 collect_android_meminfo.py 生成的 txt 文件，
输出全部信息为表格风格（ASCII 表格）：
- Properties 三列表：Property | A值 | B值
- /proc/meminfo 关键字段
- /proc/zoneinfo：zone 布局 & 大小 & 水位
- HugePages 配置
"""

import argparse
import os
import re
from typing import Dict, List

from ..config_loader import load_rules_config

_RULES = load_rules_config()
_CMP_RULES = _RULES.get('compare_android_mem_design', {}) if isinstance(_RULES, dict) else {}

# 假设普通页大小（Android 普遍是 4KB，如是 16K 可改为 16）
PAGE_SIZE_KB = int(_CMP_RULES.get('page_size_kb', 4))

# 重点关注的 meminfo 字段，可按需增删
INTERESTING_MEMINFO_KEYS = list(_CMP_RULES.get(
    'interesting_meminfo_keys',
    [
        "MemTotal",
        "Buffers",
        "Cached",
        "SwapCached",
        "SwapTotal",
        "Shmem",
        "Mapped",
        "AnonPages",
        "FilePages",          # 有些内核叫 FilePages
        "VmallocTotal",
        "CommitLimit",
        "Committed_AS",
        "HugePages_Total",
    ],
))

# 对 property 做“重点总结”的字段（会放在表顶端，便于关注）
PRIMARY_PROP_KEYS = list(_CMP_RULES.get(
    'primary_prop_keys',
    [
        "ro.board.platform",
        "ro.build.version.release",
        "dalvik.vm.heapsize",
        "dalvik.vm.heapgrowthlimit",
        "ersist.sys.systemui.compress",
        "persist.sys.miui.integrated.memory.enable",
        "persist.sys.miui.integrated.memory.pr.enable",
        "persist.sys.imr.zramuserate.limit",
        "persist.sys.imr.cpuload.limit",
        "persist.sys.imr.memfree.limit",
        "persist.sys.imr.zramfree.limit",
        "persist.sys.imr.kill.memfree.limit",
        "persist.sys.imr.launchrecliam.num",
        "persist.sys.mmms.throttled.thread",
    ],
))

# 你重点关注的 KO 名称（可以按需修改）
IMPORTANT_MODULES = list(_CMP_RULES.get(
    'important_modules',
    [
        # 举例：
        "mi_memory",
        "mi_mempool",
        "mi_mem_limit",
        "mi_mem_epoll",
        "mi_rmap_efficiency",
        "mi_async_reclaim",
        "kshrink_slabd",
        "unfairmem",
    ],
))

VM_TUNABLE_KEYS = list(_CMP_RULES.get(
    'vm_tunable_keys',
    [
        "vm.min_free_kbytes",
        "vm.extra_free_kbytes",
        "vm.watermark_scale_factor",
        "vm.watermark_boost_factor",
        "vm.lowmem_reserve_ratio",
        "vm.swappiness",
        "vm.vfs_cache_pressure",
        "vm.dirty_background_ratio",
        "vm.dirty_ratio",
        "vm.dirty_background_bytes",
        "vm.dirty_bytes",
        "vm.overcommit_memory",
        "vm.overcommit_ratio",
        "vm.zone_reclaim_mode",
        "vm.min_slab_ratio",
        "vm.min_unmapped_ratio",
        "vm.percpu_pagelist_fraction",
        "vm.compact_unevictable_allowed",
        "vm.compact_defer_shift",
    ],
))

EXTRA_NODE_SECTIONS = list(_CMP_RULES.get('extra_node_sections', []))
VMSTAT_KEYS = list(_CMP_RULES.get('vmstat_keys', []))


# ------------ 通用表格工具 ------------

def make_table(headers: List[str], rows: List[List[str]], title: str = "") -> str:
    """
    生成简单 ASCII 表格：
    title
    col1 | col2 | ...
    ---- + ---- + ...
    ...
    """
    if not rows:
        # 即使没有数据也给个空表结构
        width = [len(h) for h in headers]
    else:
        width = [len(h) for h in headers]
        for r in rows:
            for i, cell in enumerate(r):
                width[i] = max(width[i], len(cell))

    # 构建行
    def fmt_row(cells: List[str]) -> str:
        return " | ".join(c.ljust(width[i]) for i, c in enumerate(cells))

    header_line = fmt_row(headers)
    sep_line = "-+-".join("-" * w for w in width)

    lines: List[str] = []
    if title:
        lines.append(title)
    lines.append(header_line)
    lines.append(sep_line)
    for r in rows:
        lines.append(fmt_row(r))
    lines.append("")
    return "\n".join(lines)


# ------------ 基础工具函数 ------------

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

def mem_kb_to_mib(v: int) -> float:
    return v / 1024.0

def load_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def extract_section(lines: List[str], section_name: str) -> List[str]:
    """
    从 collect_android_meminfo.py 生成的文件中，提取指定 SECTION 的内容。
    section_name 例如: "proc/meminfo", "proc/zoneinfo"
    """
    start_tag = f"----- SECTION: {section_name} -----"
    in_section = False
    section_lines: List[str] = []

    for line in lines:
        if line.strip() == start_tag:
            in_section = True
            continue
        if in_section:
            if line.startswith("----- SECTION: ") or line.startswith("===== MEMORY INFO END"):
                break
            section_lines.append(line.rstrip("\n"))

    return section_lines


# ------------ 解析 properties ------------

def parse_properties(lines: List[str]) -> Dict[str, str]:
    """
    从文件中解析 PROPERTIES 区块： key = value
    """
    props: Dict[str, str] = {}
    in_props = False
    for line in lines:
        s = line.strip()
        if s == "===== PROPERTIES START =====":
            in_props = True
            continue
        if s == "===== PROPERTIES END =====":
            break
        if not in_props:
            continue
        if "=" in s:
            key, val = s.split("=", 1)
            props[key.strip()] = val.strip()
    return props


# ------------ 解析 /proc/meminfo ------------

def parse_meminfo_section(section_lines: List[str]) -> Dict[str, int]:
    """
    解析 /proc/meminfo 部分为 { key: value(int, 以原单位) }
    通常单位是 kB，不特别转换，比较差值即可。
    """
    meminfo: Dict[str, int] = {}
    pattern = re.compile(r"^(\S+):\s*(\d+)")
    for line in section_lines:
        m = pattern.match(line)
        if not m:
            continue
        key = m.group(1)
        val = int(m.group(2))
        meminfo[key] = val
    return meminfo


# ------------ 解析 /proc/zoneinfo ------------

def parse_zoneinfo_section(section_lines: List[str]) -> Dict[str, Dict[str, int]]:
    """
    解析 /proc/zoneinfo，提取每个 zone 的：
        - min / low / high
        - present / managed

    返回结构:
        {
          "node0_zoneDMA": {
              "min": 16, "low": 20, "high": 24,
              "present": 104448, "managed": 103219,
          },
          "node0_zoneNormal": {...},
          ...
        }
    """
    zones: Dict[str, Dict[str, int]] = {}

    header_pattern = re.compile(r"^Node\s+(\d+),\s+zone\s+(.+)$")
    watermark_pattern = re.compile(r"^\s*(min|low|high)\s+(\d+)")
    size_pattern = re.compile(r"^\s*(present|managed)\s+(\d+)")

    current_node = None
    current_zone = None

    for line in section_lines:
        line = line.rstrip("\n")
        m = header_pattern.match(line)
        if m:
            current_node = m.group(1)
            current_zone = m.group(2).strip()
            key = f"node{current_node}_zone{current_zone}"
            if key not in zones:
                zones[key] = {}
            continue

        if current_node is None or current_zone is None:
            continue

        wm = watermark_pattern.match(line)
        if wm:
            wm_name, wm_val = wm.group(1), int(wm.group(2))
            key = f"node{current_node}_zone{current_zone}"
            zones.setdefault(key, {})[wm_name] = wm_val
            continue

        sz = size_pattern.match(line)
        if sz:
            sz_name, sz_val = sz.group(1), int(sz.group(2))
            key = f"node{current_node}_zone{current_zone}"
            zones.setdefault(key, {})[sz_name] = sz_val
            continue

    return zones

def parse_vm_sysctl_section(section_lines: List[str]) -> Dict[str, object]:
    """
    解析 vm_sysctl section：
      vm.<name> = <value>
    返回：{ "vm.xxx": int 或 str }
    """
    vm: Dict[str, object] = {}

    for line in section_lines:
        s = line.strip()
        if not s:
            continue
        # 只解析形如 vm.xxx = yyy 的行
        if not s.startswith("vm.") or " = " not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        val = val.strip()
        # 尝试转成整数
        if re.fullmatch(r"-?\d+", val):
            vm[key] = int(val)
        else:
            vm[key] = val

    return vm


def parse_vmstat_section(section_lines: List[str]) -> Dict[str, int]:
    """
    解析 /proc/vmstat 为 { key: value(int) }。
    """
    data: Dict[str, int] = {}
    for line in section_lines:
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        try:
            val = int(parts[1])
        except Exception:
            continue
        data[key] = val
    return data


def parse_kv_section(section_lines: List[str]) -> Dict[str, str]:
    """
    解析通用 key/value 节点：
    - key = value
    - key: value
    - key value
    """
    data: Dict[str, str] = {}
    for line in section_lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-----"):
            continue
        if "=" in s:
            key, val = s.split("=", 1)
        elif ":" in s:
            key, val = s.split(":", 1)
        else:
            parts = s.split()
            if len(parts) < 2:
                continue
            key, val = parts[0], " ".join(parts[1:])
        key = key.strip()
        val = val.strip()
        if key:
            data[key] = val
    return data

def get_normal_watermarks(zones: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """
    从 zoneinfo 解析结果中找出 Normal zone 的 min/low/high。
    优先使用 node0_zoneNormal，如果没有就找第一个包含 'zoneNormal' 的。
    返回形如 {"min": x, "low": y, "high": z}，找不到则为空 dict。
    """
    normal_key = None
    # 优先 node0_zoneNormal
    for k in sorted(zones.keys()):
        if k.endswith("zoneNormal") or "zoneNormal" in k:
            normal_key = k
            if k.startswith("node0_"):
                break

    if normal_key is None:
        return {}

    z = zones.get(normal_key, {})
    return {
        "min": z.get("min"),
        "low": z.get("low"),
        "high": z.get("high"),
    }


# ------------ 解析 lsmod / /proc/modules ------------

def parse_lsmod_section(section_lines: List[str]) -> Dict[str, Dict[str, str]]:
    """
    解析 lsmod 或 /proc/modules 的输出。
    返回：
      {
        "module_name": {
            "size": "12345",   # 若解析不到则为 ""
            "raw": "原始整行"
        },
        ...
      }
    支持两种常见格式：
      lsmod：
        Module                  Size  Used by
        mi_direct             16384  0
      /proc/modules：
        mi_direct 16384 0 - Live 0x0000000000000000
    """
    modules: Dict[str, Dict[str, str]] = {}

    for line in section_lines:
        s = line.strip()
        if not s:
            continue
        # 过滤 lsmod 标题行
        if s.startswith("Module "):
            continue
        parts = s.split()
        if not parts:
            continue
        name = parts[0]
        size = ""
        if len(parts) > 1 and parts[1].isdigit():
            size = parts[1]
        modules[name] = {
            "size": size,
            "raw": s,
        }

    return modules


# ------------ 对比逻辑（全部表格化） ------------

def pages_to_mib(pages: int) -> float:
    return pages * PAGE_SIZE_KB / 1024.0


def compare_properties(props_a: Dict[str, str], props_b: Dict[str, str]) -> str:
    """
    只对比高亮字段（PRIMARY_PROP_KEYS）：
    Property | A值 | B值

    其它 getprop 抓到的字段不在表格中展示。
    """
    # 从全量 props 里只挑出高亮字段
    keys: List[str] = []
    for k in PRIMARY_PROP_KEYS:
        if k in props_a or k in props_b:
            keys.append(k)

    if not keys:
        return "=== Properties 对比列表（仅高亮字段）===\n(未找到高亮字段)\n\n"

    rows: List[List[str]] = []
    for k in keys:
        va = props_a.get(k, "<missing>")
        vb = props_b.get(k, "<missing>")
        rows.append([k, str(va), str(vb)])

    return make_table(
        headers=["Property", "A值", "B值"],
        rows=rows,
        title="=== Properties 对比列表（仅高亮字段 PRIMARY_PROP_KEYS） ===",
    )



def compare_meminfo(mem_a: Dict[str, int], mem_b: Dict[str, int]) -> str:
    """
    表格：Field | A(kB) | B(kB) | Diff(kB)
    """
    rows: List[List[str]] = []
    for key in INTERESTING_MEMINFO_KEYS:
        va = mem_a.get(key)
        vb = mem_b.get(key)
        if va is None and vb is None:
            continue
        if va is None:
            rows.append([key, "<missing>", str(vb), "N/A"])
        elif vb is None:
            rows.append([key, str(va), "<missing>", "N/A"])
        else:
            diff = vb - va
            rows.append([key, str(va), str(vb), f"{diff:+d}"])
    return make_table(
        headers=["Field", "A(kB)", "B(kB)", "Diff(kB)"],
        rows=rows,
        title="=== /proc/meminfo 关键字段对比 ===",
    )

def compare_vm_sysctl(vm_a: Dict[str, object], vm_b: Dict[str, object]) -> str:
    """
    /proc/sys/vm 内存相关参数对比：
    Param | A | B | Diff
    - 若两边都是整数，Diff = B - A
    - 否则 Diff = N/A
    """
    rows: List[List[str]] = []

    for key in VM_TUNABLE_KEYS:
        va = vm_a.get(key)
        vb = vm_b.get(key)
        if va is None and vb is None:
            # 这个 key 在两边都不存在，就跳过
            continue

        if va is None:
            a_str = "<missing>"
        else:
            a_str = str(va)
        if vb is None:
            b_str = "<missing>"
        else:
            b_str = str(vb)

        # 计算差值（仅当两边都是 int）
        if isinstance(va, int) and isinstance(vb, int):
            diff_str = f"{vb - va:+d}"
        else:
            diff_str = "N/A"

        rows.append([key, a_str, b_str, diff_str])

    if not rows:
        return "=== /proc/sys/vm 内存相关参数对比 ===\n(未采集到 vm_sysctl section 或无匹配字段)\n\n"

    return make_table(
        headers=["Param", "A", "B", "Diff"],
        rows=rows,
        title="=== /proc/sys/vm 内存相关参数对比 ===",
    )


def compare_vmstat(vmstat_a: Dict[str, int], vmstat_b: Dict[str, int]) -> str:
    """
    /proc/vmstat 对比：
    Field | A | B | Diff
    """
    rows: List[List[str]] = []
    if VMSTAT_KEYS:
        keys = [k for k in VMSTAT_KEYS if k in vmstat_a or k in vmstat_b]
    else:
        keys = sorted(set(vmstat_a.keys()) | set(vmstat_b.keys()))
    for key in keys:
        va = vmstat_a.get(key)
        vb = vmstat_b.get(key)
        if va is None and vb is None:
            continue
        if va is None:
            rows.append([key, "<missing>", str(vb), "N/A"])
        elif vb is None:
            rows.append([key, str(va), "<missing>", "N/A"])
        else:
            diff = vb - va
            rows.append([key, str(va), str(vb), f"{diff:+d}"])
    return make_table(
        headers=["Field", "A", "B", "Diff"],
        rows=rows,
        title="=== /proc/vmstat 对比 ===",
    )


def compare_kv_nodes(label: str, data_a: Dict[str, str], data_b: Dict[str, str]) -> str:
    """
    通用节点对比（key/value）：
    Field | A | B | Diff
    """
    rows: List[List[str]] = []
    keys = sorted(set(data_a.keys()) | set(data_b.keys()))
    for key in keys:
        va = data_a.get(key)
        vb = data_b.get(key)
        if va is None and vb is None:
            continue
        a_str = "<missing>" if va is None else str(va)
        b_str = "<missing>" if vb is None else str(vb)
        diff_str = "N/A"
        if va is not None and vb is not None:
            try:
                diff_str = f"{int(vb) - int(va):+d}"
            except Exception:
                diff_str = "N/A"
        rows.append([key, a_str, b_str, diff_str])
    if not rows:
        return f"=== {label} 对比 ===\n(未采集到相关节点或无有效字段)\n\n"
    return make_table(
        headers=["Field", "A", "B", "Diff"],
        rows=rows,
        title=f"=== {label} 对比 ===",
    )


def build_report_from_lines(lines_a: List[str], lines_b: List[str]) -> str:
    # 解析 properties / meminfo / zoneinfo
    props_a = parse_properties(lines_a)
    props_b = parse_properties(lines_b)

    meminfo_a = parse_meminfo_section(extract_section(lines_a, "proc/meminfo"))
    meminfo_b = parse_meminfo_section(extract_section(lines_b, "proc/meminfo"))

    zoneinfo_a = parse_zoneinfo_section(extract_section(lines_a, "proc/zoneinfo"))
    zoneinfo_b = parse_zoneinfo_section(extract_section(lines_b, "proc/zoneinfo"))

    lsmod_a = parse_lsmod_section(extract_section(lines_a, "lsmod"))
    lsmod_b = parse_lsmod_section(extract_section(lines_b, "lsmod"))

    vm_sysctl_a = parse_vm_sysctl_section(extract_section(lines_a, "vm_sysctl"))
    vm_sysctl_b = parse_vm_sysctl_section(extract_section(lines_b, "vm_sysctl"))
    vmstat_a = parse_vmstat_section(extract_section(lines_a, "proc/vmstat"))
    vmstat_b = parse_vmstat_section(extract_section(lines_b, "proc/vmstat"))

    # 组装报告（全部为表格文本）
    report_parts: List[str] = []

    # Summary 放在最前面
    report_parts.append(
        generate_summary(
            props_a, props_b,
            zoneinfo_a, zoneinfo_b,
            meminfo_a, meminfo_b,
        )
    )

    report_parts.append("====== 对比结果：内存配置 / 软件设计差异（表格版） ======\n")
    report_parts.append(compare_zone_sizes(zoneinfo_a, zoneinfo_b))
    report_parts.append(compare_zone_watermarks(zoneinfo_a, zoneinfo_b))
    report_parts.append(compare_meminfo(meminfo_a, meminfo_b))
    report_parts.append(compare_vm_sysctl(vm_sysctl_a, vm_sysctl_b))
    report_parts.append(compare_vmstat(vmstat_a, vmstat_b))
    report_parts.append(compare_important_modules(lsmod_a, lsmod_b))
    report_parts.append(compare_properties(props_a, props_b))
    report_parts.append(analyze_hugepages(meminfo_a, meminfo_b))

    # 额外节点配置
    if EXTRA_NODE_SECTIONS:
        for item in EXTRA_NODE_SECTIONS:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section") or item.get("name") or "").strip()
            if not section:
                continue
            label = str(item.get("label") or section).strip()
            data_a = parse_kv_section(extract_section(lines_a, section))
            data_b = parse_kv_section(extract_section(lines_b, section))
            report_parts.append(compare_kv_nodes(label, data_a, data_b))

    return "\n".join(report_parts)


def summarize_zone_layout(zones: Dict[str, Dict[str, int]], label: str) -> str:
    """
    只看 managed：
    Zone | managed pages | managed MiB
    最后一行 TOTAL。
    """
    rows: List[List[str]] = []
    total_managed_pages = 0

    for z in sorted(zones.keys()):
        managed = zones[z].get("managed", 0)
        total_managed_pages += managed
        rows.append([
            z,
            str(managed),
            f"{pages_to_mib(managed):.1f}",
        ])

    if zones:
        rows.append([
            "TOTAL",
            str(total_managed_pages),
            f"{pages_to_mib(total_managed_pages):.1f}",
        ])
    else:
        rows.append(["<no zones>", "-", "-"])

    return make_table(
        headers=["Zone", "managed pages", "managed MiB"],
        rows=rows,
        title=f"=== {label} 的 zone 布局总结（仅 managed, page={PAGE_SIZE_KB}KB） ===",
    )



def compare_zone_sizes(zones_a: Dict[str, Dict[str, int]],
                       zones_b: Dict[str, Dict[str, int]]) -> str:
    """
    只比较 managed：
    Zone | A.managed | B.managed | Δmanaged(pages) | Δmanaged(MiB)
    """
    all_zones = sorted(set(zones_a.keys()) | set(zones_b.keys()))
    rows: List[List[str]] = []

    for z in all_zones:
        za = zones_a.get(z, {})
        zb = zones_b.get(z, {})
        ma = za.get("managed", 0)
        mb = zb.get("managed", 0)
        dmp = mb - ma
        dmm = pages_to_mib(dmp)
        rows.append([
            z,
            str(ma),
            str(mb),
            f"{dmp:+d}",
            f"{dmm:+.1f}",
        ])

    return make_table(
        headers=["Zone", "A.managed", "B.managed", "Δmanaged(pages)", "Δmanaged(MiB)"],
        rows=rows,
        title="=== /proc/zoneinfo 各 zone 大小对比（仅 managed） ===",
    )




def compare_zone_watermarks(zones_a: Dict[str, Dict[str, int]],
                            zones_b: Dict[str, Dict[str, int]]) -> str:
    """
    水位对比表（不再显示 Δmin/Δlow/Δhigh）：
    Zone | min_A | min_B | low_A | low_B | high_A | high_B
    """
    all_zones = sorted(set(zones_a.keys()) | set(zones_b.keys()))
    rows: List[List[str]] = []

    for z in all_zones:
        za = zones_a.get(z, {})
        zb = zones_b.get(z, {})

        def gv(d, k):
            v = d.get(k)
            return "" if v is None else str(v)

        min_a = gv(za, "min")
        min_b = gv(zb, "min")
        low_a = gv(za, "low")
        low_b = gv(zb, "low")
        high_a = gv(za, "high")
        high_b = gv(zb, "high")

        rows.append([
            z,
            min_a, min_b,
            low_a, low_b,
            high_a, high_b,
        ])

    return make_table(
        headers=[
            "Zone",
            "min_A", "min_B",
            "low_A", "low_B",
            "high_A", "high_B",
        ],
        rows=rows,
        title="=== /proc/zoneinfo 水位 (min/low/high) 对比 ===",
    )


# ------------ KO / 模块对比（lsmod） ------------

def compare_important_modules(mods_a: Dict[str, Dict[str, str]],
                              mods_b: Dict[str, Dict[str, str]]) -> str:
    """
    对 IMPORTANT_MODULES 做重点对比：
    Module | A_loaded | B_loaded | A_raw | B_raw

    去掉 A_size / B_size 列，只保留是否加载 + 原始行信息。
    """
    rows: List[List[str]] = []

    for m in IMPORTANT_MODULES:
        info_a = mods_a.get(m)
        info_b = mods_b.get(m)
        a_loaded = "YES" if info_a else "NO"
        b_loaded = "YES" if info_b else "NO"
        a_raw = info_a["raw"] if info_a else ""
        b_raw = info_b["raw"] if info_b else ""
        rows.append([
            m,
            a_loaded,
            b_loaded,
            a_raw,
            b_raw,
        ])

    return make_table(
        headers=["Module", "A_loaded", "B_loaded", "A_raw", "B_raw"],
        rows=rows,
        title="=== KO 重点对比（IMPORTANT_MODULES） ===",
    )



def compare_all_modules(mods_a: Dict[str, Dict[str, str]],
                        mods_b: Dict[str, Dict[str, str]]) -> str:
    """
    对所有模块做一个总览表：
    Module | in_A | in_B | A_size | B_size
    （详细行信息可以通过 A_raw/B_raw 在上一个重点表或原始 dump 中查看）
    """
    all_names = sorted(set(mods_a.keys()) | set(mods_b.keys()))
    rows: List[List[str]] = []

    for name in all_names:
        info_a = mods_a.get(name)
        info_b = mods_b.get(name)
        in_a = "YES" if info_a else "NO"
        in_b = "YES" if info_b else "NO"
        a_size = info_a["size"] if info_a else ""
        b_size = info_b["size"] if info_b else ""
        rows.append([
            name,
            in_a,
            in_b,
            a_size,
            b_size,
        ])

    return make_table(
        headers=["Module", "in_A", "in_B", "A_size", "B_size"],
        rows=rows,
        title="=== KO 全量模块对比（lsmod） ===",
    )


def analyze_hugepages(mem_a: Dict[str, int], mem_b: Dict[str, int]) -> str:
    """
    HugePages 对比表：
    Device | HugePages_Total | HugePages_Free | Hugepagesize(kB) | Enabled
    """
    ha_total = mem_a.get("HugePages_Total", 0)
    hb_total = mem_b.get("HugePages_Total", 0)
    ha_size = mem_a.get("Hugepagesize", 0)
    hb_size = mem_b.get("Hugepagesize", 0)
    ha_free = mem_a.get("HugePages_Free", 0)
    hb_free = mem_b.get("HugePages_Free", 0)

    def enabled(total: int) -> str:
        return "YES" if total > 0 else "NO"

    rows = [
        ["A", str(ha_total), str(ha_free), str(ha_size), enabled(ha_total)],
        ["B", str(hb_total), str(hb_free), str(hb_size), enabled(hb_total)],
    ]

    table = make_table(
        headers=["Device", "HugePages_Total", "HugePages_Free", "Hugepagesize(kB)", "Enabled"],
        rows=rows,
        title="=== HugePages / 大页配置对比（基于 /proc/meminfo 的 HugeTLB） ===",
    )
    note = ("# NOTE: Transparent Huge Page(THP) 状态需要查看 "
            "/sys/kernel/mm/transparent_hugepage/*，\n"
            "#      你在采集脚本中已抓取对应信息，可单独人工查看或后续扩展解析。\n\n")
    return table + note

def generate_summary(props_a: Dict[str, str],
                     props_b: Dict[str, str],
                     zoneinfo_a: Dict[str, Dict[str, int]],
                     zoneinfo_b: Dict[str, Dict[str, int]],
                     meminfo_a: Dict[str, int],
                     meminfo_b: Dict[str, int]) -> str:
    """
    简短 summary，按条展示 3~5 个方面：
    - AB 平台 / Android 版本 / 硬件信息（单独一行）
    - Dalvik heap 对比
    - Zone managed 总量 + Normal 的 min/low/high
    - MemTotal / SwapTotal + HugePages 状态
    """
    lines: List[str] = []
    lines.append("====== Summary（简要差异概览）======")

    # 1. 平台 / Android / 硬件信息（单独一行）
    manu_a = props_a.get("ro.product.manufacturer", "?")
    manu_b = props_b.get("ro.product.manufacturer", "?")
    model_a = props_a.get("ro.product.model", "?")
    model_b = props_b.get("ro.product.model", "?")
    plat_a = props_a.get("ro.board.platform", "?")
    plat_b = props_b.get("ro.board.platform", "?")
    hw_a = props_a.get("ro.hardware", "?")
    hw_b = props_b.get("ro.hardware", "?")
    rel_a = props_a.get("ro.build.version.release", "?")
    rel_b = props_b.get("ro.build.version.release", "?")
    sdk_a = props_a.get("ro.build.version.sdk", "?")
    sdk_b = props_b.get("ro.build.version.sdk", "?")

    lines.append(
        f"- 平台/版本/硬件："
        f"A {manu_a} {model_a}, 平台={plat_a}, HW={hw_a}, Android={rel_a}(SDK={sdk_a}); "
        f"B {manu_b} {model_b}, 平台={plat_b}, HW={hw_b}, Android={rel_b}(SDK={sdk_b})."
    )

    # 2. Dalvik heap 对比
    heap_a = props_a.get("dalvik.vm.heapsize", "?")
    heap_b = props_b.get("dalvik.vm.heapsize", "?")
    growth_a = props_a.get("dalvik.vm.heapgrowthlimit", "?")
    growth_b = props_b.get("dalvik.vm.heapgrowthlimit", "?")
    lines.append(
        f"- Dalvik heap：A heapsize={heap_a}, growthlimit={growth_a}; "
        f"B heapsize={heap_b}, growthlimit={growth_b}."
    )

    # 3. Zones：总 managed + Normal min/low/high
    def total_managed(zs: Dict[str, Dict[str, int]]) -> int:
        return sum(v.get("managed", 0) for v in zs.values())

    tma = total_managed(zoneinfo_a)
    tmb = total_managed(zoneinfo_b)
    tma_mib = pages_to_mib(tma)
    tmb_mib = pages_to_mib(tmb)

    normal_a = get_normal_watermarks(zoneinfo_a)
    normal_b = get_normal_watermarks(zoneinfo_b)

    def fmt_wm(d):
        if not d:
            return "min/low/high=N/A"
        def f(k): 
            v = d.get(k)
            return "N/A" if v is None else str(v)
        return f"min={f('min')}, low={f('low')}, high={f('high')}"

    lines.append(
        f"- Zones（managed + Normal 水位）："
        f"A 总≈{tma_mib:.1f} MiB, Normal({fmt_wm(normal_a)}); "
        f"B 总≈{tmb_mib:.1f} MiB, Normal({fmt_wm(normal_b)})."
    )

    # 4. Meminfo + HugePages
    mt_a = meminfo_a.get("MemTotal", 0)
    mt_b = meminfo_b.get("MemTotal", 0)
    st_a = meminfo_a.get("SwapTotal", 0)
    st_b = meminfo_b.get("SwapTotal", 0)

    hp_a = meminfo_a.get("HugePages_Total", 0)
    hp_b = meminfo_b.get("HugePages_Total", 0)
    hs_a = meminfo_a.get("Hugepagesize", 0)
    hs_b = meminfo_b.get("Hugepagesize", 0)

    lines.append(
        f"- Meminfo：MemTotal A≈{mem_kb_to_mib(mt_a):.1f} MiB, "
        f"B≈{mem_kb_to_mib(mt_b):.1f} MiB；"
        f"SwapTotal A≈{mem_kb_to_mib(st_a):.1f} MiB, "
        f"B≈{mem_kb_to_mib(st_b):.1f} MiB。"
    )

    lines.append(
        f"- HugePages（显式大页）："
        f"A {'启用' if hp_a > 0 else '未启用'}(Total={hp_a}, size={hs_a}kB); "
        f"B {'启用' if hp_b > 0 else '未启用'}(Total={hp_b}, size={hs_b}kB)。"
    )

    lines.append("")  # 空行
    return "\n".join(lines)


# ------------ CLI ------------

def parse_args() -> argparse.Namespace:
    """
    如果未通过选项传文件名，会在运行时以 input() 方式交互式获取路径。
    """
    p = argparse.ArgumentParser(
        description="对比两个 Android 内存设计 dump（全部信息以表格形式展现）。"
    )
    p.add_argument(
        "--file-a", "-a",
        help="设备A的 dump 文件路径（如果不指定，将在运行时交互式输入）",
        default=None,
    )
    p.add_argument(
        "--file-b", "-b",
        help="设备B的 dump 文件路径（如果不指定，将在运行时交互式输入）",
        default=None,
    )
    p.add_argument(
        "--output", "-o",
        help="对比结果输出路径（默认自动生成 ‘mem_design_diff_文件A_vs_文件B.txt’）",
        default=None,
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 1. 获取文件路径（支持命令行 + 交互式输入）
    file_a = args.file_a
    file_b = args.file_b

    if not file_a:
        raw = input("请输入设备 A 的 dump 文件路径（可带或不带引号）：\n> ")
        file_a = normalize_path(raw)
    else:
        file_a = normalize_path(file_a)

    if not file_b:
        raw = input("请输入设备 B 的 dump 文件路径（可带或不带引号）：\n> ")
        file_b = normalize_path(raw)
    else:
        file_b = normalize_path(file_b)

    if not os.path.isfile(file_a):
        raise FileNotFoundError(f"设备 A 的文件不存在: {file_a}")
    if not os.path.isfile(file_b):
        raise FileNotFoundError(f"设备 B 的文件不存在: {file_b}")

    # 2. 读取内容
    lines_a = load_file(file_a)
    lines_b = load_file(file_b)

    report = build_report_from_lines(lines_a, lines_b)

    # 5. 默认生成输出文件
    if args.output:
        output_path = args.output
    else:
        base_a = os.path.splitext(os.path.basename(file_a))[0]
        base_b = os.path.splitext(os.path.basename(file_b))[0]
        output_name = f"mem_design_diff_{base_a}_vs_{base_b}.txt"
        output_path = os.path.join(os.path.dirname(os.path.abspath(file_a)), output_name)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"[INFO] 对比完成。结果已写入:\n  {output_path}")


if __name__ == "__main__":
    main()
