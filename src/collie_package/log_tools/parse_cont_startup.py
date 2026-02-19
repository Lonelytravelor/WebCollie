import json
import html
import io
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from importlib import resources
from typing import Dict, List, Optional, Tuple

from .. import state
from ..config_loader import (
    load_app_list_config,
    load_rules_config,
    resolve_app_config_path,
    to_flat_app_config,
)

# 默认高亮进程列表，用于兜底
DEFAULT_HIGHLIGHT_PROCESSES = [
    "com.tencent.mm", "com.ss.android.ugc.aweme", "com.smile.gifmaker",
    "tv.danmaku.bili", "com.ss.android.article.news", "com.dragon.read",
    "com.tencent.mobileqq", "com.alibaba.android.rimet", "com.xunmeng.pinduoduo",
    "com.baidu.searchbox", "com.ss.android.article.video", "com.tencent.qqlive",
    "com.taobao.taobao", "com.qiyi.video", "com.UCMobile", "com.kmxs.reader",
    "com.tencent.mtt", "com.youku.phone", "com.sina.weibo", "com.quark.browser",
    "com.eg.android.AlipayGphone", "com.autonavi.minimap", "com.duowan.kiwi",
    "com.sankuai.meituan", "com.jingdong.app.mall", "com.zhihu.android",
    "air.tv.douyu.android", "com.qidian.QDReader", "com.tencent.tmgp.pubgmhd",
    "com.tencent.tmgp.sgame"
]


def _candidate_app_config_paths():
    base_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = []
    yaml_cfg = resolve_app_config_path()
    if yaml_cfg is not None:
        candidates.append(str(yaml_cfg))
    candidates.extend([
        os.path.join(base_dir, "app_config.json"),
        os.path.join(os.getcwd(), "app_config.json"),
        os.path.join(os.getcwd(), "src", "collie_package", "app_config.json"),
        os.path.join(os.getcwd(), "collie_package", "app_config.json"),
    ])
    seen = set()
    uniq = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            uniq.append(path)
    return uniq


_APP_CONFIG_CACHE = None


def _load_app_config():
    """加载 app_config，优先 YAML，其次 JSON，失败时回退到包内默认配置。"""
    global _APP_CONFIG_CACHE
    if _APP_CONFIG_CACHE is not None:
        return _APP_CONFIG_CACHE

    config_data = None
    yaml_cfg = load_app_list_config()
    if isinstance(yaml_cfg, dict) and yaml_cfg:
        config_data = to_flat_app_config(yaml_cfg)
    for path in _candidate_app_config_paths():
        if os.path.exists(path):
            try:
                if path.endswith('.yaml') or path.endswith('.yml'):
                    yaml_cfg = load_app_list_config()
                    if isinstance(yaml_cfg, dict) and yaml_cfg:
                        config_data = to_flat_app_config(yaml_cfg)
                else:
                    with open(path, "r", encoding="utf-8") as fp:
                        config_data = json.load(fp)
                if config_data:
                    break
            except Exception:
                continue

    if config_data is None:
        try:
            with resources.open_text("collie_package", "app_config.json", encoding="utf-8") as fp:
                config_data = json.load(fp)
        except Exception:
            config_data = None

    _APP_CONFIG_CACHE = config_data
    return config_data


def _load_highlight_processes():
    """从连续启动配置加载高亮进程，失败时回退到默认列表。"""
    config_data = _load_app_config()
    if isinstance(config_data, dict) and 'highlight_processes' in config_data:
        highlight = config_data.get('highlight_processes')
        if isinstance(highlight, list):
            return [x for x in highlight if isinstance(x, str)] or list(DEFAULT_HIGHLIGHT_PROCESSES)

    apps = []
    if isinstance(config_data, dict) and 'app_presets' in config_data:
        config_data = config_data.get('app_presets') or {}

    if isinstance(config_data, dict):
        # 最高优先级：HighLight 列表
        for key, value in config_data.items():
            key_norm = str(key).replace(" ", "").upper()
            if key_norm == "HIGHLIGHT" and isinstance(value, list):
                seen = set()
                for pkg in value:
                    if isinstance(pkg, str) and pkg not in seen:
                        seen.add(pkg)
                        apps.append(pkg)
                if apps:
                    return apps

        seen = set()
        for key, value in config_data.items():
            if "连续启动" not in key:
                continue

            items = []
            if isinstance(value, list):
                items = value
            elif isinstance(value, dict):
                for v in value.values():
                    if isinstance(v, list):
                        items.extend(v)

            for pkg in items:
                if isinstance(pkg, str) and pkg not in seen:
                    seen.add(pkg)
                    apps.append(pkg)

    return apps if apps else list(DEFAULT_HIGHLIGHT_PROCESSES)


def _load_startup_sequence() -> List[str]:
    """
    加载连续启动顺序列表：
    - 优先显式 key: 连续启动顺序/连续启动序列/连续启动列表/启动顺序/startup_sequence 等
    - 其次：任意包含“连续启动”的列表（按配置文件顺序）
    - 兜底：空列表
    """
    config_data = _load_app_config()
    if not isinstance(config_data, dict):
        return []

    if isinstance(config_data.get('startup_sequence'), list):
        return [v for v in config_data.get('startup_sequence') if isinstance(v, str)]

    explicit_keys = [
        "连续启动顺序",
        "连续启动序列",
        "连续启动列表",
        "启动顺序",
        "startup_sequence",
        "cont_startup_sequence",
        "sequential_apps",
    ]
    for key in explicit_keys:
        value = config_data.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        if isinstance(value, dict):
            seq = value.get("sequential_apps")
            if isinstance(seq, list):
                return [v for v in seq if isinstance(v, str)]

    for key, value in config_data.items():
        if "连续启动" in str(key) and isinstance(value, list):
            return [v for v in value if isinstance(v, str)]

    return []


# 规则配置（支持 src/collie_package/config/rules.yaml）
RULES = load_rules_config()
_PARSE_RULES = RULES.get('parse_cont_startup', {}) if isinstance(RULES, dict) else {}
_PATTERNS = _PARSE_RULES.get('patterns', {}) if isinstance(_PARSE_RULES, dict) else {}

# 连续启动顺序 & 关注进程列表
STARTUP_SEQUENCE = _load_startup_sequence()
# 高亮显示的进程名列表，来源于 app_config 中的连续启动配置
HIGHLIGHT_PROCESSES = _load_highlight_processes()
# 后台存活统计使用的目标列表
TARGET_APPS = STARTUP_SEQUENCE if STARTUP_SEQUENCE else list(HIGHLIGHT_PROCESSES)
POSSIBLE_ANOMALY_START_LABEL = _PARSE_RULES.get(
    "possible_anomaly_start_label", "可能为异常启动"
)
POSSIBLE_ANOMALY_START_NOTE = _PARSE_RULES.get(
    "possible_anomaly_start_note", "需要视频/测试二次确认是否为异常"
)

KILL_TYPE_MAP = _PARSE_RULES.get(
    "kill_type_map",
    {
        "0": "NPW",
        "1": "EPW",
        "2": "CPW",
        "3": "LAUNCH",
        "4": "SUB_PROC",
        "5": "INVALID",
    },
)

MIN_SCORE_MAP = _PARSE_RULES.get(
    "min_score_map",
    {
        -1073741824: "MAIN_PROC_FACTOR | SUB_MIN_SCORE",
        -536870912: "LOWADJ_PROC_FACTOR",
        -268435456: "FORCE_PROTECT_PROC_FACTOR",
        -134217728: "LOCKED_PROC_FACTOR",
        -67108864: "RECENT_PROC_FACTOR",
        -33554432: "IMPORTANT_PROC_FACTOR",
        -1342177280: "RECENT_MIN_SCORE",
        -1140850688: "IMPORTANT_MIN_SCORE",
        -1107296256: "NORMAL_MIN_SCORE",
    },
)

# killinfo字段映射，兼容 comm,pid 与 pid,comm 两种顺序。
# 完整版（老格式，字段较多）
_default_killinfo_field_mapping = {
    0: "pid_or_comm",
    1: "pid_or_comm",
    2: "uid",
    3: "adj",
    4: "min_adj",
    5: "rss_kb",
    6: "kill_reason",
    7: "mem_total_kb",
    8: "mem_free_kb",
    9: "cached_kb",
    10: "swap_cached_kb",
    11: "buffers_kb",
    12: "shmem_kb",
    13: "unevictable_kb",
    14: "swap_total_kb",
    15: "swap_free_kb",
    16: "active_anon_kb",
    17: "inactive_anon_kb",
    18: "active_file_kb",
    19: "inactive_file_kb",
    20: "k_reclaimable_kb",
    21: "s_reclaimable_kb",
    22: "s_unreclaim_kb",
    23: "kernel_stack_kb",
    24: "page_tables_kb",
    25: "ion_heap_kb",
    26: "ion_heap_pool_kb",
    27: "cma_free_kb",
    28: "pressure_since_event_ms",
    29: "since_wakeup_ms",
    30: "wakeups_since_event",
    31: "skipped_wakeups",
    32: "proc_swap_kb",
    33: "gpu_kb",
    34: "thrashing",
    35: "max_thrashing",
    36: "psi_mem_some",
    37: "psi_mem_full",
    38: "psi_io_some",
    39: "psi_io_full",
    40: "psi_cpu_some",
}
_mapping_cfg = _PARSE_RULES.get("killinfo_field_mapping", {}) if isinstance(_PARSE_RULES, dict) else {}
_full_mapping = _mapping_cfg.get("full") if isinstance(_mapping_cfg, dict) else None
if isinstance(_full_mapping, list) and _full_mapping:
    KILLINFO_FIELD_MAPPING = {i: k for i, k in enumerate(_full_mapping)}
else:
    KILLINFO_FIELD_MAPPING = _default_killinfo_field_mapping

# 精简版（新格式，19 字段，含 swap_kb / psi / thrashing 等核心指标）
_default_killinfo_field_mapping_compact = {
    0: "pid_or_comm",
    1: "pid_or_comm",
    2: "uid",
    3: "adj",
    4: "min_adj",
    5: "rss_kb",
    6: "proc_swap_kb",
    7: "kill_reason",
    8: "mem_total_kb",
    9: "mem_free_kb",
    10: "cached_kb",
    11: "swap_free_kb",
    12: "thrashing",
    13: "max_thrashing",
    14: "psi_mem_some",
    15: "psi_mem_full",
    16: "psi_io_some",
    17: "psi_io_full",
    18: "psi_cpu_some",
}
_compact_mapping = _mapping_cfg.get("compact") if isinstance(_mapping_cfg, dict) else None
if isinstance(_compact_mapping, list) and _compact_mapping:
    KILLINFO_FIELD_MAPPING_COMPACT = {i: k for i, k in enumerate(_compact_mapping)}
else:
    KILLINFO_FIELD_MAPPING_COMPACT = _default_killinfo_field_mapping_compact

LMK_PATTERN = re.compile(
    _PATTERNS.get(
        "lmk",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'.*?lowmemorykiller:\s*(?:Kill|Killing)\s*[\'"]?(?P<process>[^\s\'"(]+)[\'"]?'
        r'\s*(?:\((?:pid\s*)?(?P<pid>\d+)[^)]*\)|pid\s*(?P<pid_alt>\d+))?(?P<tail>.*)',
    ),
    re.IGNORECASE,
)

KILLINFO_PATTERN = re.compile(
    _PATTERNS.get(
        "killinfo",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'.*?killinfo:\s*\[(?P<payload>[^\]]+)\]',
    )
)

AM_KILL_PATTERN = re.compile(
    _PATTERNS.get(
        "am_kill",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'.*?am_kill\s*:\s*\[(?P<payload>[^\]]+)\]',
    ),
    re.IGNORECASE,
)

AM_PROC_START_PATTERN = re.compile(
    _PATTERNS.get(
        "am_proc_start",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*?am_proc_start:\s*\[(?P<payload>[^\]]+)\]',
    ),
    re.IGNORECASE,
)

DISPLAYED_PATTERN = re.compile(
    _PATTERNS.get(
        "displayed",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'.*?ActivityTaskManager:\s*Displayed\s+(?P<component>[^\s]+)\s+for user \d+:\s+\+(?P<latency>.+)$',
    )
)

WM_RESUMED_PATTERN = re.compile(
    _PATTERNS.get(
        "wm_resumed",
        r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'.*?wm_set_resumed_activity:\s*\[(?P<payload>[^\]]+)\]',
    ),
    re.IGNORECASE,
)

HOME_PACKAGES = set(
    _PARSE_RULES.get(
        "home_packages",
        ["com.miui.home", "com.android.launcher3", "com.android.launcher"],
    )
)

# 判定“无效” killinfo：仅数字并且 payload 中不含明显的进程名/字符串
def _looks_like_spurious_killinfo(payload_fields):
    if not payload_fields:
        return False
    # 若有非数字字段，视为有效
    if any(not f.isdigit() for f in payload_fields):
        return False
    # 全数字，视为疑似无效
    return True


def _looks_like_package(name: str) -> bool:
    """
    判断是否为应用包名：
    - 必须是字符串，非空
    - 至少包含一个点（兼容 tv.danmaku.bili / me.ele 等）
    """
    if not isinstance(name, str) or not name:
        return False
    return "." in name


def _parse_ts(ts: str, year: int) -> Optional[datetime]:
    for fmt in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{year}-{ts}", f"%Y-{fmt}")
        except ValueError:
            continue
    return None


def describe_min_score(value: str) -> str:
    """将 minScore 数值映射为枚举名，未知值原样返回"""
    try:
        key = int(value)
    except Exception:
        return value
    return MIN_SCORE_MAP.get(key, f"未知({key})")


def parse_killinfo_payload(payload: str, field_mapping: Optional[dict] = None):
    fields = [field.strip() for field in payload.split(',')]
    # 若未指定 mapping，依据字段数量自适配新版精简格式或旧版全量格式
    if field_mapping is None:
        if len(fields) == len(KILLINFO_FIELD_MAPPING_COMPACT):
            mapping = KILLINFO_FIELD_MAPPING_COMPACT
        elif len(fields) <= len(KILLINFO_FIELD_MAPPING_COMPACT) + 1:
            # 字段略有出入但接近 19 项，倾向按新格式解析
            mapping = KILLINFO_FIELD_MAPPING_COMPACT
        else:
            mapping = KILLINFO_FIELD_MAPPING
    else:
        mapping = field_mapping
    parsed = {}
    for idx, value in enumerate(fields):
        key = mapping.get(idx, f"field_{idx}")
        parsed[key] = value
    if fields:
        first_is_digit = fields[0].isdigit()
        if first_is_digit:
            parsed.setdefault("pid", fields[0])
            if len(fields) > 1:
                parsed.setdefault("process_name", fields[1])
        else:
            parsed.setdefault("process_name", fields[0])
            if len(fields) > 1 and fields[1].isdigit():
                parsed.setdefault("pid", fields[1])
        if "uid" not in parsed and len(fields) > 2:
            parsed["uid"] = fields[2]
        if "adj" not in parsed and len(fields) > 3:
            parsed["adj"] = fields[3]
        if "kill_reason" not in parsed and len(fields) > 6:
            parsed["kill_reason"] = fields[6]
    return fields, parsed


def parse_am_kill_payload(payload: str):
    """
    解析 am_kill 的 payload，格式示例：
    [uid, pid, process, adj, reason, pss]
    """
    fields = [f.strip() for f in payload.split(',')]
    result = {
        "uid": fields[0] if len(fields) > 0 else "",
        "pid": fields[1] if len(fields) > 1 else "",
        "process_name": fields[2] if len(fields) > 2 else "",
        "adj": fields[3] if len(fields) > 3 else "",
        "reason": fields[4] if len(fields) > 4 else "",
        "pss_kb": fields[5] if len(fields) > 5 else "",
        "priority": fields[3] if len(fields) > 3 else "",
    }
    return fields, result

def _within_time_range(ts_obj: Optional[datetime], start_time: Optional[datetime], end_time: Optional[datetime]) -> bool:
    """检查时间戳是否落在给定时间段内。"""
    if ts_obj is None:
        return False if (start_time or end_time) else True
    if start_time and ts_obj < start_time:
        return False
    if end_time and ts_obj > end_time:
        return False
    return True


def _base_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    return name.split(":")[0]


def _find_nearest_proc_start(
    proc_start_by_pkg: Dict[str, List[dict]],
    pkg: str,
    target_time: datetime,
    max_before_sec: int = 8,
    max_after_sec: int = 2,
    consume: bool = False,
) -> Optional[dict]:
    best = None
    best_delta = None
    for rec in proc_start_by_pkg.get(pkg, []):
        if rec.get("_used"):
            continue
        delta = (target_time - rec["time"]).total_seconds()
        if delta < -max_after_sec or delta > max_before_sec:
            continue
        abs_delta = abs(delta)
        if best_delta is None or abs_delta < best_delta:
            best = rec
            best_delta = abs_delta
    if best and consume:
        best["_used"] = True
    return best


def _parse_wm_resumed_payload(payload: str) -> Tuple[str, str]:
    """
    解析 wm_set_resumed_activity payload:
    [user_id, component, reason]
    """
    parts = [p.strip() for p in payload.split(",", 2)]
    component = parts[1] if len(parts) > 1 else ""
    reason = parts[2] if len(parts) > 2 else ""
    return component, reason


def _build_wm_launches(wm_resumed_events: List[dict], dedup_seconds: int = 5) -> List[dict]:
    """
    从 wm_set_resumed_activity 中提取启动锚点:
    - Home 行作为分段边界
    - 同包名短时间重复 wm 行（Splash/Main/onParentChanged）去重
    """
    launches = []
    after_home = True
    last_pkg = ""
    last_time = None

    for rec in sorted(wm_resumed_events, key=lambda x: x["time"]):
        pkg = rec.get("process_name", "")
        if pkg in HOME_PACKAGES:
            after_home = True
            continue
        if not _looks_like_package(pkg):
            continue

        is_dup = False
        if (not after_home) and pkg == last_pkg and last_time is not None:
            if (rec["time"] - last_time).total_seconds() <= dedup_seconds:
                is_dup = True
        if is_dup:
            continue

        launches.append(rec)
        after_home = False
        last_pkg = pkg
        last_time = rec["time"]

    return launches


def _merge_wm_start_events(events: List[dict], wm_launches: List[dict], proc_start_by_pkg: Dict[str, List[dict]]) -> None:
    if not wm_launches:
        return

    for launch in wm_launches:
        pkg = launch["process_name"]
        proc_start = _find_nearest_proc_start(proc_start_by_pkg, pkg, launch["time"], consume=True)
        details = {
            "pid": proc_start.get("pid", "") if proc_start else "",
            "uid": proc_start.get("uid", "") if proc_start else "",
            "start_type": proc_start.get("start_type", "") if proc_start else "",
            "component": proc_start.get("component", launch.get("component", "")) if proc_start else launch.get("component", ""),
            "wm_component": launch.get("component", ""),
            "wm_reason": launch.get("reason", ""),
            "had_proc_start": bool(proc_start),
            "launch_source": "wm_set_resumed_activity",
        }
        new_event = {
            "time": launch["time"],
            "type": "start",
            "process_name": pkg,
            "full_name": pkg,
            "is_subprocess": False,
            "raw": launch.get("raw", ""),
            "details": details,
        }
        events.append(new_event)


def _merge_proc_start_fallback_events(events: List[dict], proc_start_by_pkg: Dict[str, List[dict]]) -> None:
    """
    兜底：当某次 top-activity 的 am_proc_start 没有对应 WM/Displayed 启动事件时，
    记录为“疑似后台启动”，不计入正式启动事件。
    """
    main_start_events = [e for e in events if e.get("type") == "start" and not e.get("is_subprocess")]
    for pkg, records in proc_start_by_pkg.items():
        for rec in sorted(records, key=lambda x: x["time"]):
            if rec.get("_used"):
                continue
            st = rec.get("start_type", "") or ""
            if "top-activity" not in st:
                continue
            matched = False
            for ev in main_start_events:
                if ev.get("process_name") != pkg:
                    continue
                delta = abs((ev["time"] - rec["time"]).total_seconds())
                if delta <= 3:
                    matched = True
                    break
            if matched:
                continue

            new_event = {
                "time": rec["time"],
                "type": "proc_start_only",
                "process_name": pkg,
                "full_name": rec.get("full_name", pkg),
                "is_subprocess": False,
                "raw": rec.get("raw", ""),
                "details": {
                    "pid": rec.get("pid", ""),
                    "uid": rec.get("uid", ""),
                    "start_type": st,
                    "component": rec.get("component", ""),
                    "had_proc_start": True,
                    "launch_source": "am_proc_start_only",
                    "start_kind": "unknown",
                    "start_reason": "仅am_proc_start，未命中wm_set_resumed_activity",
                },
            }
            events.append(new_event)


def _merge_displayed_start_events(events: List[dict], displayed_launches: List[dict], proc_start_by_pkg: Dict[str, List[dict]]) -> None:
    if not displayed_launches:
        return

    main_start_events = [e for e in events if e.get("type") == "start" and not e.get("is_subprocess")]

    for launch in sorted(displayed_launches, key=lambda x: x["time"]):
        pkg = launch["process_name"]
        if not _looks_like_package(pkg):
            continue

        nearest_start = None
        nearest_delta = None
        for ev in main_start_events:
            if ev.get("process_name") != pkg:
                continue
            delta = abs((ev["time"] - launch["time"]).total_seconds())
            if delta <= 3 and (nearest_delta is None or delta < nearest_delta):
                nearest_start = ev
                nearest_delta = delta

        proc_start = _find_nearest_proc_start(proc_start_by_pkg, pkg, launch["time"], consume=True)
        if nearest_start:
            details = nearest_start.setdefault("details", {})
            details["displayed_component"] = launch.get("component", "")
            details["displayed_latency"] = launch.get("latency", "")
            details["had_proc_start"] = bool(proc_start) or bool(details.get("pid"))
            details["launch_source"] = "am_proc_start+displayed"
            if proc_start:
                details.setdefault("pid", proc_start.get("pid", ""))
                details.setdefault("uid", proc_start.get("uid", ""))
                details.setdefault("start_type", proc_start.get("start_type", ""))
                details.setdefault("component", proc_start.get("component", ""))
            continue

        details = {
            "pid": proc_start.get("pid", "") if proc_start else "",
            "uid": proc_start.get("uid", "") if proc_start else "",
            "start_type": proc_start.get("start_type", "") if proc_start else "",
            "component": proc_start.get("component", launch.get("component", "")) if proc_start else launch.get("component", ""),
            "displayed_component": launch.get("component", ""),
            "displayed_latency": launch.get("latency", ""),
            "had_proc_start": bool(proc_start),
            "launch_source": "displayed",
        }
        new_event = {
            "time": launch["time"],
            "type": "start",
            "process_name": pkg,
            "full_name": pkg,
            "is_subprocess": False,
            "raw": launch.get("raw", ""),
            "details": details,
        }
        events.append(new_event)
        main_start_events.append(new_event)


def _annotate_cont_startup(events: List[dict]) -> None:
    """
    连续启动判定优先级：
    1) 命中对应 am_proc_start => 冷启动
    2) 若上一次启动进程在后续被 kill/lmk => 冷启动
    3) 其余 => 热启动
    """
    ordered_configured_targets: List[str] = []
    seen_configured = set()
    for pkg in (TARGET_APPS or []):
        base = _base_name(pkg)
        if not _looks_like_package(base):
            continue
        if base in seen_configured:
            continue
        seen_configured.add(base)
        ordered_configured_targets.append(base)
    configured_targets = set(ordered_configured_targets)
    observed_start_pkgs = {
        _base_name(e.get("process_name", ""))
        for e in events
        if e.get("type") == "start" and not e.get("is_subprocess")
    }
    observed_target_set = {p for p in observed_start_pkgs if _looks_like_package(p)}
    target_set = configured_targets if configured_targets else observed_target_set
    if not target_set:
        return

    # 后台存活快照优先按配置列表统计；配置缺失时退化为当前日志实际启动集合。
    snapshot_pkgs = list(ordered_configured_targets) if ordered_configured_targets else sorted(target_set)
    strict_sequence_enabled = bool(ordered_configured_targets)
    strict_rounds = 2
    expected_sequence = (snapshot_pkgs * strict_rounds) if strict_sequence_enabled else []
    next_expected_idx = 0

    state = defaultdict(lambda: {
        "launch_count": 0,
        "last_start_pid": "",
        "last_start_comm": "",
        "last_start_killed": False,
    })
    alive_main = {pkg: False for pkg in target_set.union(set(snapshot_pkgs))}

    for event in events:
        etype = event.get("type")
        base = _base_name(event.get("process_name", ""))
        if base not in target_set:
            continue

        if etype == "start" and not event.get("is_subprocess"):
            d = event.setdefault("details", {})
            had_proc_start = bool(d.get("had_proc_start"))
            prev_killed = bool(state[base]["last_start_killed"])

            if had_proc_start:
                start_kind = "cold"
                reason = "命中am_proc_start"
            elif prev_killed:
                start_kind = "cold"
                reason = "上次启动进程已被查杀"
            else:
                start_kind = "hot"
                reason = "未命中am_proc_start且上次进程未被查杀"

            d["start_kind"] = start_kind
            d["start_reason"] = reason
            alive_list = [pkg for pkg in snapshot_pkgs if alive_main.get(pkg)]
            d["alive_target_list_before"] = alive_list
            d["alive_target_count_before"] = len(alive_list)

            d["expected_process"] = ""
            d["sequence_slot"] = None
            d["round_pos"] = None
            d["is_second_round"] = False
            d["is_second_round_hot"] = False
            d["is_second_round_cold"] = False

            if strict_sequence_enabled:
                expected_pkg = expected_sequence[next_expected_idx] if next_expected_idx < len(expected_sequence) else ""
                d["expected_process"] = expected_pkg
                if expected_pkg and base == expected_pkg:
                    slot_idx = next_expected_idx
                    round_no = (slot_idx // len(snapshot_pkgs)) + 1
                    round_pos = (slot_idx % len(snapshot_pkgs)) + 1
                    d["round"] = round_no
                    d["round_pos"] = round_pos
                    d["sequence_slot"] = slot_idx + 1
                    d["is_second_round"] = round_no == 2
                    d["is_second_round_hot"] = d["is_second_round"] and start_kind == "hot"
                    d["is_second_round_cold"] = d["is_second_round"] and start_kind == "cold"
                    d["possible_anomaly_start"] = False
                    d.pop("anomaly_note", None)
                    next_expected_idx += 1
                else:
                    d["round"] = None
                    d["possible_anomaly_start"] = True
                    if expected_pkg:
                        d["anomaly_note"] = f"未按预设顺序启动：预期 {expected_pkg}，实际 {base}"
                    else:
                        d["anomaly_note"] = f"超出预设启动轮次（最多 {strict_rounds} 轮）"
                    # 异常启动不纳入轮次/存活统计，避免干扰后续结果
                    continue
            else:
                state[base]["launch_count"] += 1
                d["round"] = state[base]["launch_count"]
                d["is_second_round"] = d["round"] == 2
                d["is_second_round_hot"] = d["is_second_round"] and start_kind == "hot"
                d["is_second_round_cold"] = d["is_second_round"] and start_kind == "cold"
                d["possible_anomaly_start"] = False
                d.pop("anomaly_note", None)

            state[base]["last_start_pid"] = str(d.get("pid", "")).strip()
            state[base]["last_start_comm"] = event.get("full_name", event.get("process_name", base))
            state[base]["last_start_killed"] = False
            alive_main[base] = True
            continue

        if etype not in ("kill", "lmk"):
            continue
        if event.get("is_subprocess"):
            continue

        killed_pid = ""
        if etype == "kill":
            killed_pid = str(event.get("details", {}).get("proc_info", {}).get("pid", "")).strip()
        elif etype == "lmk":
            killed_pid = str(event.get("details", {}).get("pid", "")).strip()

        st = state[base]
        if st["last_start_pid"] and killed_pid and st["last_start_pid"] == killed_pid:
            st["last_start_killed"] = True
        elif _base_name(st["last_start_comm"]) == base:
            st["last_start_killed"] = True
        else:
            st["last_start_killed"] = True
        alive_main[base] = False


def parse_log_file(file_path, start_time: Optional[datetime] = None, end_time: Optional[datetime] = None):
    """解析日志文件，返回排序后的事件列表，可选时间过滤"""
    events = []
    current_year = datetime.now().year
    killinfo_by_pid = defaultdict(list)
    killinfo_by_comm = defaultdict(list)
    killinfo_all = []  # 记录全部 killinfo 供未匹配时兜底生成事件
    lmk_events = []
    displayed_launches = []
    wm_resumed_events = []
    proc_start_by_pkg = defaultdict(list)
    
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # 解析 lowmemorykiller 行
            lmk_match = LMK_PATTERN.search(line)
            if lmk_match:
                ts = lmk_match.group("ts")
                ts_obj = _parse_ts(ts, current_year) or datetime.now()
                if not _within_time_range(ts_obj, start_time, end_time):
                    continue
                pid = lmk_match.group("pid") or lmk_match.group("pid_alt") or ""
                process_name = lmk_match.group("process") or ""
                tail = lmk_match.group("tail") or ""
                adj_match = re.search(r"(?:adj|oom_score_adj)\s*(-?\d+)", tail)
                reason_match = re.search(r"(?:reason|kill_reason)\s+([A-Za-z0-9_-]+)", tail)
                rss_match = re.search(r"to free\s+(\d+)kB", tail)

                lmk_events.append(
                    {
                        'time': ts_obj,
                        'type': 'lmk',
                        'process_name': process_name,
                        'full_name': process_name,
                        'is_subprocess': ':' in process_name,
                        'raw': line,
                        'details': {
                            'pid': pid,
                            'adj': adj_match.group(1) if adj_match else "",
                            'min_adj': '',
                            'rss_kb': rss_match.group(1) if rss_match else "",
                            'reason': reason_match.group(1) if reason_match else "未知",
                            'tail': tail.strip(),
                            'killinfo': [],
                        },
                    }
                )
                continue

            # 收集 killinfo，用于之后和 lmk 对齐
            killinfo_match = KILLINFO_PATTERN.search(line)
            if killinfo_match:
                ts = killinfo_match.group("ts")
                ts_obj = _parse_ts(ts, current_year) or datetime.now()
                if not _within_time_range(ts_obj, start_time, end_time):
                    continue
                payload = killinfo_match.group("payload")
                fields, parsed_fields = parse_killinfo_payload(payload)
                # 过滤疑似无效的纯数字 killinfo
                if _looks_like_spurious_killinfo(fields):
                    continue
                pid = parsed_fields.get("pid", "")
                comm = parsed_fields.get("process_name", "")
                record = {
                    'time': ts_obj,
                    'raw_fields': fields,
                    'parsed_fields': parsed_fields,
                    'payload': payload,
                }
                if pid:
                    killinfo_by_pid[pid].append(record)
                if comm:
                    killinfo_by_comm[comm].append(record)
                killinfo_all.append(record)
                continue

            # 解析 am_kill 行，供后续与 kill ki 合并去重
            am_kill_match = AM_KILL_PATTERN.search(line)
            if am_kill_match:
                ts = am_kill_match.group("ts")
                ts_obj = _parse_ts(ts, current_year) or datetime.now()
                if not _within_time_range(ts_obj, start_time, end_time):
                    continue
                payload = am_kill_match.group("payload")
                fields, parsed_fields = parse_am_kill_payload(payload)
                # 忽略 OneKeyClean 的 am_kill
                if parsed_fields.get("reason", "").lower() == "onekeyclean":
                    continue
                proc = parsed_fields.get("process_name", "")
                events.append({
                    'time': ts_obj,
                    'type': 'am_kill',
                    'process_name': proc.split(':')[0] if ':' in proc else proc,
                    'full_name': proc,
                    'is_subprocess': ':' in proc,
                    'raw': line,
                    'details': {
                        'payload': payload,
                        'raw_fields': fields,
                        **parsed_fields
                    }
                })
                continue

            displayed_match = DISPLAYED_PATTERN.search(line)
            if displayed_match:
                ts = displayed_match.group("ts")
                ts_obj = _parse_ts(ts, current_year) or datetime.now()
                if not _within_time_range(ts_obj, start_time, end_time):
                    continue
                component = displayed_match.group("component")
                process_name = component.split("/")[0] if "/" in component else component
                displayed_launches.append(
                    {
                        "time": ts_obj,
                        "process_name": process_name,
                        "component": component,
                        "latency": displayed_match.group("latency").strip(),
                        "raw": line,
                    }
                )
                continue

            wm_resumed_match = WM_RESUMED_PATTERN.search(line)
            if wm_resumed_match:
                ts = wm_resumed_match.group("ts")
                ts_obj = _parse_ts(ts, current_year) or datetime.now()
                if not _within_time_range(ts_obj, start_time, end_time):
                    continue
                component, reason = _parse_wm_resumed_payload(wm_resumed_match.group("payload"))
                process_name = component.split("/")[0] if "/" in component else component
                wm_resumed_events.append(
                    {
                        "time": ts_obj,
                        "process_name": process_name,
                        "component": component,
                        "reason": reason,
                        "raw": line,
                    }
                )
                continue
            
            # 尝试解析启动日志
            if 'am_proc_start' in line:
                match = AM_PROC_START_PATTERN.search(line)
                if match:
                    timestamp = match.group("ts")
                    details = match.group("payload")
                    parts = [p.strip() for p in details.split(',')]
                    if len(parts) >= 6:
                        try:
                            start_type = parts[4]

                            full_name = parts[3]
                            is_subprocess = ':' in full_name
                            process_name = full_name.split(':')[0] if is_subprocess else full_name
                            
                            time_obj = datetime.strptime(f"{current_year}-{timestamp}", "%Y-%m-%d %H:%M:%S.%f")
                            if not _within_time_range(time_obj, start_time, end_time):
                                continue

                            if not is_subprocess and _looks_like_package(process_name):
                                proc_start_by_pkg[process_name].append(
                                    {
                                        "time": time_obj,
                                        "pid": parts[1],
                                        "uid": parts[2],
                                        "process_name": process_name,
                                        "full_name": full_name,
                                        "start_type": start_type,
                                        "component": parts[5],
                                        "raw": line,
                                    }
                                )

                            # 连续启动解析以 WM 前台切换为启动锚点，这里仅收集 am_proc_start 供冷/热判定
                        except Exception as e:
                            print(f"解析启动日志错误: {e} - {line}")
                            continue
            
            # 尝试解析查杀事件 (kill / trig / skip)，要求三段 [ ] 且首段以这些标签开头，避免匹配 spckill 等噪声
            else:
                match = re.match(
                    r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*?\[([Kk]ill[^\]]*|[Tt]rig[^\]]*|[Ss]kip[^\]]*)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]',
                    line
                )
                if match:
                    timestamp, part1, part2, part3 = match.groups()
                    part1_list = part1.split('|')
                    part2_list = part2.split('|')
                    part3_list = part3.split('|')
                    
                    # 确定事件类型
                    event_tag = part1_list[0]
                    event_tag_l = event_tag.lower()
                    if event_tag_l.startswith("kill"):
                        event_type = 'kill'
                    elif event_tag_l.startswith("trig"):
                        event_type = 'trig'
                    elif event_tag_l.startswith("skip"):
                        event_type = 'skip'
                    else:
                        continue
                    
                    # 检查字段数量
                    if len(part1_list) < 11 or len(part2_list) < 10 or len(part3_list) < 6:
                        print(f"查杀日志字段不足: {line}")
                        continue
                    
                    # 提取进程名
                    process_name = part2_list[0]
                    is_subprocess = ':' in process_name
                    # 兼容部分版本，进程信息可能缺省或填充 -1
                    def _norm(v: str) -> str:
                        return "" if v in ("-1", "", "None", None) else v
                    if not process_name:
                        process_name = ""
                    # 归一化数值字段中的占位 -1
                    part2_list = [_norm(v) for v in part2_list]
                    part3_list = [_norm(v) for v in part3_list]
                    
                    # 解析各部分信息
                    try:
                        time_obj = datetime.strptime(f"{current_year}-{timestamp}", "%Y-%m-%d %H:%M:%S.%f")
                        if not _within_time_range(time_obj, start_time, end_time):
                            continue

                        # 获取killType描述
                        kill_type = part1_list[1]
                        kill_type_desc = KILL_TYPE_MAP.get(kill_type, f"未知({kill_type})")
                        min_score = part1_list[2]
                        min_score_desc = describe_min_score(min_score)

                        # 构造事件对象
                        events.append({
                            'time': time_obj,
                            'type': event_type,
                            'process_name': process_name,
                            'full_name': process_name,
                            'is_subprocess': is_subprocess,
                            'raw': line,
                            'details': {
                                'event_tag': event_tag,
                                'kill_info': {
                                    'killType': part1_list[1],
                                    'killTypeDesc': kill_type_desc,  # 添加描述字段
                                    'minScore': min_score,
                                    'minScoreDesc': min_score_desc,
                                    'killableProcCount': part1_list[3],
                                    'importantAppCount': part1_list[4],
                                    'killedCount': part1_list[5],
                                    'killedImpCount': part1_list[6],
                                    'skipCount': part1_list[7],
                                    'targetMem': part1_list[8],
                                    'targetReleaseMem': part1_list[9],
                                    'killedPss': part1_list[10]
                                },
                                'proc_info': {
                                    'uid': part2_list[1],
                                    'pid': part2_list[2],
                                    'adj': part2_list[3],
                                    'score': part2_list[4],
                                    'pss': part2_list[5],
                                    'swapUsed': part2_list[6],
                                    'ret': part2_list[7],
                                    'isMain': part2_list[8],
                                    'isImp': part2_list[9]
                                },
                                'mem_info': {
                                    'memFree': part3_list[0],
                                    'memAvail': part3_list[1],
                                    'memFile': part3_list[2],
                                    'memAnon': part3_list[3],
                                    'memSwapFree': part3_list[4],
                                    'cmaFree': part3_list[5]
                                }
                            }
                        })
                    except Exception as e:
                        print(f"解析查杀日志错误: {e} - {line}")
                        continue
    # 对齐 killinfo 与 lmk 事件（按 pid/comm，就近时间匹配，阈值 5 秒）
    for event in lmk_events:
        pid = event['details']['pid']
        comm = event['process_name']
        combined = []
        seen = set()
        for rec in killinfo_by_pid.get(pid, []) + killinfo_by_comm.get(comm, []):
            key = (rec.get("time"), rec.get("payload"))
            if key in seen:
                continue
            seen.add(key)
            combined.append(rec)
        if not combined:
            continue
        event_time = event.get('time')
        if event_time:
            deltas = [
                (
                    abs((rec.get("time", event_time) - event_time).total_seconds()),
                    rec,
                )
                for rec in combined
            ]
            if deltas:
                min_delta = min(delta for delta, _ in deltas)
                nearest = [rec for delta, rec in deltas if delta == min_delta]
                if min_delta <= 5:
                    event['details']['killinfo'].extend(nearest)
                    for rec in nearest:
                        rec['used'] = True
        else:
            event['details']['killinfo'].append(combined[0])
            combined[0]['used'] = True

        # 补充缺失字段
        if event['details'].get('rss_kb', '') == "" and event['details']['killinfo']:
            event['details']['rss_kb'] = event['details']['killinfo'][0]['parsed_fields'].get('rss_kb', '')
        if event['details'].get('min_adj', '') == "" and event['details']['killinfo']:
            event['details']['min_adj'] = event['details']['killinfo'][0]['parsed_fields'].get('min_adj', '')
        if (event['details'].get('reason') in ("", "未知")) and event['details']['killinfo']:
            event['details']['reason'] = event['details']['killinfo'][0]['parsed_fields'].get('kill_reason', '') or "未知"

    events.extend(lmk_events)
    wm_launches = _build_wm_launches(wm_resumed_events)
    _merge_wm_start_events(events, wm_launches, proc_start_by_pkg)
    # Displayed 作为兼容兜底，避免某些日志缺少 wm_set_resumed_activity
    _merge_displayed_start_events(events, displayed_launches, proc_start_by_pkg)
    # am_proc_start 兜底，处理个别场景只有 proc_start 无 WM 的情况
    _merge_proc_start_fallback_events(events, proc_start_by_pkg)

    # 对未匹配的 killinfo 兜底生成 LMK 事件，防止遗漏
    for rec in killinfo_all:
        if rec.get('used'):
            continue
        parsed = rec.get('parsed_fields', {})
        proc = parsed.get('process_name', '')
        if _looks_like_package(proc):
            event_type = 'lmk'
        else:
            # 无包名视为“触发”事件，便于统计触发时内存
            event_type = 'trig'
            proc = proc or 'unknown'

        # 基础字段
        base_details = {
            'pid': parsed.get('pid', ''),
            'adj': parsed.get('adj', ''),
            'min_adj': parsed.get('min_adj', ''),
            'rss_kb': parsed.get('rss_kb', ''),
            'reason': parsed.get('kill_reason', '') or "未知",
            'tail': '',
            'killinfo': [rec],
        }

        # 若作为触发事件，补齐 kill_info / proc_info / mem_info，避免后续访问缺字段报错
        if event_type == 'trig':
            base_details.setdefault('kill_info', {
                'killType': 'trig',
                'killTypeDesc': 'trig',
                'minScore': parsed.get('minScore', ''),
                'minScoreDesc': describe_min_score(parsed.get('minScore', '')) if parsed.get('minScore') else '',
                'killableProcCount': '',
                'importantAppCount': '',
                'killedCount': '',
                'killedImpCount': '',
                'skipCount': '',
                'targetMem': '',
                'targetReleaseMem': '',
                'killedPss': '',
            })
            base_details.setdefault('proc_info', {
                'uid': parsed.get('uid', ''),
                'pid': parsed.get('pid', ''),
                'adj': parsed.get('adj', ''),
                'score': '',
                'pss': parsed.get('rss_kb', ''),
                'swapUsed': parsed.get('proc_swap_kb', ''),
                'ret': '',
                'isMain': 'true',
                'isImp': 'false',
            })
            # mem_info 从 killinfo 补充
            mem_free = parsed.get('mem_free_kb', '')
            mem_file = ''
            af = _safe_int(parsed.get('active_file_kb'))
            inf = _safe_int(parsed.get('inactive_file_kb'))
            if af is not None and inf is not None:
                mem_file = str(af + inf)
            mem_anon = ''
            aa = _safe_int(parsed.get('active_anon_kb'))
            ina = _safe_int(parsed.get('inactive_anon_kb'))
            if aa is not None and ina is not None:
                mem_anon = str(aa + ina)
            base_details.setdefault('mem_info', {
                'memFree': mem_free,
                'memAvail': '',
                'memFile': mem_file,
                'memAnon': mem_anon,
                'memSwapFree': parsed.get('swap_free_kb', ''),
                'cmaFree': parsed.get('cma_free_kb', ''),
            })

        event = {
            'time': rec.get('time'),
            'type': event_type,
            'process_name': proc,
            'full_name': proc,
            'is_subprocess': ':' in proc,
            'raw': f"killinfo-only: [{rec.get('payload','')}]",
            'details': base_details,
        }
        events.append(event)

    # 按时间排序
    events.sort(key=lambda x: x['time'])
    events = merge_kill_amkill(events)
    events.sort(key=lambda x: x['time'])
    _annotate_cont_startup(events)
    return events


