# pyright: reportMissingImports=false, reportConstantRedefinition=false, reportMissingTypeArgument=false, reportIndexIssue=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportPossiblyUnboundVariable=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportOperatorIssue=false, reportCallIssue=false

import json
import html
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from importlib import resources
from typing import Optional

from .. import state
from ...config_loader import (
    load_app_list_config,
    load_rules_config,
    resolve_app_config_path,
    to_flat_app_config,
)


_OFFLINE_CHART_JS = r"""(function(){
'use strict';
function _getCanvas(target){
  if(!target) return null;
  if(target instanceof HTMLCanvasElement) return target;
  if(target.canvas instanceof HTMLCanvasElement) return target.canvas;
  return null;
}
function _setupCanvas(canvas){
  var rect = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : {width:canvas.width,height:canvas.height};
  var dpr = window.devicePixelRatio || 1;
  var w = Math.max(1, Math.floor(rect.width || canvas.width || 600));
  var h = Math.max(1, Math.floor(rect.height || canvas.height || 240));
  if(canvas.width !== w*dpr || canvas.height !== h*dpr){
    canvas.width = w*dpr; canvas.height = h*dpr;
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  }
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx:ctx,w:w,h:h};
}
function _num(x){ x = +x; return isFinite(x) ? x : 0; }
function _max(arr){
  var m = 0;
  for(var i=0;i<(arr||[]).length;i++){ var v = _num(arr[i]); if(v > m) m = v; }
  return m;
}
function _axes(ctx,w,h,pad){
  ctx.strokeStyle = 'rgba(205,214,227,0.25)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad,pad);
  ctx.lineTo(pad,h-pad);
  ctx.lineTo(w-pad,h-pad);
  ctx.stroke();
}
function _renderBar(ctx,w,h,pad,labels,datasets){
  var values = (datasets[0] && datasets[0].data) ? datasets[0].data : [];
  var maxV = _max(values) || 1;
  var n = Math.max(1, values.length);
  var chartW = w - pad*2;
  var chartH = h - pad*2;
  var gap = 8;
  var barW = Math.max(2, (chartW - gap*(n-1)) / n);
  ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  for(var i=0;i<n;i++){
    var v = _num(values[i]);
    var bh = chartH * (v / maxV);
    var x = pad + i*(barW+gap);
    var y = (h - pad) - bh;
    ctx.fillStyle = 'rgba(123,198,255,0.55)';
    ctx.fillRect(x,y,barW,bh);
    ctx.fillStyle = '#f5f7fb';
    ctx.fillText(String(v), x + barW/2, y - 2);
  }
}

function Chart(target, config){
  this.config = config || {};
  this.canvas = _getCanvas(target) || target;
  if(!this.canvas) return;
  this.render();
}
Chart.register = function(){ };
Chart.prototype.render = function(){
  var cfg = this.config || {};
  var type = String(cfg.type || 'bar');
  var data = cfg.data || {};
  var labels = data.labels || [];
  var datasets = data.datasets || [];
  if(!datasets || !datasets.length) return;
  var s = _setupCanvas(this.canvas);
  var ctx = s.ctx, w = s.w, h = s.h;
  ctx.clearRect(0,0,w,h);
  var pad = 32;
  _axes(ctx,w,h,pad);
  if(type === 'bar') _renderBar(ctx,w,h,pad,labels,datasets);
  else _renderBar(ctx,w,h,pad,labels,datasets);
};

window.Chart = window.Chart || Chart;
window.ChartDataLabels = window.ChartDataLabels || {};
})();
"""


def _write_offline_chart_js(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, 'chart.min.js')
    if os.path.isfile(path):
        return path
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_OFFLINE_CHART_JS)
    return path

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


def _load_highlight_processes():
    """从连续启动配置加载高亮进程，失败时回退到默认列表。"""
    config_data = None
    yaml_cfg = load_app_list_config()
    if isinstance(yaml_cfg, dict) and yaml_cfg:
        config_data = to_flat_app_config(yaml_cfg)

    # 优先读取本地文件，方便用户修改
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

    # 回退到包内默认配置
    if config_data is None:
        try:
            with resources.open_text("collie_package", "app_config.json", encoding="utf-8") as fp:
                config_data = json.load(fp)
        except Exception:
            config_data = None

    apps = []
    if isinstance(config_data, dict) and 'highlight_processes' in config_data:
        highlight = config_data.get('highlight_processes')
        if isinstance(highlight, list):
            return [x for x in highlight if isinstance(x, str)] or list(DEFAULT_HIGHLIGHT_PROCESSES)
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


# 高亮显示的进程名列表，来源于 app_config 中的连续启动配置
HIGHLIGHT_PROCESSES = _load_highlight_processes()