def _safe_int(val):
    try:
        return int(float(val))
    except Exception:
        return None


def _extract_mem_metrics(event):
    """
    抽取单个事件的关键内存指标，返回 dict，缺失则返回 None。
    指标：mem_free, file_pages, anon_pages, swap_free
    """
    etype = event.get("type")
    if etype == "kill" or etype == "trig":
        mem = event.get("details", {}).get("mem_info", {}) or {}
        mem_free = _safe_int(mem.get("memFree"))
        file_pages = _safe_int(mem.get("memFile"))
        anon_pages = _safe_int(mem.get("memAnon"))
        swap_free = _safe_int(mem.get("memSwapFree"))
        # 兜底：触发事件若没有 mem_info，尝试复用 killinfo
        if mem_free is None and etype == "trig":
            ki_list = event.get("details", {}).get("killinfo") or []
            if ki_list:
                pf = ki_list[0].get("parsed_fields", {}) or {}
                mem_free = _safe_int(pf.get("mem_free_kb"))
                active_file = _safe_int(pf.get("active_file_kb"))
                inactive_file = _safe_int(pf.get("inactive_file_kb"))
                file_pages = active_file + inactive_file if (active_file is not None and inactive_file is not None) else None
                active_anon = _safe_int(pf.get("active_anon_kb"))
                inactive_anon = _safe_int(pf.get("inactive_anon_kb"))
                anon_pages = active_anon + inactive_anon if (active_anon is not None and inactive_anon is not None) else None
                swap_free = _safe_int(pf.get("swap_free_kb"))
    elif etype == "lmk":
        ki_list = event.get("details", {}).get("killinfo") or []
        if not ki_list:
            return None
        pf = ki_list[0].get("parsed_fields", {}) or {}
        mem_free = _safe_int(pf.get("mem_free_kb"))
        active_file = _safe_int(pf.get("active_file_kb"))
        inactive_file = _safe_int(pf.get("inactive_file_kb"))
        file_pages = active_file + inactive_file if (active_file is not None and inactive_file is not None) else None
        active_anon = _safe_int(pf.get("active_anon_kb"))
        inactive_anon = _safe_int(pf.get("inactive_anon_kb"))
        anon_pages = active_anon + inactive_anon if (active_anon is not None and inactive_anon is not None) else None
        swap_free = _safe_int(pf.get("swap_free_kb"))
    else:
        return None

    if all(v is None for v in (mem_free, file_pages, anon_pages, swap_free)):
        return None
    return {
        "mem_free": mem_free,
        "file_pages": file_pages,
        "anon_pages": anon_pages,
        "swap_free": swap_free,
    }

def _percentile(sorted_vals, p: float):
    """线性插值求百分位，sorted_vals需已排序"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    rank = (n - 1) * p
    low = int(rank)
    high = low + 1
    frac = rank - low
    if high >= n:
        return float(sorted_vals[-1])
    return float(sorted_vals[low] * (1 - frac) + sorted_vals[high] * frac)

def _calc_stats(values):
    """返回 dict: count/avg/median/p95/min/max"""
    if not values:
        return {'count': 0, 'avg': None, 'median': None, 'p95': None, 'min': None, 'max': None}
    vals = sorted(values)
    cnt = len(vals)
    avg = sum(vals) / cnt
    median = _percentile(vals, 0.5)
    p95 = _percentile(vals, 0.95)
    return {
        'count': cnt,
        'avg': avg,
        'median': median,
        'p95': p95,
        'min': float(vals[0]),
        'max': float(vals[-1]),
    }

def _format_duration(seconds: float) -> str:
    """将秒数格式化为简洁的 h/m/s 字符串"""
    if seconds is None:
        return "-"
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return "".join(parts)

def _format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.strftime("%m-%d %H:%M:%S")


def merge_kill_amkill(events, window_seconds: int = 3):
    """
    合并同一事件的 kill ki 与 am_kill：优先保留 kill ki 内容，
    将 am_kill 信息挂到 kill 的 details['am_kill']；若没有匹配的 kill ki，
    则将 am_kill 转化为 kill 事件参与统计。
    """
    kill_events = []
    am_only_events = []
    result = []

    for e in events:
        if e.get('type') == 'kill':
            kill_events.append(e)
        elif e.get('type') == 'am_kill':
            am_only_events.append(e)
        else:
            result.append(e)

    def _base(name: str) -> str:
        return name.split(':')[0] if isinstance(name, str) else name

    for am in am_only_events:
        am_time = am['time']
        am_pid = am['details'].get('pid', '')
        am_base = _base(am['process_name'])

        best = None
        best_delta = None
        for k in kill_events:
            k_pid = k['details']['proc_info'].get('pid', '')
            k_base = _base(k['process_name'])
            if am_pid and k_pid and am_pid == k_pid:
                pass
            elif am_base and k_base and am_base == k_base:
                pass
            else:
                continue
            delta = abs((k['time'] - am_time).total_seconds())
            if delta <= window_seconds and (best_delta is None or delta < best_delta):
                best = k
                best_delta = delta

        if best:
            best['details'].setdefault('sources', ['kill'])
            if 'am_kill' not in best['details'].get('sources', []):
                best['details']['sources'].append('am_kill')
            best['details']['am_kill'] = am['details']
        else:
            # 转化为 kill 事件以参与统计
            details = am['details']
            proc = am.get('process_name', '')
            is_sub = am.get('is_subprocess', False)
            kill_event = {
                'time': am_time,
                'type': 'kill',
                'process_name': proc,
                'full_name': am.get('full_name', proc),
                'is_subprocess': is_sub,
                'raw': am.get('raw', ''),
                    'details': {
                        'event_tag': 'am_kill',
                        'kill_info': {
                            'killType': 'am_kill',
                            'killTypeDesc': 'am_kill',
                            'minScore': '',
                            'minScoreDesc': '',
                            'killableProcCount': '',
                            'importantAppCount': '',
                            'killedCount': '1',
                            'killedImpCount': '',
                            'skipCount': '',
                            'targetMem': '',
                            'targetReleaseMem': '',
                            'killedPss': details.get('pss_kb', '') or '',
                        },
                        'proc_info': {
                            'uid': details.get('uid', ''),
                            'pid': details.get('pid', ''),
                        'adj': details.get('adj', ''),
                        'reason': details.get('reason', ''),
                        'priority': details.get('priority', ''),
                            'score': '',
                            'pss': details.get('pss_kb', ''),
                            'swapUsed': '',
                            'ret': details.get('pss_kb', ''),
                            'isMain': 'true' if not is_sub else 'false',
                        'isImp': 'false',
                    },
                    'mem_info': {
                        'memFree': '',
                        'memAvail': '',
                        'memFile': '',
                        'memAnon': '',
                        'memSwapFree': '',
                        'cmaFree': '',
                    }
                }
            }
            result.append(kill_event)

    result.extend(kill_events)
    result.sort(key=lambda x: x['time'])
    return result

def format_event_detail(event, idx):
    """格式化单个事件为文本"""
    time_str = event['time'].strftime("%m-%d %H:%M:%S.%f")[:-3]
    
    # 高亮显示特定进程
    proc_name = event.get('full_name', event['process_name'])
    if event['process_name'] in HIGHLIGHT_PROCESSES:
        proc_display = f"{proc_name}"
    else:
        proc_display = proc_name
    
    # 添加子进程标记
    if event['type'] == 'start' and event['is_subprocess']:
        proc_display += f" (子进程)"
    
    # 构造事件类型显示
    if event['type'] == 'start':
        start_kind = event.get("details", {}).get("start_kind")
        if start_kind == "cold":
            event_type = f"启动(冷)"
        elif start_kind == "hot":
            event_type = f"启动(热)"
        else:
            event_type = f"启动"
    elif event['type'] == 'kill':
        event_type = f"查杀"
    elif event['type'] == 'trig':
        event_type = f"触发查杀"
    elif event['type'] == 'skip':
        event_type = f"跳过({event['details']['event_tag'][5:]})"  # 显示跳过原因
    elif event['type'] == 'lmk':
        event_type = f"LMK查杀"
    elif event['type'] == 'proc_start_only':
        event_type = f"疑似后台启动"
    else:
        event_type = event.get('type', '未知事件')
    
    # 构造详细信息
    details = []
    if event['type'] == 'start':
        d = event.get("details", {})
        details.append(f"  进程信息:")
        details.append(f"    PID: {d.get('pid', '')}, UID: {d.get('uid', '')}")
        details.append(f"    启动方式: {d.get('start_type', '')}")
        details.append(f"    组件: {d.get('component', '')}")
        details.append(f"    是否子进程: {'是' if event['is_subprocess'] else '否'}")
        if d.get("displayed_component"):
            details.append(f"    Displayed组件: {d.get('displayed_component', '')}")
        if d.get("displayed_latency"):
            details.append(f"    Displayed耗时: +{d.get('displayed_latency', '')}")
        if "round" in d:
            details.append(f"    启动轮次: 第{d.get('round')}轮")
        if d.get("start_kind"):
            kind_cn = "冷启动" if d.get("start_kind") == "cold" else "热启动"
            details.append(f"    判定: {kind_cn} ({d.get('start_reason', '')})")
        if _is_possible_anomaly_start_record(d):
            details.append(
                f"    异常标记: {POSSIBLE_ANOMALY_START_LABEL}（{_startup_anomaly_note(d)}）"
            )
        details.append(f"    匹配am_proc_start: {'是' if d.get('had_proc_start') else '否'}")
        if "alive_target_count_before" in d:
            details.append(f"    启动前后台存活数: {d.get('alive_target_count_before', 0)}")
            alive_list = d.get("alive_target_list_before", [])
            details.append(f"    启动前后台存活列表: {', '.join(alive_list) if alive_list else '无'}")
    elif event['type'] == 'proc_start_only':
        d = event.get("details", {})
        details.append("  进程信息:")
        details.append(f"    PID: {d.get('pid', '')}, UID: {d.get('uid', '')}")
        details.append(f"    启动方式: {d.get('start_type', '')}")
        details.append(f"    组件: {d.get('component', '')}")
        details.append("    判定: 疑似后台启动（未匹配到WM前台切换）")
    elif event['type'] == 'lmk':
        d = event['details']
        kill_reason = d.get("reason") or d.get("kill_reason") or "未知"

        def kv(label: str, value: str) -> str:
            return f"    {label:<14}: {value}"

        details.append("  进程信息:")
        details.append(kv("pid", d.get("pid", "")))
        details.append(kv("adj", d.get("adj", "")))
        details.append(kv("min_adj", d.get("min_adj", "")))
        details.append(kv("rss_kb", d.get("rss_kb", "")))
        details.append(kv("reason", kill_reason))
        if d.get("tail"):
            details.append(kv("tail", d.get("tail", "")))

        killinfo_list = d.get("killinfo", [])
        if killinfo_list:
            details.append("  关联 killinfo:")
            fields_order = [
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
            for ki in killinfo_list:
                parsed_fields = ki.get("parsed_fields", {})
                kv_lines = []
                for key, label in fields_order:
                    val = parsed_fields.get(key, "")
                    if val == "":
                        continue
                    kv_lines.append(f"      {label:<12}: {val}")
                # 组合展示文件页、匿名页
                af = _safe_int(parsed_fields.get("active_file_kb"))
                inf = _safe_int(parsed_fields.get("inactive_file_kb"))
                if af is not None and inf is not None:
                    kv_lines.append(f"      file_pages   : {af + inf} (inactive {inf} active {af})")
                aa = _safe_int(parsed_fields.get("active_anon_kb"))
                ina = _safe_int(parsed_fields.get("inactive_anon_kb"))
                if aa is not None and ina is not None:
                    kv_lines.append(f"      anon_pages   : {aa + ina} (inactive {ina} active {aa})")
                if not kv_lines:
                    raw_line = "      raw: [" + ", ".join(ki.get("raw_fields", [])) + "]"
                    kv_lines.append(raw_line)
                ki_time = ki["time"].strftime("%m-%d %H:%M:%S.%f")[:-3]
                details.append(f"    {ki_time}")
                details.extend(kv_lines)
        else:
            details.append("  未找到对应的 killinfo")
    else:
        k = event['details']['kill_info']
        p = event['details']['proc_info']
        m = event['details']['mem_info']
        
        # 添加重要进程标记
        imp_mark = f" [重要进程]" if p.get('isImp', 'false') == 'true' else ''
        main_mark = f" [主进程]" if p.get('isMain', 'false') == 'true' else ''
        
        details.append(f"  查杀信息:")
        details.append(f"    查杀类型: {k.get('killTypeDesc', k['killType'])} ({k['killType']})")  
        details.append(f"    可查杀最低分值: {k['minScore']} ({k.get('minScoreDesc', describe_min_score(k['minScore']))})")
        details.append(f"    可查杀进程数: {k['killableProcCount']}")
        details.append(f"    重要应用数量: {k['importantAppCount']}")
        details.append(f"    本次已清理进程数: {k['killedCount']}")
        details.append(f"    已清理重要进程数: {k['killedImpCount']}")
        details.append(f"    跳过计数: {k['skipCount']}")
        details.append(f"    目标内存: {k['targetMem']} KB")
        details.append(f"    需要释放内存: {k['targetReleaseMem']} KB")
        if event['type'] == 'kill':
            details.append(f"    已释放内存: {k['killedPss']} KB")
        
        if event['type'] != 'trig':  # trig事件没有具体进程信息
            details.append(f"  进程信息:")
            details.append(f"    UID: {p['uid']}, PID: {p['pid']}{imp_mark}{main_mark}")
            details.append(f"    进程优先级: {p['adj']}")
            details.append(f"    评分: {p['score']}")
            details.append(f"    PSS内存: {p['pss']} KB")
            details.append(f"    交换内存: {p['swapUsed']} KB")
            if event['type'] == 'kill':
                details.append(f"    实际释放内存: {p['ret']} KB")
        
        details.append(f"  当前内存信息:")
        details.append(f"    空闲内存: {m['memFree']} KB")
        details.append(f"    可用内存: {m['memAvail']} KB")
        details.append(f"    文件缓存: {m['memFile']} KB")
        details.append(f"    匿名内存: {m['memAnon']} KB")
        details.append(f"    空闲交换区: {m['memSwapFree']} KB")
        details.append(f"    CMA空闲内存: {m['cmaFree']} KB")
    
    # 组合所有部分
    result = [
        f"事件 {idx+1}",
        f"{time_str}  {event_type}  {proc_display}",
        *details,
        "-" * 80
    ]
    
    return "\n".join(result)

def format_event_simple(event, idx):
    """格式化单个事件为文本"""
    time_str = event['time'].strftime("%m-%d %H:%M:%S.%f")[:-3]
    
    # 高亮显示特定进程
    proc_name = event.get('full_name', event['process_name'])
    if event['process_name'] in HIGHLIGHT_PROCESSES:
        proc_display = f"{proc_name}"
    else:
        proc_display = proc_name
    
    # 添加子进程标记
    if event['type'] == 'start' and event['is_subprocess']:
        proc_display += f" (子进程)"
    
    # 构造事件类型显示
    if event['type'] == 'start':
        d = event.get("details", {})
        start_kind = d.get("start_kind")
        round_no = d.get("round")
        if start_kind == "cold":
            base = "启动(冷)"
        elif start_kind == "hot":
            base = "启动(热)"
        else:
            base = "启动"
        event_type = f"{base}R{round_no}" if round_no else base
        if _is_possible_anomaly_start_record(d):
            event_type += f"[{POSSIBLE_ANOMALY_START_LABEL}]"
    elif event['type'] == 'kill':
        event_type = f"查杀"
    elif event['type'] == 'trig':
        event_type = f"触发查杀"
    elif event['type'] == 'skip':
        event_type = f"跳过({event['details']['event_tag'][5:]})"  # 显示跳过原因
    elif event['type'] == 'lmk':
        event_type = f"LMK查杀"
    elif event['type'] == 'proc_start_only':
        event_type = f"疑似后台启动"
    else:
        event_type = event.get('type', '未知事件')

    
    # 组合所有部分
    result = [
        f"事件 {idx+1} {time_str}  {event_type}  {proc_display}"
    ]
    
    return "\n".join(result)

def compute_summary_data(events):
    """计算统计数据，返回 summary 字典，供文本/HTML复用"""
    summary = {
        'total_events': len(events),
        'start_count': sum(1 for e in events if e['type'] == 'start'),
        'kill_count': sum(1 for e in events if e['type'] == 'kill'),
        'lmk_count': sum(1 for e in events if e['type'] == 'lmk'),
        'trig_count': sum(1 for e in events if e['type'] == 'trig'),
        'skip_count': sum(1 for e in events if e['type'] == 'skip'),
        'proc_start_only_count': sum(1 for e in events if e['type'] == 'proc_start_only'),
        'subprocess_start_count': sum(1 for e in events if e['type'] == 'start' and e['is_subprocess']),
        'highlight_stats': {p: {'start': 0, 'kill': 0, 'lmk': 0, 'skip': 0} for p in HIGHLIGHT_PROCESSES},
        'total_release_mem': 0,
        'total_killed': 0,
        'killed_imp_count': 0,
        'top_killed': {},
        'top_lmk_killed': {},
        'top_skipped': {},
        'kill_type_stats': {},  # 新增：killType统计
        'adj_stats': {},  # 新增：adj统计
        'min_score_stats': {},  # 新增：minScore统计
        'lmk_reason_stats': defaultdict(int),
        'lmk_adj_stats': defaultdict(int),
        'main_proc_kill_stats': defaultdict(lambda: {'main_kill': 0, 'main_lmk': 0, 'sub_kill': 0, 'sub_lmk': 0}),
        # 主进程专用统计
        'main_overall': {
            'kill': 0,
            'lmk': 0,
            'kill_type_stats': defaultdict(int),
            'adj_stats': defaultdict(int),
            'lmk_adj_stats': defaultdict(int),
            'min_score_stats': defaultdict(int),
        },
        'main_proc_detail': defaultdict(lambda: {
            'kill': 0,
            'lmk': 0,
            'kill_type_stats': defaultdict(int),
            'adj_stats': defaultdict(int),
            'lmk_adj_stats': defaultdict(int),
        }),
        # 高亮进程统计（按主包名聚合，含主+子进程）
        'highlight_overall': {
            'main_kill': 0,
            'main_lmk': 0,
            'sub_kill': 0,
            'sub_lmk': 0,
            'main_kill_type_stats': defaultdict(int),
            'sub_kill_type_stats': defaultdict(int),
            'main_adj_stats': defaultdict(int),
            'sub_adj_stats': defaultdict(int),
            'main_lmk_adj_stats': defaultdict(int),
            'sub_lmk_adj_stats': defaultdict(int),
            'main_min_score_stats': defaultdict(int),
        },
        'highlight_event_ids': defaultdict(lambda: {'kill': [], 'lmk': []}),
        'highlight_proc_detail': defaultdict(lambda: {
            'main_kill': 0,
            'main_lmk': 0,
            'sub_kill': 0,
            'sub_lmk': 0,
            'main_kill_type_stats': defaultdict(int),
            'sub_kill_type_stats': defaultdict(int),
            'main_adj_stats': defaultdict(int),
            'sub_adj_stats': defaultdict(int),
            'main_lmk_adj_stats': defaultdict(int),
            'sub_lmk_adj_stats': defaultdict(int),
        }),
        # 新增：被杀时的平均内存指标
        'mem_metrics': {
            'all': {'cnt': 0, 'mem_free': 0, 'file_pages': 0, 'anon_pages': 0, 'swap_free': 0},
            'main': {'cnt': 0, 'mem_free': 0, 'file_pages': 0, 'anon_pages': 0, 'swap_free': 0},
            'highlight_main': {'cnt': 0, 'mem_free': 0, 'file_pages': 0, 'anon_pages': 0, 'swap_free': 0},
            # 新增：触发事件整体
            'trig': {'cnt': 0, 'mem_free': 0, 'file_pages': 0, 'anon_pages': 0, 'swap_free': 0},
        },
        # 新增：被杀时内存样本，便于中位数/P95
        'mem_samples': {
            'all': defaultdict(list),
            'main': defaultdict(list),
            'highlight_main': defaultdict(list),
            'trig': defaultdict(list),
        },
        # 新增：低 memfree 查杀 TOP10（kill 事件）
        'low_memfree_kills': [],
        # 新增：高亮主进程驻留统计
        'highlight_residency_stats': {
            'per_proc': defaultdict(lambda: {'durations': [], 'starts': 0, 'kills': 0, 'alive': False, 'alive_since': None}),
            'alive_now': [],
            'all_durations': [],
            'avg_duration_sec': 0.0,
        },
        'cont_startup_stats': {
            'target_start_total': 0,
            'cold_count': 0,
            'hot_count': 0,
            'unknown_count': 0,
            'second_round_cold': 0,
            'second_round_hot': 0,
            'second_round_unknown': 0,
        },
    }
    
    def _accumulate(mem_key, metrics):
        bucket = summary['mem_metrics'].setdefault(mem_key, {'cnt': 0, 'mem_free': 0, 'file_pages': 0, 'anon_pages': 0, 'swap_free': 0})
        samples = summary['mem_samples'].setdefault(mem_key, defaultdict(list))
        bucket['cnt'] += 1
        for k in ('mem_free', 'file_pages', 'anon_pages', 'swap_free'):
            val = metrics.get(k)
            if val is not None:
                bucket[k] += val
                samples[k].append(val)
    
    # 辅助：高亮驻留状态表
    hl_res_state = summary['highlight_residency_stats']['per_proc']

        # 统计高亮进程和内存信息
    for idx, event in enumerate(events):
        if event['process_name'] in HIGHLIGHT_PROCESSES:
            if event['type'] == 'start':
                summary['highlight_stats'][event['process_name']]['start'] += 1
            elif event['type'] == 'kill':
                summary['highlight_stats'][event['process_name']]['kill'] += 1
            elif event['type'] == 'lmk':
                summary['highlight_stats'][event['process_name']]['lmk'] += 1
            elif event['type'] == 'skip':
                summary['highlight_stats'][event['process_name']]['skip'] += 1
        
        base_name = event['process_name'].split(':')[0]
        valid_pkg = _looks_like_package(base_name)
        is_anomaly_start = _is_possible_anomaly_start_record(event.get("details", {}))

        if event['type'] == 'start' and not event.get('is_subprocess') and not is_anomaly_start:
            cs = summary['cont_startup_stats']
            cs['target_start_total'] += 1
            start_kind = event.get('details', {}).get('start_kind')
            if start_kind == 'cold':
                cs['cold_count'] += 1
            elif start_kind == 'hot':
                cs['hot_count'] += 1
            else:
                cs['unknown_count'] += 1

            round_no = event.get('details', {}).get('round')
            if round_no == 2:
                if start_kind == 'cold':
                    cs['second_round_cold'] += 1
                elif start_kind == 'hot':
                    cs['second_round_hot'] += 1
                else:
                    cs['second_round_unknown'] += 1

        if event['type'] == 'kill':
            try:
                summary['total_release_mem'] += int(event['details']['kill_info']['killedPss'])
                summary['total_killed'] += 1
                
                # 统计被杀进程
                proc_name = event['process_name']
                summary['top_killed'][proc_name] = summary['top_killed'].get(proc_name, 0) + 1
                if valid_pkg:
                    if event.get('is_subprocess', False):
                        summary['main_proc_kill_stats'][base_name]['sub_kill'] += 1
                    else:
                        summary['main_proc_kill_stats'][base_name]['main_kill'] += 1
                        summary['main_overall']['kill'] += 1
                        summary['main_proc_detail'][base_name]['kill'] += 1
                    # 高亮进程统计（主名命中即可）
                    if base_name in HIGHLIGHT_PROCESSES:
                        if event.get('is_subprocess', False):
                            summary['highlight_overall']['sub_kill'] += 1
                            summary['highlight_proc_detail'][base_name]['sub_kill'] += 1
                        else:
                            summary['highlight_overall']['main_kill'] += 1
                            summary['highlight_proc_detail'][base_name]['main_kill'] += 1
                            summary['highlight_event_ids'][base_name]['kill'].append(idx + 1)
                
                # 统计重要进程
                if event['details']['proc_info'].get('isImp', 'false') == 'true':
                    summary['killed_imp_count'] += 1
                
                # 统计killType分布
                kill_type = event['details']['kill_info']['killType']
                kill_type_desc = event['details']['kill_info'].get('killTypeDesc', kill_type)
                summary['kill_type_stats'][kill_type_desc] = summary['kill_type_stats'].get(kill_type_desc, 0) + 1
                if valid_pkg:
                    if not event.get('is_subprocess', False):
                        summary['main_overall']['kill_type_stats'][kill_type_desc] += 1
                        summary['main_proc_detail'][base_name]['kill_type_stats'][kill_type_desc] += 1
                    if base_name in HIGHLIGHT_PROCESSES:
                        if event.get('is_subprocess', False):
                            summary['highlight_overall']['sub_kill_type_stats'][kill_type_desc] += 1
                            summary['highlight_proc_detail'][base_name]['sub_kill_type_stats'][kill_type_desc] += 1
                        else:
                            summary['highlight_overall']['main_kill_type_stats'][kill_type_desc] += 1
                            summary['highlight_proc_detail'][base_name]['main_kill_type_stats'][kill_type_desc] += 1

                # 统计minScore分布
                min_score = event['details']['kill_info'].get('minScore', '')
                min_score_desc = event['details']['kill_info'].get('minScoreDesc') or describe_min_score(min_score)
                summary['min_score_stats'][min_score_desc] = summary['min_score_stats'].get(min_score_desc, 0) + 1
                if valid_pkg and not event.get('is_subprocess', False):
                    summary['main_overall']['min_score_stats'][min_score_desc] += 1
                    if base_name in HIGHLIGHT_PROCESSES:
                        summary['highlight_overall']['main_min_score_stats'][min_score_desc] += 1
                
                # 统计adj分布
                adj = event['details']['proc_info']['adj']
                summary['adj_stats'][adj] = summary['adj_stats'].get(adj, 0) + 1
                if valid_pkg:
                    if not event.get('is_subprocess', False):
                        summary['main_overall']['adj_stats'][adj] += 1
                        summary['main_proc_detail'][base_name]['adj_stats'][adj] += 1
                    if base_name in HIGHLIGHT_PROCESSES:
                        if event.get('is_subprocess', False):
                            summary['highlight_overall']['sub_adj_stats'][adj] += 1
                            summary['highlight_proc_detail'][base_name]['sub_adj_stats'][adj] += 1
                        else:
                            summary['highlight_overall']['main_adj_stats'][adj] += 1
                            summary['highlight_proc_detail'][base_name]['main_adj_stats'][adj] += 1
            except:
                pass
        elif event['type'] == 'lmk':
            summary['top_lmk_killed'][event['process_name']] = summary['top_lmk_killed'].get(event['process_name'], 0) + 1
            adj = event['details'].get('adj', '')
            if adj:
                summary['lmk_adj_stats'][adj] += 1
            reason = event['details'].get('reason') or "未知"
            if reason:
                summary['lmk_reason_stats'][reason] += 1
            base_name = event['process_name'].split(':')[0]
            if valid_pkg:
                if event.get('is_subprocess', False):
                    summary['main_proc_kill_stats'][base_name]['sub_lmk'] += 1
                else:
                    summary['main_proc_kill_stats'][base_name]['main_lmk'] += 1
                    summary['main_overall']['lmk'] += 1
                    summary['main_proc_detail'][base_name]['lmk'] += 1
                    if adj:
                        summary['main_overall']['lmk_adj_stats'][adj] += 1
                        summary['main_proc_detail'][base_name]['lmk_adj_stats'][adj] += 1
                if base_name in HIGHLIGHT_PROCESSES:
                    if event.get('is_subprocess', False):
                        summary['highlight_overall']['sub_lmk'] += 1
                        summary['highlight_proc_detail'][base_name]['sub_lmk'] += 1
                        if adj:
                            summary['highlight_overall']['sub_lmk_adj_stats'][adj] += 1
                            summary['highlight_proc_detail'][base_name]['sub_lmk_adj_stats'][adj] += 1
                    else:
                        summary['highlight_overall']['main_lmk'] += 1
                        summary['highlight_proc_detail'][base_name]['main_lmk'] += 1
                        summary['highlight_event_ids'][base_name]['lmk'].append(idx + 1)
                        if adj:
                            summary['highlight_overall']['main_lmk_adj_stats'][adj] += 1
                            summary['highlight_proc_detail'][base_name]['main_lmk_adj_stats'][adj] += 1
        elif event['type'] == 'skip':
            try:
                # 统计跳过进程
                proc_name = event['process_name']
                summary['top_skipped'][proc_name] = summary['top_skipped'].get(proc_name, 0) + 1
            except:
                pass

        # 记录内存指标（kill 和 lmk）
        if event['type'] in ('kill', 'lmk', 'trig'):
            metrics = _extract_mem_metrics(event)
            if metrics:
                _accumulate('all', metrics)
                if not event.get('is_subprocess', False):
                    _accumulate('main', metrics)
                    if event['process_name'].split(':')[0] in HIGHLIGHT_PROCESSES:
                        _accumulate('highlight_main', metrics)
                if event['type'] == 'trig':
                    _accumulate('trig', metrics)
                # 记录 kill 事件的 mem_free，用于后续 TOP10 统计
                if event['type'] == 'kill':
                    mem_free_val = metrics.get('mem_free')
                    if mem_free_val is not None:
                        summary['low_memfree_kills'].append({
                            'mem_free': mem_free_val,
                            'process': event.get('full_name', event['process_name']),
                            'event_id': idx + 1,
                            'time': event.get('time'),
                            'type': event.get('type')
                        })

        # 高亮主进程驻留统计（仅主进程）
        base = event['process_name'].split(':')[0]
        if base in HIGHLIGHT_PROCESSES and not event.get('is_subprocess', False):
            state = hl_res_state[base]
            if event['type'] == 'start':
                if _is_possible_anomaly_start_record(event.get("details", {})):
                    continue
                # 若已有存活实例，视为重启，先结算上一段
                if state['alive'] and state['alive_since']:
                    duration = (event['time'] - state['alive_since']).total_seconds()
                    if duration >= 0:
                        state['durations'].append(duration)
                        summary['highlight_residency_stats']['all_durations'].append(duration)
                state['alive'] = True
                state['alive_since'] = event['time']
                state['starts'] += 1
            elif event['type'] in ('kill', 'lmk'):
                if state['alive'] and state['alive_since']:
                    duration = (event['time'] - state['alive_since']).total_seconds()
                    if duration >= 0:
                        state['durations'].append(duration)
                        summary['highlight_residency_stats']['all_durations'].append(duration)
                state['alive'] = False
                state['alive_since'] = None
                state['kills'] += 1

    # 计算平均值
    summary['mem_avg'] = {}
    for key, bucket in summary['mem_metrics'].items():
        cnt = bucket['cnt'] or 1  # 防止除零
        summary['mem_avg'][key] = {
            'mem_free': bucket['mem_free'] / cnt,
            'file_pages': bucket['file_pages'] / cnt,
            'anon_pages': bucket['anon_pages'] / cnt,
            'swap_free': bucket['swap_free'] / cnt,
            'count': bucket['cnt'],
        }

    # 计算内存统计（中位数/P95/最小/最大）
    summary['mem_stats'] = {}
    for key, sample_dict in summary['mem_samples'].items():
        summary['mem_stats'][key] = {}
        for metric in ('mem_free', 'file_pages', 'anon_pages', 'swap_free'):
            stats = _calc_stats(sample_dict.get(metric, []))
            summary['mem_stats'][key][metric] = stats

    # 结算仍存活的高亮主进程驻留时长（到日志结束）
    if events:
        end_time = events[-1]['time']
        for base, state in hl_res_state.items():
            if state['alive'] and state.get('alive_since'):
                duration = (end_time - state['alive_since']).total_seconds()
                if duration >= 0:
                    state['durations'].append(duration)
                    summary['highlight_residency_stats']['all_durations'].append(duration)
                summary['highlight_residency_stats']['alive_now'].append(base)

    # 计算平均驻留
    all_durations = summary['highlight_residency_stats']['all_durations']
    if all_durations:
        summary['highlight_residency_stats']['avg_duration_sec'] = sum(all_durations) / len(all_durations)

    # 取 memfree 最低的查杀 TOP10
    if summary['low_memfree_kills']:
        summary['low_memfree_kills'] = sorted(
            summary['low_memfree_kills'],
            key=lambda x: x.get('mem_free', float('inf'))
        )[:10]

    return summary


def generate_summary(events):
    """生成分析总结报告"""
    summary = compute_summary_data(events)
    highlight_timeline = build_highlight_timeline(events)
    highlight_residency = build_highlight_residency(events)
    highlight_runs = compute_highlight_runs(events)

    def _fmt_mem_lines(key, label):
        stats_map = summary.get('mem_stats', {}).get(key, {})
        avg_map = summary.get('mem_avg', {}).get(key, {})
        if not stats_map or not avg_map or avg_map.get('count', 0) == 0:
            return [f"  {label}: 无数据"]
        def _fmt_metric(metric_label, metric_key):
            st = stats_map.get(metric_key, {})
            if not st or st.get('count', 0) == 0:
                return f"    {metric_label:<8} 无数据"
            return (f"    {metric_label:<8} avg {st['avg']:.1f} | p50 {st['median']:.1f} | "
                    f"p95 {st['p95']:.1f} | min {st['min']:.1f} | max {st['max']:.1f}")
        lines = [f"  {label} (样本 {avg_map.get('count',0)})"]
        lines.append(_fmt_metric('memfree', 'mem_free'))
        lines.append(_fmt_metric('file', 'file_pages'))
        lines.append(_fmt_metric('anon', 'anon_pages'))
        lines.append(_fmt_metric('swapfree', 'swap_free'))
        return lines
    
    # 生成总结文本
    report = [
        f"=" * 50 + " 分析总结 " + "=" * 50 + f"",
        f"总事件数: {summary['total_events']} (启动: {summary['start_count']}, 查杀: {summary['kill_count']}, LMK查杀: {summary['lmk_count']}, "
        f"触发查杀: {summary['trig_count']}, 跳过: {summary['skip_count']})",
        f"疑似后台启动(am_proc_start-only): {summary.get('proc_start_only_count', 0)}",
        f"子进程启动: {summary['subprocess_start_count']}",
        f"总释放内存: {summary['total_release_mem']:,} KB ({summary['total_release_mem']/1024:.2f} MB)",
        f"总杀死进程数: {summary['total_killed']} (含重要进程: {summary['killed_imp_count']})",
        f"\n被杀/触发时内存统计 (单位KB):",
        f"\n查杀类型分布:"
    ]
    cs = summary.get("cont_startup_stats", {})
    if cs.get("target_start_total", 0) > 0:
        report.append(
            f"\n连续启动判定(启动APP主进程): 总启动 {cs['target_start_total']}, "
            f"冷启动 {cs['cold_count']}, 热启动 {cs['hot_count']}, 未知 {cs['unknown_count']}"
        )
        report.append(
            f"第二轮判定: 冷启动 {cs['second_round_cold']}, 热启动 {cs['second_round_hot']}, 未知 {cs['second_round_unknown']}"
        )
    report.extend(_fmt_mem_lines('all', '全部进程'))
    report.extend(_fmt_mem_lines('main', '主进程'))
    report.extend(_fmt_mem_lines('highlight_main', '高亮主进程(主)'))
    report.extend(_fmt_mem_lines('trig', '触发事件'))
    # 高亮进程驻留表（前两轮）
    if highlight_runs:
        report.append("\n高亮主进程驻留表（前两轮）:")
        for r in highlight_runs:
            report.append(
                f"  {r['proc']}: 冷启动(第2轮) {r['second_cold']} | "
                f"启动1 {r['start1']} -> 被杀1 {r['kill1']} | 驻留1 {r['dur1']} | "
                f"启动2 {r['start2']} -> 被杀2 {r['kill2']} | 驻留2 {r['dur2']} | "
                f"平均驻留 {r['avg']}"
            )
    # 添加killType分布统计
    if summary['kill_type_stats']:
        for kill_type, count in sorted(summary['kill_type_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"  {kill_type:<20} {count:>3}次")
    
    # 添加 minScore 分布统计
    report.append(f"\n可查杀最低分值分布:")
    if summary['min_score_stats']:
        for ms, count in sorted(summary['min_score_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"  {ms:<30} {count:>3}次")

    # 添加adj分布统计
    report.append(f"\n进程优先级(adj)分布:")
    if summary['adj_stats']:
        for adj, count in sorted(summary['adj_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"  adj={adj:<5} {count:>3}次")

    # LMK 统计
    report.append(f"\nLMK 查杀原因分布:")
    if summary['lmk_reason_stats']:
        for reason, count in sorted(summary['lmk_reason_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"  {reason:<20} {count:>3}次")
    report.append(f"\nLMK 进程优先级(adj)分布:")
    if summary['lmk_adj_stats']:
        for adj, count in sorted(summary['lmk_adj_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"  adj={adj:<5} {count:>3}次")
    
    # 添加高亮进程统计
    report.append(f"\n高亮进程统计:")
    for proc, stats in summary['highlight_stats'].items():
        if stats['start'] > 0 or stats['kill'] > 0 or stats['skip'] > 0 or stats['lmk'] > 0:
            ratio_kill = stats['kill']/stats['start'] if stats['start'] > 0 else float('inf')
            ratio_lmk = stats['lmk']/stats['start'] if stats['start'] > 0 else float('inf')
            ratio_skip = stats['skip']/stats['start'] if stats['start'] > 0 else float('inf')
            ratio_kill_str = f"{ratio_kill:.2f}" if ratio_kill != float('inf') else "N/A"
            ratio_lmk_str = f"{ratio_lmk:.2f}" if ratio_lmk != float('inf') else "N/A"
            ratio_skip_str = f"{ratio_skip:.2f}" if ratio_skip != float('inf') else "N/A"
            report.append(f"  {proc:<30} 启动: {stats['start']:>3}次, 查杀: {stats['kill']:>3}次, LMK查杀: {stats['lmk']:>3}次, 跳过: {stats['skip']:>3}次, "
                         f"查杀/启动比: {ratio_kill_str}, LMK/启动比: {ratio_lmk_str}, 跳过/启动比: {ratio_skip_str}")
    
    # 添加最常被杀进程
    if summary['top_killed']:
        top_killed = sorted(summary['top_killed'].items(), key=lambda x: x[1], reverse=True)[:10]
        report.append(f"\n最常被杀进程 TOP10:")
        for proc, count in top_killed:
            is_highlight = "是" if proc in HIGHLIGHT_PROCESSES else "否"
            report.append(f"  {proc:<30} {count}次 {'[高亮]' if is_highlight == '是' else ''}")

    if summary['top_lmk_killed']:
        top_lmk = sorted(summary['top_lmk_killed'].items(), key=lambda x: x[1], reverse=True)[:10]
        report.append(f"\n最常 LMK 查杀进程 TOP10:")
        for proc, count in top_lmk:
            is_highlight = "是" if proc in HIGHLIGHT_PROCESSES else "否"
            report.append(f"  {proc:<30} {count}次 {'[高亮]' if is_highlight == '是' else ''}")
    
    # 主进程专项摘要
    report.append(f"\n主进程查杀专题:")
    main_total_kill = summary['main_overall']['kill']
    main_total_lmk = summary['main_overall']['lmk']
    report.append(f"  主进程总计: kill {main_total_kill} 次, lmk {main_total_lmk} 次")
    if main_total_kill:
        report.append("  主进程查杀类型分布:")
        for kt, c in sorted(summary['main_overall']['kill_type_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"    {kt:<20} {c}")
        if summary['main_overall']['min_score_stats']:
            report.append("  主进程 minScore 分布:")
            for ms, c in sorted(summary['main_overall']['min_score_stats'].items(), key=lambda x: x[1], reverse=True):
                report.append(f"    {ms:<24} {c}")
    if summary['main_overall']['adj_stats']:
        report.append("  主进程查杀 adj 分布:")
        for adj, c in sorted(summary['main_overall']['adj_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"    adj={adj:<5} {c}")
    if summary['main_overall']['lmk_adj_stats']:
        report.append("  主进程 LMK adj 分布:")
        for adj, c in sorted(summary['main_overall']['lmk_adj_stats'].items(), key=lambda x: x[1], reverse=True):
            report.append(f"    adj={adj:<5} {c}")
    # 按包名列出主进程详情
    if summary['main_proc_detail']:
        report.append("  主进程明细:")
        ranked_base = sorted(
            summary['main_proc_detail'].items(),
            key=lambda kv: (kv[1]['kill'] + kv[1]['lmk']),
            reverse=True,
        )
        for base, stats in ranked_base:
            total = stats['kill'] + stats['lmk']
            if total == 0:
                continue
            report.append(f"    {base}: kill {stats['kill']}, lmk {stats['lmk']}")
            if stats['kill_type_stats']:
                kt_parts = [f"{kt}:{cnt}" for kt, cnt in sorted(stats['kill_type_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      killType -> {'; '.join(kt_parts)}")
            if stats['adj_stats']:
                adj_parts = [f"{adj}:{cnt}" for adj, cnt in sorted(stats['adj_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      adj(kill) -> {'; '.join(adj_parts)}")
            if stats['lmk_adj_stats']:
                lmk_adj_parts = [f"{adj}:{cnt}" for adj, cnt in sorted(stats['lmk_adj_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      adj(lmk) -> {'; '.join(lmk_adj_parts)}")

    # 高亮进程专题
    report.append(f"\n高亮进程专题（仅主进程）:")
    hl_main_kill = summary['highlight_overall']['main_kill']
    hl_main_lmk = summary['highlight_overall']['main_lmk']
    report.append(f"  总计: kill {hl_main_kill} 次, lmk {hl_main_lmk} 次")
    if summary['highlight_overall']['main_kill_type_stats']:
        parts = [f"{kt}:{c}" for kt, c in sorted(summary['highlight_overall']['main_kill_type_stats'].items(), key=lambda x: x[1], reverse=True)]
        report.append(f"  查杀类型: {'; '.join(parts)}")
    if summary['highlight_overall']['main_min_score_stats']:
        parts = [f"{ms}:{c}" for ms, c in sorted(summary['highlight_overall']['main_min_score_stats'].items(), key=lambda x: x[1], reverse=True)]
        report.append(f"  minScore: {'; '.join(parts)}")
    if summary['highlight_overall']['main_adj_stats']:
        parts = [f"{adj}:{c}" for adj, c in sorted(summary['highlight_overall']['main_adj_stats'].items(), key=lambda x: x[1], reverse=True)]
        report.append(f"  adj分布(kill): {'; '.join(parts)}")
    if summary['highlight_overall']['main_lmk_adj_stats']:
        parts = [f"{adj}:{c}" for adj, c in sorted(summary['highlight_overall']['main_lmk_adj_stats'].items(), key=lambda x: x[1], reverse=True)]
        report.append(f"  adj分布(lmk): {'; '.join(parts)}")
    # 高亮驻留统计
    hres = summary.get('highlight_residency_stats', {})
    avg_res = hres.get('avg_duration_sec', 0)
    report.append(f"  平均驻留时长: {_format_duration(avg_res)}")
    alive_now = hres.get('alive_now', [])
    if alive_now:
        report.append(f"  当前仍存活: {', '.join(alive_now)}")
    if hres.get('per_proc'):
        report.append(f"  进程驻留明细:")
        for base, st in sorted(hres['per_proc'].items(), key=lambda kv: -(sum(kv[1]['durations'])/len(kv[1]['durations']) if kv[1]['durations'] else 0)):
            if not st['durations']:
                continue
            avg = sum(st['durations'])/len(st['durations'])
            report.append(f"    {base}: 平均{_format_duration(avg)} | 启动 {st['starts']} | kill/lmk {st['kills']} | 段数 {len(st['durations'])}")
    if summary['highlight_proc_detail']:
        report.append("  高亮进程明细:")
        ranked_hl = sorted(
            summary['highlight_proc_detail'].items(),
            key=lambda kv: (kv[1]['main_kill'] + kv[1]['main_lmk']),
            reverse=True,
        )
        for base, stats in ranked_hl:
            total = stats['main_kill'] + stats['main_lmk']
            if total == 0:
                continue
            report.append(f"    {base}: kill {stats['main_kill']}, lmk {stats['main_lmk']}")
            evt_ids = summary.get('highlight_event_ids', {}).get(base, {})
            if evt_ids:
                kill_ids = evt_ids.get('kill') or []
                lmk_ids = evt_ids.get('lmk') or []
                if kill_ids:
                    report.append(f"      事件号(kill): {', '.join(map(str, kill_ids))}")
                if lmk_ids:
                    report.append(f"      事件号(lmk): {', '.join(map(str, lmk_ids))}")
            if stats['main_kill_type_stats']:
                kt_parts = [f"{kt}:{cnt}" for kt, cnt in sorted(stats['main_kill_type_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      killType -> {'; '.join(kt_parts)}")
            if stats['main_adj_stats']:
                adj_parts = [f"{adj}:{cnt}" for adj, cnt in sorted(stats['main_adj_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      adj(kill) -> {'; '.join(adj_parts)}")
            if stats['main_lmk_adj_stats']:
                lmk_adj_parts = [f"{adj}:{cnt}" for adj, cnt in sorted(stats['main_lmk_adj_stats'].items(), key=lambda x: x[1], reverse=True)]
                report.append(f"      adj(lmk) -> {'; '.join(lmk_adj_parts)}")

    # 高亮进程启动/查杀时间线（主进程）
    if highlight_timeline:
        report.append(f"\n高亮进程时间线（仅主进程）:")
        max_len = 0
        for it in highlight_timeline:
            if it.get('label_class', '').startswith('start'):
                max_len = max(max_len, len(f"{it['label']}    {it['process']}"))
            else:
                max_len = max(max_len, len(f"{it['process']}   {it['label']}"))
        inner_width = max(max_len, 70)
        def _fmt_line(it):
            ts = it['time']
            label = it['label']
            proc = it['process']
            if it.get('label_class', '').startswith('start'):
                content = f"{label}    {proc}".ljust(inner_width)
            else:
                content = f"{proc}   {label}".rjust(inner_width)
            return f"- {ts:<19} | {content} |"
        for item in highlight_timeline:
            report.append(_fmt_line(item))

    if highlight_residency:
        report.append(f"\n高亮进程驻留率（前5次窗口 & 全量，主进程）:")
        report.append("轮次 启动类型 序号 应用 启动前存活数/总(前5) 全部存活/总 前1 前2 前3 前4 前5")
        for rec in highlight_residency:
            round_txt = rec.get("round") if rec.get("round") is not None else "-"
            start_kind_txt = rec.get("start_kind_cn", "未知")
            if rec.get("is_anomaly"):
                note_txt = rec.get("anomaly_note", "") or POSSIBLE_ANOMALY_START_NOTE
                report.append(
                    f"{str(round_txt):>2} {start_kind_txt:<4} {rec['seq']:>2} "
                    f"{rec['process']:<24} - - - - - - -  [{POSSIBLE_ANOMALY_START_LABEL}: {note_txt}]"
                )
                continue
            row = (
                f"{str(round_txt):>2} {start_kind_txt:<4} {rec['seq']:>2} "
                f"{rec['process']:<24} "
                f"{rec['alive_cnt']}/{rec['window_total']} "
                f"{rec['all_rate']:<18} "
                f"{rec['per_window'][1]['rate']:<20} "
                f"{rec['per_window'][2]['rate']:<20} "
                f"{rec['per_window'][3]['rate']:<20} "
                f"{rec['per_window'][4]['rate']:<20} "
                f"{rec['per_window'][5]['rate']:<20}"
            )
            report.append(row)
            if rec['killed_list']:
                report.append(f"    被杀: {', '.join(rec['killed_list'])}")
        # 全量平均驻留率
        all_rates = []
        for rec in highlight_residency:
            if rec.get("is_anomaly"):
                continue
            pct_full = rec.get("all_rate_value")
            if pct_full is not None:
                all_rates.append(pct_full)
        if all_rates:
            avg_full = sum(all_rates) / len(all_rates)
            report.append(f"  全部前序平均驻留率: {avg_full:.1f}%")

    # memfree 最低的查杀 TOP10
    low_memfree = summary.get('low_memfree_kills', [])
    if low_memfree:
        report.append(f"\nmemfree 最低的查杀 TOP10（Kill事件）:")
        report.append("排名 memfree(KB) 时间 应用 事件号")
        for rank, rec in enumerate(low_memfree, 1):
            ts = _format_dt(rec.get('time'))
            proc = rec.get('process', '')
            event_id = rec.get('event_id', '-')
            mem_free = rec.get('mem_free', '-')
            report.append(f" {rank:>2}  {mem_free:>8}  {ts:<19}  {proc:<30}  #{event_id}")

    # 主/子进程查杀分布
    report.append(f"\n主进程查杀统计:")
    if summary['main_proc_kill_stats']:
        ranked_main = sorted(
            summary['main_proc_kill_stats'].items(),
            key=lambda kv: (kv[1]['main_kill'] + kv[1]['main_lmk'] + kv[1]['sub_kill'] + kv[1]['sub_lmk']),
            reverse=True,
        )
        for base, stats in ranked_main:
            main_total = stats['main_kill'] + stats['main_lmk']
            total = main_total + stats['sub_kill'] + stats['sub_lmk']
            # 仅展示有主进程命中的条目，避免只有子进程时被误认为主进程被杀
            if main_total == 0:
                continue
            if total == 0:
                continue
            report.append(
                f"  {base:<30} 主进程: kill {stats['main_kill']}, lmk {stats['main_lmk']} | "
                f"子进程: kill {stats['sub_kill']}, lmk {stats['sub_lmk']}"
            )

    # 添加最常跳过进程
    if summary['top_skipped']:
        top_skipped = sorted(summary['top_skipped'].items(), key=lambda x: x[1], reverse=True)[:10]
        report.append(f"\n最常跳过进程 TOP10:")
        for proc, count in top_skipped:
            is_highlight = "是" if proc in HIGHLIGHT_PROCESSES else "否"
            report.append(f"  {proc:<30} {count}次 {'[高亮]' if is_highlight == '是' else ''}")
    
    # 添加最频繁启动进程
    start_counts = {}
    for event in events:
        if event['type'] == 'start':
            proc_name = event['process_name']
            start_counts[proc_name] = start_counts.get(proc_name, 0) + 1
    
    if start_counts:
        top_started = sorted(start_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        report.append(f"\n最频繁启动进程 TOP10:")
        for proc, count in top_started:
            is_highlight = "是" if proc in HIGHLIGHT_PROCESSES else "否"
            report.append(f"  {proc:<30} {count}次 {'[高亮]' if is_highlight == '是' else ''}")
    
    report.append("=" * 110)
    return "\n".join(report)

def generate_report(events, output_file):
    """生成文本报告"""
    with open(output_file, 'w', encoding='utf-8') as f:
        # 写入标题
        f.write(f"=" * 50 + " 进程启动与查杀分析报告 " + "=" * 50 + f"\n\n")
        
        # 写入统计信息
        start_count = sum(1 for e in events if e['type'] == 'start')
        kill_count = sum(1 for e in events if e['type'] == 'kill')
        lmk_count = sum(1 for e in events if e['type'] == 'lmk')
        trig_count = sum(1 for e in events if e['type'] == 'trig')
        skip_count = sum(1 for e in events if e['type'] == 'skip')
        proc_start_only_count = sum(1 for e in events if e['type'] == 'proc_start_only')
        subprocess_start_count = sum(1 for e in events if e['type'] == 'start' and e['is_subprocess'])
        total_release_mem = sum(int(e['details']['kill_info']['killedPss']) for e in events if e['type'] == 'kill')
        total_killed = kill_count  # 每行代表一个被杀死的进程
        
        f.write(f"总事件数: {len(events)}\n")
        f.write(f"启动事件: {start_count} (子进程启动: {subprocess_start_count})\n")
        f.write(f"查杀事件: {kill_count}，LMK查杀事件: {lmk_count}\n")
        f.write(f"触发查杀事件: {trig_count}\n")
        f.write(f"跳过事件: {skip_count}\n")
        f.write(f"疑似后台启动(am_proc_start-only): {proc_start_only_count}\n")
        f.write(f"释放内存: {total_release_mem:,} KB ({total_release_mem/1024:.2f} MB)\n")
        f.write(f"杀死进程数: {total_killed}\n\n")
        
        # 写入事件时间线
        f.write(f"事件时间线:\n")
        f.write(f"{'-' * 100}\n")
        
        for idx, event in enumerate(events):
            f.write(format_event_simple(event, idx) + "\n")

        f.write(f"{'-' * 100}\n")

        for idx, event in enumerate(events):
            f.write(format_event_detail(event, idx) + "\n")
        
        # 写入总结报告
        f.write(generate_summary(events))


def _to_plain(obj):
    """递归将 defaultdict 等转换为普通类型，便于 JSON 序列化"""
    if isinstance(obj, defaultdict):
        obj = dict(obj)
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _is_possible_anomaly_start_record(details: Optional[dict]) -> bool:
    d = details or {}
    return bool(d.get("possible_anomaly_start"))


def _startup_anomaly_note(details: Optional[dict]) -> str:
    d = details or {}
    note = str(d.get("anomaly_note", "") or "").strip()
    if note:
        return note
    if _is_possible_anomaly_start_record(d):
        return POSSIBLE_ANOMALY_START_NOTE
    return ""


def build_highlight_timeline(events):
    """构建高亮主进程的启动/查杀时间线"""
    items = []
    for e in events:
        if e.get('is_subprocess'):
            continue
        base = e.get('process_name', '').split(':')[0]
        if base not in HIGHLIGHT_PROCESSES:
            continue
        if e.get('type') not in ('start', 'kill', 'lmk'):
            continue
        dt = e['time']
        time_str = dt.strftime("%m-%d %H:%M:%S.%f")[:-3]
        if e['type'] == 'start':
            details = e.get("details") or {}
            if _is_possible_anomaly_start_record(details):
                label = POSSIBLE_ANOMALY_START_LABEL
                label_class = "start_anomaly"
            else:
                start_kind = details.get("start_kind")
                if start_kind == "cold":
                    label = "冷启动"
                    label_class = "start_cold"
                elif start_kind == "hot":
                    label = "热启动"
                    label_class = "start_hot"
                else:
                    label = "启动"
                    label_class = "start"
        elif e['type'] == 'kill':
            label = '查杀'
            label_class = 'kill'
        else:
            label = 'LMK'
            label_class = 'lmk'
        items.append({
            'dt': dt,
            'time': time_str,
            'label': label,
            'label_class': label_class,
            'process': e.get('full_name', e.get('process_name', '')),
            'is_anomaly': bool(e.get("type") == "start" and _is_possible_anomaly_start_record(e.get("details", {}))),
            'note': _startup_anomaly_note(e.get("details", {})),
        })
    return sorted(items, key=lambda x: x['dt'])


def build_highlight_residency(events, window_size: int = 5):
    """
    驻留率表：对第 i 次高亮主进程启动，回看最近最多 window_size 次高亮主进程启动，
    判定这些前序启动在该时刻是否已被 kill/lmk（主进程）。驻留率 = 存活数 / 前序数 * 100。
    返回每次启动的明细，包含前1~前5的存活信息。
    额外增加：对“全部前序启动”进行驻留统计，便于观察在当前启动时此前所有高亮主进程的存活情况。
    """
    starts = []
    for e in events:
        if e.get("type") != "start" or e.get("is_subprocess"):
            continue
        base = e.get("process_name", "").split(":")[0]
        if base not in HIGHLIGHT_PROCESSES:
            continue
        d = e.get("details", {}) or {}
        starts.append(
            {
                "base": base,
                "dt": e["time"],
                "round": d.get("round"),
                "start_kind": d.get("start_kind", "unknown"),
                "is_anomaly": _is_possible_anomaly_start_record(d),
                "anomaly_note": _startup_anomaly_note(d),
            }
        )

    kill_map = defaultdict(list)
    for e in events:
        if e.get("type") not in ("kill", "lmk") or e.get("is_subprocess"):
            continue
        base = e.get("process_name", "").split(":")[0]
        if base in HIGHLIGHT_PROCESSES:
            kill_map[base].append(e["time"])
    for v in kill_map.values():
        v.sort()

    def is_killed(base, start_dt, ref_dt):
        for t in kill_map.get(base, []):
            if start_dt < t < ref_dt:
                return True
        return False

    residency = []
    for idx, cur in enumerate(starts):
        start_kind = cur.get("start_kind", "unknown")
        if start_kind == "cold":
            start_kind_cn = "冷启动"
        elif start_kind == "hot":
            start_kind_cn = "热启动"
        else:
            start_kind_cn = "未知"

        if cur.get("is_anomaly"):
            residency.append({
                "seq": idx + 1,
                "process": cur["base"],
                "round": cur.get("round"),
                "start_kind": start_kind,
                "start_kind_cn": start_kind_cn,
                "is_anomaly": True,
                "anomaly_note": cur.get("anomaly_note", ""),
                "alive_cnt": 0,
                "alive_list": [],
                "killed_list": [],
                "window_total": 0,
                "window_rate_value": None,
                "all_alive_cnt": 0,
                "all_alive_list": [],
                "all_killed_list": [],
                "all_total": 0,
                "all_rate_value": None,
                "all_rate": "-",
                "per_window": {n: {"rate": "-", "rate_value": None, "alive": []} for n in range(1, window_size + 1)},
            })
            continue

        # 驻留统计排除“疑似异常启动”样本，只使用正常启动数据作为分母。
        prev_window_all = starts[max(0, idx - window_size):idx]
        prev_all = [ps for ps in prev_window_all if not ps.get("is_anomaly")]
        total = len(prev_all)
        alive_list = []
        killed_list = []
        for ps in prev_all:
            if is_killed(ps["base"], ps["dt"], cur["dt"]):
                killed_list.append(ps["base"])
            else:
                alive_list.append(ps["base"])
        alive_cnt = len(alive_list)
        rate = round(alive_cnt / total * 100, 1) if total else None

        prev_full = [ps for ps in starts[:idx] if not ps.get("is_anomaly")]
        total_full = len(prev_full)
        alive_full = []
        killed_full = []
        for ps in prev_full:
            if is_killed(ps["base"], ps["dt"], cur["dt"]):
                killed_full.append(ps["base"])
            else:
                alive_full.append(ps["base"])
        alive_cnt_full = len(alive_full)
        rate_full = round(alive_cnt_full / total_full * 100, 1) if total_full else None

        per_window = {}
        for n in range(1, window_size + 1):
            subset = prev_all[-n:] if len(prev_all) >= n else []
            if not subset:
                per_window[n] = {"rate": "-", "rate_value": None, "alive": []}
                continue
            sub_total = len(subset)
            sub_alive = []
            for ps in subset:
                if not is_killed(ps["base"], ps["dt"], cur["dt"]):
                    sub_alive.append(ps["base"])
            sub_rate = round(len(sub_alive) / sub_total * 100, 1)
            per_window[n] = {
                "rate": f"{len(sub_alive)}/{sub_total} ({sub_rate}%)",
                "rate_value": sub_rate,
                "alive": sub_alive,
            }

        residency.append({
            "seq": idx + 1,
            "process": cur["base"],
            "round": cur.get("round"),
            "start_kind": start_kind,
            "start_kind_cn": start_kind_cn,
            "is_anomaly": False,
            "anomaly_note": "",
            "alive_cnt": alive_cnt,
            "alive_list": alive_list,
            "killed_list": killed_list,
            "window_total": total,
            "window_rate_value": rate,
            "all_alive_cnt": alive_cnt_full,
            "all_alive_list": alive_full,
            "all_killed_list": killed_full,
            "all_total": total_full,
            "all_rate_value": rate_full,
            "all_rate": f"{alive_cnt_full}/{total_full}",
            "per_window": per_window,
        })
    return residency

def compute_highlight_runs(events):
    """
    构建高亮主进程前两轮启动/被杀及驻留时间表。
    返回列表，每个元素包含 proc/start1/kill1/start2/kill2/dur1/dur2/avg/second_cold。
    """
    runs = []
    events_sorted = sorted(events, key=lambda e: e['time'])
    for proc in HIGHLIGHT_PROCESSES:
        starts = []
        kills = []
        alive = False
        last_start = None
        for e in events_sorted:
            base = e.get('process_name', '').split(':')[0]
            if base != proc or e.get('is_subprocess'):
                continue
            if e.get('type') == 'start':
                if _is_possible_anomaly_start_record(e.get("details", {})):
                    continue
                starts.append(e)
                alive = True
                last_start = e['time']
            elif e.get('type') in ('kill', 'lmk'):
                if alive and last_start:
                    kills.append(e['time'])
                    alive = False
                    last_start = None
        if not starts:
            continue
        start1_evt = starts[0]
        start2_evt = starts[1] if len(starts) > 1 else None
        start1 = start1_evt['time']
        start2 = start2_evt['time'] if start2_evt else None
        kill1 = kills[0] if len(kills) > 0 else None
        kill2 = kills[1] if len(kills) > 1 else None
        dur1_s = (kill1 - start1).total_seconds() if (start1 and kill1) else None
        dur2_s = (kill2 - start2).total_seconds() if (start2 and kill2) else None
        avg_s = None
        durations = [d for d in (dur1_s, dur2_s) if d is not None]
        if durations:
            avg_s = sum(durations) / len(durations)
        if not start2_evt:
            second_cold = "无第二轮"
        else:
            start2_kind = (start2_evt.get('details') or {}).get('start_kind')
            if start2_kind == 'cold':
                second_cold = "是"
            elif start2_kind == 'hot':
                second_cold = "否"
            else:
                second_cold = "未知"
        runs.append({
            "proc": proc,
            "start1": _format_dt(start1),
            "kill1": _format_dt(kill1),
            "start2": _format_dt(start2),
            "kill2": _format_dt(kill2),
            "dur1": _format_duration(dur1_s),
            "dur2": _format_duration(dur2_s),
            "avg": _format_duration(avg_s),
            "second_cold": second_cold,
        })
    return runs


def build_startup_survival_heatmap(events, app_list: Optional[List[str]] = None):
    """
    构建“连续启动生存热力图”数据：
    - 纵轴：固定 app 列表（优先外部传入，否则 HIGHLIGHT_PROCESSES）
    - 横轴：固定两轮槽位（R1-01..R1-N, R2-01..R2-N），按日志中主进程启动出现顺序依次落槽
    - 值：0=未存活，1=存活，2=该槽位被启动（且存活）
    """
    events_sorted = sorted(events, key=lambda e: e["time"])

    # 无外部输入时，严格回退到高亮主进程列表
    source_apps = list(app_list) if app_list else list(HIGHLIGHT_PROCESSES)

    apps = []
    app_seen = set()
    for pkg in source_apps:
        base = _base_name(pkg)
        if not _looks_like_package(base):
            continue
        if base in app_seen:
            continue
        app_seen.add(base)
        apps.append(base)

    if not apps:
        return {
            "apps": [],
            "slots": [],
            "matrix": [],
            "row_stats": [],
            "col_stats": [],
            "total_slots": 0,
            "app_count": 0,
            "anomaly_start_count": 0,
        }

    app_set = set(apps)
    app_count = len(apps)
    total_slots = app_count * 2

    # 固定槽位：R1-01..R1-N, R2-01..R2-N
    slots = []
    for round_no in (1, 2):
        for idx, pkg in enumerate(apps, start=1):
            slots.append(
                {
                    "index": (round_no - 1) * app_count + idx,
                    "label": f"R{round_no}-{idx:02d}",
                    "round": round_no,
                    "round_pos": idx,
                    "expected_process": pkg,
                    "process": pkg,
                    "start_kind": "unknown",
                    "start_kind_cn": "未知",
                    "time": "-",
                    "alive_count": 0,
                    "dead_count": app_count,
                    "snapshot": [0] * app_count,
                    "has_event": False,
                }
            )

    expected_pkgs = [slot["expected_process"] for slot in slots]
    # 优先使用 _annotate_cont_startup 标注的固定槽位，保证“轮次定义”和“热力图槽位”一致。
    slot_event_idx: Dict[int, int] = {}
    has_sequence_slot = False
    anomaly_start_count = 0
    for idx, e in enumerate(events_sorted):
        if e.get("type") != "start" or e.get("is_subprocess"):
            continue
        if _is_possible_anomaly_start_record(e.get("details", {})):
            anomaly_start_count += 1
            continue
        base = _base_name(e.get("process_name", ""))
        if base not in app_set:
            continue
        details = e.get("details", {}) or {}
        seq_slot = details.get("sequence_slot")
        if isinstance(seq_slot, int) and 1 <= seq_slot <= total_slots:
            has_sequence_slot = True
            slot_pos = seq_slot - 1
            if slot_pos in slot_event_idx:
                continue
            if expected_pkgs[slot_pos] != base:
                continue
            slot_event_idx[slot_pos] = idx

    if not has_sequence_slot:
        # 兼容兜底：旧事件数据没有 sequence_slot 时，回退到原有贪心顺序匹配。
        observed_start_indices = []
        observed_start_pkgs = []
        for idx, e in enumerate(events_sorted):
            if e.get("type") != "start" or e.get("is_subprocess"):
                continue
            if _is_possible_anomaly_start_record(e.get("details", {})):
                continue
            base = _base_name(e.get("process_name", ""))
            if base not in app_set:
                continue
            observed_start_indices.append(idx)
            observed_start_pkgs.append(base)

        slot_event_idx = {}
        last_expected_pos = -1
        for obs_idx, obs_pkg in zip(observed_start_indices, observed_start_pkgs):
            match_pos = None
            for pos in range(last_expected_pos + 1, total_slots):
                if expected_pkgs[pos] == obs_pkg and pos not in slot_event_idx:
                    match_pos = pos
                    break
            if match_pos is None:
                continue
            slot_event_idx[match_pos] = obs_idx
            last_expected_pos = match_pos

    # 预计算：每个事件后的存活状态快照
    alive = {pkg: False for pkg in apps}
    alive_states_after = []
    for e in events_sorted:
        if not e.get("is_subprocess"):
            etype = e.get("type")
            if etype in ("start", "kill", "lmk"):
                base = _base_name(e.get("process_name", ""))
                if base in app_set:
                    if etype == "start":
                        if _is_possible_anomaly_start_record(e.get("details", {})):
                            alive_states_after.append(dict(alive))
                            continue
                        alive[base] = True
                    else:
                        alive[base] = False
        alive_states_after.append(dict(alive))

    # 严格按给定包名顺序填槽位：
    # - 命中预期启动日志 -> 正常写入
    # - 未命中 -> 标记异常槽位（淡红），其余进程状态沿用上一个槽位
    last_state = {pkg: False for pkg in apps}
    for slot_pos, slot in enumerate(slots):
        expected_pkg = slot.get("expected_process")
        start_idx = slot_event_idx.get(slot_pos)

        if start_idx is None:
            snapshot = [1 if last_state[pkg] else 0 for pkg in apps]
            alive_count = sum(1 for pkg in apps if last_state[pkg])
            slot.update(
                {
                    "process": expected_pkg,
                    "start_kind": "missing",
                    "start_kind_cn": "缺失",
                    "time": "-",
                    "alive_count": alive_count,
                    "dead_count": app_count - alive_count,
                    "snapshot": snapshot,
                    "has_event": False,
                    "matched_expected": False,
                    "is_missing_expected_start": True,
                }
            )
            continue

        event = events_sorted[start_idx]
        state_now = alive_states_after[start_idx] if 0 <= start_idx < len(alive_states_after) else dict(last_state)
        details = event.get("details", {}) or {}
        start_kind = details.get("start_kind", "unknown")
        if start_kind == "cold":
            start_kind_cn = "冷启动"
        elif start_kind == "hot":
            start_kind_cn = "热启动"
        else:
            start_kind_cn = "未知"

        snapshot = []
        alive_count = 0
        for pkg in apps:
            if state_now.get(pkg):
                snapshot.append(2 if pkg == expected_pkg else 1)
                alive_count += 1
            else:
                snapshot.append(0)

        slot.update(
            {
                "process": expected_pkg,
                "start_kind": start_kind,
                "start_kind_cn": start_kind_cn,
                "time": event["time"].strftime("%m-%d %H:%M:%S.%f")[:-3] if event.get("time") else "-",
                "alive_count": alive_count,
                "dead_count": app_count - alive_count,
                "snapshot": snapshot,
                "has_event": True,
                "matched_expected": True,
                "is_missing_expected_start": False,
            }
        )
        last_state = dict(state_now)

    label_stride = 1
    if total_slots >= 36:
        label_stride = 4
    elif total_slots > 24:
        label_stride = 3
    elif total_slots > 12:
        label_stride = 2

    for idx, slot in enumerate(slots, start=1):
        show_label = (
            idx == 1
            or idx == total_slots
            or (slot.get("round_pos") == 1)
            or (idx % label_stride == 0)
        )
        slot["display_label"] = slot["label"] if show_label else ""

    matrix = []
    row_stats = []
    for row_idx, pkg in enumerate(apps):
        row_vals = []
        alive_slots = 0
        launch_slots = 0
        for slot in slots:
            v = slot["snapshot"][row_idx]
            row_vals.append(v)
            if v > 0:
                alive_slots += 1
            if v == 2:
                launch_slots += 1
        matrix.append(row_vals)
        alive_rate = (alive_slots / total_slots * 100.0) if total_slots else 0.0
        row_stats.append(
            {
                "process": pkg,
                "alive_slots": alive_slots,
                "launch_slots": launch_slots,
                "total_slots": total_slots,
                "alive_rate": alive_rate,
            }
        )

    col_stats = []
    for slot in slots:
        col_stats.append(
            {
                "label": slot["label"],
                "alive_count": slot["alive_count"],
                "dead_count": slot["dead_count"],
            }
        )

    return {
        "apps": apps,
        "slots": slots,
        "matrix": matrix,
        "row_stats": row_stats,
        "col_stats": col_stats,
        "total_slots": total_slots,
        "app_count": app_count,
        "anomaly_start_count": anomaly_start_count,
    }


def generate_report_html(
    events,
    summary,
    output_file,
    heatmap_apps: Optional[List[str]] = None,
    device_info: Optional[dict] = None,
    meminfo_bundle: Optional[dict] = None,
    include_startup_section: bool = True,
):
    """生成仅包含 summary 的 HTML 报告（三板块：全部 / 主进程 / 高亮主进程）。"""
    s = _to_plain(summary)
    highlight_timeline = build_highlight_timeline(events)
    highlight_residency = build_highlight_residency(events)
    highlight_runs = compute_highlight_runs(events)
    startup_heatmap = build_startup_survival_heatmap(events, app_list=heatmap_apps)
    device_info_data = _to_plain(device_info or {})
    meminfo_data = _to_plain(meminfo_bundle or {})

    def _residency_avg(res_list):
        valid_res = [rec for rec in (res_list or []) if not rec.get("is_anomaly")]
        if not valid_res:
            return {
                "alive": 0,
                "rates": {n: 0 for n in range(1, 6)},
                "all_rate": 0,
                "all_alive": 0,
            }
        rates = {n: [] for n in range(1, 6)}
        all_rates = []
        alive_list = []
        all_alive_list = []
        for rec in valid_res:
            alive_list.append(rec.get("alive_cnt", 0))
            all_alive_list.append(rec.get("all_alive_cnt", 0))
            # 全量前序均值
            pct_full = rec.get("all_rate_value")
            if pct_full is not None:
                all_rates.append(pct_full)
            for n in range(1, 6):
                pct = rec["per_window"][n].get("rate_value")
                if pct is not None:
                    rates[n].append(pct)
        avg_alive = sum(alive_list) / len(alive_list) if alive_list else 0
        avg_all_alive = sum(all_alive_list) / len(all_alive_list) if all_alive_list else 0
        avg_rates = {n: (sum(v) / len(v) if v else 0) for n, v in rates.items()}
        avg_all = sum(all_rates) / len(all_rates) if all_rates else 0
        return {"alive": avg_alive, "rates": avg_rates, "all_rate": avg_all, "all_alive": avg_all_alive}

    residency_avg = _residency_avg(highlight_residency)
    html_escape = html.escape

    def _device_info_value(key: str) -> str:
        value = str(device_info_data.get(key, "") or "").strip()
        return value if value else "-"

    device_rows = [
        ("Build fingerprint", _device_info_value("build_fingerprint")),
        ("ro.product.device", _device_info_value("ro_product_device")),
        ("ro.board.platform", _device_info_value("ro_board_platform")),
        ("/proc/meminfo MemTotal", _device_info_value("mem_total")),
        ("/proc/meminfo SwapTotal", _device_info_value("swap_total")),
        ("Linux version", _device_info_value("linux_version")),
    ]
    proc_mv_text = str(device_info_data.get("proc_mv", "") or "").strip()
    if not proc_mv_text:
        proc_mv_text = "未匹配到 /proc/mv 内容"
    proc_mv_block_html = html_escape(proc_mv_text)
    auto_match_info = _to_plain(device_info_data.get("auto_match_info", {}) or {})
    if auto_match_info:
        def _bool_cn(v) -> str:
            return "是" if bool(v) else "否"

        status_text = str(auto_match_info.get("status", "") or "").strip() or "-"
        device_rows.append(("自动匹配状态", status_text))
        auto_lines = [
            f"自动匹配启用: {_bool_cn(auto_match_info.get('enabled'))}",
            f"目标应用数: {auto_match_info.get('target_app_count', '-')}",
            f"目标轮次: {auto_match_info.get('rounds', '-')}",
            f"识别到候选窗口: {_bool_cn(auto_match_info.get('detected'))}",
            f"采用候选窗口: {_bool_cn(auto_match_info.get('used'))}",
            f"状态: {status_text}",
        ]
        if auto_match_info.get("window_start") or auto_match_info.get("window_end"):
            auto_lines.append(
                f"候选时间段: {auto_match_info.get('window_start', '-')}"
                f" ~ {auto_match_info.get('window_end', '-')}"
            )
        if auto_match_info.get("match_score") is not None:
            auto_lines.append(
                f"匹配度: {auto_match_info.get('match_score')}% "
                f"(LCS {auto_match_info.get('matched_start_count', '-')}/"
                f"{auto_match_info.get('expected_count', '-')})"
            )
        if auto_match_info.get("mismatch_count") is not None:
            auto_lines.append(
                f"误差/容差: {auto_match_info.get('mismatch_count', '-')}/"
                f"{auto_match_info.get('tolerance', '-')}"
            )
        if auto_match_info.get("observed_count") is not None:
            auto_lines.append(
                f"数量校验: 预期 {auto_match_info.get('expected_count', '-')}"
                f", 实际窗口 {auto_match_info.get('observed_count', '-')}"
            )
        if auto_match_info.get("match_variant"):
            auto_lines.append(f"匹配策略: {auto_match_info.get('match_variant')}")
        if auto_match_info.get("duration_sec") is not None:
            auto_lines.append(f"过程时长: {float(auto_match_info.get('duration_sec')):.1f}s")
        if auto_match_info.get("tail_gap_sec") is not None:
            auto_lines.append(f"距日志末尾: {float(auto_match_info.get('tail_gap_sec')):.1f}s")
        if auto_match_info.get("confidence"):
            auto_lines.append(f"置信度: {auto_match_info.get('confidence')}")
        if auto_match_info.get("file_end_time"):
            auto_lines.append(f"日志最晚时间: {auto_match_info.get('file_end_time')}")
        if auto_match_info.get("bugreport_time_hint"):
            auto_lines.append(f"bugreport文件时间: {auto_match_info.get('bugreport_time_hint')}")
        if auto_match_info.get("bugreport_to_log_end_gap_sec") is not None:
            auto_lines.append(
                f"bugreport与日志最晚时间差: {float(auto_match_info.get('bugreport_to_log_end_gap_sec')):.1f}s"
            )
        if auto_match_info.get("applied_start_time") or auto_match_info.get("applied_end_time"):
            auto_lines.append(
                f"最终应用过滤时间段: {auto_match_info.get('applied_start_time', '-')}"
                f" ~ {auto_match_info.get('applied_end_time', '-')}"
            )
        if auto_match_info.get("detection_error"):
            auto_lines.append(f"自动匹配异常: {auto_match_info.get('detection_error')}")
    else:
        auto_lines = ["未提供自动匹配信息"]
    auto_match_block_html = html_escape("\n".join(str(line) for line in auto_lines if str(line).strip()))
    device_info_rows_html = "".join(
        f"<tr><th>{html_escape(label)}</th><td>{html_escape(value)}</td></tr>"
        for label, value in device_rows
    )
    # 预构建高亮驻留表 HTML
    if highlight_runs:
        hl_runs_rows = []
        for r in highlight_runs:
            hl_runs_rows.append(
                f"<tr><td>{html_escape(r['proc'])}</td>"
                f"<td>{r['second_cold']}</td>"
                f"<td>{r['start1']}</td>"
                f"<td>{r['kill1']}</td>"
                f"<td>{r['dur1']}</td>"
                f"<td>{r['start2']}</td>"
                f"<td>{r['kill2']}</td>"
                f"<td>{r['dur2']}</td>"
                f"<td>{r['avg']}</td></tr>"
            )
        hl_runs_table_html = "".join(hl_runs_rows)
    else:
        hl_runs_table_html = '<tr><td colspan="9" style="text-align:center;color:#9fb3c8;">无数据</td></tr>'


    def card(label, value):
        return f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div></div>'

    def card_row(label, value):
        return f'<div class="card-row"><span class="row-label">{label}</span><span class="row-value">{value}</span></div>'

    def overview_block(title, data):
        rows = "".join([
            card_row("事件总数", data.get('total', 0)),
            card_row("启动", data.get('start', 0)),
            card_row("上层/一体化查杀", data.get('kill', 0)),
            card_row("底层/LMKD查杀", data.get('lmk', 0)),
            card_row("触发", data.get('trig', 0)),
            card_row("跳过", data.get('skip', 0)),
            card_row("释放内存(KB)", f"{data.get('mem', 0):,}"),
        ])
        return f'<div class="card card-wide"><div class="card-title">{title}</div>{rows}</div>'

    overall_cards = overview_block("全部", {
        'total': s['total_events'],
        'start': s['start_count'],
        'kill': s['kill_count'],
        'lmk': s['lmk_count'],
        'trig': s['trig_count'],
        'skip': s['skip_count'],
        'mem': s['total_release_mem'],
    })

    def fmt_num(value):
        try:
            return f"{int(round(float(value))):,}"
        except (TypeError, ValueError):
            return "-"

    def fmt_kb(value):
        parsed = _parse_kb_value(value)
        if parsed is None:
            return "-"
        return f"{parsed:,}K ({parsed / 1024:.1f} MB)"

    meminfo_total_proc = meminfo_data.get("total_proc", {}) or {}
    meminfo_top20_ratio = float(meminfo_data.get("top20_ratio", 0.0) or 0.0)
    meminfo_oom_rows = meminfo_data.get("oom_by_priority", []) or []
    meminfo_priority_groups = meminfo_data.get("priority_groups", {}) or {}

    meminfo_summary_cards = [
        ("进程总数", fmt_num(meminfo_total_proc.get("count", 0))),
        ("总 PSS", fmt_kb(meminfo_total_proc.get("total_pss_kb"))),
        ("Top20 占比", f"{meminfo_top20_ratio * 100:.1f}%"),
    ]
    meminfo_summary_cards_html = "".join(
        (
            '<div class="summary-item">'
            f'<div class="summary-label">{html_escape(label)}</div>'
            f'<div class="summary-value">{html_escape(value)}</div>'
            '</div>'
        )
        for label, value in meminfo_summary_cards
    )

    meminfo_top_processes = (meminfo_total_proc.get("processes", []) or [])[:20]
    if meminfo_top_processes:
        meminfo_top_process_rows_html = "".join(
            (
                "<tr>"
                f"<td>{idx}</td>"
                f"<td>{html_escape(str(proc.get('name', '')))}</td>"
                f"<td>{fmt_num(proc.get('pss_kb', 0))}</td>"
                f"<td>{fmt_num(proc.get('swap_kb', 0)) if proc.get('swap_kb') is not None else '-'}</td>"
                "</tr>"
            )
            for idx, proc in enumerate(meminfo_top_processes, start=1)
        )
    else:
        meminfo_top_process_rows_html = "<tr><td colspan='4' class='summary-empty'>无数据</td></tr>"

    meminfo_priority_rows = []
    for label, value in meminfo_priority_groups.items():
        meminfo_priority_rows.append((str(label), int((value or {}).get("count", 0) or 0), int((value or {}).get("pss_kb", 0) or 0)))
    meminfo_priority_rows.sort(key=lambda x: x[2], reverse=True)
    if meminfo_priority_rows:
        meminfo_priority_rows_html = "".join(
            (
                "<tr>"
                f"<td>{html_escape(label)}</td>"
                f"<td>{fmt_num(cnt)}</td>"
                f"<td>{fmt_num(pss)}</td>"
                "</tr>"
            )
            for label, cnt, pss in meminfo_priority_rows
        )
    else:
        meminfo_priority_rows_html = "<tr><td colspan='3' class='summary-empty'>无数据</td></tr>"

    if meminfo_oom_rows:
        meminfo_oom_rows_html = "".join(
            (
                "<tr>"
                f"<td>{html_escape(str(row.get('priority_label', '')))}</td>"
                f"<td>{html_escape(str(row.get('name', '')))}</td>"
                f"<td>{fmt_num(row.get('process_count', 0))}</td>"
                f"<td>{fmt_num(row.get('total_pss_kb', 0))}</td>"
                f"<td>{fmt_num(row.get('swap_kb', 0)) if row.get('swap_kb') is not None else '-'}</td>"
                "</tr>"
            )
            for row in meminfo_oom_rows
        )
    else:
        meminfo_oom_rows_html = "<tr><td colspan='5' class='summary-empty'>无数据</td></tr>"

    priority_alias_desc = {
        "必要": "NATIVE / SYSTEM / PERSISTENT",
        "高优先级": "FG(foreground), VIS(visible), PER(perceptible), HOME, PREV(previous), HW(heavy weight)",
        "低优先级": "PER-LOW, CACHED, B-SVC(b services), BACKUP, EMPTY",
        "其它": "未命中上述规则",
    }
    categories_by_group = defaultdict(list)
    for row in meminfo_oom_rows:
        group = str(row.get("priority_label", "其它"))
        name = str(row.get("name", "")).strip()
        if name and name not in categories_by_group[group]:
            categories_by_group[group].append(name)
    priority_mapping_rows = []
    for label in ("必要", "高优先级", "低优先级", "其它"):
        mapped = "、".join(categories_by_group.get(label, [])) if categories_by_group.get(label) else "-"
        priority_mapping_rows.append((label, priority_alias_desc.get(label, "-"), mapped))
    priority_mapping_rows_html = "".join(
        (
            "<tr>"
            f"<td>{html_escape(label)}</td>"
            f"<td>{html_escape(alias_desc)}</td>"
            f"<td>{html_escape(mapped)}</td>"
            "</tr>"
        )
        for label, alias_desc, mapped in priority_mapping_rows
    )

    meminfo_source_desc = str(meminfo_data.get("source_desc", "") or "").strip()
    meminfo_error = str(meminfo_data.get("error", "") or "").strip()
    meminfo_error_html = (
        f"<div class='mem-low-note' style='color:#ff8b8b;'>解析失败: {html_escape(meminfo_error)}</div>"
        if meminfo_error
        else ""
    )

    kill_total = s.get("kill_count", 0) + s.get("lmk_count", 0)
    main_kill_total = s.get("main_overall", {}).get("kill", 0) + s.get("main_overall", {}).get("lmk", 0)
    hl_main_kill_total = s.get("highlight_overall", {}).get("main_kill", 0) + s.get("highlight_overall", {}).get("main_lmk", 0)

    main_start_count = 0
    hl_main_start_count = 0
    second_round_hot_count = int((s.get("cont_startup_stats") or {}).get("second_round_hot", 0) or 0)
    for e in events:
        if e.get("type") != "start" or e.get("is_subprocess"):
            continue
        main_start_count += 1
        if e.get("process_name", "").split(":")[0] in HIGHLIGHT_PROCESSES:
            hl_main_start_count += 1

    def _memfree_row(label, key):
        st = s.get("mem_stats", {}).get(key, {}).get("mem_free", {})
        if not st or st.get("count", 0) == 0:
            return (
                f"<tr><td>{label}</td>"
                "<td class='summary-empty'>-</td>"
                "<td class='summary-empty'>-</td>"
                "<td class='summary-empty'>-</td></tr>"
            )
        return (
            f"<tr><td>{label}</td>"
            f"<td>{fmt_num(st.get('avg'))}</td>"
            f"<td>{fmt_num(st.get('median'))}</td>"
            f"<td>{fmt_num(st.get('min'))}</td></tr>"
        )

    summary_mem_rows_html = "".join([
        _memfree_row("全部进程", "all"),
        _memfree_row("主进程", "main"),
        _memfree_row("高亮主进程(主)", "highlight_main"),
    ])

    def _lmk_reason_stats(scope_key):
        stats = defaultdict(int)
        for e in events:
            if e.get("type") != "lmk":
                continue
            if scope_key in ("main", "highlight_main") and e.get("is_subprocess"):
                continue
            if scope_key == "highlight_main":
                base = e.get("process_name", "").split(":")[0]
                if base not in HIGHLIGHT_PROCESSES:
                    continue
            reason = (e.get("details") or {}).get("reason") or "未知"
            stats[reason] += 1
        return dict(stats)

    main_lmk_reason_stats = _lmk_reason_stats("main")
    hl_lmk_reason_stats = _lmk_reason_stats("highlight_main")

    def _min_score_chart_label(raw_label):
        txt = str(raw_label or "").strip()
        up = txt.upper()
        if "NORMAL" in up:
            return "NORMAL"
        if "IMPORTANT" in up:
            return "IMPORTANT"
        if "RECENT" in up:
            return "RECENT"

        token = ""
        m = re.search(r"\(([^()]*)\)", txt)
        if m and m.group(1).strip():
            token = m.group(1).strip()
        elif txt:
            token = txt
        else:
            token = "OTHER"
        token = token.upper()
        if len(token) > 24:
            token = token[:24].rstrip("_- ")
        return f"MIN({token})"

    def _compact_min_score_stats(stats_map):
        merged = defaultdict(int)
        for k, v in (stats_map or {}).items():
            merged[_min_score_chart_label(k)] += int(v or 0)

        ordered = {}
        for key in ("NORMAL", "IMPORTANT", "RECENT"):
            if merged.get(key, 0) > 0:
                ordered[key] = merged[key]
        for k, v in sorted(merged.items(), key=lambda kv: kv[1], reverse=True):
            if k in ordered:
                continue
            ordered[k] = v
        return ordered

    min_score_chart_stats = _compact_min_score_stats(s.get("min_score_stats", {}))
    main_min_score_chart_stats = _compact_min_score_stats(
        s.get("main_overall", {}).get("min_score_stats", {})
    )
    hl_min_score_chart_stats = _compact_min_score_stats(
        s.get("highlight_overall", {}).get("main_min_score_stats", {})
    )

    def kill_scope_card(title, total_cnt, kill_cnt, lmk_cnt):
        return (
            '<div class="kill-scope-card">'
            f'<div class="kill-scope-title">{title}</div>'
            f'<div class="kill-scope-row"><span>事件总数</span><strong>{fmt_num(total_cnt)}</strong></div>'
            f'<div class="kill-scope-row"><span>上层/一体化查杀</span><strong>{fmt_num(kill_cnt)}</strong></div>'
            f'<div class="kill-scope-row"><span>底层/LMKD查杀</span><strong>{fmt_num(lmk_cnt)}</strong></div>'
            '</div>'
        )

    kill_scope_all_html = kill_scope_card("全部", kill_total, s.get("kill_count", 0), s.get("lmk_count", 0))
    kill_scope_main_html = kill_scope_card(
        "主进程",
        main_kill_total,
        s.get("main_overall", {}).get("kill", 0),
        s.get("main_overall", {}).get("lmk", 0),
    )
    kill_scope_hl_html = kill_scope_card(
        "高亮主进程",
        hl_main_kill_total,
        s.get("highlight_overall", {}).get("main_kill", 0),
        s.get("highlight_overall", {}).get("main_lmk", 0),
    )

    hl_killtype_map = s.get("highlight_overall", {}).get("main_kill_type_stats", {}) or {}
    hl_adj_map = defaultdict(int)
    for k, v in (s.get("highlight_overall", {}).get("main_adj_stats", {}) or {}).items():
        hl_adj_map[k] += v
    for k, v in (s.get("highlight_overall", {}).get("main_lmk_adj_stats", {}) or {}).items():
        hl_adj_map[k] += v

    hl_proc_map = {}
    for proc, d in (s.get("highlight_proc_detail", {}) or {}).items():
        cnt = (d.get("main_kill", 0) or 0) + (d.get("main_lmk", 0) or 0)
        if cnt > 0:
            hl_proc_map[proc] = cnt

    def _build_option_html(stats_map):
        options = ['<option value="">全部</option>']
        for k, v in sorted((stats_map or {}).items(), key=lambda kv: kv[1], reverse=True):
            key_txt = str(k)
            options.append(f'<option value="{html_escape(key_txt)}">{html_escape(key_txt)} ({fmt_num(v)})</option>')
        return "".join(options)

    def _build_adj_option_html(stats_map):
        options = ['<option value="">全部</option>']

        def _adj_sort_key(item):
            key_txt = str(item[0]).strip()
            try:
                return (0, int(key_txt), key_txt)
            except ValueError:
                return (1, 0, key_txt)

        for k, v in sorted((stats_map or {}).items(), key=_adj_sort_key):
            key_txt = str(k)
            options.append(f'<option value="{html_escape(key_txt)}">{html_escape(key_txt)} ({fmt_num(v)})</option>')
        return "".join(options)

    hl_filter_killtype_options_html = _build_option_html(hl_killtype_map)
    hl_filter_adj_options_html = _build_adj_option_html(hl_adj_map)
    hl_filter_proc_options_html = _build_option_html(hl_proc_map)

    def _pad_col(txt, width):
        s_txt = str(txt or "-")
        if len(s_txt) > width:
            if width <= 3:
                return s_txt[:width]
            return s_txt[:width - 3] + "..."
        return s_txt.ljust(width)

    def mem_card_row(label, sample_count, value_html):
        return (
            '<section class="mem-block">'
            '<div class="mem-block-head">'
            f'<h4 class="mem-block-title">{label}</h4>'
            f'<div class="mem-block-sub">样本数 {fmt_num(sample_count)}</div>'
            '</div>'
            f'<div class="mem-block-body">{value_html}</div>'
            '</section>'
        )

    def mem_avg_card():
        def table_for(key):
            stats_map = s.get("mem_stats", {}).get(key, {})
            avg_map = s.get("mem_avg", {}).get(key, {})
            sample_count = avg_map.get("count", 0)
            if not stats_map or not avg_map or sample_count == 0:
                return sample_count, "<div class='mem-empty'>无数据</div>"
            def row(metric_label, metric_key):
                st = stats_map.get(metric_key, {})
                if not st or st.get("count", 0) == 0:
                    return (
                        "<tr>"
                        f"<td class='mem-metric'>{metric_label}</td>"
                        "<td class='mem-empty-cell' colspan='5'>无数据</td>"
                        "</tr>"
                    )
                return (
                    f"<tr>"
                    f"<td class='mem-metric'>{metric_label}</td>"
                    f"<td>{fmt_num(st.get('avg'))}</td>"
                    f"<td>{fmt_num(st.get('median'))}</td>"
                    f"<td>{fmt_num(st.get('p95'))}</td>"
                    f"<td>{fmt_num(st.get('min'))}</td>"
                    f"<td>{fmt_num(st.get('max'))}</td>"
                    f"</tr>"
                )
            return (
                sample_count,
                "<div class='mem-metric-wrap'>"
                "<div class='mem-table-wrap'>"
                "<table class='mem-table'>"
                "<thead><tr><th>指标</th><th>Avg</th><th>P50</th><th>P95</th><th>Min</th><th>Max</th></tr></thead>"
                "<tbody>"
                f"{row('memfree', 'mem_free')}"
                f"{row('file', 'file_pages')}"
                f"{row('anon', 'anon_pages')}"
                f"{row('swapfree', 'swap_free')}"
                "</tbody></table>"
                "</div>"
                "</div>"
            )
        all_sample, all_html = table_for('all')
        main_sample, main_html = table_for('main')
        hl_sample, hl_html = table_for('highlight_main')
        trig_sample, trig_html = table_for('trig')
        return (
            '<div class="card card-wide">'
            '<div class="card-title">内存统计 (KB)</div>'
            '<div class="mem-block-grid">'
            f"{mem_card_row('全部进程', all_sample, all_html)}"
            f"{mem_card_row('主进程', main_sample, main_html)}"
            f"{mem_card_row('高亮主进程(主)', hl_sample, hl_html)}"
            f"{mem_card_row('触发事件', trig_sample, trig_html)}"
            '</div>'
            "</div>"
        )
    mem_avg_card_html = mem_avg_card()

    def _build_dist_curve(values, max_points=80):
        vals = []
        for v in values or []:
            iv = _safe_int(v)
            if iv is not None:
                vals.append(iv)
        vals.sort()
        n = len(vals)
        if n == 0:
            return {"labels": [], "values": [], "count": 0}
        if n == 1:
            return {"labels": ["100%"], "values": [vals[0]], "count": 1}

        points = min(max_points, n)
        labels = []
        sampled = []
        for i in range(points):
            idx = round(i * (n - 1) / (points - 1))
            pct = int(round((idx + 1) * 100 / n))
            labels.append(f"{pct}%")
            sampled.append(vals[idx])
        return {"labels": labels, "values": sampled, "count": n}

    hl_mem_samples = (s.get("mem_samples", {}) or {}).get("highlight_main", {}) or {}
    hl_mem_dist = {
        "mem_free": _build_dist_curve(hl_mem_samples.get("mem_free", [])),
        "file_pages": _build_dist_curve(hl_mem_samples.get("file_pages", [])),
        "anon_pages": _build_dist_curve(hl_mem_samples.get("anon_pages", [])),
        "swap_free": _build_dist_curve(hl_mem_samples.get("swap_free", [])),
    }

    def _collect_hl_mem_low_events():
        rows = []
        for idx, e in enumerate(events):
            if e.get("type") not in ("kill", "lmk"):
                continue
            if e.get("is_subprocess"):
                continue
            base = e.get("process_name", "").split(":")[0]
            if base not in HIGHLIGHT_PROCESSES:
                continue
            metrics = _extract_mem_metrics(e)
            if not metrics:
                continue
            time_txt = e.get("time").strftime("%m-%d %H:%M:%S.%f")[:-3] if e.get("time") else "-"
            rows.append({
                "event_id": idx + 1,
                "type": e.get("type", ""),
                "type_label": "KILL" if e.get("type") == "kill" else "LMKD",
                "process": base,
                "time": time_txt,
                "mem_free": metrics.get("mem_free"),
                "file_pages": metrics.get("file_pages"),
                "anon_pages": metrics.get("anon_pages"),
                "swap_free": metrics.get("swap_free"),
                "detail": format_event_detail(e, idx),
            })
        return rows

    hl_mem_low_events = _collect_hl_mem_low_events()

    # 高亮主进程明细 HTML 预构建，避免 f-string 中复杂表达式报错
    def build_hl_detail():
        items = []
        for idx, e in enumerate(events):
            if e.get("type") not in ("kill", "lmk"):
                continue
            if e.get("is_subprocess"):
                continue
            base = e.get("process_name", "").split(":")[0]
            if base not in HIGHLIGHT_PROCESSES:
                continue

            etype = e.get("type")
            if etype == "kill":
                adj_val = e.get("details", {}).get("proc_info", {}).get("adj") or "未知"
                killtype_val = (
                    e.get("details", {}).get("kill_info", {}).get("killTypeDesc")
                    or e.get("details", {}).get("kill_info", {}).get("killType")
                    or "未知"
                )
            else:
                adj_val = e.get("details", {}).get("adj") or "未知"
                killtype_val = "LMK"

            adj_txt = str(adj_val)
            time_txt = e.get("time").strftime("%m-%d %H:%M:%S.%f")[:-3] if e.get("time") else "-"
            detail_txt = format_event_detail(e, idx)
            etype_txt = "KILL" if etype == "kill" else "LMKD"
            summary_txt = (
                f"{_pad_col(f'EVENT {idx+1}', 10)}  "
                f"{_pad_col(f'TYPE {etype_txt}', 10)}  "
                f"{_pad_col(f'PKG {base}', 42)}  "
                f"{_pad_col(f'ADJ {adj_txt}', 12)}  "
                f"{_pad_col(f'KILLTYPE {killtype_val}', 24)}  "
                f"{_pad_col(time_txt, 18)}"
            )

            items.append(
                f'<details class="hl-event-item" '
                f'data-proc="{html_escape(base)}" '
                f'data-adj="{html_escape(adj_txt)}" '
                f'data-killtype="{html_escape(str(killtype_val))}" '
                f'data-etype="{html_escape(str(etype))}">'
                f'<summary><span class="hl-event-summary-line">{html_escape(summary_txt)}</span></summary>'
                f'<pre>{html_escape(detail_txt)}</pre>'
                f'</details>'
            )

        if not items:
            return '<div class="kill-index-empty">暂无匹配事件</div>'
        return "".join(items)

    hl_detail_html = build_hl_detail()
    # 高亮时间线 HTML
    def build_hl_timeline():
        if not highlight_timeline:
            return '<div style="color:#9fb3c8;">暂无数据</div>'
        rows = []
        for item in highlight_timeline:
            left = ""
            right = ""
            row_class = "timeline-row"
            if item.get("label_class") == "start_cold":
                left = f'<span class="pill pill-start-cold">冷启动</span>&nbsp;{html_escape(item["process"])}'
            elif item.get("label_class") == "start_hot":
                left = f'<span class="pill pill-start-hot">热启动</span>&nbsp;{html_escape(item["process"])}'
            elif item.get("label_class") == "start_anomaly":
                row_class += " anomaly-row"
                note = str(item.get("note") or POSSIBLE_ANOMALY_START_NOTE)
                left = (
                    f'<span class="pill pill-start-anomaly">{POSSIBLE_ANOMALY_START_LABEL}</span>&nbsp;'
                    f'{html_escape(item["process"])}'
                    f'<span class="tl-anomaly-note">&nbsp;({html_escape(note)})</span>'
                )
            elif item.get("label_class") == "start":
                left = f'<span class="pill pill-start">启动</span>&nbsp;{html_escape(item["process"])}'
            elif item["label"] == "查杀":
                right = f'{html_escape(item["process"])}&nbsp;<span class="pill pill-kill">上层/一体化</span>'
            else:
                right = f'{html_escape(item["process"])}&nbsp;<span class="pill pill-lmk">底层/LMKD</span>'
            rows.append(
                f'<div class="{row_class}">'
                f'<div class="tl-time">{html_escape(item["time"])}</div>'
                f'<div class="tl-content"><span class="tl-left">{left}</span><span class="tl-right">{right}</span></div>'
                f'</div>'
            )
        legend = (
            '<div class="tl-legend">'
            '<span class="pill pill-start-cold">冷启动</span>'
            '<span class="pill pill-start-hot">热启动</span>'
            '<span class="pill pill-start-anomaly">可能为异常启动</span>'
            '<span class="pill pill-kill">上层/一体化</span>'
            '<span class="pill pill-lmk">底层/LMKD</span>'
            '</div>'
        )
        return legend + "".join(rows)

    hl_timeline_html = build_hl_timeline()
    # 高亮驻留率表 HTML
    def build_hl_residency():
        if not highlight_residency:
            return '<div style="color:#9fb3c8;">暂无数据</div>'
        rows = []
        anomaly_count = 0
        for rec in highlight_residency:
            round_txt = rec.get("round") if rec.get("round") is not None else "-"
            start_kind_txt = rec.get("start_kind_cn", "未知")
            start_kind_class = ""
            is_anomaly = bool(rec.get("is_anomaly"))
            if "冷" in str(start_kind_txt):
                start_kind_class = "hl-start-cold"
            elif "热" in str(start_kind_txt):
                start_kind_class = "hl-start-hot"
            if is_anomaly:
                start_kind_class = "hl-start-anomaly"
                anomaly_count += 1

            process_txt = str(rec.get("process", ""))
            process_class = ""
            if process_txt.strip() in {"com.tencent.mm", "com.tencent.mobileqq"}:
                process_class = "hl-proc-em"
            if is_anomaly:
                process_class = (process_class + " " if process_class else "") + "hl-anomaly-cell"
                note_txt = str(rec.get("anomaly_note", "") or POSSIBLE_ANOMALY_START_NOTE)
                start_kind_display = f"{start_kind_txt}（{POSSIBLE_ANOMALY_START_LABEL}）"
                row_cells = [
                    f"<td>{round_txt}</td>",
                    f"<td class='{start_kind_class}'>{html_escape(start_kind_display)}</td>",
                    f"<td>{rec['seq']}</td>",
                    (
                        f"<td class='{process_class}'>{html_escape(process_txt)}"
                        f"<div class='hl-anomaly-note'>{html_escape(note_txt)}</div></td>"
                    ),
                    f"<td class='hl-anomaly-cell' title='{html_escape(note_txt)}'>-</td>",
                    f"<td class='hl-anomaly-cell' title='{html_escape(note_txt)}'>-</td>",
                ]
                for _ in range(1, 6):
                    row_cells.append(f"<td class='hl-anomaly-cell' title='{html_escape(note_txt)}'>-</td>")
                rows.append("<tr class='hl-anomaly-row'>" + "".join(row_cells) + "</tr>")
                continue

            alive_list = rec.get("alive_list", [])
            alive_txt = "无" if not alive_list else ", ".join(alive_list)
            all_alive_list = rec.get("all_alive_list", [])
            all_alive_txt = "无" if not all_alive_list else ", ".join(all_alive_list)
            start_rate_val = rec.get("window_rate_value")
            start_rate_class = ""
            if start_rate_val is not None:
                start_rate_class = "rate-ok" if start_rate_val >= 100.0 else "rate-bad"
            all_rate_val = rec.get("all_rate_value")
            all_rate_class = ""
            if all_rate_val is not None:
                all_rate_class = "rate-ok" if all_rate_val >= 100.0 else "rate-bad"
            row_cells = [
                f"<td>{round_txt}</td>",
                f"<td class='{start_kind_class}'>{html_escape(str(start_kind_txt))}</td>" if start_kind_class else f"<td>{html_escape(str(start_kind_txt))}</td>",
                f"<td>{rec['seq']}</td>",
                f"<td class='{process_class}'>{html_escape(process_txt)}</td>" if process_class else f"<td>{html_escape(process_txt)}</td>",
                f"<td class='{start_rate_class}' title='{html_escape(alive_txt)}'>{rec['alive_cnt']}/{rec['window_total']}</td>",
                f"<td class='{all_rate_class}' title='{html_escape(all_alive_txt)}'>{rec['all_rate']}</td>",
            ]
            for n in range(1, 6):
                cell = rec['per_window'][n]
                if cell["rate"] == "-":
                    row_cells.append("<td>-</td>")
                else:
                    alive = cell["alive"]
                    alive_tip = "无" if not alive else ", ".join(alive)
                    rate_val = cell.get("rate_value")
                    color_class = "rate-ok" if (rate_val is not None and rate_val >= 100.0) else "rate-bad"
                    row_cells.append(
                        f"<td class='{color_class}' title='{html_escape(alive_tip)}'>{cell['rate']}</td>"
                    )
            rows.append("<tr>" + "".join(row_cells) + "</tr>")

        # 计算平均驻留率与平均存活数（排除疑似异常启动）
        avg_cells = []
        avg_alive_value = "均存活数 0.00"
        avg_all_alive_value = "全量均存活 0.00"
        avg_all_rate_class = ""
        valid_records = [rec for rec in highlight_residency if not rec.get("is_anomaly")]
        if valid_records:
            cols = {n: [] for n in range(1, 6)}
            alive_counts = []
            all_alive_counts = []
            for rec in valid_records:
                alive_counts.append(rec["alive_cnt"])
                all_alive_counts.append(rec.get("all_alive_cnt", 0))
                for n in range(1, 6):
                    pct = rec['per_window'][n].get("rate_value")
                    if pct is not None:
                        cols[n].append(pct)
            for n in range(1, 6):
                if cols[n]:
                    avg_pct = sum(cols[n]) / len(cols[n])
                    color_class = "rate-ok" if avg_pct >= 100.0 else "rate-bad"
                    avg_cells.append((f"均值 {avg_pct:.1f}%", color_class))
                else:
                    avg_cells.append(("-", ""))
            avg_alive = sum(alive_counts) / len(alive_counts) if alive_counts else 0
            avg_alive_value = f"均存活数 {avg_alive:.2f}"
            avg_all_alive = sum(all_alive_counts) / len(all_alive_counts) if all_alive_counts else 0
            avg_all_alive_value = f"全量均存活 {avg_all_alive:.2f}"
        else:
            avg_cells = [("-", "")] * 5
            avg_alive_value = "-"
            avg_all_alive_value = "-"
            avg_all_rate_class = ""

        foot_cells = [
            "<td></td>",
            "<td></td>",
            "<td></td>",
            "<td></td>",
            f"<td>{avg_alive_value}</td>",
            f"<td class='{avg_all_rate_class}'>{avg_all_alive_value}</td>",
        ]
        for val, cls in avg_cells:
            if val == "-":
                foot_cells.append("<td>-</td>")
            else:
                foot_cells.append(f"<td class='{cls}'>{val}</td>")

        anomaly_meta = ""
        if anomaly_count > 0:
            anomaly_meta = (
                f"&nbsp;&nbsp;|&nbsp;&nbsp;{POSSIBLE_ANOMALY_START_LABEL}: "
                f"<strong>{fmt_num(anomaly_count)}</strong>（已置灰且不纳入均值/热力图）"
            )
        table = (
            f'<div class="hl-residency-meta">第二轮热启动数量: <strong>{fmt_num(second_round_hot_count)}</strong>{anomaly_meta}</div>'
            '<div style="overflow-x:auto;">'
            '<table class="hl-run-table">'
            '<thead><tr><th>轮次</th><th>启动类型</th><th>序号</th><th>应用</th><th>启动前存活(前5)</th><th>全部</th>'
            '<th>前1</th><th>前2</th><th>前3</th><th>前4</th><th>前5</th></tr></thead>'
            '<tbody>'
            + "".join(rows) +
            '</tbody>'
            '<tfoot><tr>' + "".join(foot_cells) + '</tr></tfoot>'
            '</table></div>'
        )
        return table

    def build_startup_heatmap_html():
        apps = startup_heatmap.get("apps", []) or []
        slots = startup_heatmap.get("slots", []) or []
        matrix = startup_heatmap.get("matrix", []) or []
        row_stats = startup_heatmap.get("row_stats", []) or []
        col_stats = startup_heatmap.get("col_stats", []) or []
        if not apps or not slots or not matrix:
            return '<div class="startup-heatmap-empty">暂无可用于热力图的启动数据</div>'

        configured_cnt = int(startup_heatmap.get("app_count", len(apps)) or len(apps))
        expected_slots = configured_cnt * 2 if configured_cnt else 0
        expected_text = f"预期两轮槽位: {expected_slots}" if expected_slots else "未配置固定两轮槽位"
        anomaly_start_count = int(startup_heatmap.get("anomaly_start_count", 0) or 0)
        total_apps = len(apps)
        total_slots = len(slots)

        head_cells = ['<th class="startup-heatmap-head startup-heatmap-proc-head">包名</th>']
        for slot in slots:
            missing_start = bool(slot.get("is_missing_expected_start"))
            tip = (
                f"{slot.get('label', '')} | {slot.get('time', '-')}"
                f" | {slot.get('start_kind_cn', '未知')} | 启动 {slot.get('process', '')}"
            )
            if missing_start:
                tip += " | 启动异常: 未命中预期start日志"
            label = slot.get("display_label", "")
            label_cls = "startup-slot-label"
            if slot.get("round_pos") == 1:
                label_cls += " round-start"
            label_html = f"<span class='{label_cls}'>{html_escape(label)}</span>" if label else ""
            head_cells.append(
                f"<th class='startup-heatmap-head startup-heatmap-slot-head' "
                f"title='{html_escape(tip)}'>{label_html}</th>"
            )
        head_cells.append('<th class="startup-heatmap-head startup-heatmap-stat-head">存活率</th>')

        body_rows = []
        row_index_by_pkg = {pkg: idx for idx, pkg in enumerate(apps)}
        for row_idx, pkg in enumerate(apps):
            stats = row_stats[row_idx] if row_idx < len(row_stats) else {}
            row_cells = [f"<th class='startup-heatmap-proc'>{html_escape(pkg)}</th>"]
            for col_idx, slot in enumerate(slots):
                val = 0
                if row_idx < len(matrix) and col_idx < len(matrix[row_idx]):
                    val = matrix[row_idx][col_idx]
                expected_pkg = slot.get("expected_process", "")
                expected_row = row_index_by_pkg.get(expected_pkg, -1)
                is_missing_expected_start = bool(slot.get("is_missing_expected_start")) and (row_idx == expected_row)
                if is_missing_expected_start:
                    level_cls = "lvl-miss"
                    status = "启动异常(未命中预期start日志)"
                elif val == 2:
                    level_cls = "lvl-launch"
                    status = "本槽位启动"
                elif val == 1:
                    level_cls = "lvl-alive"
                    status = "存活"
                else:
                    level_cls = "lvl-dead"
                    status = "未存活"
                tip = f"{pkg} | {slot.get('label', '')} | {status}"
                row_cells.append(
                    f"<td class='startup-heatmap-cell {level_cls}' title='{html_escape(tip)}'><span></span></td>"
                )
            alive_slots = int(stats.get("alive_slots", 0) or 0)
            total_slot_cnt = int(stats.get("total_slots", total_slots) or total_slots)
            alive_rate = float(stats.get("alive_rate", 0.0) or 0.0)
            row_cells.append(
                f"<td class='startup-heatmap-stat'>{alive_slots}/{total_slot_cnt} ({alive_rate:.1f}%)</td>"
            )
            body_rows.append("<tr>" + "".join(row_cells) + "</tr>")

        col_alive_cells = ['<th class="startup-heatmap-footlabel">槽位存活</th>']
        col_dead_cells = ['<th class="startup-heatmap-footlabel">槽位失败</th>']
        for col in col_stats:
            alive_cnt = int(col.get("alive_count", 0) or 0)
            dead_cnt = int(col.get("dead_count", 0) or 0)
            tip = f"{col.get('label', '')} | 存活 {alive_cnt}/{total_apps} | 失败 {dead_cnt}/{total_apps}"
            col_alive_cells.append(
                f"<td class='startup-heatmap-colstat' title='{html_escape(tip)}'>{alive_cnt}</td>"
            )
            col_dead_cells.append(
                f"<td class='startup-heatmap-colstat dead' title='{html_escape(tip)}'>{dead_cnt}</td>"
            )
        col_alive_cells.append("<td></td>")
        col_dead_cells.append("<td></td>")

        return (
            '<div class="startup-heatmap-meta">'
            f"<span>应用数: <strong>{total_apps}</strong></span>"
            f"<span>启动槽位: <strong>{total_slots}</strong></span>"
            f"<span>{html_escape(expected_text)}</span>"
            f"<span>{POSSIBLE_ANOMALY_START_LABEL}: <strong>{fmt_num(anomaly_start_count)}</strong>（已排除，不计入热力图）</span>"
            "</div>"
            '<div class="startup-heatmap-legend">'
            '<span><i class="startup-heatmap-cell lvl-dead"><span></span></i> 未存活</span>'
            '<span><i class="startup-heatmap-cell lvl-alive"><span></span></i> 存活</span>'
            '<span><i class="startup-heatmap-cell lvl-launch"><span></span></i> 本槽位启动</span>'
            '<span><i class="startup-heatmap-cell lvl-miss"><span></span></i> 启动异常(缺失)</span>'
            "</div>"
            '<div class="startup-heatmap-wrap">'
            '<table class="startup-heatmap-table">'
            "<thead><tr>" + "".join(head_cells) + "</tr></thead>"
            "<tbody>" + "".join(body_rows) + "</tbody>"
            "<tfoot>"
            "<tr>" + "".join(col_alive_cells) + "</tr>"
            "<tr>" + "".join(col_dead_cells) + "</tr>"
            "</tfoot>"
            "</table>"
            "</div>"
        )

    hl_residency_html = build_hl_residency()
    startup_heatmap_html = build_startup_heatmap_html()

    if include_startup_section:
        startup_summary_card_html = (
            '<section class="summary-card">'
            '<div class="summary-title">启动</div>'
            '<div class="summary-stack">'
            '<div class="summary-row cols-3">'
            f"<div class='summary-item'><div class='summary-label'>启动全部进程</div><div class='summary-value'>{fmt_num(s['start_count'])}</div></div>"
            f"<div class='summary-item'><div class='summary-label'>启动主进程</div><div class='summary-value'>{fmt_num(main_start_count)}</div></div>"
            f"<div class='summary-item'><div class='summary-label'>启动高亮主进程</div><div class='summary-value'>{fmt_num(hl_main_start_count)}</div></div>"
            '</div>'
            '<div class="summary-row cols-7">'
            f"<div class='summary-item compact'><div class='summary-label'>前1驻留率</div><div class='summary-value'>{residency_avg['rates'][1]:.1f}%</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>前2驻留率</div><div class='summary-value'>{residency_avg['rates'][2]:.1f}%</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>前3驻留率</div><div class='summary-value'>{residency_avg['rates'][3]:.1f}%</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>前4驻留率</div><div class='summary-value'>{residency_avg['rates'][4]:.1f}%</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>前5驻留率</div><div class='summary-value'>{residency_avg['rates'][5]:.1f}%</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>平均驻留</div><div class='summary-value'>{residency_avg['all_alive']:.2f}</div></div>"
            f"<div class='summary-item compact'><div class='summary-label'>第二轮热启动</div><div class='summary-value'>{fmt_num(second_round_hot_count)}</div></div>"
            '</div>'
            '</div>'
            '</section>'
        )
        startup_tab_button_html = '<button class="tab-btn" type="button" data-tab="tab-start">启动</button>'
        startup_tab_panel_html = f"""
  <div class="tab-panel" id="tab-start">
    <div class="section">
      <h3>高亮主进程驻留率（前5次窗口）</h3>
      {hl_residency_html}
      <h3 class="start-subtitle">连续启动存活热力图（GitHub风格）</h3>
      {startup_heatmap_html}
      <h3 class="start-subtitle">高亮主进程启动/驻留表（前两轮）</h3>
      <div class="cards single">
        <div class="card card-wide">
          <div style="overflow-x:auto;">
            <table class="hl-run-table">
              <thead>
                <tr>
                  <th>进程</th>
                  <th>第2轮冷启动</th>
                  <th>启动1</th>
                  <th>被杀1</th>
                  <th>驻留1</th>
                  <th>启动2</th>
                  <th>被杀2</th>
                  <th>驻留2</th>
                  <th>平均驻留</th>
                </tr>
              </thead>
              <tbody>{hl_runs_table_html}</tbody>
            </table>
          </div>
        </div>
      </div>
      <h3>高亮主进程时间线</h3>
      <div class="timeline">{hl_timeline_html}</div>
    </div>
  </div>