RULES = load_rules_config()
_PARSE_RULES = RULES.get('parse_cont_startup', {}) if isinstance(RULES, dict) else {}
_PATTERNS = _PARSE_RULES.get('patterns', {}) if isinstance(_PARSE_RULES, dict) else {}

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
    - 必须以 com. 开头（用户约束）
    """
    if not isinstance(name, str) or not name:
        return False
    return name.startswith("com.")


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


def parse_log_file(file_path, start_time: Optional[datetime] = None, end_time: Optional[datetime] = None):
    """解析日志文件，返回排序后的事件列表，可选时间过滤"""
    events = []
    current_year = datetime.now().year
    killinfo_by_pid = defaultdict(list)
    killinfo_by_comm = defaultdict(list)
    killinfo_all = []  # 记录全部 killinfo 供未匹配时兜底生成事件
    lmk_events = []
    
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
            
            # 尝试解析启动日志
            if 'am_proc_start' in line:
                match = re.match(r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*am_proc_start: \[([^\]]+)\]', line)
                if match:
                    timestamp, details = match.groups()
                    parts = [p.strip() for p in details.split(',')]
                    if len(parts) >= 6:
                        try:
                            # 仅保留正常点击启动：start_type 必须包含 prestart-top-activity
                            start_type = parts[4]
                            if 'prestart-top-activity' not in start_type:
                                continue

                            # 提取进程名和判断是否子进程
                            full_name = parts[3]
                            is_subprocess = ':' in full_name
                            process_name = full_name.split(':')[0] if is_subprocess else full_name
                            
                            time_obj = datetime.strptime(f"{current_year}-{timestamp}", "%Y-%m-%d %H:%M:%S.%f")
                            if not _within_time_range(time_obj, start_time, end_time):
                                continue
                            
                            events.append({
                                'time': time_obj,
                                'type': 'start',
                                'process_name': process_name,
                                'full_name': full_name,
                                'is_subprocess': is_subprocess,
                                'raw': line,
                                'details': {
                                    'pid': parts[1],
                                    'uid': parts[2],
                                    'start_type': start_type,
                                    'component': parts[5]
                                }
                            })
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
        event_type = f"启动"
    elif event['type'] == 'kill':
        event_type = f"查杀"
    elif event['type'] == 'trig':
        event_type = f"触发查杀"
    elif event['type'] == 'skip':
        event_type = f"跳过({event['details']['event_tag'][5:]})"  # 显示跳过原因
    elif event['type'] == 'lmk':
        event_type = f"LMK查杀"
    
    # 构造详细信息
    details = []
    if event['type'] == 'start':
        details.append(f"  进程信息:")
        details.append(f"    PID: {event['details']['pid']}, UID: {event['details']['uid']}")
        details.append(f"    启动方式: {event['details']['start_type']}")
        details.append(f"    组件: {event['details']['component']}")
        details.append(f"    是否子进程: {'是' if event['is_subprocess'] else '否'}")
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
        event_type = f"启动"
    elif event['type'] == 'kill':
        event_type = f"查杀"
    elif event['type'] == 'trig':
        event_type = f"触发查杀"
    elif event['type'] == 'skip':
        event_type = f"跳过({event['details']['event_tag'][5:]})"  # 显示跳过原因
    elif event['type'] == 'lmk':
        event_type = f"LMK查杀"

    
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
        f"子进程启动: {summary['subprocess_start_count']}",
        f"总释放内存: {summary['total_release_mem']:,} KB ({summary['total_release_mem']/1024:.2f} MB)",
        f"总杀死进程数: {summary['total_killed']} (含重要进程: {summary['killed_imp_count']})",
        f"\n被杀/触发时内存统计 (单位KB):",
        f"\n查杀类型分布:"
    ]
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
            if it['label'] == '启动':
                max_len = max(max_len, len(f"{it['label']}    {it['process']}"))
            else:
                max_len = max(max_len, len(f"{it['process']}   {it['label']}"))
        inner_width = max(max_len, 70)
        def _fmt_line(it):
            ts = it['time']
            label = it['label']
            proc = it['process']
            if label == '启动':
                content = f"{label}    {proc}".ljust(inner_width)
            else:
                content = f"{proc}   {label}".rjust(inner_width)
            return f"- {ts:<19} | {content} |"
        for item in highlight_timeline:
            report.append(_fmt_line(item))

    if highlight_residency:
        report.append(f"\n高亮进程驻留率（前5次窗口 & 全量，主进程）:")
        report.append("轮次 序号 应用 启动前存活数/总 存活列表 全部存活/总 前1 前2 前3 前4 前5")
        for rec in highlight_residency:
            alive_list = rec['alive_list']
            alive_txt = ", ".join(alive_list) if alive_list else "无"
            row = (
                f"{1:>2} {rec['seq']:>2} "
                f"{rec['process']:<24} "
                f"{rec['alive_cnt']}/{rec['window_total']} "
                f"{alive_txt:<30} "
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
            rate_full = rec.get('all_rate')
            if rate_full and rate_full != "-":
                try:
                    pct_full = float(rate_full.split('(')[1].split('%')[0])
                    all_rates.append(pct_full)
                except Exception:
                    pass
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
        subprocess_start_count = sum(1 for e in events if e['type'] == 'start' and e['is_subprocess'])
        total_release_mem = sum(int(e['details']['kill_info']['killedPss']) for e in events if e['type'] == 'kill')
        total_killed = kill_count  # 每行代表一个被杀死的进程
        
        f.write(f"总事件数: {len(events)}\n")
        f.write(f"启动事件: {start_count} (子进程启动: {subprocess_start_count})\n")
        f.write(f"查杀事件: {kill_count}，LMK查杀事件: {lmk_count}\n")
        f.write(f"触发查杀事件: {trig_count}\n")
        f.write(f"跳过事件: {skip_count}\n")
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
            label = '启动'
            label_class = 'start'
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
        if base in HIGHLIGHT_PROCESSES:
            starts.append({"base": base, "dt": e["time"]})

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
        if idx == 0:
            continue
        # 受限窗口（默认前5次）
        prev_all = starts[max(0, idx - window_size):idx]
        total = len(prev_all)
        alive_list = []
        killed_list = []
        for ps in prev_all:
            if is_killed(ps["base"], ps["dt"], cur["dt"]):
                killed_list.append(ps["base"])
            else:
                alive_list.append(ps["base"])
        alive_cnt = len(alive_list)
        rate = round(alive_cnt / total * 100, 1) if total else 0

        # 全量前序（所有已出现的高亮主进程启动）
        prev_full = starts[:idx]
        total_full = len(prev_full)
        alive_full = []
        killed_full = []
        for ps in prev_full:
            if is_killed(ps["base"], ps["dt"], cur["dt"]):
                killed_full.append(ps["base"])
            else:
                alive_full.append(ps["base"])
        alive_cnt_full = len(alive_full)
        rate_full = round(alive_cnt_full / total_full * 100, 1) if total_full else 0

        per_window = {}
        for n in range(1, window_size + 1):
            subset = prev_all[-n:] if len(prev_all) >= n else []
            if not subset:
                per_window[n] = {"rate": "-", "alive": []}
                continue
            sub_total = len(subset)
            sub_alive = []
            for ps in subset:
                if not is_killed(ps["base"], ps["dt"], cur["dt"]):
                    sub_alive.append(ps["base"])
            sub_rate = round(len(sub_alive) / sub_total * 100, 1)
            per_window[n] = {
                "rate": f"{len(sub_alive)}/{sub_total} ({sub_rate}%)",
                "alive": sub_alive,
            }

        residency.append({
            "seq": idx + 1,
            "process": cur["base"],
            "alive_cnt": alive_cnt,
            "alive_list": alive_list,
            "killed_list": killed_list,
            "window_total": total,
            "all_alive_cnt": alive_cnt_full,
            "all_alive_list": alive_full,
            "all_killed_list": killed_full,
            "all_total": total_full,
            "all_rate": f"{alive_cnt_full}/{total_full} ({rate_full}%)" if total_full else "-",
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
                starts.append(e['time'])
                alive = True
                last_start = e['time']
            elif e.get('type') in ('kill', 'lmk'):
                if alive and last_start:
                    kills.append(e['time'])
                    alive = False
                    last_start = None
        if not starts:
            continue
        start1 = starts[0]
        start2 = starts[1] if len(starts) > 1 else None
        kill1 = kills[0] if len(kills) > 0 else None
        kill2 = kills[1] if len(kills) > 1 else None
        dur1_s = (kill1 - start1).total_seconds() if (start1 and kill1) else None
        dur2_s = (kill2 - start2).total_seconds() if (start2 and kill2) else None
        avg_s = None
        durations = [d for d in (dur1_s, dur2_s) if d is not None]
        if durations:
            avg_s = sum(durations) / len(durations)
        second_cold = "是" if (start2 and kill1) else ("未知" if start2 else "无第二轮")
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


def generate_report_html(events, summary, output_file):
    """生成仅包含 summary 的 HTML 报告（三板块：全部 / 主进程 / 高亮主进程）"""
    _write_offline_chart_js(os.path.dirname(output_file) or os.getcwd())
    s = _to_plain(summary)
    highlight_timeline = build_highlight_timeline(events)
    highlight_residency = build_highlight_residency(events)
    highlight_runs = compute_highlight_runs(events)

    def _residency_avg(res_list):
        if not res_list:
            return {"alive": 0, "rates": {n: 0 for n in range(1, 6)}, "all_rate": 0}
        rates = {n: [] for n in range(1, 6)}
        all_rates = []
        alive_list = []
        for rec in res_list:
            alive_list.append(rec.get("alive_cnt", 0))
            # 全量前序均值
            rate_full = rec.get("all_rate")
            if rate_full and rate_full != "-":
                try:
                    pct_full = float(rate_full.split("(")[1].split("%")[0])
                    all_rates.append(pct_full)
                except Exception:
                    pass
            for n in range(1, 6):
                rate_str = rec["per_window"][n]["rate"]
                if rate_str == "-":
                    continue
                try:
                    pct = float(rate_str.split("(")[1].split("%")[0])
                    rates[n].append(pct)
                except Exception:
                    continue
        avg_alive = sum(alive_list) / len(alive_list) if alive_list else 0
        avg_rates = {n: (sum(v) / len(v) if v else 0) for n, v in rates.items()}
        avg_all = sum(all_rates) / len(all_rates) if all_rates else 0
        return {"alive": avg_alive, "rates": avg_rates, "all_rate": avg_all}

    residency_avg = _residency_avg(highlight_residency)
    html_escape = html.escape
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

    def mem_avg_card():
        def table_for(key):
            stats_map = s.get("mem_stats", {}).get(key, {})
            avg_map = s.get("mem_avg", {}).get(key, {})
            if not stats_map or not avg_map or avg_map.get("count", 0) == 0:
                return "<div class='row-value'>无数据</div>"
            def row(metric_label, metric_key):
                st = stats_map.get(metric_key, {})
                if not st or st.get("count", 0) == 0:
                    return f"<tr><td class='mem-metric'>{metric_label}</td><td colspan='5'>无数据</td></tr>"
                return (
                    f"<tr>"
                    f"<td class='mem-metric'>{metric_label}</td>"
                    f"<td>{st['avg']:.1f}</td>"
                    f"<td>{st['median']:.1f}</td>"
                    f"<td>{st['p95']:.1f}</td>"
                    f"<td>{st['min']:.1f}</td>"
                    f"<td>{st['max']:.1f}</td>"
                    f"</tr>"
                )
            return (
                f"<div class='row-value'>样本 {avg_map.get('count',0)}</div>"
                f"<table class='mem-table'>"
                f"<thead><tr><th>指标</th><th>Avg</th><th>P50</th><th>P95</th><th>Min</th><th>Max</th></tr></thead>"
                f"<tbody>"
                f"{row('memfree', 'mem_free')}"
                f"{row('file', 'file_pages')}"
                f"{row('anon', 'anon_pages')}"
                f"{row('swapfree', 'swap_free')}"
                f"</tbody></table>"
            )
        return (
            '<div class="card card-wide">'
            '<div class="card-title">内存统计 (KB)</div>'
            f"{card_row('全部进程', table_for('all'))}"
            f"{card_row('主进程', table_for('main'))}"
            f"{card_row('高亮主进程(主)', table_for('highlight_main'))}"
            f"{card_row('触发事件', table_for('trig'))}"
            "</div>"
        )
    mem_avg_card_html = mem_avg_card()

    # 高亮主进程明细 HTML 预构建，避免 f-string 中复杂表达式报错
    def build_hl_detail():
        items = []
        hl_detail = s.get("highlight_proc_detail", {})
        hl_events = {}
        # 收集高亮主进程的 kill 与 lmk 事件，按 adj 分组
        for idx, e in enumerate(events):
            if e.get("type") not in ("kill", "lmk"):
                continue
            proc_full = e.get("full_name", e.get("process_name", ""))
            base = proc_full.split(":")[0]
            if base not in HIGHLIGHT_PROCESSES:
                continue
            if e.get("is_subprocess"):
                continue
            if e.get("type") == "kill":
                adj_val = e.get("details", {}).get("proc_info", {}).get("adj") or "未知"
            else:
                adj_val = e.get("details", {}).get("adj") or "未知"
            hl_events.setdefault(base, {}).setdefault(adj_val, []).append(
                (idx, e.get("type"), format_event_detail(e, idx))
            )

        for idx, (proc, stats) in enumerate(sorted(
            ((p, d) for p, d in hl_detail.items() if d.get("main_kill", 0) + d.get("main_lmk", 0) > 0),
            key=lambda kv: kv[1]["main_kill"] + kv[1]["main_lmk"],
            reverse=True,
        )):
            killtypes = " ".join(
                f'<span class="pill">{k}:{v}</span>' for k, v in sorted(stats.get("main_kill_type_stats", {}).items(), key=lambda x: -x[1])
            ) or "无"
            adj_kill = " ".join(
                f'<span class="pill">{k}:{v}</span>' for k, v in sorted(stats.get("main_adj_stats", {}).items(), key=lambda x: -x[1])
            ) or "无"
            adj_lmk = " ".join(
                f'<span class="pill">{k}:{v}</span>' for k, v in sorted(stats.get("main_lmk_adj_stats", {}).items(), key=lambda x: -x[1])
            ) or "无"

            adj_sections = []
            for adj_idx, (adj_val, detail_items) in enumerate(sorted(hl_events.get(proc, {}).items(), key=lambda x:-len(x[1]))):
                kill_c = sum(1 for _, t, _ in detail_items if t == "kill")
                lmk_c = sum(1 for _, t, _ in detail_items if t == "lmk")
                detail_html = "".join(
                    f'<details><summary>事件 {eid+1} ({etype})</summary><pre>{html_escape(txt)}</pre></details>'
                    for eid, etype, txt in detail_items
                ) or '<div style="color:#9fb3c8;">暂无匹配事件</div>'
                adj_sections.append(
                    f'<div class="acc-item">'
                    f'<div class="acc-header" data-target="acc-{idx}-adj-{adj_idx}">'
                    f'<div class="acc-title">adj {adj_val}</div>'
                    f'<div class="acc-meta">数量 {len(detail_items)} (kill {kill_c}, lmk {lmk_c})</div>'
                    f'</div>'
                    f'<div class="acc-body" id="acc-{idx}-adj-{adj_idx}">{detail_html}</div>'
                    f'</div>'
                )

            adj_block = "".join(adj_sections)

            items.append(
                f'<div class="acc-item">'
                f'<div class="acc-header" data-target="acc-{idx}">'
                f'<div class="acc-title">{proc}</div>'
                f'<div class="acc-meta">kill {stats.get("main_kill",0)} | lmk {stats.get("main_lmk",0)}</div>'
                f'</div>'
                f'<div class="acc-body" id="acc-{idx}">'
                f'<div class="kv"><strong>KillType:</strong> {killtypes}</div>'
                f'<div class="kv"><strong>adj(kill):</strong> {adj_kill}</div>'
                f'<div class="kv"><strong>adj(lmk):</strong> {adj_lmk}</div>'
                f'<div class="kv"><strong>事件详情(按adj分组):</strong></div>'
                f'{adj_block}'
                f'</div>'
                f'</div>'
            )
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
            if item["label"] == "启动":
                left = f'<span class="pill pill-start">启动应用</span>&nbsp;{html_escape(item["process"])}'
            elif item["label"] == "查杀":
                right = f'{html_escape(item["process"])}&nbsp;<span class="pill pill-kill">上层/一体化</span>'
            else:
                right = f'{html_escape(item["process"])}&nbsp;<span class="pill pill-lmk">底层/LMKD</span>'
            rows.append(
                f'<div class="timeline-row">'
                f'<div class="tl-time">{html_escape(item["time"])}</div>'
                f'<div class="tl-content"><span class="tl-left">{left}</span><span class="tl-right">{right}</span></div>'
                f'</div>'
            )
        legend = (
            '<div class="tl-legend">'
            '<span class="pill pill-start">启动应用</span>'
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
        for rec in highlight_residency:
            row_cells = [
                f"<td>{rec['seq']}</td>",
                f"<td>{html_escape(rec['process'])}</td>",
                f"<td>{rec['alive_cnt']}/{rec['window_total']}</td>",
                f"<td>{rec['all_rate']}</td>",
            ]
            for n in range(1, 6):
                cell = rec['per_window'][n]
                if cell["rate"] == "-":
                    row_cells.append("<td>-</td>")
                else:
                    alive = cell["alive"]
                    alive_tip = "无" if not alive else ", ".join(alive)
                    color_class = "rate-ok" if "100%" in cell["rate"] else "rate-bad"
                    row_cells.append(
                        f"<td class='{color_class}' title='{html_escape(alive_tip)}'>{cell['rate']}</td>"
                    )
            rows.append("<tr>" + "".join(row_cells) + "</tr>")

        # 计算平均驻留率与平均存活数（含全量）
        avg_cells = []
        avg_alive_value = "均存活数 0"
        avg_all_rate = "全量均值 0%"
        if highlight_residency:
            cols = {n: [] for n in range(1, 6)}
            all_rates = []
            alive_counts = []
            for rec in highlight_residency:
                alive_counts.append(rec["alive_cnt"])
                rate_full = rec.get("all_rate")
                if rate_full and rate_full != "-":
                    try:
                        pct_full = float(rate_full.split("(")[1].split("%")[0])
                        all_rates.append(pct_full)
                    except Exception:
                        pass
                for n in range(1, 6):
                    rate_str = rec['per_window'][n]["rate"]
                    if rate_str != "-":
                        try:
                            pct = float(rate_str.split("(")[1].split("%")[0])
                            cols[n].append(pct)
                        except Exception:
                            pass
            for n in range(1, 6):
                if cols[n]:
                    avg_pct = sum(cols[n]) / len(cols[n])
                    color_class = "rate-ok" if avg_pct == 100.0 else "rate-bad"
                    avg_cells.append((f"均值 {avg_pct:.1f}%", color_class))
                else:
                    avg_cells.append(("-", ""))
            avg_alive = sum(alive_counts) / len(alive_counts) if alive_counts else 0
            avg_alive_value = f"均存活数 {avg_alive:.2f}"
            avg_all = sum(all_rates) / len(all_rates) if all_rates else 0
            avg_all_rate = f"全量均值 {avg_all:.1f}%" if all_rates else "全量均值 -"
        else:
            avg_cells = [("-", "")] * 5
            avg_alive_value = "-"
            avg_all_rate = "全量均值 -"

        foot_cells = [
            "<td></td>",
            "<td></td>",
            f"<td>{avg_alive_value}</td>",
            f"<td>{avg_all_rate}</td>",
        ]
        for val, cls in avg_cells:
            if val == "-":
                foot_cells.append("<td>-</td>")
            else:
                foot_cells.append(f"<td class='{cls}'>{val}</td>")

        table = (
            '<div class="hl-res-table-wrapper">'
            '<table class="hl-res-table">'
            '<thead><tr><th>序号</th><th>应用</th><th>启动前存活</th><th>全部</th>'
            '<th>前1</th><th>前2</th><th>前3</th><th>前4</th><th>前5</th></tr></thead>'
            '<tbody>'
            + "".join(rows) +
            '</tbody>'
            '<tfoot><tr>' + "".join(foot_cells) + '</tr></tfoot>'
            '</table></div>'
        )
        return table

    hl_residency_html = build_hl_residency()

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>进程启动与查杀分析报告 - Summary</title>
  <script src="chart.min.js"></script>
  <style>
    body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background:#0b1118; color:#e6edf3; margin:0; padding:24px; }}
    .page {{ max-width:72vw; margin:0 auto; }}
    h1, h2 {{ margin: 0 0 10px; }}
    h3 {{ margin: 14px 0 8px; }}
    .section {{ margin-bottom: 28px; }}
    .cards.single {{ display:block; }}
    .card {{ background:#141c26; padding:12px 14px; border-radius:10px; border:1px solid #1f2a36; box-shadow:0 8px 24px rgba(0,0,0,0.35); }}
    .card-wide {{ width:100%; box-sizing:border-box; }}
    .card-title {{ font-weight:700; color:#f5f7fb; margin-bottom:8px; letter-spacing:0.3px; }}
    .card-row {{ display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #1b2634; }}
    .card-row:last-child {{ border-bottom:none; }}
    .row-label {{ color:#9fb3c8; font-size:13px; }}
    .row-value {{ color:#f5f7fb; font-weight:600; }}
    .chart-grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
    .chart-card {{ background:#101821; border:1px solid #1f2a36; border-radius:12px; padding:10px; box-shadow:0 6px 18px rgba(0,0,0,0.35); position:relative; }}
    .chart-title {{ position:absolute; left:12px; top:10px; color:#9fb3c8; font-size:12px; letter-spacing:0.2px; }}
    .chart-card canvas {{ margin-top:18px; }}
    .timeline {{ border:1px solid #1f2a36; border-radius:12px; background:#101821; padding:10px; max-width:72%; margin:0 auto; }}
    .timeline-row {{ display:grid; grid-template-columns:120px 1fr; gap:8px; padding:6px 8px; border-bottom:1px solid #1b2634; align-items:center; }}
    .timeline-row:last-child {{ border-bottom:none; }}
    .tl-time {{ color:#cdd6e3; font-size:12px; letter-spacing:0.2px; }}
    .tl-content {{ display:flex; justify-content:space-between; width:100%; }}
    .tl-left {{ color:#f5f7fb; font-size:14px; text-align:left; min-height:1.2em; }}
    .tl-right {{ color:#f5f7fb; font-size:14px; text-align:right; min-height:1.2em; }}
    .tl-legend {{ display:flex; gap:8px; padding:4px 8px 8px 8px; }}
    .pill {{ padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; border:1px solid transparent; }}
    .pill-start {{ background:#123b26; color:#6cf0a7; border-color:#1f8a52; }}
    .pill-kill {{ background:#3b1a1a; color:#ffb1b1; border-color:#a54444; }}
    .pill-lmk {{ background:#2b2140; color:#d6b6ff; border-color:#6f4bb7; }}
    .hl-res-table-wrapper {{ max-width:72vw; margin:12px auto 0 auto; overflow-x:auto; }}
    .hl-res-table {{ width:100%; border-collapse: collapse; background:#101821; border:1px solid #1f2a36; }}
    .hl-res-table th, .hl-res-table td {{ padding:8px 10px; border-bottom:1px solid #1f2a36; text-align:left; color:#e6edf3; }}
    .hl-res-table th {{ color:#9fb3c8; font-size:12px; letter-spacing:0.3px; }}
    .hl-res-table tbody tr:hover {{ background:#142032; }}
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
    .kv {{ margin:2px 0; }}
    .kv strong {{ color:#8fb4ff; }}
    .pill {{ display:inline-block; padding:2px 8px; margin:2px 4px 2px 0; border-radius:999px; background:#16263a; color:#cfe1ff; font-size:12px; }}
    .headline-card {{ display:grid; grid-template-columns:60% 40%; gap:16px; background:#101821; border:1px solid #1f2a36; border-radius:12px; padding:16px; box-shadow:0 8px 24px rgba(0,0,0,0.35); margin-bottom:20px; }}
    .head-left, .head-right {{ display:flex; flex-direction:column; gap:10px; }}
    .head-title {{ color:#9fb3c8; font-size:13px; letter-spacing:0.4px; text-transform:uppercase; }}
    .head-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap:10px; }}
    .head-item {{ background:#0f1722; border:1px solid #1f2a36; border-radius:10px; padding:10px; }}
    .hi-label {{ color:#9fb3c8; font-size:12px; }}
    .hi-val {{ font-size:22px; font-weight:700; color:#f5f7fb; }}
    .hi-val.danger {{ color:#ff8b8b; }}
    .hi-val.lmk {{ color:#d6b6ff; }}
    .head-big {{ background:#0f1722; border:1px solid #1f2a36; border-radius:10px; padding:12px; }}
    .big-line {{ font-size:18px; font-weight:700; color:#cfe1ff; display:flex; gap:8px; align-items:center; }}
    .head-sub {{ color:#9fb3c8; font-size:13px; display:flex; flex-direction:column; gap:4px; }}
    .head-micro {{ color:#9fb3c8; font-size:12px; line-height:1.4; }}
    .mem-table {{ width:100%; border-collapse: collapse; margin-top:6px; }}
    .mem-table th, .mem-table td {{ padding:6px 8px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:left; }}
    .mem-table th {{ color:#9fb3c8; font-weight:600; }}
    .mem-table tbody tr:hover {{ background:#142032; }}
    .mem-metric {{ color:#cfe1ff; font-weight:600; white-space:nowrap; }}
    .hl-run-table {{ width:100%; border-collapse:collapse; background:#101821; border:1px solid #1f2a36; }}
    .hl-run-table th, .hl-run-table td {{ padding:6px 8px; border-bottom:1px solid #1f2a36; color:#e6edf3; font-size:12px; text-align:left; }}
    .hl-run-table th {{ color:#9fb3c8; font-weight:600; }}
    .hl-run-table tbody tr:nth-child(odd) {{ background:#111b27; }}
  </style>
</head>
<body>
  <div class="page">
  <h1>进程启动与查杀分析报告（Summary）</h1>

  <div class="headline-card">
    <div class="head-left">
      <div class="head-title">全部概览</div>
      <div class="head-grid">
        <div class="head-item"><div class="hi-label">事件总数</div><div class="hi-val">{s['total_events']}</div></div>
        <div class="head-item"><div class="hi-label">启动</div><div class="hi-val">{s['start_count']}</div></div>
        <div class="head-item"><div class="hi-label">上层/一体化查杀</div><div class="hi-val danger">{s['kill_count']}</div></div>
        <div class="head-item"><div class="hi-label">底层/LMKD 查杀</div><div class="hi-val lmk">{s['lmk_count']}</div></div>
        <div class="head-item span2"><div class="hi-label">释放内存(KB)</div><div class="hi-val">{s['total_release_mem']:,}</div></div>
      </div>
    </div>
    <div class="head-right">
      <div class="head-title">高亮主进程</div>
      <div class="head-big">
        <div class="big-line">kill <span class="hi-val danger">{s['highlight_overall']['main_kill']}</span></div>
        <div class="big-line">lmk <span class="hi-val lmk">{s['highlight_overall']['main_lmk']}</span></div>
      </div>
      <div class="head-sub">
        <div>主进程 kill {s['main_overall']['kill']}</div>
        <div>主进程 lmk {s['main_overall']['lmk']}</div>
      </div>
        <div class="head-micro">
        <div>高亮驻留均值：全量 {residency_avg['all_rate']:.1f}% | 前1 {residency_avg['rates'][1]:.1f}% | 前2 {residency_avg['rates'][2]:.1f}% | 前3 {residency_avg['rates'][3]:.1f}% | 前4 {residency_avg['rates'][4]:.1f}% | 前5 {residency_avg['rates'][5]:.1f}%</div>
        <div>平均存活数：{residency_avg['alive']:.2f}</div>
      </div>
    </div>
  </div>

  <div class="cards single">
    {mem_avg_card_html}
  </div>

  <div class="section">
    <h2>全部</h2>
    <div class="cards single">{overall_cards}</div>
    <div class="chart-grid">
      <div class="chart-card"><div class="chart-title">查杀类型分布（NPW/EPW/…）</div><canvas id="chartKillType"></canvas></div>
      <div class="chart-card"><div class="chart-title">一体化分数分布（minScore）</div><canvas id="chartMinScore"></canvas></div>
      <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj 分布</div><canvas id="chartAdj"></canvas></div>
      <div class="chart-card"><div class="chart-title">底层/LMKD 查杀原因</div><canvas id="chartLmkReason"></canvas></div>
      <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj 分布</div><canvas id="chartLmkAdj"></canvas></div>
    </div>
  </div>

  <div class="section">
    <h2>主进程</h2>
    <div class="cards single">
      {overview_block("主进程", {
        'total': s['main_overall']['kill'] + s['main_overall']['lmk'],
        'start': 0,
        'kill': s['main_overall']['kill'],
        'lmk': s['main_overall']['lmk'],
        'trig': 0,
        'skip': 0,
        'mem': s['total_release_mem'],
      })}
    </div>
    <div class="cards single">
      <div class="card card-wide">
        <div class="card-title">高亮主进程启动/驻留表（前两轮）</div>
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
    <div class="chart-grid">
      <div class="chart-card"><div class="chart-title">查杀类型分布（主进程）</div><canvas id="chartMainKillType"></canvas></div>
      <div class="chart-card"><div class="chart-title">一体化分数分布（主进程）</div><canvas id="chartMainMinScore"></canvas></div>
      <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj（主进程）</div><canvas id="chartMainAdj"></canvas></div>
      <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj（主进程）</div><canvas id="chartMainLmkAdj"></canvas></div>
    </div>
  </div>

  <div class="section">
    <h2>高亮主进程</h2>
    <div class="cards single">
      {overview_block("高亮主进程", {
        'total': s['highlight_overall']['main_kill'] + s['highlight_overall']['main_lmk'],
        'start': 0,
        'kill': s['highlight_overall']['main_kill'],
        'lmk': s['highlight_overall']['main_lmk'],
        'trig': 0,
        'skip': 0,
        'mem': s['total_release_mem'],
      })}
    </div>
    <div class="cards single">
      <div class="card card-wide">
        <div class="card-title">高亮主进程驻留</div>
        <div class="card-row"><span class="row-label">平均驻留时长</span><span class="row-value">{_format_duration(s.get('highlight_residency_stats',{}).get('avg_duration_sec',0))}</span></div>
        <div class="card-row"><span class="row-label">当前仍存活</span><span class="row-value">{', '.join(s.get('highlight_residency_stats',{}).get('alive_now',[])) or '无'}</span></div>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-card"><div class="chart-title">查杀类型分布（高亮主进程）</div><canvas id="chartHlKillType"></canvas></div>
      <div class="chart-card"><div class="chart-title">一体化分数分布（高亮主进程）</div><canvas id="chartHlMinScore"></canvas></div>
      <div class="chart-card"><div class="chart-title">上层/一体化查杀 adj（高亮主进程）</div><canvas id="chartHlAdj"></canvas></div>
      <div class="chart-card"><div class="chart-title">底层/LMKD 查杀 adj（高亮主进程）</div><canvas id="chartHlLmkAdj"></canvas></div>
    </div>
    <h3>高亮主进程明细（可展开）</h3>
    <div class="accordion" id="hlAccordion">{hl_detail_html}</div>
    <h3>高亮主进程时间线</h3>
    <div class="timeline">{hl_timeline_html}</div>
    <h3>高亮主进程驻留率（前5次窗口）</h3>
    {hl_residency_html}
  </div>
  </div>

  <script>
    const charts = {json.dumps(_to_plain({
        "kill_type": s.get("kill_type_stats", {}),
        "min_score": s.get("min_score_stats", {}),
        "adj": s.get("adj_stats", {}),
        "lmk_reason": s.get("lmk_reason_stats", {}),
        "lmk_adj": s.get("lmk_adj_stats", {}),
        "main_kill_type": s.get("main_overall", {}).get("kill_type_stats", {}),
        "main_min_score": s.get("main_overall", {}).get("min_score_stats", {}),
        "main_adj": s.get("main_overall", {}).get("adj_stats", {}),
        "main_lmk_adj": s.get("main_overall", {}).get("lmk_adj_stats", {}),
        "hl_kill_type": s.get("highlight_overall", {}).get("main_kill_type_stats", {}),
        "hl_min_score": s.get("highlight_overall", {}).get("main_min_score_stats", {}),
        "hl_adj": s.get("highlight_overall", {}).get("main_adj_stats", {}),
        "hl_lmk_adj": s.get("highlight_overall", {}).get("main_lmk_adj_stats", {}),
    }), ensure_ascii=False)};

    function renderBar(canvasId, dataObj, label) {{
      const ctx = document.getElementById(canvasId);
      if (!ctx) return;
      const labels = Object.keys(dataObj || {{}}); 
      const values = Object.values(dataObj || {{}});
      if (labels.length === 0) {{
        ctx.parentElement.innerHTML = '<div style="color:#9fb3c8;font-size:12px;padding:8px;">暂无数据</div>';
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
    renderBar('chartMainAdj', charts.main_adj, '主进程 adj');                  renderList('listMainAdj', charts.main_adj);
    renderBar('chartMainLmkAdj', charts.main_lmk_adj, '主进程 LMK adj');       renderList('listMainLmkAdj', charts.main_lmk_adj);

    renderBar('chartHlKillType', charts.hl_kill_type, '高亮主进程 kill 类型'); renderList('listHlKillType', charts.hl_kill_type);
    renderBar('chartHlMinScore', charts.hl_min_score, '高亮主进程 minScore');  renderList('listHlMinScore', charts.hl_min_score);
    renderBar('chartHlAdj', charts.hl_adj, '高亮主进程 adj');                 renderList('listHlAdj', charts.hl_adj);
    renderBar('chartHlLmkAdj', charts.hl_lmk_adj, '高亮主进程 LMK adj');      renderList('listHlLmkAdj', charts.hl_lmk_adj);

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

def analyze_log_file(
    file_path: str,
    output_dir: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    output_name: Optional[str] = None,
) -> str:
    """
    无需交互地解析指定日志文件，返回生成的报告路径。
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

    print(f"正在解析日志文件: {file_path}...")
    events = parse_log_file(file_path, start_time=start_time, end_time=end_time)
    print(f"解析完成，共发现 {len(events)} 个事件")

    print(f"正在生成报告: {output_file}...")
    generate_report(events, output_file)
    summary = compute_summary_data(events)
    print(f"正在生成可视化报告: {output_file_html}...")
    generate_report_html(events, summary, output_file_html)
    print(f"报告生成成功: {os.path.abspath(output_file)}")
    print(f"HTML报告: {os.path.abspath(output_file_html)}")

    return output_file