"""
    else:
        startup_summary_card_html = ""
        startup_tab_button_html = ""
        startup_tab_panel_html = ""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>进程启动与查杀分析报告 - Summary</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"></script>
  <style>
    body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background:#0b1118; color:#e6edf3; margin:0; padding:24px; }}
    .page {{ max-width:min(1760px,96vw); margin:0 auto; }}
    h1, h2 {{ margin: 0 0 10px; }}
    h3 {{ margin: 14px 0 8px; }}
    .section {{ margin-bottom: 28px; }}
    .cards.single {{ display:block; }}
    .card {{ background:#141c26; padding:12px 14px; border-radius:10px; border:1px solid #1f2a36; box-shadow:0 8px 24px rgba(0,0,0,0.35); }}
    .card-wide {{ width:100%; box-sizing:border-box; }}
    .card-title {{ font-weight:700; color:#f5f7fb; margin-bottom:10px; letter-spacing:0.3px; font-size:20px; }}
    .card-row {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:6px 0; border-bottom:1px solid #172334; }}
    .card-row:last-child {{ border-bottom:none; }}
    .row-label {{ color:#9fb3c8; font-size:13px; }}
    .row-value {{ color:#f5f7fb; font-weight:600; font-variant-numeric:tabular-nums; }}
    .mem-block-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
    .mem-block {{ display:block; padding:12px; border:1px solid #223142; border-radius:10px; background:#0f1825; }}
    .mem-block-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:8px; }}
    .mem-block-title {{ margin:0; color:#dce9fa; font-size:16px; font-weight:700; letter-spacing:0.2px; }}
    .mem-block-sub {{ color:#8da6bf; font-size:12px; font-weight:700; letter-spacing:0.2px; }}
    .mem-block-body {{ min-width:0; }}
    .mem-metric-wrap {{ display:flex; flex-direction:column; gap:8px; }}
    .mem-empty {{ padding:10px 12px; border:1px dashed #29415c; border-radius:8px; color:#9fb3c8; background:#0f1722; }}
    .chart-grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
    .chart-card {{ background:#101821; border:1px solid #1f2a36; border-radius:12px; padding:10px; box-shadow:0 6px 18px rgba(0,0,0,0.35); position:relative; }}
    .chart-title {{ position:absolute; left:12px; top:10px; color:#9fb3c8; font-size:12px; letter-spacing:0.2px; }}
    .chart-card canvas {{ margin-top:18px; }}
    .timeline {{ border:1px solid #1f2a36; border-radius:12px; background:#101821; padding:10px; max-width:72%; margin:0 auto; }}
    .timeline-row {{ display:grid; grid-template-columns:120px 1fr; gap:8px; padding:6px 8px; border-bottom:1px solid #1b2634; align-items:center; }}
    .timeline-row:last-child {{ border-bottom:none; }}
    .timeline-row.anomaly-row {{ background:rgba(120, 130, 144, 0.18); }}
    .tl-time {{ color:#cdd6e3; font-size:12px; letter-spacing:0.2px; }}
    .tl-content {{ display:flex; justify-content:space-between; width:100%; }}
    .tl-left {{ color:#f5f7fb; font-size:14px; text-align:left; min-height:1.2em; }}
    .tl-right {{ color:#f5f7fb; font-size:14px; text-align:right; min-height:1.2em; }}
    .tl-anomaly-note {{ color:#a8b3c1; font-size:12px; }}
    .tl-legend {{ display:flex; gap:8px; padding:4px 8px 8px 8px; }}
    .pill {{ padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; border:1px solid transparent; }}
    .pill-start {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .pill-start-cold {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .pill-start-hot {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .pill-start-anomaly {{ background:#2f3440; color:#d2d9e3; border-color:#768395; }}
    .pill-kill {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .pill-lmk {{ background:#2b2140; color:#d6b6ff; border-color:#6f4bb7; }}
    .hl-res-table-wrapper {{ max-width:min(1600px,95vw); margin:12px auto 0 auto; overflow-x:auto; }}
    .hl-res-table {{ width:100%; border-collapse: collapse; background:#101821; border:1px solid #1f2a36; }}
    .hl-res-table th, .hl-res-table td {{ padding:8px 10px; border-bottom:1px solid #1f2a36; text-align:left; color:#e6edf3; }}
    .hl-res-table th {{ color:#9fb3c8; font-size:12px; letter-spacing:0.3px; }}
    .hl-res-table tbody tr:hover {{ background:#142032; }}
    .rate-ok {{ color:#6cf0a7 !important; font-weight:600; }}
    .rate-bad {{ color:#ff8b8b !important; font-weight:700; }}
    details summary {{ cursor:pointer; color:#8fb4ff; }}
    canvas {{ width:100%; height:240px; }}
    .accordion {{ border:1px solid #1f2a36; border-radius:12px; background:#0f1722; }}
    .acc-item {{ border-bottom:1px solid #1f2a36; }}
    .acc-item:last-child {{ border-bottom:none; }}
    .acc-header {{ padding:10px 12px; cursor:pointer; display:flex; justify-content:space-between; align-items:center; }}
    .acc-header:hover {{ background:#131c28; }}
    .acc-title {{ font-weight:600; color:#f5f7fb; }}
    .acc-meta {{ color:#9fb3c8; font-size:12px; }}
    .acc-body {{ display:none; padding:0 12px 12px 12px; font-size:13px; line-height:1.6; color:#d8e2f2; }}
    .hl-event-list {{ padding:8px 10px; }}
    .hl-event-item {{ border-bottom:1px solid #1f2a36; padding:6px 0; }}
    .hl-event-item:last-child {{ border-bottom:none; }}
    .hl-event-item summary {{ cursor:pointer; color:#cfe1ff; font-size:12px; font-weight:600; overflow-x:auto; }}
    .hl-event-summary-line {{ display:block; white-space:pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; line-height:1.45; letter-spacing:0.1px; }}
    .hl-event-item pre {{ margin:8px 0 4px 0; padding:8px; background:#0b141f; border:1px solid #1f2a36; border-radius:8px; color:#d8e2f2; white-space:pre-wrap; }}
    .mem-low-event-item {{ border-bottom:1px solid #1f2a36; padding:6px 0; }}
    .mem-low-event-item:last-child {{ border-bottom:none; }}
    .mem-low-event-item summary {{ cursor:pointer; color:#cfe1ff; font-size:12px; font-weight:600; overflow-x:auto; }}
    .mem-low-event-item pre {{ margin:8px 0 4px 0; padding:8px; background:#0b141f; border:1px solid #1f2a36; border-radius:8px; color:#d8e2f2; white-space:pre-wrap; }}
    .kv {{ margin:2px 0; }}
    .kv strong {{ color:#8fb4ff; }}
    .pill {{ display:inline-block; padding:2px 8px; margin:2px 4px 2px 0; border-radius:999px; background:#16263a; color:#cfe1ff; font-size:12px; }}
    .summary-board {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; margin-bottom:16px; }}
    .summary-card {{ background:#101821; border:1px solid #1f2a36; border-radius:12px; padding:12px; }}
    .summary-title {{ color:#dce9fa; font-size:15px; font-weight:700; margin-bottom:10px; }}
    .summary-stack {{ display:flex; flex-direction:column; gap:8px; }}
    .summary-row {{ display:grid; gap:8px; }}
    .summary-row.cols-2 {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
    .summary-row.cols-3 {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
    .summary-row.cols-6 {{ grid-template-columns:repeat(6, minmax(0, 1fr)); }}
    .summary-row.cols-7 {{ grid-template-columns:repeat(7, minmax(0, 1fr)); }}
    .summary-item {{ background:#0f1722; border:1px solid #1f2a36; border-radius:8px; padding:8px 9px; min-height:58px; }}
    .summary-item.compact {{ padding:7px 8px; min-height:52px; }}
    .summary-item.compact .summary-label {{ font-size:11px; }}
    .summary-item.compact .summary-value {{ font-size:17px; }}
    .summary-label {{ color:#8da6bf; font-size:12px; }}
    .summary-value {{ color:#f5f7fb; font-size:22px; font-weight:700; line-height:1.2; margin-top:2px; font-variant-numeric:tabular-nums; }}
    .summary-value.danger {{ color:#ff8b8b; }}
    .summary-value.lmk {{ color:#d6b6ff; }}
    .summary-mem-note {{ color:#8da6bf; font-size:11px; margin-bottom:8px; }}
    .summary-mem-table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
    .summary-mem-table th, .summary-mem-table td {{ padding:7px 8px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:right; font-variant-numeric:tabular-nums; }}
    .summary-mem-table th:first-child, .summary-mem-table td:first-child {{ text-align:left; }}
    .summary-mem-table th {{ color:#9fb3c8; font-weight:700; background:#111a27; }}
    .summary-empty {{ color:#8ea4bc; }}
    .detail-tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px 0; }}
    .tab-btn {{ appearance:none; border:1px solid #2a3b50; background:#0f1722; color:#b9cee5; border-radius:999px; padding:6px 12px; font-size:12px; font-weight:700; cursor:pointer; }}
    .tab-btn:hover {{ border-color:#4f77a3; color:#dcebff; }}
    .tab-btn.active {{ border-color:#4f77a3; color:#dcebff; background:#16263a; }}
    .tab-panel {{ display:none; }}
    .tab-panel.active {{ display:block; }}
    .subsection {{ margin-top:12px; }}
    .subsection:first-of-type {{ margin-top:0; }}
    .kill-scope-single {{ margin:0 0 10px 0; }}
    .kill-scope-card {{ background:#0f1722; border:1px solid #1f2a36; border-radius:10px; padding:10px; }}
    .kill-scope-title {{ color:#dce9fa; font-size:13px; font-weight:700; margin-bottom:8px; }}
    .kill-scope-row {{ display:flex; justify-content:space-between; align-items:center; gap:8px; color:#9fb3c8; font-size:12px; padding:3px 0; }}
    .kill-scope-row strong {{ color:#f5f7fb; font-size:16px; font-weight:700; font-variant-numeric:tabular-nums; }}
    .kill-row-5 {{ display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:14px; }}
    .kill-row-5 canvas {{ height:185px; }}
    .kill-index-empty {{ color:#8ea4bc; font-size:12px; }}
    .kill-index-filters {{ display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; margin-bottom:10px; }}
    .kill-filter-item {{ background:#0f1722; border:1px solid #1f2a36; border-radius:8px; padding:8px; }}
    .kill-filter-item label {{ display:block; color:#8da6bf; font-size:12px; margin-bottom:6px; }}
    .kill-filter-item select {{ width:100%; background:#0b141f; color:#dce9fa; border:1px solid #2a3b50; border-radius:6px; padding:6px 8px; font-size:12px; }}
    .mem-table-wrap {{ width:100%; overflow-x:auto; }}
    .mem-table {{ width:100%; border-collapse: collapse; table-layout:fixed; }}
    .mem-table th, .mem-table td {{ padding:7px 10px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
    .mem-table th:first-child, .mem-table td:first-child {{ text-align:left; }}
    .mem-table th {{ color:#9fb3c8; font-weight:700; background:#111a27; }}
    .mem-table tbody tr:hover {{ background:#142032; }}
    .mem-metric {{ color:#d6e7ff; font-weight:700; white-space:nowrap; }}
    .mem-empty-cell {{ color:#8ea4bc; text-align:center !important; font-style:italic; }}
    .hl-run-table {{ width:100%; border-collapse:collapse; background:#101821; border:1px solid #1f2a36; }}
    .hl-run-table th, .hl-run-table td {{ padding:6px 8px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:left; }}
    .hl-run-table th {{ color:#9fb3c8; font-weight:600; }}
    .hl-run-table tbody tr:nth-child(odd) {{ background:#111b27; }}
    .mem-dist-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin-top:8px; }}
    .mem-dist-grid .chart-card canvas {{ height:200px; }}
    .mem-low-filters {{ display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; margin:8px 0 10px 0; }}
    .mem-low-item {{ background:#0f1722; border:1px solid #1f2a36; border-radius:8px; padding:8px; }}
    .mem-low-item label {{ display:block; color:#8da6bf; font-size:12px; margin-bottom:6px; }}
    .mem-low-item select {{ width:100%; background:#0b141f; color:#dce9fa; border:1px solid #2a3b50; border-radius:6px; padding:6px 8px; font-size:12px; }}
    .mem-low-note {{ color:#8ea4bc; font-size:12px; margin-bottom:8px; }}
    .meminfo-source {{ color:#8da6bf; font-size:12px; margin:2px 0 10px 0; word-break:break-all; }}
    .meminfo-summary-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:10px; margin-bottom:10px; }}
    .meminfo-chart-grid {{ display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:12px; margin-bottom:12px; }}
    .meminfo-table-grid {{ display:grid; grid-template-columns:1.1fr 1fr; gap:12px; }}
    .meminfo-table-wrap {{ width:100%; overflow-x:auto; border:1px solid #1f2a36; border-radius:10px; background:#101821; }}
    .meminfo-table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
    .meminfo-table th, .meminfo-table td {{ padding:8px 10px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:right; font-variant-numeric:tabular-nums; }}
    .meminfo-table th:first-child, .meminfo-table td:first-child {{ text-align:left; }}
    .meminfo-table th:nth-child(2), .meminfo-table td:nth-child(2) {{ text-align:left; }}
    .meminfo-table th {{ color:#9fb3c8; font-weight:700; background:#111a27; }}
    .meminfo-table tbody tr:hover {{ background:#142032; }}
    .meminfo-subtabs {{ display:flex; gap:8px; margin:8px 0 10px 0; }}
    .meminfo-subtab-btn {{ appearance:none; border:1px solid #2a3b50; background:#0f1722; color:#b9cee5; border-radius:999px; padding:5px 11px; font-size:12px; font-weight:700; cursor:pointer; }}
    .meminfo-subtab-btn:hover {{ border-color:#4f77a3; color:#dcebff; }}
    .meminfo-subtab-btn.active {{ border-color:#4f77a3; color:#dcebff; background:#16263a; }}
    .meminfo-subpanel {{ display:none; }}
    .meminfo-subpanel.active {{ display:block; }}
    .meminfo-priority-row {{ display:grid; grid-template-columns:1fr 340px 1fr; gap:12px; align-items:start; }}
    .meminfo-priority-chart {{ height:340px; }}
    .meminfo-priority-canvas-wrap {{ width:min(280px, 100%); height:280px; margin:0 auto; }}
    .meminfo-priority-canvas-wrap canvas {{ width:100% !important; height:100% !important; margin-top:8px; }}
    .device-table-wrap {{ width:100%; overflow-x:auto; }}
    .device-info-table {{ width:100%; border-collapse:collapse; table-layout:fixed; background:#101821; border:1px solid #1f2a36; }}
    .device-info-table th, .device-info-table td {{ padding:9px 10px; border-bottom:1px solid #1f2a36; text-align:left; color:#e6edf3; font-size:12px; vertical-align:top; }}
    .device-info-table th {{ width:260px; color:#9fb3c8; font-weight:700; }}
    .device-info-pre {{ margin:0; padding:10px; color:#dce9fa; background:#0f1722; border:1px solid #1f2a36; border-radius:8px; overflow-x:auto; white-space:pre-wrap; word-break:break-word; font-size:12px; line-height:1.55; }}
    .hl-residency-meta {{ color:#9fb3c8; font-size:12px; margin:6px 0 8px 0; }}
    .hl-residency-meta strong {{ color:#f5f7fb; font-variant-numeric:tabular-nums; }}
    .hl-start-cold {{ color:#6cf0a7 !important; font-weight:700; }}
    .hl-start-hot {{ color:#ff8b8b !important; font-weight:700; }}
    .hl-start-anomaly {{ color:#d1dae5 !important; font-weight:700; }}
    .hl-proc-em {{ color:#8fc8ff !important; font-weight:700; }}
    .hl-anomaly-row {{ background:#2b3240 !important; opacity:0.72; }}
    .hl-anomaly-cell {{ color:#a7b4c3 !important; }}
    .hl-anomaly-note {{ color:#99a9bc; font-size:11px; margin-top:2px; }}
    .start-subtitle {{ margin-top:16px; }}
    .startup-heatmap-meta {{ display:flex; flex-wrap:wrap; gap:14px; color:#9fb3c8; font-size:12px; margin:2px 0 8px 0; }}
    .startup-heatmap-meta strong {{ color:#f5f7fb; font-variant-numeric:tabular-nums; }}
    .startup-heatmap-legend {{ display:flex; flex-wrap:wrap; gap:12px; color:#9fb3c8; font-size:12px; margin:4px 0 10px 0; }}
    .startup-heatmap-legend span {{ display:inline-flex; align-items:center; gap:6px; }}
    .startup-heatmap-legend i.startup-heatmap-cell {{ display:inline-block; }}
    .startup-heatmap-legend .startup-heatmap-cell {{ width:14px; min-width:14px; height:14px; line-height:0; border-radius:3px; box-sizing:border-box; border:1px solid #2a3443; }}
    .startup-heatmap-wrap {{ --heat-cell-size: 14px; overflow-x:auto; border:1px solid #1f2a36; border-radius:10px; background:#0f1722; padding:8px; }}
    .startup-heatmap-table {{ border-collapse:separate; border-spacing:4px; min-width:max-content; table-layout:fixed; }}
    .startup-heatmap-table th, .startup-heatmap-table td {{ vertical-align:middle; }}
    .startup-heatmap-head {{ color:#8da6bf; font-size:12px; font-weight:600; text-align:center; white-space:nowrap; }}
    .startup-heatmap-proc-head, .startup-heatmap-proc {{ position:sticky; left:0; z-index:2; background:#0f1722; }}
    .startup-heatmap-proc {{ color:#dce9fa; font-size:12px; font-weight:600; text-align:left; padding:0 8px 0 2px; min-width:190px; max-width:320px; white-space:nowrap; }}
    .startup-heatmap-slot-head {{ position:relative; min-width:var(--heat-cell-size); width:var(--heat-cell-size); max-width:var(--heat-cell-size); height:30px; padding:0; font-variant-numeric:tabular-nums; overflow:visible; }}
    .startup-slot-label {{ position:absolute; left:50%; top:4px; transform:translateX(-50%) rotate(-28deg); transform-origin:center top; white-space:nowrap; font-size:12px; color:#8da6bf; pointer-events:none; }}
    .startup-slot-label.round-start {{ color:#b9d9ff; font-weight:700; }}
    .startup-heatmap-stat-head, .startup-heatmap-stat {{ white-space:nowrap; font-variant-numeric:tabular-nums; color:#cdd6e3; font-size:12px; text-align:left; padding-left:8px; min-width:130px; }}
    td.startup-heatmap-cell {{
      width:var(--heat-cell-size) !important;
      min-width:var(--heat-cell-size) !important;
      max-width:var(--heat-cell-size) !important;
      height:var(--heat-cell-size) !important;
      min-height:var(--heat-cell-size) !important;
      max-height:var(--heat-cell-size) !important;
      aspect-ratio:1 / 1;
      padding:0 !important;
      line-height:0;
      border-radius:3px;
      box-sizing:border-box;
      border:1px solid #2a3443;
      overflow:hidden;
    }}
    .startup-heatmap-cell span {{ display:block; width:100%; height:100%; margin:0; padding:0; }}
    .startup-heatmap-cell.lvl-dead {{ background:#1a2431; border-color:#2a3443; }}
    .startup-heatmap-cell.lvl-alive {{ background:#1f6f3d; border-color:#2ea043; }}
    .startup-heatmap-cell.lvl-launch {{ background:#2ea043; border-color:#56d364; box-shadow:0 0 0 1px rgba(86,211,100,0.28); }}
    .startup-heatmap-cell.lvl-miss {{ background:#5b2a2a; border-color:#d07b7b; box-shadow:0 0 0 1px rgba(208,123,123,0.22); }}
    .startup-heatmap-colstat {{ color:#9fb3c8; font-size:11px; text-align:center; font-variant-numeric:tabular-nums; }}
    .startup-heatmap-colstat.dead {{ color:#ffb4b4; }}
    .startup-heatmap-footlabel {{ position:sticky; left:0; z-index:2; background:#0f1722; color:#8da6bf; font-size:11px; font-weight:600; text-align:left; padding:0 6px 0 2px; }}
    .startup-heatmap-empty {{ color:#9fb3c8; border:1px dashed #29415c; border-radius:8px; padding:10px 12px; background:#0f1722; }}
    @media (max-width: 980px) {{
      body {{ padding:14px; }}
      .page {{ max-width:100%; }}
      .summary-board {{ grid-template-columns:1fr; }}
      .summary-row.cols-6 {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
      .summary-row.cols-7 {{ grid-template-columns:repeat(4, minmax(0, 1fr)); }}
      .detail-tabs {{ gap:6px; }}
      .kill-row-5, .kill-index-filters {{ grid-template-columns:1fr; gap:10px; }}
      .timeline {{ max-width:100%; }}
      .mem-block-grid {{ grid-template-columns:1fr; }}
      .mem-dist-grid {{ grid-template-columns:1fr; }}
      .mem-low-filters {{ grid-template-columns:1fr; }}
      .meminfo-summary-grid {{ grid-template-columns:repeat(2, minmax(0,1fr)); }}
      .meminfo-chart-grid {{ grid-template-columns:1fr; }}
      .meminfo-table-grid {{ grid-template-columns:1fr; }}
      .meminfo-priority-row {{ grid-template-columns:1fr; }}
      .meminfo-priority-canvas-wrap {{ height:240px; }}
      .mem-block-head {{ flex-wrap:wrap; gap:6px; margin-bottom:6px; }}
      .mem-block-title {{ font-size:15px; }}
      .chart-grid {{ grid-template-columns:1fr; }}
      .hl-event-summary-line {{ white-space:normal; }}
      .startup-heatmap-proc {{ min-width:140px; max-width:220px; }}
    }}
  </style>
</head>
<body>
  <div class="page">
  <h1>进程启动与查杀分析报告（Summary）</h1>

  <div class="summary-board">
    <section class="summary-card">
      <div class="summary-title">查杀</div>
      <div class="summary-stack">
        <div class="summary-row cols-3">
          <div class="summary-item"><div class="summary-label">总查杀数</div><div class="summary-value">{fmt_num(kill_total)}</div></div>
          <div class="summary-item"><div class="summary-label">主进程查杀数</div><div class="summary-value">{fmt_num(main_kill_total)}</div></div>
          <div class="summary-item"><div class="summary-label">高亮主进程查杀数</div><div class="summary-value">{fmt_num(hl_main_kill_total)}</div></div>
        </div>
        <div class="summary-row cols-2">
          <div class="summary-item"><div class="summary-label">一体化查杀</div><div class="summary-value danger">{fmt_num(s['kill_count'])}</div></div>
          <div class="summary-item"><div class="summary-label">LMK 查杀</div><div class="summary-value lmk">{fmt_num(s['lmk_count'])}</div></div>
        </div>
      </div>
    </section>
    {startup_summary_card_html}
    <section class="summary-card">
      <div class="summary-title">内存状态</div>
      <div class="summary-mem-note">memfree，单位 KB（查杀时）</div>
      <table class="summary-mem-table">
        <thead><tr><th>范围</th><th>Avg</th><th>P50</th><th>Min</th></tr></thead>
        <tbody>{summary_mem_rows_html}</tbody>
      </table>
    </section>
  </div>

  <div class="detail-tabs">
    <button class="tab-btn active" type="button" data-tab="tab-kill">查杀</button>
    {startup_tab_button_html}
    <button class="tab-btn" type="button" data-tab="tab-memory">内存状态</button>
    <button class="tab-btn" type="button" data-tab="tab-meminfo">Meminfo结构</button>
    <button class="tab-btn" type="button" data-tab="tab-device">设备信息</button>
  </div>

  <div class="tab-panel active" id="tab-kill">
    <div class="section">
      <div class="subsection">
        <h3>全部</h3>
        <div class="kill-scope-single">
          {kill_scope_all_html}
        </div>
        <div class="kill-row-5">
          <div class="chart-card"><div class="chart-title">查杀类型</div><canvas id="chartKillType"></canvas></div>
          <div class="chart-card"><div class="chart-title">查杀最低分值</div><canvas id="chartMinScore"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀原因</div><canvas id="chartLmkReason"></canvas></div>
          <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj</div><canvas id="chartAdj"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj</div><canvas id="chartLmkAdj"></canvas></div>
        </div>
      </div>
      <div class="subsection">
        <h3>主进程</h3>
        <div class="kill-scope-single">
          {kill_scope_main_html}
        </div>
        <div class="kill-row-5">
          <div class="chart-card"><div class="chart-title">查杀类型</div><canvas id="chartMainKillType"></canvas></div>
          <div class="chart-card"><div class="chart-title">查杀最低分值</div><canvas id="chartMainMinScore"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀原因</div><canvas id="chartMainLmkReason"></canvas></div>
          <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj</div><canvas id="chartMainAdj"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj</div><canvas id="chartMainLmkAdj"></canvas></div>
        </div>
      </div>
      <div class="subsection">
        <h3>高亮主进程</h3>
        <div class="kill-scope-single">
          {kill_scope_hl_html}
        </div>
        <div class="kill-row-5">
          <div class="chart-card"><div class="chart-title">查杀类型</div><canvas id="chartHlKillType"></canvas></div>
          <div class="chart-card"><div class="chart-title">查杀最低分值</div><canvas id="chartHlMinScore"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀原因</div><canvas id="chartHlLmkReason"></canvas></div>
          <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj</div><canvas id="chartHlAdj"></canvas></div>
          <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj</div><canvas id="chartHlLmkAdj"></canvas></div>
        </div>
        <h3>高亮主进程明细索引</h3>
        <div class="kill-index-filters">
          <div class="kill-filter-item">
            <label for="hlFilterKillType">KillType</label>
            <select id="hlFilterKillType">{hl_filter_killtype_options_html}</select>
          </div>
          <div class="kill-filter-item">
            <label for="hlFilterAdj">adj</label>
            <select id="hlFilterAdj">{hl_filter_adj_options_html}</select>
          </div>
          <div class="kill-filter-item">
            <label for="hlFilterProc">被杀进程</label>
            <select id="hlFilterProc">{hl_filter_proc_options_html}</select>
          </div>
        </div>
        <h3>高亮主进程明细（可展开）</h3>
        <div id="hlDetailEmpty" class="kill-index-empty" style="display:none;">无匹配详情</div>
        <div class="accordion hl-event-list" id="hlDetailList">{hl_detail_html}</div>
      </div>
    </div>
  </div>

  {startup_tab_panel_html}

  <div class="tab-panel" id="tab-memory">
    <div class="section">
      <div class="cards single">
        {mem_avg_card_html}
      </div>
      <h3>高亮主进程(主)内存分布（查杀时）</h3>
      <div class="mem-dist-grid">
        <div class="chart-card"><div class="chart-title">memfree 分布（KB）</div><canvas id="chartMemDistMemfree"></canvas></div>
        <div class="chart-card"><div class="chart-title">file 分布（KB）</div><canvas id="chartMemDistFile"></canvas></div>
        <div class="chart-card"><div class="chart-title">anon 分布（KB）</div><canvas id="chartMemDistAnon"></canvas></div>
        <div class="chart-card"><div class="chart-title">swapfree 分布（KB）</div><canvas id="chartMemDistSwap"></canvas></div>
      </div>
      <h3>高亮主进程(主)低内存明细（可展开）</h3>
      <div class="mem-low-note">按指标筛选后展示最低的 5/10/20/50 条查杀事件（含上层/一体化与底层/LMKD）。</div>
      <div class="mem-low-filters">
        <div class="mem-low-item">
          <label for="memLowMetric">指标</label>
          <select id="memLowMetric">
            <option value="mem_free">memfree</option>
            <option value="file_pages">file</option>
            <option value="anon_pages">anon</option>
            <option value="swap_free">swapfree</option>
          </select>
        </div>
        <div class="mem-low-item">
          <label for="memLowLimit">条数</label>
          <select id="memLowLimit">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
          </select>
        </div>
      </div>
      <div id="memLowDetailEmpty" class="kill-index-empty" style="display:none;">无匹配详情</div>
      <div class="accordion hl-event-list" id="memLowDetailList"></div>
    </div>
  </div>

  <div class="tab-panel" id="tab-meminfo">
    <div class="section">
      <h3>dumpsys meminfo 结构化视图</h3>
      <div class="meminfo-source">来源: {html_escape(meminfo_source_desc or "-")}</div>
      {meminfo_error_html}

      <h3>Total PSS by process (Top 20)</h3>
      <div class="meminfo-summary-grid">
        {meminfo_summary_cards_html}
      </div>
      <div class="meminfo-subtabs">
        <button class="meminfo-subtab-btn active" type="button" data-mem-group="topproc" data-mem-panel="table">表格</button>
        <button class="meminfo-subtab-btn" type="button" data-mem-group="topproc" data-mem-panel="chart">图表</button>
      </div>
      <div class="meminfo-subpanel active" data-mem-group="topproc" data-mem-panel="table">
        <div class="meminfo-table-wrap">
          <table class="meminfo-table">
            <thead><tr><th>#</th><th>进程</th><th>PSS(KB)</th><th>Swap(KB)</th></tr></thead>
            <tbody>{meminfo_top_process_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class="meminfo-subpanel" data-mem-group="topproc" data-mem-panel="chart">
        <div class="meminfo-chart-grid" style="grid-template-columns:1fr;">
          <div class="chart-card"><div class="chart-title">Top进程 PSS (KB)</div><canvas id="chartMeminfoTopProc"></canvas></div>
        </div>
      </div>

      <h3>PSS by OOM adjustment</h3>
      <div class="meminfo-subtabs">
        <button class="meminfo-subtab-btn active" type="button" data-mem-group="oom" data-mem-panel="table">表格</button>
        <button class="meminfo-subtab-btn" type="button" data-mem-group="oom" data-mem-panel="chart">图表</button>
      </div>
      <div class="meminfo-subpanel active" data-mem-group="oom" data-mem-panel="table">
        <div class="meminfo-table-wrap">
          <table class="meminfo-table">
            <thead><tr><th>优先级组</th><th>OOM 分类</th><th>进程数</th><th>PSS(KB)</th><th>Swap(KB)</th></tr></thead>
            <tbody>{meminfo_oom_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class="meminfo-subpanel" data-mem-group="oom" data-mem-panel="chart">
        <div class="meminfo-chart-grid" style="grid-template-columns:1fr;">
          <div class="chart-card"><div class="chart-title">OOM 分类占用 (KB)</div><canvas id="chartMeminfoOom"></canvas></div>
        </div>
      </div>

      <h3>优先级视角 (基于 OOM 类别)</h3>
      <div class="meminfo-priority-row">
        <div class="meminfo-table-wrap">
          <table class="meminfo-table">
            <thead><tr><th>优先级组</th><th>进程数</th><th>PSS(KB)</th></tr></thead>
            <tbody>{meminfo_priority_rows_html}</tbody>
          </table>
        </div>
        <div class="chart-card meminfo-priority-chart">
          <div class="chart-title">优先级占比 (PSS)</div>
          <div class="meminfo-priority-canvas-wrap"><canvas id="chartMeminfoPriority"></canvas></div>
        </div>
        <div class="meminfo-table-wrap">
          <table class="meminfo-table">
            <thead><tr><th>优先级组</th><th>规则映射(示例)</th><th>本报告命中类别</th></tr></thead>
            <tbody>{priority_mapping_rows_html}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="tab-panel" id="tab-device">
    <div class="section">
      <h3>Bugreport 设备信息</h3>
      <div class="cards single">
        <div class="card card-wide">
          <div class="device-table-wrap">
            <table class="device-info-table">
              <tbody>{device_info_rows_html}</tbody>
            </table>
          </div>
        </div>
      </div>
      <h3>自动匹配结果</h3>
      <div class="cards single">
        <div class="card card-wide">
          <pre class="device-info-pre">{auto_match_block_html}</pre>
        </div>
      </div>
      <h3>/proc/mv</h3>
      <div class="cards single">
        <div class="card card-wide">
          <pre class="device-info-pre">{proc_mv_block_html}</pre>
        </div>
      </div>
    </div>
  </div>
  </div>

  <script>
    const charts = {json.dumps(_to_plain({
        "kill_type": s.get("kill_type_stats", {}),
        "min_score": min_score_chart_stats,
        "adj": s.get("adj_stats", {}),
        "lmk_reason": s.get("lmk_reason_stats", {}),
        "lmk_adj": s.get("lmk_adj_stats", {}),
        "main_kill_type": s.get("main_overall", {}).get("kill_type_stats", {}),
        "main_min_score": main_min_score_chart_stats,
        "main_lmk_reason": main_lmk_reason_stats,
        "main_adj": s.get("main_overall", {}).get("adj_stats", {}),
        "main_lmk_adj": s.get("main_overall", {}).get("lmk_adj_stats", {}),
        "hl_kill_type": s.get("highlight_overall", {}).get("main_kill_type_stats", {}),
        "hl_min_score": hl_min_score_chart_stats,
        "hl_lmk_reason": hl_lmk_reason_stats,
        "hl_adj": s.get("highlight_overall", {}).get("main_adj_stats", {}),
        "hl_lmk_adj": s.get("highlight_overall", {}).get("main_lmk_adj_stats", {}),
        "hl_mem_dist": hl_mem_dist,
        "meminfo_top_process": meminfo_data.get("chart_top_process", {}),
        "meminfo_oom": meminfo_data.get("chart_oom", {}),
        "meminfo_priority": meminfo_data.get("chart_priority", {}),
    }), ensure_ascii=False)};
    const hlMemLowEvents = {json.dumps(_to_plain(hl_mem_low_events), ensure_ascii=False)};

    function renderBar(canvasId, dataObj, label) {{
      const ctx = document.getElementById(canvasId);
      if (!ctx) return;
      const labels = Object.keys(dataObj || {{}}); 
      const values = Object.values(dataObj || {{}});
      if (labels.length === 0) {{
        const titleEl = ctx.parentElement.querySelector('.chart-title');
        const titleHtml = titleEl ? titleEl.outerHTML : '';
        ctx.parentElement.innerHTML = titleHtml + '<div style="color:#9fb3c8;font-size:12px;padding:8px;">暂无数据（该范围无样本）</div>';
        return;
      }}
      const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 260);
      gradient.addColorStop(0, 'rgba(123,198,255,0.9)');
      gradient.addColorStop(1, 'rgba(123,198,255,0.2)');
      new Chart(ctx, {{
        type: 'bar',
        data: {{
          labels,
          datasets: [{{
            label,
            data: values,
            backgroundColor: gradient,
            borderColor: 'rgba(123,198,255,0.95)',
            borderWidth: 1.2,
            borderRadius: 6,
            hoverBackgroundColor: 'rgba(123,198,255,0.95)'
          }}]
        }},
        options: {{
          responsive: true,
          plugins: {{
            legend: {{ display: false }},
            datalabels: {{
              anchor: 'end',
              align: 'end',
              color: '#f5f7fb',
              font: {{ size: 11, weight: '600' }},
              formatter: (value) => value
            }}
          }},
          scales: {{
            x: {{ ticks: {{ color: '#cdd6e3' }} }},
            y: {{ ticks: {{ color: '#cdd6e3' }}, beginAtZero:true }}
          }}
        }}
      }});
    }}

    function renderLine(canvasId, curveObj, label) {{
      const ctx = document.getElementById(canvasId);
      if (!ctx) return;
      const labels = (curveObj && curveObj.labels) || [];
      const values = (curveObj && curveObj.values) || [];
      if (!labels.length || !values.length) {{
        const titleEl = ctx.parentElement.querySelector('.chart-title');
        const titleHtml = titleEl ? titleEl.outerHTML : '';
        ctx.parentElement.innerHTML = titleHtml + '<div style="color:#9fb3c8;font-size:12px;padding:8px;">暂无数据（该范围无样本）</div>';
        return;
      }}

      const lineColor = 'rgba(123,198,255,0.95)';
      const fillColor = 'rgba(123,198,255,0.18)';
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels,
          datasets: [{{
            label,
            data: values,
            borderColor: lineColor,
            backgroundColor: fillColor,
            pointBackgroundColor: 'rgba(184,225,255,0.95)',
            pointRadius: 1.8,
            pointHoverRadius: 3,
            borderWidth: 1.8,
            tension: 0.28,
            fill: true
          }}]
        }},
        options: {{
          responsive: true,
          plugins: {{
            legend: {{ display: false }},
            datalabels: {{ display: false }}
          }},
          scales: {{
            x: {{ ticks: {{ color: '#cdd6e3', maxTicksLimit: 8 }} }},
            y: {{
              ticks: {{
                color: '#cdd6e3',
                callback: (value) => Number(value).toLocaleString('en-US')
              }},
              beginAtZero: false
            }}
          }}
        }}
      }});
    }}

    function renderDoughnut(canvasId, dataObj, label) {{
      const ctx = document.getElementById(canvasId);
      if (!ctx) return;
      const entries = Object.entries(dataObj || {{}})
        .filter(([, v]) => Number(v) > 0)
        .sort((a, b) => Number(b[1]) - Number(a[1]));
      if (!entries.length) {{
        const titleEl = ctx.parentElement.querySelector('.chart-title');
        const titleHtml = titleEl ? titleEl.outerHTML : '';
        ctx.parentElement.innerHTML = titleHtml + '<div style="color:#9fb3c8;font-size:12px;padding:8px;">暂无数据（该范围无样本）</div>';
        return;
      }}
      const palette = [
        'rgba(123,198,255,0.92)',
        'rgba(128,226,196,0.92)',
        'rgba(255,187,120,0.92)',
        'rgba(247,140,140,0.92)',
        'rgba(189,156,255,0.92)',
        'rgba(255,214,92,0.92)',
      ];
      const labels = entries.map((e) => e[0]);
      const values = entries.map((e) => Number(e[1]));
      const colors = labels.map((_, i) => palette[i % palette.length]);
      new Chart(ctx, {{
        type: 'doughnut',
        data: {{
          labels,
          datasets: [{{
            label,
            data: values,
            backgroundColor: colors,
            borderColor: '#0f1722',
            borderWidth: 2,
            hoverOffset: 4,
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{
              position: 'bottom',
              labels: {{ color: '#cdd6e3', boxWidth: 12, font: {{ size: 11 }} }}
            }},
            datalabels: {{ display: false }}
          }}
        }}
      }});
    }}

    function renderList(listId, dataObj) {{
      const container = document.getElementById(listId);
      if (!container) return;
      const entries = Object.entries(dataObj || {{}}).sort((a,b)=>b[1]-a[1]);
      if (!entries.length) {{
        container.innerHTML = '<div style="color:#9fb3c8;">暂无数据</div>';
        return;
      }}
      container.innerHTML = entries.map(([k,v]) => `
        <div><span class="key">${{k}}</span><span class="val">${{v}}</span></div>
      `).join('');
    }}

    renderBar('chartKillType', charts.kill_type, '查杀类型');    renderList('listKillType', charts.kill_type);
    renderBar('chartMinScore', charts.min_score, '可查杀最低分值'); renderList('listMinScore', charts.min_score);
    renderBar('chartAdj', charts.adj, 'adj 分布');              renderList('listAdj', charts.adj);
    renderBar('chartLmkReason', charts.lmk_reason, 'LMK 原因');   renderList('listLmkReason', charts.lmk_reason);
    renderBar('chartLmkAdj', charts.lmk_adj, 'LMK adj');          renderList('listLmkAdj', charts.lmk_adj);

    renderBar('chartMainKillType', charts.main_kill_type, '主进程 kill 类型'); renderList('listMainKillType', charts.main_kill_type);
    renderBar('chartMainMinScore', charts.main_min_score, '主进程 minScore');  renderList('listMainMinScore', charts.main_min_score);
    renderBar('chartMainLmkReason', charts.main_lmk_reason, '主进程 LMK 原因'); renderList('listMainLmkReason', charts.main_lmk_reason);
    renderBar('chartMainAdj', charts.main_adj, '主进程 adj');                  renderList('listMainAdj', charts.main_adj);
    renderBar('chartMainLmkAdj', charts.main_lmk_adj, '主进程 LMK adj');       renderList('listMainLmkAdj', charts.main_lmk_adj);

    renderBar('chartHlKillType', charts.hl_kill_type, '高亮主进程 kill 类型'); renderList('listHlKillType', charts.hl_kill_type);
    renderBar('chartHlMinScore', charts.hl_min_score, '高亮主进程 minScore');  renderList('listHlMinScore', charts.hl_min_score);
    renderBar('chartHlLmkReason', charts.hl_lmk_reason, '高亮主进程 LMK 原因'); renderList('listHlLmkReason', charts.hl_lmk_reason);
    renderBar('chartHlAdj', charts.hl_adj, '高亮主进程 adj');                 renderList('listHlAdj', charts.hl_adj);
    renderBar('chartHlLmkAdj', charts.hl_lmk_adj, '高亮主进程 LMK adj');      renderList('listHlLmkAdj', charts.hl_lmk_adj);

    renderLine('chartMemDistMemfree', charts.hl_mem_dist && charts.hl_mem_dist.mem_free, 'memfree 分布');
    renderLine('chartMemDistFile', charts.hl_mem_dist && charts.hl_mem_dist.file_pages, 'file 分布');
    renderLine('chartMemDistAnon', charts.hl_mem_dist && charts.hl_mem_dist.anon_pages, 'anon 分布');
    renderLine('chartMemDistSwap', charts.hl_mem_dist && charts.hl_mem_dist.swap_free, 'swapfree 分布');

    const meminfoChartRendered = {{
      topproc: false,
      oom: false,
      priority: false,
    }};
    function renderMeminfoChart(group) {{
      if (group === 'topproc' && !meminfoChartRendered.topproc) {{
        renderBar('chartMeminfoTopProc', charts.meminfo_top_process, 'PSS(KB)');
        meminfoChartRendered.topproc = true;
      }}
      if (group === 'oom' && !meminfoChartRendered.oom) {{
        renderBar('chartMeminfoOom', charts.meminfo_oom, 'PSS(KB)');
        meminfoChartRendered.oom = true;
      }}
      if (group === 'priority' && !meminfoChartRendered.priority) {{
        renderDoughnut('chartMeminfoPriority', charts.meminfo_priority, '优先级');
        meminfoChartRendered.priority = true;
      }}
    }}

    // 章节标签页
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabPanels = document.querySelectorAll('.tab-panel');
    function activateTab(tabId) {{
      tabButtons.forEach((btn) => {{
        const isActive = btn.dataset.tab === tabId;
        btn.classList.toggle('active', isActive);
      }});
      tabPanels.forEach((panel) => {{
        panel.classList.toggle('active', panel.id === tabId);
      }});
    }}
    tabButtons.forEach((btn) => {{
      btn.addEventListener('click', () => activateTab(btn.dataset.tab));
    }});

    // Meminfo 子标签页
    const memSubButtons = document.querySelectorAll('.meminfo-subtab-btn');
    const memSubPanels = document.querySelectorAll('.meminfo-subpanel');
    function activateMemSubTab(group, panel) {{
      memSubButtons.forEach((btn) => {{
        if (btn.dataset.memGroup !== group) return;
        btn.classList.toggle('active', btn.dataset.memPanel === panel);
      }});
      memSubPanels.forEach((item) => {{
        if (item.dataset.memGroup !== group) return;
        item.classList.toggle('active', item.dataset.memPanel === panel);
      }});
      if (panel === 'chart') renderMeminfoChart(group);
    }}
    memSubButtons.forEach((btn) => {{
      btn.addEventListener('click', () => activateMemSubTab(btn.dataset.memGroup, btn.dataset.memPanel));
    }});
    document.querySelectorAll('.meminfo-subpanel.active[data-mem-panel="chart"]').forEach((panel) => {{
      if (panel.dataset.memGroup) renderMeminfoChart(panel.dataset.memGroup);
    }});
    renderMeminfoChart('priority');

    // 内存低值明细过滤
    const memLowMetric = document.getElementById('memLowMetric');
    const memLowLimit = document.getElementById('memLowLimit');
    const memLowList = document.getElementById('memLowDetailList');
    const memLowEmpty = document.getElementById('memLowDetailEmpty');

    function escapeHtml(text) {{
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function fmtMetric(value) {{
      if (value === null || value === undefined || value === '') return '-';
      const num = Number(value);
      return Number.isFinite(num) ? num.toLocaleString('en-US') : String(value);
    }}

    function padRight(text, width) {{
      const src = String(text ?? '');
      if (src.length > width) {{
        return width <= 3 ? src.slice(0, width) : src.slice(0, width - 3) + '...';
      }}
      return src.padEnd(width, ' ');
    }}

    function buildMemLowSummary(rec) {{
      return [
        padRight(`EVENT ${{rec.event_id}}`, 10),
        padRight(`TYPE ${{rec.type_label}}`, 10),
        padRight(`PKG ${{rec.process}}`, 34),
        padRight(`MEMFREE ${{fmtMetric(rec.mem_free)}}`, 18),
        padRight(`FILE ${{fmtMetric(rec.file_pages)}}`, 14),
        padRight(`ANON ${{fmtMetric(rec.anon_pages)}}`, 14),
        padRight(`SWAPFREE ${{fmtMetric(rec.swap_free)}}`, 18),
        padRight(rec.time || '-', 18),
      ].join('  ');
    }}

    function applyMemLowFilter() {{
      if (!memLowMetric || !memLowLimit || !memLowList || !memLowEmpty) return;
      const metricKey = memLowMetric.value || 'mem_free';
      const limit = parseInt(memLowLimit.value || '10', 10);
      const filtered = (hlMemLowEvents || [])
        .filter((rec) => rec && rec[metricKey] !== null && rec[metricKey] !== undefined)
        .sort((a, b) => Number(a[metricKey]) - Number(b[metricKey]))
        .slice(0, limit);

      if (!filtered.length) {{
        memLowList.innerHTML = '';
        memLowEmpty.style.display = 'block';
        return;
      }}

      memLowEmpty.style.display = 'none';
      memLowList.innerHTML = filtered.map((rec) => {{
        const summaryLine = buildMemLowSummary(rec);
        return `
          <details class="mem-low-event-item">
            <summary><span class="hl-event-summary-line">${{escapeHtml(summaryLine)}}</span></summary>
            <pre>${{escapeHtml(rec.detail || '')}}</pre>
          </details>
        `;
      }}).join('');
    }}

    [memLowMetric, memLowLimit].forEach((el) => {{
      if (el) el.addEventListener('change', applyMemLowFilter);
    }});
    applyMemLowFilter();

    // 高亮主进程明细索引过滤
    const hlFilterKillType = document.getElementById('hlFilterKillType');
    const hlFilterAdj = document.getElementById('hlFilterAdj');
    const hlFilterProc = document.getElementById('hlFilterProc');
    function applyHlDetailFilter() {{
      if (!hlFilterKillType || !hlFilterAdj || !hlFilterProc) return;
      const killTypeVal = hlFilterKillType.value || '';
      const adjVal = hlFilterAdj.value || '';
      const procVal = hlFilterProc.value || '';
      let visibleEventCount = 0;
      document.querySelectorAll('.hl-event-item').forEach((itemEl) => {{
        const matchesKillType = !killTypeVal || itemEl.dataset.killtype === killTypeVal;
        const matchesAdj = !adjVal || itemEl.dataset.adj === adjVal;
        const matchesProc = !procVal || itemEl.dataset.proc === procVal;
        const visible = matchesKillType && matchesAdj && matchesProc;
        itemEl.style.display = visible ? '' : 'none';
        if (visible) visibleEventCount += 1;
      }});

      const emptyTip = document.getElementById('hlDetailEmpty');
      if (emptyTip) emptyTip.style.display = visibleEventCount > 0 ? 'none' : 'block';
    }}
    const hlFilters = [hlFilterKillType, hlFilterAdj, hlFilterProc].filter(Boolean);
    function onHlFilterChange(changedEl) {{
      if (!changedEl) return;
      const selectedVal = changedEl.value || '';
      if (selectedVal) {{
        hlFilters.forEach((el) => {{
          if (el !== changedEl) el.value = '';
        }});
      }}
      applyHlDetailFilter();
    }}
    hlFilters.forEach((el) => {{
      el.addEventListener('change', () => onHlFilterChange(el));
    }});
    applyHlDetailFilter();

    // 折叠明细
    document.querySelectorAll('.acc-header').forEach(function(h) {{
      h.addEventListener('click', function() {{
        var target = document.getElementById(h.dataset.target);
        if (!target) return;
        var visible = target.style.display === 'block';
        target.style.display = visible ? 'none' : 'block';
      }});
    }});
  </script>
</body>
</html>
"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)


def _normalize_app_list(items: List[str]) -> List[str]:
    apps = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        pkg = _base_name(item.strip())
        if not _looks_like_package(pkg):
            continue
        if pkg in seen:
            continue
        seen.add(pkg)
        apps.append(pkg)
    return apps


def _parse_heatmap_app_list_spec(spec: str) -> List[str]:
    """
    解析热力图 app 列表输入：
    - 逗号/空白分隔包名字符串
    - 文件路径（.txt/.csv/.json）
    """
    text = (spec or "").strip()
    if not text:
        return []

    if len(text) > 1:
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()

    def _split_plain(raw: str) -> List[str]:
        return [x for x in re.split(r"[\s,;]+", raw) if x]

    if os.path.isfile(text):
        lower = text.lower()
        if lower.endswith(".json"):
            try:
                with open(text, "r", encoding="utf-8") as fp:
                    obj = json.load(fp)
                if isinstance(obj, list):
                    return _normalize_app_list([str(x) for x in obj])
                if isinstance(obj, dict):
                    keys = [
                        "apps",
                        "app_list",
                        "list",
                        "highlight",
                        "HighLight",
                        "HIGHLIGHT",
                        "startup_sequence",
                        "cont_startup_sequence",
                        "连续启动顺序",
                        "连续启动列表",
                    ]
                    for key in keys:
                        val = obj.get(key)
                        if isinstance(val, list):
                            return _normalize_app_list([str(x) for x in val])
                    merged = []
                    for val in obj.values():
                        if isinstance(val, list):
                            merged.extend(str(x) for x in val)
                    return _normalize_app_list(merged)
            except Exception:
                return []

        try:
            with open(text, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read()
            return _normalize_app_list(_split_plain(content))
        except Exception:
            return []

    return _normalize_app_list(_split_plain(text))


def _extract_named_app_lists_from_config(config_data: Optional[dict] = None) -> List[Tuple[str, List[str]]]:
    """从 app_config 顶层提取可选 app list（仅 list[str]）。"""
    data = config_data if config_data is not None else _load_app_config()
    if not isinstance(data, dict):
        return []
    options: List[Tuple[str, List[str]]] = []
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        apps = _normalize_app_list([str(x) for x in value])
        if apps:
            options.append((str(key), apps))
    return options


def _load_named_app_list_from_config(list_name: str) -> List[str]:
    for name, apps in _extract_named_app_lists_from_config():
        if name == list_name:
            return apps
    return []


def _prompt_select_app_list_from_config() -> Optional[Tuple[str, List[str]]]:
    options = _extract_named_app_lists_from_config()
    if not options:
        print("app_config.json 中未找到可用的应用列表。")
        return None

    print("\n请选择 app_config.json 中的应用列表（输入序号，0 取消）:")
    for idx, (name, apps) in enumerate(options, start=1):
        print(f"  {idx:>2}. {name} ({len(apps)}个)")

    while True:
        choice = input("请输入序号: ").strip()
        if choice in {"0", "q", "Q", "exit", "EXIT"}:
            return None
        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(options):
                return options[num - 1]
        print("输入无效，请重新输入。")


@contextmanager
def _temporary_highlight_processes(apps: Optional[List[str]]):
    """
    临时覆盖全局高亮主进程列表，使整份解析报告都按指定列表统计。
    """
    normalized = _normalize_app_list(apps or [])
    if not normalized:
        yield
        return

    global HIGHLIGHT_PROCESSES, TARGET_APPS
    old_highlight = list(HIGHLIGHT_PROCESSES)
    old_target = list(TARGET_APPS)
    try:
        HIGHLIGHT_PROCESSES = list(normalized)
        TARGET_APPS = list(normalized)
        yield
    finally:
        HIGHLIGHT_PROCESSES = old_highlight
        TARGET_APPS = old_target


def analyze_log_file(
    file_path: str,
    output_dir: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    output_name: Optional[str] = None,
    heatmap_apps: Optional[List[str]] = None,
    highlight_apps: Optional[List[str]] = None,
    auto_match_info: Optional[dict] = None,
    include_startup_section: bool = True,
) -> str:
    """
    无需交互地解析指定日志文件（支持 .txt 或 .zip bugreport），返回生成的报告路径。
    output_dir 为空时沿用 state.FILE_DIR 或当前目录。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"日志文件不存在: {file_path}")

    output_dir = output_dir or state.FILE_DIR or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    if not output_name:
        output_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_name
    output_file = os.path.join(output_dir, f"{base}.txt")
    output_file_html = os.path.join(output_dir, f"{base}.html")
    output_file_device_info = os.path.join(output_dir, f"{base}_device_info.txt")
    output_file_meminfo = os.path.join(output_dir, f"{base}_meminfo_summary.txt")

    effective_highlight = _normalize_app_list(highlight_apps or [])
    effective_heatmap = _normalize_app_list(heatmap_apps or effective_highlight)

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path

    try:
        resolved_file_path, cleanup_path, source_desc = _resolve_log_input_path(file_path)
        with _temporary_highlight_processes(effective_highlight):
            print(f"正在解析日志文件: {source_desc}...")
            events = parse_log_file(resolved_file_path, start_time=start_time, end_time=end_time)
            device_info = _extract_device_info_from_bugreport(resolved_file_path)
            auto_match_payload = _to_plain(auto_match_info or {})
            if auto_match_payload:
                device_info = dict(device_info or {})
                device_info["auto_match_info"] = auto_match_payload
            meminfo_bundle = _build_meminfo_summary_bundle(resolved_file_path, source_desc)
            print(f"解析完成，共发现 {len(events)} 个事件")

            print(f"正在生成报告: {output_file}...")
            generate_report(events, output_file)
            summary = compute_summary_data(events)
            print(f"正在生成可视化报告: {output_file_html}...")
            generate_report_html(
                events,
                summary,
                output_file_html,
                heatmap_apps=effective_heatmap,
                device_info=device_info,
                meminfo_bundle=meminfo_bundle,
                include_startup_section=include_startup_section,
            )
            save_device_info_report_text(
                device_info,
                output_file_device_info,
                source_desc=source_desc,
            )
            report_txt = str(meminfo_bundle.get("report_txt", "") or "").strip()
            if report_txt:
                with open(output_file_meminfo, "w", encoding="utf-8") as f:
                    f.write(report_txt)
            print(f"报告生成成功: {os.path.abspath(output_file)}")
            print(f"HTML报告: {os.path.abspath(output_file_html)}")
            print(f"设备信息报告: {os.path.abspath(output_file_device_info)}")
            if report_txt:
                print(f"meminfo报告: {os.path.abspath(output_file_meminfo)}")
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    return output_file