def main():
    def _prompt_time(label: str) -> Optional[datetime]:
        """提示用户输入时间字符串并转换为 datetime；空输入返回 None。"""
        current_year = datetime.now().year
        value = input(f"请输入{label} (格式: MM-DD HH:MM:SS.mmm，回车不限制): ").strip()
        if not value:
            return None
        for fmt in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{current_year}-{value}", f"%Y-{fmt}")
            except ValueError:
                continue
        print("时间格式不正确，已忽略该时间限制。")
        return None

    while True:
        # 获取用户输入
        file_path = input("请输入日志文件路径: ").strip()

        if file_path == "q" or file_path == "exit":
            return
        
        # 校验输入是否为空
        if not file_path:
            print("错误：文件路径不能为空，请重新输入。")
            continue

        # 检查并去除两端的引号（单引号或双引号）
        if len(file_path) > 1:
            if (file_path.startswith('"') and file_path.endswith('"')) or \
               (file_path.startswith("'") and file_path.endswith("'")):
                file_path = file_path[1:-1].strip()
            
        # 校验文件是否存在
        if not os.path.isfile(file_path):
            print(f"错误：文件 '{file_path}' 不存在，请重新输入。")
            continue
            
        # 所有校验通过，退出循环
        break
    # 询问是否启用时间过滤
    apply_time_filter = input("是否按时间段过滤日志? 按Y启用，回车跳过: ").strip().lower() == "y"
    start_time = end_time = None
    if apply_time_filter:
        start_time = _prompt_time("起始时间")
        end_time = _prompt_time("结束时间")

    # 询问输出文件名（不含扩展名），默认使用当前时间戳
    default_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_name = input(f"请输入输出文件名(不含扩展名)，回车默认 {default_name}: ").strip() or default_name

    try:
        output_file = analyze_log_file(file_path, start_time=start_time, end_time=end_time, output_name=output_name)
        # 在支持ANSI颜色的终端中显示提示
        print(f"\n提示: 在支持ANSI颜色的终端中查看报告以获得最佳效果")
        print(f"     在Windows PowerShell中使用: Get-Content {output_file} -Encoding UTF8")
        print(f"     在Linux/macOS中使用: cat {output_file}")
    except Exception as e:
        print(f"处理日志时出错: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