def _select_bugreport_text_member(zip_path: str, zip_file: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [m for m in zip_file.infolist() if not m.is_dir()]
    if not members:
        raise ValueError(f"压缩包为空: {zip_path}")

    def _pick(candidates: List[zipfile.ZipInfo]) -> Optional[zipfile.ZipInfo]:
        if not candidates:
            return None
        return sorted(candidates, key=lambda x: (-x.file_size, x.filename.lower()))[0]

    bugreport_txt = [
        m for m in members
        if "bugreport" in m.filename.lower() and m.filename.lower().endswith(".txt")
    ]
    picked = _pick(bugreport_txt)
    if picked:
        return picked

    bugreport_any = [m for m in members if "bugreport" in m.filename.lower()]
    picked = _pick(bugreport_any)
    if picked:
        return picked

    txt_any = [m for m in members if m.filename.lower().endswith(".txt")]
    picked = _pick(txt_any)
    if picked:
        return picked

    raise ValueError(
        f"压缩包中未找到可解析的日志文件（优先匹配包含 bugreport 的 .txt）: {zip_path}"
    )


def _resolve_log_input_path(file_path: str) -> Tuple[str, Optional[str], str]:
    """
    解析输入路径：
    - 普通文件：直接返回原路径
    - .zip：自动定位 bugreport 文本并解压到临时文件后返回
    返回：(可解析文件路径, 清理路径, 展示描述)
    """
    if not file_path.lower().endswith(".zip"):
        return file_path, None, file_path

    temp_path = None
    try:
        with zipfile.ZipFile(file_path, "r") as zip_file:
            member = _select_bugreport_text_member(file_path, zip_file)
            fd, temp_path = tempfile.mkstemp(prefix="collie_bugreport_", suffix=".txt")
            os.close(fd)
            with zip_file.open(member, "r") as src, open(temp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        raise ValueError(f"无效的 zip 文件: {file_path}") from e
    except OSError as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise ValueError(f"读取 zip 文件失败: {file_path}，原因: {e}") from e

    source_desc = f"{file_path} -> {member.filename}"
    return temp_path, temp_path, source_desc


def _extract_property_value_from_line(line: str, prop_name: str) -> Optional[str]:
    escaped = re.escape(prop_name)
    patterns = [
        rf"\[\s*{escaped}\s*\]\s*:\s*\[(?P<value>[^\]]+)\]",
        rf"\b{escaped}\b\s*[:=]\s*(?P<value>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if not match:
            continue
        value = (match.group("value") or "").strip().strip('"').strip("'")
        if value:
            return value
    return None


def _extract_device_info_from_bugreport(file_path: str, max_proc_mv_lines: int = 24) -> dict:
    """
    从 bugreport 文本提取设备关键字段：
    - Build fingerprint
    - ro.product.device / ro.board.platform
    - /proc/meminfo 的 MemTotal / SwapTotal
    - Linux version
    - /proc/mv（截取首段）
    """
    info = {
        "build_fingerprint": "",
        "ro_product_device": "",
        "ro_board_platform": "",
        "mem_total": "",
        "swap_total": "",
        "linux_version": "",
        "proc_mv": "",
    }

    if not file_path or not os.path.isfile(file_path):
        return info

    in_proc_meminfo = False
    mem_total_fallback = ""
    swap_total_fallback = ""
    in_proc_mv_section = False
    proc_mv_lines: List[str] = []

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()

            if not info["build_fingerprint"]:
                match = re.search(r"Build fingerprint:\s*(?P<value>.+)$", stripped, re.IGNORECASE)
                if match:
                    info["build_fingerprint"] = (match.group("value") or "").strip()
                else:
                    info["build_fingerprint"] = _extract_property_value_from_line(
                        stripped,
                        "ro.build.fingerprint",
                    ) or ""

            if not info["ro_product_device"]:
                info["ro_product_device"] = _extract_property_value_from_line(stripped, "ro.product.device") or ""

            if not info["ro_board_platform"]:
                info["ro_board_platform"] = _extract_property_value_from_line(stripped, "ro.board.platform") or ""

            if not info["linux_version"]:
                match = re.search(r"(Linux version\s+\S.*)$", stripped, re.IGNORECASE)
                if match:
                    info["linux_version"] = (match.group(1) or "").strip()

            if "/proc/meminfo" in lower:
                in_proc_meminfo = True
            elif in_proc_meminfo and stripped.startswith("------") and "/proc/meminfo" not in lower:
                in_proc_meminfo = False

            mem_match = re.match(r"MemTotal:\s*(?P<value>.+)$", stripped, re.IGNORECASE)
            if mem_match:
                if in_proc_meminfo and not info["mem_total"]:
                    info["mem_total"] = (mem_match.group("value") or "").strip()
                elif not mem_total_fallback:
                    mem_total_fallback = (mem_match.group("value") or "").strip()

            swap_match = re.match(r"SwapTotal:\s*(?P<value>.+)$", stripped, re.IGNORECASE)
            if swap_match:
                if in_proc_meminfo and not info["swap_total"]:
                    info["swap_total"] = (swap_match.group("value") or "").strip()
                elif not swap_total_fallback:
                    swap_total_fallback = (swap_match.group("value") or "").strip()

            if "proc/mv" in lower and not proc_mv_lines:
                proc_mv_lines.append(stripped)
                in_proc_mv_section = stripped.startswith("------")
                continue

            if in_proc_mv_section:
                if stripped.startswith("------") and "proc/mv" not in lower:
                    in_proc_mv_section = False
                else:
                    proc_mv_lines.append(stripped)
                    if len(proc_mv_lines) >= max_proc_mv_lines:
                        in_proc_mv_section = False

            if (
                info["build_fingerprint"]
                and info["ro_product_device"]
                and info["ro_board_platform"]
                and info["mem_total"]
                and info["swap_total"]
                and info["linux_version"]
                and proc_mv_lines
                and not in_proc_mv_section
            ):
                break

    if not info["mem_total"]:
        info["mem_total"] = mem_total_fallback
    if not info["swap_total"]:
        info["swap_total"] = swap_total_fallback
    if proc_mv_lines:
        info["proc_mv"] = "\n".join(proc_mv_lines)
    return info


def _parse_kb_value(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return None
    text = str(value)
    match = re.search(r"([0-9][0-9,]*)\s*[Kk]\b", text)
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except Exception:
            return None
    match = re.search(r"([0-9][0-9,]*)", text)
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except Exception:
            return None
    return None


def _shorten_label(text: str, max_len: int = 28) -> str:
    value = str(text or "").strip()
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return value[: max_len - 3] + "..."


def _build_meminfo_summary_bundle(file_path: str, source_desc: str) -> dict:
    """
    复用 collie_package.utilities.meminfo_summary 的解析结构：
    - 生成 meminfo 汇总 txt 内容
    - 生成 HTML 图表所需结构化数据
    """
    bundle = {
        "available": False,
        "source_desc": source_desc,
        "error": "",
        "report_txt": "",
        "total_proc": {},
        "oom_categories": [],
        "oom_by_priority": [],
        "priority_groups": {},
        "chart_top_process": {},
        "chart_oom": {},
        "chart_priority": {},
        "top20_ratio": 0.0,
    }

    if not file_path or not os.path.isfile(file_path):
        bundle["error"] = "输入文件不存在"
        return bundle

    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from ..utilities import meminfo_summary
    except Exception as exc:
        bundle["error"] = f"导入 meminfo_summary 失败: {exc}"
        return bundle

    try:
        raw_meminfo_text = meminfo_summary._load_meminfo_from_file(file_path)
        total_proc = meminfo_summary._parse_total_pss_by_process(raw_meminfo_text)
        oom_categories = meminfo_summary._parse_pss_by_oom(raw_meminfo_text)
        report_txt = meminfo_summary.generate_report(raw_meminfo_text, source_desc)
    except Exception as exc:
        bundle["error"] = f"解析 meminfo 失败: {exc}"
        return bundle

    priority_key_map = {
        "necessary": "必要",
        "high": "高优先级",
        "low": "低优先级",
        "other": "其它",
    }
    priority_groups = {v: {"pss_kb": 0, "count": 0} for v in priority_key_map.values()}
    priority_rank = {"necessary": 0, "high": 1, "low": 2, "other": 3}
    oom_by_priority = []
    for cat in oom_categories:
        key = str(meminfo_summary._classify_priority(cat.get("name", "")) or "other")
        group = priority_key_map.get(key, "其它")
        priority_groups[group]["pss_kb"] += int(cat.get("total_pss_kb", 0) or 0)
        priority_groups[group]["count"] += int(cat.get("process_count", 0) or 0)
        oom_by_priority.append(
            {
                "priority_key": key,
                "priority_label": group,
                "name": str(cat.get("name", "") or ""),
                "process_count": int(cat.get("process_count", 0) or 0),
                "total_pss_kb": int(cat.get("total_pss_kb", 0) or 0),
                "swap_kb": cat.get("swap_kb", None),
            }
        )

    oom_by_priority.sort(
        key=lambda x: (
            priority_rank.get(str(x.get("priority_key", "other")), 99),
            -int(x.get("total_pss_kb", 0) or 0),
            str(x.get("name", "")),
        )
    )

    top_proc_chart = {}
    for proc in (total_proc.get("processes", []) or [])[:20]:
        name = _shorten_label(proc.get("name", ""), 24)
        top_proc_chart[name] = int(proc.get("pss_kb", 0) or 0)

    oom_chart = {}
    for cat in oom_by_priority[:10]:
        name = _shorten_label(cat.get("name", ""), 24)
        oom_chart[name] = int(cat.get("total_pss_kb", 0) or 0)

    priority_chart = {
        label: int(data.get("pss_kb", 0) or 0)
        for label, data in priority_groups.items()
        if int(data.get("pss_kb", 0) or 0) > 0
    }

    top20_sum = sum(int((p or {}).get("pss_kb", 0) or 0) for p in (total_proc.get("processes", []) or [])[:20])
    total_pss = int(total_proc.get("total_pss_kb", 0) or 0)
    top20_ratio = (top20_sum / total_pss) if total_pss else 0.0

    bundle.update(
        {
            "report_txt": report_txt,
            "total_proc": total_proc,
            "oom_categories": oom_categories,
            "oom_by_priority": oom_by_priority,
            "priority_groups": priority_groups,
            "chart_top_process": top_proc_chart,
            "chart_oom": oom_chart,
            "chart_priority": priority_chart,
            "top20_ratio": top20_ratio,
        }
    )
    bundle["available"] = bool(
        top_proc_chart
        or oom_chart
        or priority_chart
        or report_txt
    )
    return bundle


def extract_bugreport_device_info(file_path: str, max_proc_mv_lines: int = 24) -> Tuple[dict, str]:
    """
    独立提取设备信息（支持 .txt / .zip bugreport）。
    返回: (device_info, source_desc)
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"日志文件不存在: {file_path}")

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path
    try:
        resolved_file_path, cleanup_path, source_desc = _resolve_log_input_path(file_path)
        info = _extract_device_info_from_bugreport(
            resolved_file_path,
            max_proc_mv_lines=max_proc_mv_lines,
        )
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
    return info, source_desc


def format_device_info_report_text(device_info: dict, source_desc: str = "") -> str:
    """将设备信息格式化为可读文本。"""
    info = _to_plain(device_info or {})

    def _v(key: str) -> str:
        value = str(info.get(key, "") or "").strip()
        return value if value else "-"

    lines = [
        "=" * 40 + " 设备信息 " + "=" * 40,
    ]
    if source_desc:
        lines.append(f"来源: {source_desc}")
    lines.extend(
        [
            f"Build fingerprint: {_v('build_fingerprint')}",
            f"ro.product.device: {_v('ro_product_device')}",
            f"ro.board.platform: {_v('ro_board_platform')}",
            f"/proc/meminfo MemTotal: {_v('mem_total')}",
            f"/proc/meminfo SwapTotal: {_v('swap_total')}",
            f"Linux version: {_v('linux_version')}",
            "",
            "/proc/mv:",
            str(info.get("proc_mv", "") or "未匹配到 /proc/mv 内容").strip() or "未匹配到 /proc/mv 内容",
            "=" * 92,
        ]
    )
    return "\n".join(lines)


def save_device_info_report_text(device_info: dict, output_file: str, source_desc: str = "") -> str:
    """保存设备信息文本到指定文件。"""
    content = format_device_info_report_text(device_info, source_desc=source_desc)
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(content)
    return output_file


def export_bugreport_device_info(file_path: str, output_file: str, max_proc_mv_lines: int = 24) -> str:
    """
    一键导出设备信息到 txt（独立方法）：
    - 输入：bugreport txt 或 zip
    - 输出：设备信息文本路径
    """
    info, source_desc = extract_bugreport_device_info(
        file_path,
        max_proc_mv_lines=max_proc_mv_lines,
    )
    return save_device_info_report_text(info, output_file, source_desc=source_desc)


def _match_last_expected_start_sequence(
    start_events: List[dict],
    expected_pkgs: List[str],
    max_inter_gap_sec: int = 300,
) -> Optional[List[int]]:
    """
    从末尾回溯，匹配 expected_pkgs 的最后一段有序子序列。
    返回匹配到的 start_events 索引列表（升序）。
    """
    if not start_events or not expected_pkgs:
        return None

    cursor = len(start_events) - 1
    later_time = None
    matched_rev: List[int] = []

    for expected in reversed(expected_pkgs):
        found_idx = None
        while cursor >= 0:
            rec = start_events[cursor]
            if rec["pkg"] != expected:
                cursor -= 1
                continue

            if later_time is not None:
                gap = (later_time - rec["time"]).total_seconds()
                if gap > max_inter_gap_sec:
                    return None

            found_idx = cursor
            matched_rev.append(cursor)
            later_time = rec["time"]
            cursor -= 1
            break

        if found_idx is None:
            return None

    matched_rev.reverse()
    return matched_rev


def _extract_bugreport_datetime_hint(text: str) -> Optional[datetime]:
    """
    从文件名/路径中提取 bugreport 时间戳，支持:
    - YYYY-MM-DD-HH-MM-SS
    - YYYY-MM-DD_HH-MM-SS
    """
    if not text:
        return None
    candidates = [
        r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[-_](?P<h>\d{2})-(?P<mi>\d{2})-(?P<s>\d{2})",
    ]
    for pattern in candidates:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return datetime(
                int(match.group("y")),
                int(match.group("m")),
                int(match.group("d")),
                int(match.group("h")),
                int(match.group("mi")),
                int(match.group("s")),
            )
        except Exception:
            continue
    return None


def _strip_wrapped_quotes(text: str) -> str:
    value = (text or "").strip()
    if len(value) > 1:
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1].strip()
    return value


def _sanitize_output_name(text: str, fallback: str = "report") -> str:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return cleaned or fallback


def _guess_output_name_from_log_path(file_path: str, fallback_idx: int = 1) -> str:
    """
    默认输出名规则：
    - 优先从 bugreport 文件名中提取设备名 + 时间：{device}_{MM-DD-HH-MM-SS}
    - 失败时回退到 stem + 当前时间
    """
    basename = os.path.basename(file_path or "")
    stem, _ = os.path.splitext(basename)
    now_part = datetime.now().strftime("%m-%d-%H-%M-%S")

    dt_hint = _extract_bugreport_datetime_hint(basename) or _extract_bugreport_datetime_hint(stem)
    time_part = dt_hint.strftime("%m-%d-%H-%M-%S") if dt_hint else now_part

    device = ""
    m = re.search(r"bugreport[-_](?P<name>[A-Za-z0-9._-]+?)(?:[-_][A-Z]{2,}[A-Za-z0-9._-]*)?(?:[-_]\d{4}-\d{2}-\d{2})", stem)
    if m:
        device = m.group("name")
    else:
        m2 = re.search(r"bugreport[-_](?P<name>[A-Za-z0-9._-]+)", stem)
        if m2:
            device = m2.group("name")
    if not device:
        parts = [p for p in re.split(r"[-_]+", stem) if p]
        if parts:
            if parts[0].lower() == "bugreport" and len(parts) > 1:
                device = parts[1]
            else:
                device = parts[0]
    device = _sanitize_output_name(device, fallback=f"file{fallback_idx}")
    return f"{device}_{time_part}"


def _parse_input_file_paths(raw_input: str) -> List[str]:
    """
    解析用户输入的文件路径，支持：
    - 单文件
    - 逗号/分号/换行分隔的两个文件
    """
    raw = (raw_input or "").strip()
    if not raw:
        return []
    items = []
    for seg in re.split(r"[,;\n]+", raw):
        val = _strip_wrapped_quotes(seg)
        if val:
            items.append(val)
    return items


def _format_file_size(size_bytes: int) -> str:
    size = float(max(0, int(size_bytes or 0)))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _extract_wm_launches_from_file(file_path: str, year_hint: Optional[int] = None) -> List[dict]:
    """仅提取 wm_set_resumed_activity 启动锚点（app->home->app），用于精准序列匹配。"""
    wm_resumed_events = []
    current_year = int(year_hint or datetime.now().year)
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            wm_resumed_match = WM_RESUMED_PATTERN.search(line)
            if not wm_resumed_match:
                continue
            ts = wm_resumed_match.group("ts")
            ts_obj = _parse_ts(ts, current_year) or datetime.now()
            component, reason = _parse_wm_resumed_payload(wm_resumed_match.group("payload"))
            process_name = component.split("/")[0] if "/" in component else component
            wm_resumed_events.append(
                {
                    "time": ts_obj,
                    "process_name": process_name,
                    "component": component,
                    "reason": reason,
                    "raw": line,
                }
            )
    return _build_wm_launches(wm_resumed_events)


def _lcs_length(a: List[str], b: List[str]) -> int:
    """计算两个序列的 LCS 长度（保持顺序）。"""
    if not a or not b:
        return 0
    n = len(b)
    dp = [0] * (n + 1)
    for x in a:
        prev = 0
        for j in range(1, n + 1):
            tmp = dp[j]
            if x == b[j - 1]:
                dp[j] = prev + 1
            else:
                dp[j] = max(dp[j], dp[j - 1])
            prev = tmp
    return dp[n]


def _find_last_window_by_sequence_tolerance(
    launches: List[dict],
    expected_pkgs: List[str],
    tolerance: int = 2,
) -> Optional[dict]:
    """
    在 wm 启动序列中寻找“最后一个”与 expected 序列顺序一致且误差 <= tolerance 的窗口。
    误差定义：基于 LCS 的插入/缺失总数。
    """
    if not launches or not expected_pkgs:
        return None

    observed_pkgs = [rec["process_name"] for rec in launches]
    expected_len = len(expected_pkgs)
    min_len = max(1, expected_len - tolerance)
    max_len = min(len(observed_pkgs), expected_len + tolerance)

    for end_idx in range(len(observed_pkgs) - 1, -1, -1):
        local_best = None
        for win_len in range(max_len, min_len - 1, -1):
            start_idx = end_idx - win_len + 1
            if start_idx < 0:
                continue
            window = observed_pkgs[start_idx:end_idx + 1]
            lcs = _lcs_length(expected_pkgs, window)
            mismatch = (expected_len - lcs) + (len(window) - lcs)
            if mismatch > tolerance:
                continue

            precision = (lcs / len(window)) if window else 0.0
            recall = (lcs / expected_len) if expected_len else 0.0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
            candidate = {
                "start_idx": start_idx,
                "end_idx": end_idx,
                "observed_count": len(window),
                "matched_count": lcs,
                "mismatch_count": mismatch,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "window_sequence": window,
            }

            if local_best is None:
                local_best = candidate
                continue
            better = (
                candidate["mismatch_count"] < local_best["mismatch_count"]
                or (
                    candidate["mismatch_count"] == local_best["mismatch_count"]
                    and candidate["matched_count"] > local_best["matched_count"]
                )
                or (
                    candidate["mismatch_count"] == local_best["mismatch_count"]
                    and candidate["matched_count"] == local_best["matched_count"]
                    and candidate["f1"] > local_best["f1"]
                )
            )
            if better:
                local_best = candidate

        if local_best:
            return local_best

    return None


def detect_last_complete_cont_startup_window(
    file_path: str,
    target_apps: List[str],
    rounds: int = 2,
) -> Optional[dict]:
    """
    依据给定 app 顺序，从日志中定位“最后完整连续启动过程”时间段。
    - 仅基于 wm_set_resumed_activity 序列匹配
    - 顺序和数量一致，允许误差 <= 10（插入/缺失）
    """
    apps = _normalize_app_list(target_apps or [])
    if not apps:
        return None

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path
    try:
        resolved_file_path, cleanup_path, source_desc = _resolve_log_input_path(file_path)
        bugreport_ts_hint = _extract_bugreport_datetime_hint(source_desc)
        launches = _extract_wm_launches_from_file(
            resolved_file_path,
            year_hint=bugreport_ts_hint.year if bugreport_ts_hint else None,
        )
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    if not launches:
        return None

    rounds = max(1, int(rounds))
    # 允许最多 10 个意外启动/缺失，提升复杂场景下的窗口命中率。
    tolerance = 10
    expected_variants = [
        ("full_round_double", apps * rounds),
        ("per_app_double", [pkg for pkg in apps for _ in range(rounds)]),
    ]

    candidates: List[dict] = []
    for variant_name, expected_pkgs in expected_variants:
        matched = _find_last_window_by_sequence_tolerance(
            launches,
            expected_pkgs,
            tolerance=tolerance,
        )
        if not matched:
            continue
        candidates.append(
            {
                "variant": variant_name,
                "expected_pkgs": expected_pkgs,
                "match": matched,
            }
        )

    if not candidates:
        return None

    all_times = [rec.get("time") for rec in launches if isinstance(rec.get("time"), datetime)]
    if not all_times:
        return None
    min_time = min(all_times)
    max_time = max(all_times)

    for c in candidates:
        m = c["match"]
        c["window_start_time"] = launches[m["start_idx"]]["time"]
        c["window_end_time"] = launches[m["end_idx"]]["time"]

    candidates.sort(
        key=lambda x: (
            x["window_end_time"],
            -x["match"]["mismatch_count"],
            x["match"]["matched_count"],
            x["match"]["f1"],
        ),
        reverse=True,
    )
    best = candidates[0]
    match = best["match"]

    first_time = best["window_start_time"]
    last_time = best["window_end_time"]
    total_duration_sec = (last_time - first_time).total_seconds()
    max_total_sec = max(240, len(best["expected_pkgs"]) * 30)
    if total_duration_sec > max_total_sec:
        return None

    window_start = max(min_time, first_time - timedelta(seconds=5))
    window_end = min(max_time, last_time + timedelta(seconds=30))
    if window_end < last_time:
        window_end = last_time

    expected_count = len(best["expected_pkgs"])
    observed_count = match["observed_count"]
    matched_count = match["matched_count"]
    mismatch_count = match["mismatch_count"]
    match_score = int(round(max(match["f1"], 0.0) * 100))
    tail_gap_sec = max((max_time - last_time).total_seconds(), 0.0)
    bugreport_ts_hint = _extract_bugreport_datetime_hint(source_desc)
    bugreport_gap_sec = None
    if bugreport_ts_hint:
        bugreport_gap_sec = abs((bugreport_ts_hint - max_time).total_seconds())

    confidence = "LOW"
    if mismatch_count == 0 and match_score >= 95 and tail_gap_sec <= 180:
        confidence = "HIGH"
    elif mismatch_count <= 1 and match_score >= 85 and tail_gap_sec <= 600:
        confidence = "MEDIUM"

    return {
        "window_start": window_start,
        "window_end": window_end,
        "first_start_time": first_time,
        "last_start_time": last_time,
        "file_end_time": max_time,
        "tail_gap_sec": tail_gap_sec,
        "duration_sec": total_duration_sec,
        "app_count": len(apps),
        "rounds": rounds,
        "expected_count": expected_count,
        "observed_count": observed_count,
        "matched_start_count": matched_count,
        "mismatch_count": mismatch_count,
        "tolerance": tolerance,
        "precision": match["precision"],
        "recall": match["recall"],
        "f1": match["f1"],
        "match_score": match_score,
        "match_variant": best["variant"],
        "bugreport_time_hint": bugreport_ts_hint,
        "bugreport_to_log_end_gap_sec": bugreport_gap_sec,
        "confidence": confidence,
    }


def main(
    preset_apps: Optional[List[str]] = None,
    preset_label: str = "",
    lock_preset_apps: bool = False,
    allow_highlight_override: bool = True,
    enable_auto_window_detection: bool = True,
    include_startup_section: bool = True,
):
    def _normalize_partial_time(value: str) -> str:
        """
        规范化不完整时间输入：
        - 支持 MM-DD HH:MM:SS(.ms)
        - 秒仅 1 位时按“后补0”处理，例如 13:48:1 -> 13:48:10
        - 毫秒位不足 3 位时后补 0
        - 若只给到 HH:MM，则补成 HH:MM:00
        """
        m = re.match(
            r'^(?P<md>\d{2}-\d{2})\s+'
            r'(?P<hh>\d{2}):(?P<mm>\d{2})'
            r'(?::(?P<ss>\d{1,2})(?:\.(?P<ms>\d{1,6}))?)?$',
            value,
        )
        if not m:
            return value

        md = m.group("md")
        hh = m.group("hh")
        mm = m.group("mm")
        ss = m.group("ss")
        ms = m.group("ms")

        if ss is None:
            ss = "00"
        elif len(ss) == 1:
            ss = ss + "0"

        if ms is not None:
            ms = ms.ljust(3, "0")[:6]
            return f"{md} {hh}:{mm}:{ss}.{ms}"
        return f"{md} {hh}:{mm}:{ss}"

    def _prompt_time(label: str) -> Optional[datetime]:
        """提示用户输入时间字符串并转换为 datetime；空输入返回 None。"""
        current_year = datetime.now().year
        value = input(f"请输入{label} (格式: MM-DD HH:MM:SS.mmm，可不完整并自动后补0，回车不限制): ").strip()
        if not value:
            return None
        normalized = _normalize_partial_time(value)
        for fmt in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{current_year}-{normalized}", f"%Y-{fmt}")
            except ValueError:
                continue
        print(f"时间格式不正确（输入: {value}，规范化后: {normalized}），已忽略该时间限制。")
        return None

    def _prompt_time_filter() -> Tuple[Optional[datetime], Optional[datetime]]:
        apply_time_filter = input("是否按时间段过滤日志? 按Y启用，回车跳过: ").strip().lower() == "y"
        if not apply_time_filter:
            return None, None
        return _prompt_time("起始时间"), _prompt_time("结束时间")

    def _fmt_abs_time(dt: Optional[datetime]) -> str:
        if not dt:
            return "-"
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _prompt_input_file_paths() -> Optional[List[str]]:
        while True:
            raw = input("请输入日志文件路径（支持 .txt 或 bugreport .zip，可输入1~2个，用英文逗号分隔）: ").strip()
            if raw in {"q", "Q", "exit", "EXIT"}:
                return None
            paths = _parse_input_file_paths(raw)
            if not paths:
                print("错误：文件路径不能为空，请重新输入。")
                continue
            if len(paths) > 2:
                print("错误：最多同时解析2个文件，请重新输入。")
                continue

            normalized = []
            seen = set()
            invalid = []
            for p in paths:
                fp = _strip_wrapped_quotes(p)
                if not fp:
                    continue
                if fp in seen:
                    continue
                seen.add(fp)
                if not os.path.isfile(fp):
                    invalid.append(fp)
                    continue
                normalized.append(fp)

            if invalid:
                print("以下文件不存在，请重新输入：")
                for item in invalid:
                    print(f"  - {item}")
                continue
            if not normalized:
                print("错误：未找到有效文件，请重新输入。")
                continue
            return normalized

    file_paths = _prompt_input_file_paths()
    if not file_paths:
        return

    output_name_map = {}
    print("\n文件校验结果：")
    for idx, file_path in enumerate(file_paths, start=1):
        abs_path = os.path.abspath(file_path)
        size_text = _format_file_size(os.path.getsize(file_path))
        default_output_name = _guess_output_name_from_log_path(file_path, fallback_idx=idx)
        output_name_map[file_path] = default_output_name
        print(f"  [{idx}] {os.path.basename(file_path)}")
        print(f"      路径: {abs_path}")
        print(f"      大小: {size_text}")
        print(f"      默认输出名: {default_output_name}")

    for idx, file_path in enumerate(file_paths, start=1):
        default_name = output_name_map[file_path]
        custom = input(f"请输入文件[{idx}]输出名(回车默认 {default_name}): ").strip()
        if custom:
            output_name_map[file_path] = _sanitize_output_name(custom, fallback=default_name)

    heatmap_apps = _normalize_app_list(preset_apps or [])
    if not allow_highlight_override:
        heatmap_apps = []
        print("已使用默认 HighLight 列表。")
    elif lock_preset_apps and heatmap_apps:
        label = preset_label or "预设"
        print(f"已使用预设高亮主进程列表: {label} ({len(heatmap_apps)}个)")
    else:
        app_list_spec = input(
            "高亮主进程列表(可选，支持逗号分隔包名或 txt/json 文件路径；回车使用默认 HighLight): "
        ).strip()
        if app_list_spec:
            parsed = _parse_heatmap_app_list_spec(app_list_spec)
            if parsed:
                heatmap_apps = parsed
                print(f"高亮主进程列表已加载: {len(parsed)} 个应用")
            else:
                print("未解析到有效包名，已回退到默认 HighLight 列表。")
                heatmap_apps = []

    detect_apps = _normalize_app_list(heatmap_apps or list(HIGHLIGHT_PROCESSES))

    success_items = []
    failed_items = []

    for idx, file_path in enumerate(file_paths, start=1):
        print("\n" + "=" * 96)
        print(f"开始处理文件[{idx}/{len(file_paths)}]: {file_path}")
        start_time = end_time = None
        auto_window = None
        auto_match_info = {
            "enabled": bool(enable_auto_window_detection and detect_apps),
            "target_app_count": len(detect_apps),
            "rounds": 2 if (enable_auto_window_detection and detect_apps) else 0,
            "detected": False,
            "used": False,
            "status": "",
        }
        if not enable_auto_window_detection:
            auto_match_info["status"] = "当前模式未启用自动匹配"
        elif not detect_apps:
            auto_match_info["status"] = "自动匹配未执行（未提供可匹配应用列表）"
        if enable_auto_window_detection and detect_apps:
            print(
                f"正在自动定位最后完整连续启动过程（基于wm_set_resumed_activity，目标 {len(detect_apps)} 个应用 x 2次）..."
            )
            try:
                auto_window = detect_last_complete_cont_startup_window(file_path, detect_apps, rounds=2)
            except Exception as e:
                print(f"自动定位失败: {e}")
                auto_window = None
                auto_match_info["status"] = "自动定位失败"
                auto_match_info["detection_error"] = str(e)

        if auto_window:
            auto_match_info.update(
                {
                    "detected": True,
                    "window_start": _fmt_abs_time(auto_window.get("window_start")),
                    "window_end": _fmt_abs_time(auto_window.get("window_end")),
                    "match_score": auto_window.get("match_score"),
                    "matched_start_count": auto_window.get("matched_start_count"),
                    "expected_count": auto_window.get("expected_count"),
                    "mismatch_count": auto_window.get("mismatch_count"),
                    "tolerance": auto_window.get("tolerance"),
                    "observed_count": auto_window.get("observed_count"),
                    "match_variant": auto_window.get("match_variant"),
                    "duration_sec": auto_window.get("duration_sec"),
                    "tail_gap_sec": auto_window.get("tail_gap_sec"),
                    "confidence": auto_window.get("confidence"),
                    "file_end_time": _fmt_abs_time(auto_window.get("file_end_time")),
                    "bugreport_time_hint": _fmt_abs_time(auto_window.get("bugreport_time_hint")),
                    "bugreport_to_log_end_gap_sec": auto_window.get("bugreport_to_log_end_gap_sec"),
                }
            )
            print("\n已识别到候选时间段（请 double check）:")
            print(
                f"  过滤时间段: {_fmt_abs_time(auto_window['window_start'])} ~ {_fmt_abs_time(auto_window['window_end'])}"
            )
            print(
                f"  匹配度: {auto_window['match_score']}%  "
                f"(LCS匹配 {auto_window['matched_start_count']}/{auto_window['expected_count']}，"
                f"误差 {auto_window['mismatch_count']}，容差 <= {auto_window['tolerance']})"
            )
            print(
                f"  数量校验: 预期 {auto_window['expected_count']} 次, 实际窗口 {auto_window['observed_count']} 次, "
                f"匹配策略 {auto_window['match_variant']}"
            )
            print(
                f"  时序校验: 过程时长约 {auto_window['duration_sec']:.1f}s，"
                f"距日志末尾约 {auto_window['tail_gap_sec']:.1f}s，置信度 {auto_window['confidence']}"
            )
            print(f"  日志最晚时间: {_fmt_abs_time(auto_window['file_end_time'])}")
            bugreport_hint = auto_window.get("bugreport_time_hint")
            if bugreport_hint:
                print(f"  bugreport文件时间: {_fmt_abs_time(bugreport_hint)}")
                gap = auto_window.get("bugreport_to_log_end_gap_sec")
                if gap is not None:
                    print(f"  bugreport时间与日志最晚时间差: {gap:.1f}s")
            use_auto = input("该时间段是否正确？回车确认，输入N改为手动按时间段过滤: ").strip().lower()
            if use_auto in {"", "y", "yes"}:
                start_time = auto_window["window_start"]
                end_time = auto_window["window_end"]
                auto_match_info["used"] = True
                auto_match_info["status"] = "已识别并采用自动匹配时间段"
                print("已启用自动识别时间段。")
            else:
                start_time, end_time = _prompt_time_filter()
                auto_match_info["status"] = "已识别候选窗口，但改为手动时间段"
        elif enable_auto_window_detection:
            print("未识别到满足顺序/数量要求的完整测试窗口，将回退到手动时间段过滤。")
            start_time, end_time = _prompt_time_filter()
            if not auto_match_info.get("status"):
                auto_match_info["status"] = "未识别到满足顺序/数量要求的完整测试窗口"
        else:
            print("当前模式默认全量解析；如需限定时间段可手动输入。")
            start_time, end_time = _prompt_time_filter()

        auto_match_info["applied_start_time"] = _fmt_abs_time(start_time)
        auto_match_info["applied_end_time"] = _fmt_abs_time(end_time)

        try:
            output_file = analyze_log_file(
                file_path,
                start_time=start_time,
                end_time=end_time,
                output_name=output_name_map.get(file_path),
                heatmap_apps=heatmap_apps,
                highlight_apps=heatmap_apps,
                auto_match_info=auto_match_info,
                include_startup_section=include_startup_section,
            )
            success_items.append((file_path, output_file))
        except Exception as e:
            failed_items.append((file_path, str(e)))
            print(f"处理日志时出错: {e}")

    print("\n" + "=" * 96)
    if success_items:
        print("解析成功文件：")
        for idx, (src_path, out_txt) in enumerate(success_items, start=1):
            print(f"  [{idx}] {src_path}")
            print(f"       TXT: {out_txt}")
            print(f"       HTML: {os.path.splitext(out_txt)[0]}.html")
            print(f"       Device: {os.path.splitext(out_txt)[0]}_device_info.txt")
            print(f"       Meminfo: {os.path.splitext(out_txt)[0]}_meminfo_summary.txt")
        print("\n提示: 在支持ANSI颜色的终端中查看报告以获得最佳效果")
        print("     Windows PowerShell: Get-Content <report.txt> -Encoding UTF8")
        print("     Linux/macOS: cat <report.txt>")
    if failed_items:
        print("\n解析失败文件：")
        for idx, (src_path, err_text) in enumerate(failed_items, start=1):
            print(f"  [{idx}] {src_path}")
            print(f"       原因: {err_text}")

    if success_items and not failed_items:
        print("\n解析成功。")
    elif success_items and failed_items:
        print("\n部分文件解析成功。")
    else:
        print("\n未成功解析任何文件。")
    input("按回车返回菜单...")


def _fmt_event_time(event_time: Optional[datetime]) -> str:
    if not isinstance(event_time, datetime):
        return "-"
    return event_time.strftime("%m-%d %H:%M:%S.%f")[:-3]


def _extract_kill_reason(event: dict) -> str:
    etype = event.get("type")
    details = event.get("details", {}) or {}
    if etype == "kill":
        am_reason = str((details.get("am_kill") or {}).get("reason", "")).strip()
        if am_reason:
            return am_reason
        kill_info = details.get("kill_info", {}) or {}
        kill_type_desc = kill_info.get("killTypeDesc") or kill_info.get("killType") or "kill"
        min_score_desc = kill_info.get("minScoreDesc") or ""
        if min_score_desc:
            return f"{kill_type_desc} | {min_score_desc}"
        return str(kill_type_desc)
    if etype == "lmk":
        reason = str(details.get("reason") or details.get("kill_reason") or "").strip()
        if reason:
            return reason
        ki_list = details.get("killinfo") or []
        if ki_list:
            reason = str((ki_list[0].get("parsed_fields") or {}).get("kill_reason", "")).strip()
            if reason:
                return reason
    return "未知"


def _extract_event_mem_snapshot(event: dict) -> dict:
    snap = {}
    details = event.get("details", {}) or {}
    etype = event.get("type")

    if etype in ("kill", "trig"):
        mem = details.get("mem_info", {}) or {}
        snap["mem_free_kb"] = mem.get("memFree", "")
        snap["mem_avail_kb"] = mem.get("memAvail", "")
        snap["mem_file_kb"] = mem.get("memFile", "")
        snap["mem_anon_kb"] = mem.get("memAnon", "")
        snap["swap_free_kb"] = mem.get("memSwapFree", "")
        snap["cma_free_kb"] = mem.get("cmaFree", "")

    if etype == "lmk":
        snap["rss_kb"] = details.get("rss_kb", "")
        snap["adj"] = details.get("adj", "")
        snap["min_adj"] = details.get("min_adj", "")

    ki_list = details.get("killinfo") or []
    if ki_list:
        pf = (ki_list[0] or {}).get("parsed_fields", {}) or {}
        snap.setdefault("rss_kb", pf.get("rss_kb", ""))
        snap.setdefault("adj", pf.get("adj", ""))
        snap.setdefault("min_adj", pf.get("min_adj", ""))
        snap.setdefault("mem_total_kb", pf.get("mem_total_kb", ""))
        if not snap.get("mem_free_kb"):
            snap["mem_free_kb"] = pf.get("mem_free_kb", "")
        snap.setdefault("cached_kb", pf.get("cached_kb", ""))
        if not snap.get("swap_free_kb"):
            snap["swap_free_kb"] = pf.get("swap_free_kb", "")
        snap.setdefault("thrashing", pf.get("thrashing", ""))
        snap.setdefault("psi_mem_some", pf.get("psi_mem_some", ""))
        snap.setdefault("psi_mem_full", pf.get("psi_mem_full", ""))
        snap.setdefault("psi_io_some", pf.get("psi_io_some", ""))
        snap.setdefault("psi_io_full", pf.get("psi_io_full", ""))
        snap.setdefault("psi_cpu_some", pf.get("psi_cpu_some", ""))

        af = _safe_int(pf.get("active_file_kb"))
        inf = _safe_int(pf.get("inactive_file_kb"))
        if af is not None and inf is not None and not snap.get("mem_file_kb"):
            snap["mem_file_kb"] = str(af + inf)
        aa = _safe_int(pf.get("active_anon_kb"))
        ina = _safe_int(pf.get("inactive_anon_kb"))
        if aa is not None and ina is not None and not snap.get("mem_anon_kb"):
            snap["mem_anon_kb"] = str(aa + ina)

    return snap


def _format_mem_snapshot_lines(mem_snapshot: dict, indent: str = "  ") -> List[str]:
    fields = [
        ("mem_free_kb", "memFree"),
        ("mem_avail_kb", "memAvail"),
        ("mem_file_kb", "memFile"),
        ("mem_anon_kb", "memAnon"),
        ("swap_free_kb", "swapFree"),
        ("cma_free_kb", "cmaFree"),
        ("rss_kb", "rss"),
        ("adj", "adj"),
        ("min_adj", "min_adj"),
        ("cached_kb", "cached"),
        ("mem_total_kb", "memTotal"),
        ("thrashing", "thrashing"),
        ("psi_mem_some", "psi_mem_some"),
        ("psi_mem_full", "psi_mem_full"),
        ("psi_io_some", "psi_io_some"),
        ("psi_io_full", "psi_io_full"),
        ("psi_cpu_some", "psi_cpu_some"),
    ]
    lines = []
    for key, label in fields:
        val = mem_snapshot.get(key, "")
        if val in ("", None):
            continue
        lines.append(f"{indent}{label:<12}: {val}")
    return lines


def _find_kill_candidates_for_package(events: List[dict], package_name: str) -> List[Tuple[int, dict]]:
    name = str(package_name or "").strip()
    if not name:
        return []
    exact_process = ":" in name

    candidates = []
    for idx, event in enumerate(events):
        etype = event.get("type")
        if etype not in ("kill", "lmk"):
            continue
        process_name = event.get("process_name", "") or ""
        full_name = event.get("full_name", process_name) or process_name
        base_name = _base_name(full_name or process_name)
        if exact_process:
            matched = (full_name == name) or (process_name == name)
        else:
            # 包名模式仅匹配主进程，避免子进程混入候选导致“多条被杀记录”选择噪声。
            if bool(event.get("is_subprocess")):
                continue
            matched = base_name == name
        if matched:
            candidates.append((idx, event))
    return candidates


def extract_kill_focus_bugreport_context(
    file_path: str,
    target_event: dict,
    before_lines: int = 300,
    after_lines: int = 100,
) -> dict:
    """
    提取目标被杀事件在原始 bugreport 中的上下文行（前300/后100可配置）。
    返回:
      {
        center_line_no, start_line_no, end_line_no, total_lines,
        before_lines, after_lines, matched_by, context_text
      }
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"日志文件不存在: {file_path}")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    total = len(lines)
    result = {
        "center_line_no": None,
        "start_line_no": None,
        "end_line_no": None,
        "total_lines": total,
        "before_lines": int(max(0, before_lines)),
        "after_lines": int(max(0, after_lines)),
        "matched_by": "",
        "context_text": "",
    }
    if not lines:
        return result

    event_raw = str(target_event.get("raw") or "").strip()
    process_name = str(target_event.get("process_name") or "").strip()
    full_name = str(target_event.get("full_name") or process_name).strip()
    base_name = _base_name(full_name or process_name)
    event_type = str(target_event.get("type") or "").strip().lower()
    evt_time = target_event.get("time")
    ts_token = evt_time.strftime("%m-%d %H:%M:%S") if isinstance(evt_time, datetime) else ""

    center_idx = -1

    if event_raw:
        for idx, line in enumerate(lines):
            line_stripped = line.strip()
            if line_stripped == event_raw or event_raw in line:
                center_idx = idx
                result["matched_by"] = "raw"
                break

    type_keywords = []
    if event_type == "lmk":
        type_keywords = ["lowmemorykiller"]
    elif event_type == "kill":
        type_keywords = ["am_kill", "killinfo"]
    else:
        type_keywords = ["am_kill", "killinfo", "lowmemorykiller"]

    if center_idx < 0:
        for idx, line in enumerate(lines):
            line_l = line.lower()
            if ts_token and ts_token not in line:
                continue
            if base_name and (base_name not in line) and (process_name and process_name not in line):
                continue
            if type_keywords and not any(k in line_l for k in type_keywords):
                continue
            center_idx = idx
            result["matched_by"] = "ts+proc+type"
            break

    if center_idx < 0 and ts_token:
        for idx, line in enumerate(lines):
            if ts_token not in line:
                continue
            if base_name and base_name not in line:
                continue
            center_idx = idx
            result["matched_by"] = "ts+proc"
            break

    if center_idx < 0:
        return result

    start_idx = max(0, center_idx - int(max(0, before_lines)))
    end_idx = min(total - 1, center_idx + int(max(0, after_lines)))
    context_text = "".join(lines[start_idx : end_idx + 1])

    result.update(
        {
            "center_line_no": center_idx + 1,
            "start_line_no": start_idx + 1,
            "end_line_no": end_idx + 1,
            "context_text": context_text,
        }
    )
    return result


def format_kill_focus_bugreport_context(context: dict, source_desc: str = "") -> str:
    lines = []
    lines.append("=" * 32 + " 被杀时刻原始Bugreport片段 " + "=" * 32)
    if source_desc:
        lines.append(f"来源日志: {source_desc}")
    total = context.get("total_lines")
    if total:
        lines.append(f"日志总行数: {total}")

    center = context.get("center_line_no")
    start = context.get("start_line_no")
    end = context.get("end_line_no")
    before_lines = context.get("before_lines", 300)
    after_lines = context.get("after_lines", 100)
    matched_by = context.get("matched_by", "")

    if not center:
        lines.append("未能在原始日志中精确定位目标被杀事件，未生成上下文片段。")
        lines.append("=" * 92)
        return "\n".join(lines)

    lines.append(
        f"定位结果: 命中行 L{center}（匹配方式: {matched_by or '-'}）"
    )
    lines.append(
        f"提取范围: L{start} ~ L{end} （前{before_lines}行 + 后{after_lines}行）"
    )
    lines.append("-" * 92)

    raw = str(context.get("context_text") or "")
    if raw:
        for offset, raw_line in enumerate(raw.splitlines(), start=start):
            lines.append(f"L{offset:>7}: {raw_line}")
    else:
        lines.append("(空)")

    lines.append("-" * 92)
    lines.append("=" * 92)
    return "\n".join(lines)


def _select_kill_candidate_interactively(candidates: List[Tuple[int, dict]], package_name: str) -> Optional[Tuple[int, dict]]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    print(f"\n包名 {package_name} 共匹配到 {len(candidates)} 条被杀记录：")
    for i, (event_idx, event) in enumerate(candidates, start=1):
        proc = event.get("full_name", event.get("process_name", ""))
        reason = _extract_kill_reason(event)
        mem_free = _extract_event_mem_snapshot(event).get("mem_free_kb", "") or "-"
        print(
            f"  {i}. 事件#{event_idx + 1}  {_fmt_event_time(event.get('time'))}  "
            f"{event.get('type')}  {proc}  reason={reason}  memFree={mem_free}"
        )

    while True:
        choice = input("请输入序号选择目标事件（q 退出）: ").strip().lower()
        if choice in {"q", "quit", "exit"}:
            return None
        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(candidates):
                return candidates[num - 1]
        print("输入无效，请输入列表中的序号。")


def _collect_kill_events_in_window(
    events: List[dict],
    center_time: datetime,
    window_seconds: int = 60,
) -> Tuple[List[Tuple[int, dict]], datetime, datetime]:
    start_time = center_time - timedelta(seconds=window_seconds)
    end_time = center_time + timedelta(seconds=window_seconds)
    window_events = []
    for idx, event in enumerate(events):
        etype = event.get("type")
        if etype not in ("kill", "lmk"):
            continue
        evt_time = event.get("time")
        if not isinstance(evt_time, datetime):
            continue
        if start_time <= evt_time <= end_time:
            window_events.append((idx, event))
    return window_events, start_time, end_time


def _is_third_party_package(pkg: str) -> bool:
    base = _base_name(pkg)
    if not _looks_like_package(base):
        return False
    lower = base.lower()
    blocked_prefixes = (
        "android.",
        "com.android.",
        "com.google.android.",
        "com.miui.",
        "com.xiaomi.",
        "com.qualcomm.",
        "com.mediatek.",
        "org.codeaurora.",
    )
    return not any(lower.startswith(prefix) for prefix in blocked_prefixes)


def _is_highlight_third_party_package(pkg: str) -> bool:
    base = _base_name(pkg)
    return base in HIGHLIGHT_PROCESSES and _is_third_party_package(base)


def _collect_events_between(
    events: List[dict],
    start_time: datetime,
    end_time: datetime,
    event_types: Tuple[str, ...] = ("kill", "lmk"),
) -> List[Tuple[int, dict]]:
    items = []
    for idx, event in enumerate(events):
        if event.get("type") not in event_types:
            continue
        evt_time = event.get("time")
        if not isinstance(evt_time, datetime):
            continue
        if start_time <= evt_time <= end_time:
            items.append((idx, event))
    return items


def _calc_mem_stats_for_event_items(items: List[Tuple[int, dict]]) -> dict:
    mem_values = defaultdict(list)
    for _, event in items:
        metrics = _extract_mem_metrics(event)
        if not metrics:
            continue
        for metric_key in ("mem_free", "file_pages", "anon_pages", "swap_free"):
            val = metrics.get(metric_key)
            if val is not None:
                mem_values[metric_key].append(val)
    stats_map = {}
    for metric_key in ("mem_free", "file_pages", "anon_pages", "swap_free"):
        stats_map[metric_key] = _calc_stats(mem_values.get(metric_key, []))
    return {
        "event_count": len(items),
        "metric_stats": stats_map,
    }


def _determine_focus_window_by_third_party_starts(
    events: List[dict],
    ref_time: datetime,
    lookback_count: int = 20,
    max_window_seconds: int = 300,
) -> dict:
    window_floor = ref_time - timedelta(seconds=max_window_seconds)
    third_party_starts_in_5m: List[Tuple[int, dict]] = []

    for idx, event in enumerate(events):
        evt_time = event.get("time")
        if not isinstance(evt_time, datetime):
            continue
        if evt_time > ref_time or evt_time < window_floor:
            continue

        if event.get("type") == "start" and not event.get("is_subprocess"):
            base = _base_name(event.get("process_name", ""))
            if not _is_highlight_third_party_package(base):
                continue
            third_party_starts_in_5m.append((idx, event))

    if lookback_count > 0 and len(third_party_starts_in_5m) >= lookback_count:
        selected_starts = third_party_starts_in_5m[-lookback_count:]
        window_start = selected_starts[0][1].get("time") or window_floor
        selection_mode = "last_n_starts"
    else:
        selected_starts = list(third_party_starts_in_5m)
        window_start = window_floor
        selection_mode = "five_min_fallback"

    return {
        "window_start": window_start,
        "window_end": ref_time,
        "window_floor": window_floor,
        "selection_mode": selection_mode,
        "selected_starts": selected_starts,
        "starts_in_5m_count": len(third_party_starts_in_5m),
        "lookback_count": lookback_count,
        "max_window_seconds": max_window_seconds,
    }


def _infer_background_app_state(
    recent_starts: List[Tuple[int, dict]],
    events: List[dict],
    ref_time: datetime,
) -> Tuple[List[dict], List[str]]:
    kill_by_base = defaultdict(list)
    for idx, event in enumerate(events):
        if event.get("type") not in ("kill", "lmk") or event.get("is_subprocess"):
            continue
        evt_time = event.get("time")
        if not isinstance(evt_time, datetime) or evt_time > ref_time:
            continue
        base = _base_name(event.get("process_name", ""))
        if not _looks_like_package(base):
            continue
        kill_by_base[base].append((idx, event))

    rows = []
    alive_latest = {}
    for seq, (start_idx, start_event) in enumerate(recent_starts, start=1):
        base = _base_name(start_event.get("process_name", ""))
        start_time = start_event.get("time")
        kill_hit = None
        for kill_idx, kill_event in kill_by_base.get(base, []):
            kill_time = kill_event.get("time")
            if not isinstance(kill_time, datetime):
                continue
            if kill_time >= start_time:
                kill_hit = (kill_idx, kill_event)
                break

        if kill_hit:
            status = "已被查杀"
            end_time = kill_hit[1].get("time")
            kill_event_id = kill_hit[0] + 1
        else:
            status = "存活(推测)"
            end_time = ref_time
            kill_event_id = None
            alive_latest[base] = start_event

        rows.append(
            {
                "seq": seq,
                "start_event_id": start_idx + 1,
                "process": base,
                "start_time": start_time,
                "end_time": end_time,
                "status": status,
                "start_source": (start_event.get("details", {}) or {}).get("launch_source", ""),
                "had_proc_start": bool((start_event.get("details", {}) or {}).get("had_proc_start")),
                "start_kind": (start_event.get("details", {}) or {}).get("start_kind", ""),
                "kill_event_id": kill_event_id,
            }
        )

    alive_apps = sorted(alive_latest.keys())
    return rows, alive_apps


def build_kill_focus_report(
    events: List[dict],
    source_desc: str,
    package_name: str,
    target_event_idx: int,
    target_event: dict,
    window_seconds: int = 60,
    lookback_starts: int = 20,
) -> str:
    target_time = target_event.get("time")
    kill_time = target_time if isinstance(target_time, datetime) else None
    check_time = (kill_time - timedelta(seconds=30)) if kill_time else None
    proc = target_event.get("full_name", target_event.get("process_name", ""))
    reason = _extract_kill_reason(target_event)
    target_mem = _extract_event_mem_snapshot(target_event)
    pid = ""
    if target_event.get("type") == "kill":
        pid = str((target_event.get("details", {}) or {}).get("proc_info", {}).get("pid", ""))
    elif target_event.get("type") == "lmk":
        pid = str((target_event.get("details", {}) or {}).get("pid", ""))

    lines = []
    lines.append("=" * 32 + " 指定进程被杀时刻分析 " + "=" * 32)
    lines.append(f"输入日志: {source_desc}")
    lines.append(f"目标包名: {package_name}")
    lines.append("")

    lines.append("1) 指定进程被查杀详情")
    lines.append(f"  目标事件: #{target_event_idx + 1}")
    lines.append(f"  被杀时刻: {_fmt_event_time(kill_time)}")
    lines.append(f"  查杀时刻(估算): {_fmt_event_time(check_time)}  (被杀前30s)")
    lines.append(f"  事件类型: {target_event.get('type')}")
    lines.append(f"  进程: {proc}")
    lines.append(f"  PID: {pid or '-'}")
    lines.append(f"  是否子进程: {'是' if target_event.get('is_subprocess') else '否'}")
    lines.append(f"  查杀原因: {reason}")
    mem_lines = _format_mem_snapshot_lines(target_mem, indent="  ")
    if mem_lines:
        lines.append("  当前内存状态:")
        lines.extend(mem_lines)
    else:
        lines.append("  当前内存状态: 未命中可用字段")

    if not kill_time:
        lines.append("")
        lines.append("目标事件时间异常，无法继续构建前后窗口分析。")
        lines.append("=" * 88)
        return "\n".join(lines)

    window_events, win_start, win_end = _collect_kill_events_in_window(
        events,
        check_time,
        window_seconds=window_seconds,
    )
    lines.append("")
    lines.append("2) 查杀时刻前后一分钟查杀信息与内存")
    lines.append(f"  时间窗口: {_fmt_event_time(win_start)} ~ {_fmt_event_time(win_end)}")
    lines.append(f"  查杀事件数(kill+lmk): {len(window_events)}")

    for idx, event in window_events:
        proc_name = event.get("full_name", event.get("process_name", ""))
        mem_snapshot = _extract_event_mem_snapshot(event)
        mem_free = mem_snapshot.get("mem_free_kb", "") or "-"
        evt_reason = _extract_kill_reason(event)
        lines.append(
            f"  - #{idx + 1} {_fmt_event_time(event.get('time'))} "
            f"{event.get('type')} {proc_name} reason={evt_reason} memFree={mem_free}"
        )

    lines.append("  被杀前窗口内存统计(KB，窗口终点=被杀时刻):")
    metric_label = {"mem_free": "memFree", "file_pages": "memFile", "anon_pages": "memAnon", "swap_free": "swapFree"}
    for sec in (60, 30, 15, 5):
        seg_start = kill_time - timedelta(seconds=sec)
        seg_items = _collect_events_between(events, seg_start, kill_time, event_types=("kill", "lmk"))
        seg_stats = _calc_mem_stats_for_event_items(seg_items)
        lines.append(
            f"    窗口 {sec:>2}s: {_fmt_event_time(seg_start)} ~ {_fmt_event_time(kill_time)}  "
            f"事件数={seg_stats['event_count']}"
        )
        has_metric = False
        for metric_key in ("mem_free", "file_pages", "anon_pages", "swap_free"):
            stats = seg_stats["metric_stats"][metric_key]
            if stats["count"] == 0:
                continue
            has_metric = True
            lines.append(
                f"      {metric_label[metric_key]:<8} avg={stats['avg']:.1f} p50={stats['median']:.1f} "
                f"p95={stats['p95']:.1f} min={stats['min']:.1f} max={stats['max']:.1f}"
            )
        if not has_metric:
            lines.append("      无可用内存样本")

    focus_window = _determine_focus_window_by_third_party_starts(
        events,
        kill_time,
        lookback_count=lookback_starts,
        max_window_seconds=300,
    )
    focus_start = focus_window["window_start"]
    focus_end = focus_window["window_end"]
    infer_starts = list(focus_window.get("selected_starts") or [])
    kills_in_focus_all = _collect_events_between(events, focus_start, focus_end, event_types=("kill", "lmk"))
    kills_in_focus_highlight_main = [
        pair for pair in kills_in_focus_all
        if (not pair[1].get("is_subprocess"))
        and _is_highlight_third_party_package(_base_name((pair[1] or {}).get("process_name", "")))
    ]
    starts_in_focus_all_main = [
        pair for pair in _collect_events_between(events, focus_start, focus_end, event_types=("start",))
        if (not pair[1].get("is_subprocess"))
        and _looks_like_package(_base_name((pair[1] or {}).get("process_name", "")))
    ]

    bg_rows, alive_apps = _infer_background_app_state(
        recent_starts=infer_starts,
        events=events,
        ref_time=kill_time,
    )
    lines.append("")
    lines.append("3) 被杀时刻后台三方占用推测（前序20条高亮三方主进程启动为准）")
    lines.append("  判定方法: 先用高亮三方主进程启动确定窗口，再观察该窗口查杀，最终存活计算仅使用这批高亮三方启动")
    lines.append(f"  计算窗口: {_fmt_event_time(focus_start)} ~ {_fmt_event_time(focus_end)}")
    if focus_window.get("selection_mode") == "last_n_starts":
        lines.append(
            f"  窗口确定方式: 取最近{lookback_starts}条高亮三方主进程启动（均在被杀前5分钟内）"
        )
    else:
        lines.append(
            f"  窗口确定方式: 被杀前5分钟内高亮三方启动不足{lookback_starts}条"
            f"（实际{focus_window.get('starts_in_5m_count', 0)}条），按5分钟窗口统计"
        )
    lines.append(f"  启动样本(最终计算基准，高亮三方): {len(infer_starts)}")
    lines.append(f"  窗口内查杀(全量 kill+lmk): {len(kills_in_focus_all)}")
    lines.append(f"  窗口内查杀(高亮三方主进程 kill+lmk): {len(kills_in_focus_highlight_main)}")
    lines.append(f"  窗口内启动(全量主进程): {len(starts_in_focus_all_main)}")
    lines.append(f"  推测仍存活三方主进程数: {len(alive_apps)}")
    if alive_apps:
        lines.append(f"  推测存活列表: {', '.join(alive_apps)}")
    else:
        lines.append("  推测存活列表: 无")

    if infer_starts:
        lines.append("  前序高亮三方主进程启动样本(按时间从旧到新，最终计算基准):")
        for seq, (idx, event) in enumerate(infer_starts, start=1):
            process = event.get("full_name", event.get("process_name", ""))
            source = (event.get("details", {}) or {}).get("launch_source", "")
            lines.append(
                f"    S{seq:>2} 事件#{idx + 1} {_fmt_event_time(event.get('time'))} "
                f"{process} source={source or '-'}"
            )

    if kills_in_focus_all:
        lines.append("  窗口内查杀明细(全量，按时间从旧到新):")
        for seq, (idx, event) in enumerate(kills_in_focus_all, start=1):
            process = event.get("full_name", event.get("process_name", ""))
            evt_reason = _extract_kill_reason(event)
            lines.append(
                f"    K{seq:>2} 事件#{idx + 1} {_fmt_event_time(event.get('time'))} "
                f"{event.get('type')} {process} reason={evt_reason}"
            )

    if bg_rows:
        lines.append("  启动-存活明细:")
        for row in bg_rows:
            period = f"{_fmt_event_time(row['start_time'])} -> {_fmt_event_time(row['end_time'])}"
            source = row.get("start_source") or "-"
            start_kind = row.get("start_kind") or "-"
            kill_id = row.get("kill_event_id")
            kill_text = f"#{kill_id}" if kill_id else "-"
            lines.append(
                f"    [{row['seq']:>2}] 事件#{row['start_event_id']} {row['process']}  "
                f"区间={period}  状态={row['status']}  kill事件={kill_text}  "
                f"source={source}  proc_start={'Y' if row['had_proc_start'] else 'N'}  start_kind={start_kind}"
            )

    lines.append("=" * 88)
    return "\n".join(lines)


def generate_kill_focus_report_html(report_text: str, output_file: str) -> None:
    """将 kill focus 文本报告转换为 Summary 风格 HTML（总览 + 四标签页）。"""
    text = str(report_text or "").strip()
    lines = text.splitlines() if text else []

    def _parse_mmdd_ts(ts_text: str) -> Optional[datetime]:
        ts = str(ts_text or "").strip()
        if not ts:
            return None
        for fmt in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    def _parse_num_text(v: str) -> Optional[float]:
        sv = str(v or "").strip().replace(",", "")
        if not sv:
            return None
        try:
            return float(sv)
        except ValueError:
            return None

    def _fmt_num(v) -> str:
        num = _parse_num_text(v)
        if num is None:
            return "-"
        if abs(num - int(num)) < 1e-9:
            return f"{int(num):,}"
        return f"{num:,.1f}"

    def _esc(v) -> str:
        return html.escape(str(v if v not in (None, "") else "-"))

    # 基础字段
    meta = {
        "source": "",
        "package": "",
        "target_event": "",
        "killed_time": "",
        "check_time": "",
        "event_type": "",
        "process": "",
        "pid": "",
        "is_subprocess": "",
        "reason": "",
        "alive_count": "",
        "alive_list": "",
    }
    for line in lines:
        s = line.strip()
        if s.startswith("输入日志:"):
            meta["source"] = s.split(":", 1)[1].strip()
        elif s.startswith("目标包名:"):
            meta["package"] = s.split(":", 1)[1].strip()
        elif s.startswith("目标事件:"):
            meta["target_event"] = s.split(":", 1)[1].strip()
        elif s.startswith("被杀时刻:"):
            meta["killed_time"] = s.split(":", 1)[1].strip()
        elif s.startswith("查杀时刻(估算):"):
            meta["check_time"] = s.split(":", 1)[1].strip()
        elif s.startswith("事件类型:"):
            meta["event_type"] = s.split(":", 1)[1].strip()
        elif s.startswith("进程:"):
            meta["process"] = s.split(":", 1)[1].strip()
        elif s.startswith("PID:"):
            meta["pid"] = s.split(":", 1)[1].strip()
        elif s.startswith("是否子进程:"):
            meta["is_subprocess"] = s.split(":", 1)[1].strip()
        elif s.startswith("查杀原因:"):
            meta["reason"] = s.split(":", 1)[1].strip()
        elif s.startswith("推测仍存活三方主进程数:"):
            meta["alive_count"] = s.split(":", 1)[1].strip()
        elif s.startswith("推测存活列表:"):
            meta["alive_list"] = s.split(":", 1)[1].strip()

    # 章节拆分
    section_map: Dict[str, List[str]] = {}
    current_title = ""
    current_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\d+\)", stripped):
            if current_title:
                section_map[current_title] = list(current_lines)
            current_title = stripped
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)
    if current_title:
        section_map[current_title] = list(current_lines)

    sec1 = next((v for k, v in section_map.items() if k.startswith("1)")), [])
    sec2 = next((v for k, v in section_map.items() if k.startswith("2)")), [])
    sec3 = next((v for k, v in section_map.items() if k.startswith("3)")), [])

    # Section1: key/value + 当前内存状态
    sec1_pairs: List[Tuple[str, str]] = []
    mem_snapshot: List[Tuple[str, str]] = []
    in_mem_block = False
    mem_line_re = re.compile(r"^(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<v>.+)$")
    for line in sec1:
        s = line.strip()
        if not s:
            continue
        if s.startswith("当前内存状态"):
            in_mem_block = True
            continue
        if in_mem_block:
            mm = mem_line_re.match(s)
            if mm:
                mem_snapshot.append((mm.group("k"), mm.group("v")))
                continue
            in_mem_block = False
        if ":" in s:
            k, v = s.split(":", 1)
            sec1_pairs.append((k.strip(), v.strip()))

    # Section2: 一分钟前后查杀 + 被杀前窗口内存统计
    sec2_window_range = ""
    sec2_kill_count_text = ""
    sec2_kill_items = []
    kill_line_re = re.compile(
        r"^-\s+#(?P<eid>\d+)\s+(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+"
        r"(?P<etype>kill|lmk)\s+(?P<proc>\S+)\s+reason=(?P<reason>.+?)\s+memFree=(?P<mem>.+)$"
    )
    for line in sec2:
        s = line.strip()
        if s.startswith("时间窗口:"):
            sec2_window_range = s.split(":", 1)[1].strip()
        elif s.startswith("查杀事件数"):
            sec2_kill_count_text = s.split(":", 1)[1].strip()
        m = kill_line_re.match(s)
        if m:
            sec2_kill_items.append(
                {
                    "event_id": m.group("eid"),
                    "time": m.group("ts"),
                    "dt": _parse_mmdd_ts(m.group("ts")),
                    "etype": m.group("etype"),
                    "proc": m.group("proc"),
                    "reason": m.group("reason"),
                    "mem_free": m.group("mem"),
                }
            )

    pre_kill_windows = []
    cur_window = None
    window_head_re = re.compile(
        r"^窗口\s*(?P<sec>\d+)s:\s*(?P<start>.+?)\s*~\s*(?P<end>.+?)\s*事件数=(?P<count>\d+)$"
    )
    metric_re = re.compile(
        r"^(?P<metric>memFree|memFile|memAnon|swapFree)\s+"
        r"avg=(?P<avg>[-0-9.]+)\s+p50=(?P<p50>[-0-9.]+)\s+p95=(?P<p95>[-0-9.]+)\s+"
        r"min=(?P<min>[-0-9.]+)\s+max=(?P<max>[-0-9.]+)$"
    )
    for line in sec2:
        s = line.strip()
        head = window_head_re.match(s)
        if head:
            if cur_window:
                pre_kill_windows.append(cur_window)
            cur_window = {
                "sec": int(head.group("sec")),
                "start": head.group("start"),
                "end": head.group("end"),
                "count": int(head.group("count")),
                "metrics": [],
            }
            continue
        if cur_window:
            mm = metric_re.match(s)
            if mm:
                cur_window["metrics"].append(
                    {
                        "metric": mm.group("metric"),
                        "avg": mm.group("avg"),
                        "p50": mm.group("p50"),
                        "p95": mm.group("p95"),
                        "min": mm.group("min"),
                        "max": mm.group("max"),
                    }
                )
    if cur_window:
        pre_kill_windows.append(cur_window)
    pre_kill_windows.sort(key=lambda x: -x["sec"])
    pre_kill_window_map = {win["sec"]: win for win in pre_kill_windows}

    # 60s窗口（终点=被杀时刻）查杀明细
    killed_dt = _parse_mmdd_ts(meta.get("killed_time", ""))
    kill_60_items = []
    if killed_dt:
        floor = killed_dt - timedelta(seconds=60)
        for item in sec2_kill_items:
            dt = item.get("dt")
            if dt and floor <= dt <= killed_dt:
                kill_60_items.append(item)
    if not kill_60_items:
        kill_60_items = list(sec2_kill_items)

    # Section3: 摘要 + 启动样本 + 查杀样本 + 启动存活明细
    sec3_summary_lines = []
    sec3_start_lines = []
    sec3_kill_lines = []
    sec3_detail_lines = []
    mode = "summary"
    for line in sec3:
        s = line.strip()
        if not s:
            continue
        if s.startswith("前序高亮三方主进程启动样本"):
            mode = "start"
            continue
        if s.startswith("窗口内查杀明细"):
            mode = "kill"
            continue
        if s.startswith("启动-存活明细"):
            mode = "detail"
            continue
        if mode == "summary":
            sec3_summary_lines.append(s)
        elif mode == "start" and s.startswith("S"):
            sec3_start_lines.append(s)
        elif mode == "kill" and s.startswith("K"):
            sec3_kill_lines.append(s)
        elif mode == "detail" and s.startswith("["):
            sec3_detail_lines.append(s)

    sec3_summary_kv = []
    for s in sec3_summary_lines:
        if ":" in s:
            k, v = s.split(":", 1)
            key = k.strip()
            if key.startswith("推测存活列表"):
                continue
            sec3_summary_kv.append((key, v.strip()))
        else:
            sec3_summary_kv.append((s.strip(), ""))

    def _find_summary_value(prefix: str) -> str:
        for k, v in sec3_summary_kv:
            if k.startswith(prefix):
                return v
        return ""

    start_re = re.compile(
        r"^S\s*(?P<seq>\d+)\s+事件#(?P<eid>\d+)\s+(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+"
        r"(?P<proc>\S+)(?:\s+source=(?P<src>.+))?$"
    )
    kill_re = re.compile(
        r"^K\s*(?P<seq>\d+)\s+事件#(?P<eid>\d+)\s+(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+"
        r"(?P<etype>kill|lmk)\s+(?P<proc>\S+)\s+reason=(?P<reason>.+)$"
    )
    detail_re = re.compile(
        r"^\[\s*(?P<seq>\d+)\]\s+事件#(?P<eid>\d+)\s+(?P<proc>\S+)\s+"
        r"区间=(?P<period>.+?)\s+状态=(?P<status>\S+)\s+kill事件=(?P<kill_evt>\S+)\s+"
        r"source=(?P<src>\S+)\s+proc_start=(?P<proc_start>[YN])\s+start_kind=(?P<start_kind>.+)$"
    )

    start_rows = []
    for raw in sec3_start_lines:
        m = start_re.match(raw)
        if not m:
            continue
        start_rows.append(
            {
                "seq": int(m.group("seq")),
                "event_id": m.group("eid"),
                "time": m.group("ts"),
                "dt": _parse_mmdd_ts(m.group("ts")),
                "proc": m.group("proc"),
                "source": (m.group("src") or "-").strip(),
            }
        )

    sec3_kill_rows = []
    for raw in sec3_kill_lines:
        m = kill_re.match(raw)
        if not m:
            continue
        sec3_kill_rows.append(
            {
                "seq": int(m.group("seq")),
                "event_id": m.group("eid"),
                "time": m.group("ts"),
                "dt": _parse_mmdd_ts(m.group("ts")),
                "etype": m.group("etype"),
                "proc": m.group("proc"),
                "reason": m.group("reason"),
            }
        )

    detail_rows = []
    detail_raw_unparsed = []
    for raw in sec3_detail_lines:
        m = detail_re.match(raw)
        if not m:
            detail_raw_unparsed.append(raw)
            continue
        detail_rows.append(
            {
                "seq": m.group("seq"),
                "event_id": m.group("eid"),
                "proc": m.group("proc"),
                "period": m.group("period"),
                "status": m.group("status"),
                "kill_evt": m.group("kill_evt"),
                "source": m.group("src"),
                "proc_start": m.group("proc_start"),
                "start_kind": m.group("start_kind"),
            }
        )

    def _normalize_start_kind(raw_kind: str, source: str) -> str:
        text_kind = str(raw_kind or "").strip().lower()
        text_source = str(source or "").strip().lower()
        merged = f"{text_kind} {text_source}"
        if "cold" in merged or "冷" in merged:
            return "cold"
        if "hot" in merged or "热" in merged:
            return "hot"
        return "unknown"

    start_kind_by_event_id = {}
    for drow in detail_rows:
        start_kind_by_event_id[str(drow.get("event_id", ""))] = _normalize_start_kind(
            drow.get("start_kind", ""), drow.get("source", "")
        )

    for row in start_rows:
        row_kind = start_kind_by_event_id.get(str(row.get("event_id", "")))
        if not row_kind:
            row_kind = _normalize_start_kind("", row.get("source", ""))
        row["start_kind"] = row_kind

    sec3_kill_rows_hl = []
    for r in sec3_kill_rows:
        proc = str(r.get("proc", ""))
        base = _base_name(proc)
        if not _is_highlight_third_party_package(base):
            continue
        if proc != base:
            continue
        sec3_kill_rows_hl.append(r)

    recent_start_rows = sorted(start_rows, key=lambda x: (x.get("dt") is None, x.get("dt")), reverse=True)[:5]
    recent_start_rows = [r for r in recent_start_rows if r]

    timeline_items = []
    for r in start_rows:
        kind = r.get("start_kind", "unknown")
        if kind == "cold":
            label = "冷启动"
            label_class = "start_cold"
        elif kind == "hot":
            label = "热启动"
            label_class = "start_hot"
        else:
            label = "启动"
            label_class = "start"
        timeline_items.append(
            {
                "dt": r.get("dt"),
                "time": r.get("time", ""),
                "label": label,
                "label_class": label_class,
                "process": r.get("proc", ""),
                "extra": f"source={r.get('source', '-')}",
            }
        )

    for r in sec3_kill_rows_hl:
        timeline_items.append(
            {
                "dt": r.get("dt"),
                "time": r.get("time", ""),
                "label": "查杀" if r.get("etype") == "kill" else "LMK",
                "label_class": "kill" if r.get("etype") == "kill" else "lmk",
                "process": r.get("proc", ""),
                "extra": r.get("reason", ""),
            }
        )
    timeline_items.sort(key=lambda x: (x.get("dt") is None, x.get("dt"), x.get("time", "")))

    # 顶部 summary 使用的聚合文本
    sec3_window_text = _find_summary_value("计算窗口")
    sec3_base_count = _find_summary_value("启动样本(最终计算基准，高亮三方)")
    sec3_focus_kill_count = _find_summary_value("窗口内查杀(高亮三方主进程 kill+lmk)")
    sec3_focus_all_kill_count = _find_summary_value("窗口内查杀(全量 kill+lmk)")
    sec3_focus_start_count = _find_summary_value("窗口内启动(全量主进程)")

    # summary: 内存表（优先 memFree）
    def _window_metric(sec: int, metric: str) -> Optional[dict]:
        win = pre_kill_window_map.get(sec)
        if not win:
            return None
        for m in win.get("metrics", []):
            if m.get("metric") == metric:
                return m
        return None

    summary_mem_rows = []
    for sec in (60, 30, 15, 5):
        mm = _window_metric(sec, "memFree")
        if mm:
            summary_mem_rows.append(
                f"<tr><td>前{sec}s</td><td>{_esc(_fmt_num(mm.get('avg')))}</td><td>{_esc(_fmt_num(mm.get('p50')))}</td><td>{_esc(_fmt_num(mm.get('min')))}</td></tr>"
            )
        else:
            summary_mem_rows.append(f"<tr><td>前{sec}s</td><td>-</td><td>-</td><td>-</td></tr>")
    summary_mem_rows_html = "".join(summary_mem_rows)

    # 当前内存快照（sec1）
    mem_label_map = {
        "memFree": "memfree",
        "memAvail": "memavail",
        "memFile": "file",
        "memAnon": "anon",
        "swapFree": "swapfree",
        "cmaFree": "cmafree",
        "rss": "rss",
        "cached": "cached",
        "memTotal": "memtotal",
        "adj": "adj",
        "min_adj": "min_adj",
        "thrashing": "thrashing",
        "psi_mem_some": "psi_mem_some",
        "psi_mem_full": "psi_mem_full",
        "psi_io_some": "psi_io_some",
        "psi_io_full": "psi_io_full",
        "psi_cpu_some": "psi_cpu_some",
    }
    mem_snapshot_rows_html = "".join(
        f"<tr><td>{_esc(mem_label_map.get(k, k))}</td><td>{_esc(v)}</td></tr>"
        for k, v in mem_snapshot
    ) or "<tr><td colspan='2' class='summary-empty'>无数据</td></tr>"

    # sec2 查杀表
    sec2_kill_rows_html = "".join(
        (
            "<tr>"
            f"<td>#{_esc(it.get('event_id'))}</td>"
            f"<td>{_esc(it.get('time'))}</td>"
            f"<td><span class='etype-pill {'kill' if it.get('etype') == 'kill' else 'lmk'}'>{'查杀' if it.get('etype') == 'kill' else 'LMK'}</span></td>"
            f"<td>{_esc(it.get('proc'))}</td>"
            f"<td>{_esc(it.get('reason'))}</td>"
            f"<td>{_esc(it.get('mem_free'))}</td>"
            "</tr>"
        )
        for it in sec2_kill_items
    ) or "<tr><td colspan='6' class='summary-empty'>无数据</td></tr>"

    # 60s折叠内容
    kill_60_pre = "\n".join(
        f"#{it['event_id']} {it['time']} {it['etype']} {it['proc']} reason={it['reason']} memFree={it['mem_free']}"
        for it in kill_60_items
    ) or "无数据"

    # sec3 summary 表
    sec3_summary_rows_html = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in sec3_summary_kv
    ) or "<tr><td colspan='2' class='summary-empty'>无数据</td></tr>"

    # sec3 start/kill table
    sec3_start_rows_html = "".join(
        (
            "<tr>"
            f"<td>S{_esc(r.get('seq'))}</td>"
            f"<td>#{_esc(r.get('event_id'))}</td>"
            f"<td>{_esc(r.get('time'))}</td>"
            f"<td>{_esc(r.get('proc'))}</td>"
            f"<td>{_esc(r.get('source'))}</td>"
            "</tr>"
        )
        for r in start_rows
    ) or "<tr><td colspan='5' class='summary-empty'>无数据</td></tr>"

    detail_rows_html = "".join(
        (
            "<tr>"
            f"<td>{_esc(r.get('seq'))}</td>"
            f"<td>#{_esc(r.get('event_id'))}</td>"
            f"<td>{_esc(r.get('proc'))}</td>"
            f"<td>{_esc(r.get('period'))}</td>"
            f"<td>{_esc(r.get('status'))}</td>"
            f"<td>{_esc(r.get('kill_evt'))}</td>"
            f"<td>{_esc(r.get('source'))}</td>"
            f"<td>{_esc(r.get('proc_start'))}</td>"
            f"<td>{_esc(r.get('start_kind'))}</td>"
            "</tr>"
        )
        for r in detail_rows
    ) or "<tr><td colspan='9' class='summary-empty'>无数据</td></tr>"

    recent_start_rows_html = "".join(
        (
            "<tr>"
            f"<td>前{idx}</td>"
            f"<td>{_esc(r.get('time'))}</td>"
            f"<td>{_esc(r.get('proc'))}</td>"
            f"<td>{_esc('冷启动' if r.get('start_kind') == 'cold' else ('热启动' if r.get('start_kind') == 'hot' else '启动'))}</td>"
            f"<td>{_esc(r.get('source'))}</td>"
            "</tr>"
        )
        for idx, r in enumerate(recent_start_rows, start=1)
    ) or "<tr><td colspan='5' class='summary-empty'>无数据</td></tr>"

    def _build_hl_timeline_html() -> str:
        if not timeline_items:
            return '<div style="color:#9fb3c8;">暂无数据</div>'
        rows = []
        for item in timeline_items:
            left = ""
            right = ""
            if item.get("label_class") == "start_cold":
                left = f'<span class="pill pill-start-cold">冷启动</span>&nbsp;{_esc(item.get("process"))}'
            elif item.get("label_class") == "start_hot":
                left = f'<span class="pill pill-start-hot">热启动</span>&nbsp;{_esc(item.get("process"))}'
            elif item.get("label_class") == "start":
                left = f'<span class="pill pill-start">启动</span>&nbsp;{_esc(item.get("process"))}'
            elif item.get("label_class") == "kill":
                right = f'{_esc(item.get("process"))}&nbsp;<span class="pill pill-kill">上层/一体化</span>'
            else:
                right = f'{_esc(item.get("process"))}&nbsp;<span class="pill pill-lmk">底层/LMKD</span>'
            rows.append(
                f'<div class="timeline-row">'
                f'<div class="tl-time">{_esc(item.get("time"))}</div>'
                f'<div class="tl-content"><span class="tl-left">{left}</span><span class="tl-right">{right}</span></div>'
                f'</div>'
            )
        legend = (
            '<div class="tl-legend">'
            '<span class="pill pill-start-cold">冷启动</span>'
            '<span class="pill pill-start-hot">热启动</span>'
            '<span class="pill pill-kill">上层/一体化</span>'
            '<span class="pill pill-lmk">底层/LMKD</span>'
            '</div>'
        )
        return legend + "".join(rows)

    hl_timeline_html = _build_hl_timeline_html()

    # pre-kill窗口卡片
    metric_label = {"memFree": "memfree", "memFile": "file", "memAnon": "anon", "swapFree": "swapfree"}
    mem_window_cards = []
    for sec in (60, 30, 15, 5):
        win = pre_kill_window_map.get(sec)
        if not win:
            mem_window_cards.append(
                (
                    "<div class='mem-window-card'>"
                    f"<div class='mem-window-title'>前{sec}s（无样本）</div>"
                    "<div class='summary-empty'>无可用数据</div>"
                    "</div>"
                )
            )
            continue
        metric_rows = []
        for m in win.get("metrics", []):
            metric_rows.append(
                "<tr>"
                f"<td>{_esc(metric_label.get(m.get('metric'), m.get('metric')))}</td>"
                f"<td>{_esc(_fmt_num(m.get('avg')))}</td>"
                f"<td>{_esc(_fmt_num(m.get('p50')))}</td>"
                f"<td>{_esc(_fmt_num(m.get('p95')))}</td>"
                f"<td>{_esc(_fmt_num(m.get('min')))}</td>"
                f"<td>{_esc(_fmt_num(m.get('max')))}</td>"
                "</tr>"
            )
        if not metric_rows:
            metric_rows.append("<tr><td colspan='6' class='summary-empty'>无可用内存样本</td></tr>")
        mem_window_cards.append(
            (
                "<div class='mem-window-card'>"
                f"<div class='mem-window-title'>前{sec}s（事件 {int(win.get('count', 0))}）</div>"
                f"<div class='summary-mem-note'>{_esc(win.get('start'))} ~ {_esc(win.get('end'))}</div>"
                "<table class='mem-table'>"
                "<thead><tr><th>指标</th><th>Avg</th><th>P50</th><th>P95</th><th>Min</th><th>Max</th></tr></thead>"
                f"<tbody>{''.join(metric_rows)}</tbody>"
                "</table>"
                "</div>"
            )
        )
    mem_window_cards_html = "".join(mem_window_cards)

    # 顶部 summary 卡片内容
    def _summary_item(label: str, value: str, value_cls: str = "") -> str:
        cls = f"summary-value {value_cls}".strip()
        return (
            "<div class='summary-item'>"
            f"<div class='summary-label'>{_esc(label)}</div>"
            f"<div class='{cls}'>{_esc(value)}</div>"
            "</div>"
        )

    prev1_start = recent_start_rows[0] if recent_start_rows else None
    prev1_text = "-"
    prev5_text = "-"
    if prev1_start:
        prev1_kind = "冷启动" if prev1_start.get("start_kind") == "cold" else ("热启动" if prev1_start.get("start_kind") == "hot" else "启动")
        prev1_text = f"{prev1_start.get('proc')} ({prev1_start.get('time')}, {prev1_kind})"
        prev5_text = ", ".join([str(r.get("proc", "-")) for r in recent_start_rows])

    kill_summary_items = "".join(
        [
            _summary_item("目标包名", meta.get("package") or "-", "text"),
            _summary_item("目标进程", meta.get("process") or "-", "text"),
            _summary_item("被杀时刻", meta.get("killed_time") or "-", "text"),
            _summary_item("PID", meta.get("pid") or "-"),
            _summary_item("事件类型", meta.get("event_type") or "-", "text"),
            _summary_item("是否子进程", meta.get("is_subprocess") or "-", "text"),
            _summary_item("查杀原因", meta.get("reason") or "-", "text"),
            _summary_item("目标事件", meta.get("target_event") or "-", "text"),
            _summary_item("查杀时刻(估算)", meta.get("check_time") or "-", "text"),
        ]
    )
    start_summary_items = "".join(
        [
            _summary_item("推测仍存活三方主进程数", meta.get("alive_count") or "-"),
            _summary_item("启动样本(高亮三方)", sec3_base_count or str(len(start_rows))),
            _summary_item("窗口内查杀(高亮主进程)", sec3_focus_kill_count or str(len(sec3_kill_rows_hl))),
            _summary_item("窗口内查杀(全量)", sec3_focus_all_kill_count or "-"),
            _summary_item("窗口内启动(全量主进程)", sec3_focus_start_count or "-"),
            _summary_item("上一个启动(高亮主进程)", prev1_text, "text"),
            _summary_item("前1~前5启动进程", prev5_text, "text"),
            _summary_item("计算窗口", sec3_window_text or "-", "text"),
        ]
    )
    mem_summary_note = (
        f"被查杀时刻内存快照：{', '.join([f'{k}:{v}' for k, v in mem_snapshot[:6]])}" if mem_snapshot else "被查杀时刻内存快照：无数据"
    )

    # 设备信息字段
    device_rows = [
        ("输入日志", meta.get("source") or "-"),
        ("目标包名", meta.get("package") or "-"),
        ("目标进程", meta.get("process") or "-"),
        ("目标事件", meta.get("target_event") or "-"),
        ("事件类型", meta.get("event_type") or "-"),
        ("PID", meta.get("pid") or "-"),
        ("被杀时刻", meta.get("killed_time") or "-"),
        ("查杀时刻(估算)", meta.get("check_time") or "-"),
    ]
    device_rows_html = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in device_rows)

    sec1_pairs_html = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in sec1_pairs) or "<tr><td colspan='2' class='summary-empty'>无数据</td></tr>"
    detail_raw_html = "\n".join(detail_raw_unparsed) if detail_raw_unparsed else "无"

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>指定进程被杀时刻分析 - Summary</title>
  <style>
    :root {{
      --bg: #0b1220;
      --surface: #101821;
      --surface-2: #0f1722;
      --border: #1f2a36;
      --text: #e6edf3;
      --sub: #9fb3c8;
      --accent: #8fb4ff;
      --green: #7ee2a7;
      --red: #ff9f9f;
      --purple: #c9a7ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(1200px 500px at 10% -10%, #122340 0%, #0b1220 42%, #09101c 100%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .page {{ max-width: 1760px; margin: 0 auto; padding: 18px 18px 26px; }}
    h1 {{ margin: 0 0 14px; font-size: 32px; font-weight: 800; letter-spacing: .2px; }}
    .summary-board {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 12px; margin-bottom: 14px; }}
    .summary-card {{
      background: linear-gradient(180deg, #101b2a 0%, #0f1723 100%);
      border: 1px solid #233145;
      border-radius: 12px;
      padding: 12px 14px;
      min-width: 0;
    }}
    .summary-title {{ color:#dce9fa; font-size: 15px; font-weight: 700; margin-bottom: 10px; }}
    .summary-stack {{ display:flex; flex-direction:column; gap:8px; }}
    .summary-row {{ display:grid; gap:8px; }}
    .summary-item {{
      background: #0d1724;
      border: 1px solid #243447;
      border-radius: 10px;
      padding: 10px 10px;
      min-height: 64px;
      overflow-wrap: anywhere;
    }}
    .summary-label {{ color:#8da6bf; font-size:12px; margin-bottom:2px; }}
    .summary-value {{ color:#f5f7fb; font-size:18px; font-weight:700; line-height:1.25; margin-top:2px; font-variant-numeric:tabular-nums; }}
    .summary-value.text {{ font-size:13px; font-weight:600; line-height:1.35; }}
    .summary-mem-note {{ color:#8da6bf; font-size:11px; margin-bottom:8px; line-height: 1.4; }}
    .summary-mem-table {{ width: 100%; border-collapse: collapse; }}
    .summary-mem-table th, .summary-mem-table td {{
      padding: 8px 8px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      font-size: 12px;
      white-space: nowrap;
    }}
    .summary-mem-table th {{ color: var(--sub); font-weight: 700; }}
    .summary-empty {{ color: var(--sub); text-align: center; font-size: 13px; }}
    .detail-tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px 0; }}
    .tab-btn {{
      appearance:none;
      border:1px solid #2a3b50;
      background:#0f1722;
      color:#b9cee5;
      border-radius:999px;
      padding:6px 12px;
      font-size:12px;
      font-weight:700;
      cursor:pointer;
    }}
    .tab-btn:hover {{ border-color:#4f77a3; color:#dcebff; }}
    .tab-btn.active {{ border-color:#4f77a3; color:#dcebff; background:#16263a; }}
    .tab-panel {{ display:none; }}
    .tab-panel.active {{ display:block; }}
    .section {{
      background: var(--surface-2);
      border: 1px solid #223145;
      border-radius: 12px;
      padding: 12px;
      margin-bottom: 12px;
    }}
    .subsection {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      margin-bottom: 10px;
    }}
    .subsection:last-child {{ margin-bottom: 0; }}
    .subsection h3 {{ margin:0 0 10px; font-size: 18px; }}
    .data-table, .kv-table, .mem-table, .hl-run-table {{
      width: 100%;
      border-collapse: collapse;
      background: #101821;
      border: 1px solid #1f2a36;
      border-radius: 8px;
      overflow: hidden;
    }}
    .data-table th, .data-table td,
    .kv-table th, .kv-table td,
    .mem-table th, .mem-table td,
    .hl-run-table th, .hl-run-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid #1f2a36;
      text-align: left;
      font-size: 12px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    .data-table th, .kv-table th, .mem-table th, .hl-run-table th {{
      color: var(--sub);
      font-size: 12px;
      font-weight: 700;
    }}
    .etype-pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.2;
    }}
    .etype-pill.start {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .etype-pill.kill {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .etype-pill.lmk {{ background:#2b2140; color:#d6b6ff; border-color:#6f4bb7; }}
    .timeline {{ border:1px solid #1f2a36; border-radius:12px; background:#101821; padding:10px; max-width:72%; margin:0 auto; }}
    .timeline-row {{ display:grid; grid-template-columns:120px 1fr; gap:8px; padding:6px 8px; border-bottom:1px solid #1b2634; align-items:center; }}
    .timeline-row:last-child {{ border-bottom:none; }}
    .tl-time {{ color:#cdd6e3; font-size:12px; letter-spacing:0.2px; }}
    .tl-content {{ display:flex; justify-content:space-between; width:100%; gap:10px; }}
    .tl-left {{ color:#f5f7fb; font-size:13px; text-align:left; min-height:1.2em; }}
    .tl-right {{ color:#f5f7fb; font-size:13px; text-align:right; min-height:1.2em; }}
    .tl-legend {{ display:flex; gap:8px; padding:4px 8px 8px 8px; flex-wrap:wrap; }}
    .pill {{ padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; border:1px solid transparent; }}
    .pill-start {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .pill-start-cold {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .pill-start-hot {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .pill-kill {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .pill-lmk {{ background:#2b2140; color:#d6b6ff; border-color:#6f4bb7; }}
    .fold {{
      background:#111a27;
      border:1px solid #25364b;
      border-radius:10px;
      overflow:hidden;
    }}
    .fold summary {{
      list-style:none;
      cursor:pointer;
      padding:10px 12px;
      background:linear-gradient(90deg,#142132 0%,#1b2b41 100%);
      color:#d9e8ff;
      font-weight:700;
      font-size: 13px;
    }}
    .fold summary::-webkit-details-marker {{ display:none; }}
    .fold-body {{ padding:10px; }}
    pre {{
      margin:0;
      background:#0b111a;
      border:1px solid #1f2d3f;
      border-radius:8px;
      padding:10px;
      color:#dce6f2;
      font-size:12px;
      line-height:1.45;
      overflow:auto;
      white-space:pre;
      font-family:"JetBrains Mono","Consolas","Menlo",monospace;
    }}
    .mem-window-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0,1fr));
      gap: 10px;
    }}
    .mem-window-card {{
      background:#101a27;
      border:1px solid #223247;
      border-radius:10px;
      padding:10px;
    }}
    .mem-window-title {{ font-size: 14px; font-weight: 700; margin-bottom: 6px; }}
    .inline-note {{ color: var(--sub); font-size: 12px; margin: 0 0 8px; }}
    @media (max-width: 1360px) {{
      .summary-board {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 28px; }}
      .summary-title {{ font-size: 14px; }}
      .summary-label, .summary-mem-note,
      .tab-btn, .subsection h3,
      .summary-mem-table th, .summary-mem-table td,
      .data-table th, .data-table td,
      .kv-table th, .kv-table td,
      .mem-table th, .mem-table td,
      .hl-run-table th, .hl-run-table td,
      .inline-note, .fold summary, pre, .etype-pill {{
        font-size: 13px;
      }}
      .mem-window-title {{ font-size: 14px; }}
      .timeline {{ max-width:100%; }}
    }}
    @media (max-width: 920px) {{
      .mem-window-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <h1>指定进程被杀时刻分析（Summary）</h1>

    <div class="summary-board">
      <section class="summary-card">
        <div class="summary-title">查杀</div>
        <div class="summary-stack">
          <div class="summary-row" style="grid-template-columns:repeat(3,minmax(0,1fr));">{kill_summary_items}</div>
        </div>
      </section>
      <section class="summary-card">
        <div class="summary-title">启动</div>
        <div class="summary-stack">
          <div class="summary-row" style="grid-template-columns:repeat(2,minmax(0,1fr));">{start_summary_items}</div>
        </div>
      </section>
      <section class="summary-card">
        <div class="summary-title">内存状态</div>
        <div class="summary-mem-note">{_esc(mem_summary_note)}</div>
        <table class="summary-mem-table">
          <thead><tr><th>范围</th><th>Avg</th><th>P50</th><th>Min</th></tr></thead>
          <tbody>{summary_mem_rows_html}</tbody>
        </table>
      </section>
    </div>

    <div class="detail-tabs">
      <button class="tab-btn active" type="button" data-tab="tab-kill">查杀</button>
      <button class="tab-btn" type="button" data-tab="tab-start">启动</button>
      <button class="tab-btn" type="button" data-tab="tab-memory">内存状态</button>
      <button class="tab-btn" type="button" data-tab="tab-device">设备信息</button>
    </div>

    <div class="tab-panel active" id="tab-kill">
      <div class="section">
        <div class="subsection">
          <h3>目标查杀详情</h3>
          <table class="kv-table">
            <tbody>{sec1_pairs_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>查杀时刻前后一分钟（kill+lmk）</h3>
          <div class="inline-note">时间窗口: {_esc(sec2_window_range or "-")}，查杀事件数: {_esc(sec2_kill_count_text or str(len(sec2_kill_items)))}</div>
          <table class="data-table">
            <thead><tr><th>事件</th><th>时间</th><th>类型</th><th>进程</th><th>原因</th><th>memFree(KB)</th></tr></thead>
            <tbody>{sec2_kill_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>被杀前60s查杀明细（终点=被杀时刻）</h3>
          <details class="fold">
            <summary>默认折叠，点击展开（共 {len(kill_60_items)} 条）</summary>
            <div class="fold-body"><pre>{html.escape(kill_60_pre)}</pre></div>
          </details>
        </div>
      </div>
    </div>

    <div class="tab-panel" id="tab-start">
      <div class="section">
        <div class="subsection">
          <h3>后台三方占用推测摘要</h3>
          <table class="kv-table">
            <thead><tr><th>字段</th><th>值</th></tr></thead>
            <tbody>{sec3_summary_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>高亮三方主进程启动样本（前序20条）</h3>
          <table class="hl-run-table">
            <thead><tr><th>序号</th><th>事件</th><th>时间</th><th>进程</th><th>来源</th></tr></thead>
            <tbody>{sec3_start_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>被杀时刻前最近1~5个高亮三方主进程启动</h3>
          <table class="hl-run-table">
            <thead><tr><th>顺位</th><th>时间</th><th>进程</th><th>启动类型</th><th>来源</th></tr></thead>
            <tbody>{recent_start_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>高亮主进程时间线（启动 + 查杀）</h3>
          <div class="timeline">{hl_timeline_html}</div>
        </div>
        <div class="subsection">
          <h3>启动-存活判定明细</h3>
          <table class="hl-run-table">
            <thead><tr><th>序号</th><th>启动事件</th><th>进程</th><th>区间</th><th>状态</th><th>kill事件</th><th>source</th><th>proc_start</th><th>start_kind</th></tr></thead>
            <tbody>{detail_rows_html}</tbody>
          </table>
          <details class="fold" style="margin-top:10px;">
            <summary>未完全解析的原始行（点击展开）</summary>
            <div class="fold-body"><pre>{html.escape(detail_raw_html)}</pre></div>
          </details>
        </div>
      </div>
    </div>

    <div class="tab-panel" id="tab-memory">
      <div class="section">
        <div class="subsection">
          <h3>被查杀时刻内存快照</h3>
          <table class="mem-table">
            <thead><tr><th>指标</th><th>值</th></tr></thead>
            <tbody>{mem_snapshot_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>被杀前窗口内存统计（KB，窗口终点=被杀时刻）</h3>
          <div class="mem-window-grid">{mem_window_cards_html}</div>
        </div>
        <div class="subsection">
          <h3>60s 窗口具体查杀（默认折叠）</h3>
          <details class="fold">
            <summary>点击展开明细（共 {len(kill_60_items)} 条）</summary>
            <div class="fold-body"><pre>{html.escape(kill_60_pre)}</pre></div>
          </details>
        </div>
      </div>
    </div>

    <div class="tab-panel" id="tab-device">
      <div class="section">
        <div class="subsection">
          <h3>设备与输入信息</h3>
          <table class="kv-table">
            <thead><tr><th>字段</th><th>值</th></tr></thead>
            <tbody>{device_rows_html}</tbody>
          </table>
        </div>
        <div class="subsection">
          <h3>原始文本报告</h3>
          <pre>{html.escape(text or "无数据")}</pre>
        </div>
      </div>
    </div>
  </div>
  <script>
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabPanels = document.querySelectorAll('.tab-panel');
    function activateTab(tabId) {{
      tabButtons.forEach((btn) => {{
        btn.classList.toggle('active', btn.dataset.tab === tabId);
      }});
      tabPanels.forEach((panel) => {{
        panel.classList.toggle('active', panel.id === tabId);
      }});
    }}
    tabButtons.forEach((btn) => {{
      btn.addEventListener('click', () => activateTab(btn.dataset.tab));
    }});
  </script>
</body>
</html>
"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)


def analyze_bugreport_kill_focus(
    file_path: str,
    package_name: str,
    output_name: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"日志文件不存在: {file_path}")

    pkg = str(package_name or "").strip()
    if not pkg:
        raise ValueError("包名不能为空")

    resolved_file_path, cleanup_path, source_desc = _resolve_log_input_path(file_path)
    try:
        events = parse_log_file(resolved_file_path)
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    if not events:
        raise ValueError("日志中未解析到任何事件")

    candidates = _find_kill_candidates_for_package(events, pkg)
    if not candidates:
        raise ValueError(f"未找到包名 {pkg} 的 kill/lmk 事件")

    selected = _select_kill_candidate_interactively(candidates, pkg)
    if selected is None:
        raise ValueError("用户取消选择")
    event_idx, target_event = selected

    report_text = build_kill_focus_report(
        events=events,
        source_desc=source_desc,
        package_name=pkg,
        target_event_idx=event_idx,
        target_event=target_event,
    )
    context_info = extract_kill_focus_bugreport_context(
        resolved_file_path,
        target_event=target_event,
        before_lines=300,
        after_lines=100,
    )
    context_text = format_kill_focus_bugreport_context(context_info, source_desc=source_desc)

    output_dir = output_dir or state.FILE_DIR or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    if not output_name:
        default_base = _guess_output_name_from_log_path(file_path, fallback_idx=1)
        output_name = f"{default_base}_{_sanitize_output_name(pkg, fallback='proc')}_kill_focus"
    safe_name = _sanitize_output_name(output_name, fallback="kill_focus")
    output_file = os.path.join(output_dir, f"{safe_name}.txt")
    output_file_html = os.path.join(output_dir, f"{safe_name}.html")
    output_file_context = os.path.join(output_dir, f"{safe_name}_bugreport_context.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report_text)
        f.write("\n\n")
        f.write(context_text)
        f.write("\n")
    with open(output_file_context, "w", encoding="utf-8") as f:
        f.write(context_text)
    generate_kill_focus_report_html(report_text, output_file_html)
    return output_file


def main_bugreport_kill_focus():
    print("指定进程被杀时刻分析：支持 bugreport .txt / .zip。")
    file_path = ""
    while True:
        raw = input("请输入 bugreport 文件路径（q 退出）: ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return
        file_path = _strip_wrapped_quotes(raw)
        if not file_path:
            print("错误：文件路径不能为空。")
            continue
        if not os.path.isfile(file_path):
            print(f"错误：文件不存在 -> {file_path}")
            continue
        break

    while True:
        package_name = input("请输入被杀包名/进程名（例如 com.xxx.app 或 com.xxx.app:push）: ").strip()
        if package_name:
            break
        print("错误：包名不能为空。")

    default_name = f"{_guess_output_name_from_log_path(file_path, fallback_idx=1)}_{_sanitize_output_name(package_name, fallback='proc')}_kill_focus"
    output_name = input(f"请输入输出文件名(不含扩展名)，回车默认 {default_name}: ").strip() or default_name

    try:
        output_file = analyze_bugreport_kill_focus(
            file_path=file_path,
            package_name=package_name,
            output_name=output_name,
        )
        print(f"\n报告生成成功: {os.path.abspath(output_file)}")
        print(f"HTML报告: {os.path.splitext(os.path.abspath(output_file))[0]}.html")
    except Exception as e:
        print(f"处理失败: {e}")
    input("按回车返回菜单...")



def main_bugreport_dynamic_performance_model():
    key = "动态性能模型(TOP20)"
    apps = _load_named_app_list_from_config(key)
    if not apps:
        print(f"未在 app_config.json 中找到有效列表: {key}，回退到默认 HighLight。")
    main(preset_apps=apps, preset_label=key, lock_preset_apps=True)


def main_bugreport_nine_scene_residency():
    key = "九大场景-驻留"
    apps = _load_named_app_list_from_config(key)
    if not apps:
        print(f"未在 app_config.json 中找到有效列表: {key}，回退到默认 HighLight。")
    main(preset_apps=apps, preset_label=key, lock_preset_apps=True)


def main_bugreport_custom():
    print("通用分析模式：使用默认 HighLight 列表，默认全量解析（可选手动时间段）。")
    main(
        preset_apps=None,
        preset_label="通用分析",
        lock_preset_apps=True,
        allow_highlight_override=False,
        enable_auto_window_detection=False,
        include_startup_section=False,
    )


if __name__ == "__main__":
    main()
