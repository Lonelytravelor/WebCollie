#!/usr/bin/env python3
# pyright: reportGeneralTypeIssues=false, reportAttributeAccessIssue=false, reportMissingTypeArgument=false, reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownVariableType=false
"""
Collie Bugreport Web Analyzer
基于bugreport的智能分析方法 Web应用
支持按IP隔离用户数据
"""

import os
import sys
import uuid
import json
import html as html_lib
import shutil
import time
import schedule
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Flask, current_app, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.utils import secure_filename
import threading
import mimetypes
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.resolve()
project_root = BASE_DIR.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 添加项目源码路径
src_path = project_root / 'src'
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from web_app.utilities_webadb import register_utilities_routes  # pyright: ignore[reportImplicitRelativeImport, reportMissingImports]

from collie_package.config_loader import load_app_settings
from collie_package.log_tools import parse_cont_startup
from collie_package.log_tools.parse_cont_startup import detect_last_complete_cont_startup_window, detect_cont_startup_windows

bp = Blueprint('collie_web', __name__)

APP_SETTINGS = load_app_settings()

DEFAULT_DATA_FOLDER = BASE_DIR / str(
    APP_SETTINGS.get('storage', {}).get('data_folder', 'user_data')
)
data_folder = DEFAULT_DATA_FOLDER
ALLOWED_EXTENSIONS = set(APP_SETTINGS.get('upload', {}).get('allowed_extensions', ['txt', 'zip']))
MAX_CONTENT_LENGTH = int(
    APP_SETTINGS.get('upload', {}).get('max_content_length_mb', 500)
) * 1024 * 1024
DATA_RETENTION_DAYS = int(
    APP_SETTINGS.get('storage', {}).get('data_retention_days', 7)
)
DOCS_DIR = project_root / 'docs'

tasks = {}
tasks_lock = threading.Lock()


def create_app(test_config=None):
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / 'templates'),
        static_folder=str(BASE_DIR / 'static'),
    )
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        DATA_FOLDER=str(DEFAULT_DATA_FOLDER),
        DATA_RETENTION_DAYS=DATA_RETENTION_DAYS,
        TRUST_PROXY_HEADERS=bool(APP_SETTINGS.get('server', {}).get('trust_proxy_headers', False)),
    )
    if test_config:
        app.config.update(test_config)

    data_dir = Path(str(app.config.get('DATA_FOLDER') or DEFAULT_DATA_FOLDER)).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    global data_folder
    data_folder = data_dir
    app.config['DATA_FOLDER'] = str(data_folder)

    app.register_blueprint(bp)
    register_utilities_routes(app, get_client_ip, get_user_folder)
    return app

def get_client_ip():
    """获取客户端真实IP"""
    trust_proxy = bool(current_app.config.get('TRUST_PROXY_HEADERS', False))
    if trust_proxy:
        forwarded_for = request.headers.get('X-Forwarded-For')
        if forwarded_for:
            ip = forwarded_for.split(',')[0].strip()
        elif request.headers.get('X-Real-IP'):
            ip = request.headers.get('X-Real-IP')
        else:
            ip = request.remote_addr
    else:
        ip = request.remote_addr
    return ip or 'unknown'


def _is_within_dir(base: Path, target: Path) -> bool:
    try:
        return os.path.commonpath([str(base), str(target)]) == str(base)
    except Exception:
        return False

def get_user_folder(ip):
    """获取用户专属目录"""
    data_root = Path(str(current_app.config.get('DATA_FOLDER') or data_folder))
    user_folder = data_root / ip.replace(':', '_')
    user_folder.mkdir(exist_ok=True)
    (user_folder / 'uploads').mkdir(exist_ok=True)
    (user_folder / 'results').mkdir(exist_ok=True)
    return user_folder


def get_user_rd_mem_compare_folder(ip):
    user_folder = get_user_folder(ip)
    mem_compare_folder = user_folder / 'rd_mem_compare'
    mem_compare_folder.mkdir(exist_ok=True)
    return mem_compare_folder


def _allowed_mem_design_file(filename: str) -> bool:
    name = str(filename or '')
    return '.' in name and name.rsplit('.', 1)[1].lower() == 'txt'


def _build_mem_design_compare_report(file_a: str, file_b: str) -> str:
    from collie_package.utilities import compare_android_mem_design as cad

    lines_a = cad.load_file(file_a)
    lines_b = cad.load_file(file_b)

    return cad.build_report_from_lines(lines_a, lines_b)

def get_user_uploads_folder(ip):
    """获取用户上传目录"""
    return get_user_folder(ip) / 'uploads'

def get_user_results_folder(ip):
    """获取用户结果目录"""
    return get_user_folder(ip) / 'results'


def _format_event_time_ms(dt):
    if not dt:
        return ''
    return dt.strftime("%m-%d %H:%M:%S.%f")[:-3]


def _build_memory_analysis_bundle(events, summary, highlight_apps):
    if not events or not summary:
        return {}

    pcs = parse_cont_startup
    highlight_list = pcs._normalize_app_list(highlight_apps or [])
    highlight_set = set(highlight_list)
    include_all = not highlight_set
    process_metrics = defaultdict(lambda: defaultdict(list))
    highlight_events = []
    all_kill_events = []
    highlight_timeline = []
    max_events = 400

    for idx, event in enumerate(events):
        etype = event.get('type')
        if etype not in ('kill', 'lmk', 'start'):
            continue
        if event.get('is_subprocess'):
            continue

        base = event.get('process_name', '').split(':')[0]
        event_time = event.get('time')
        time_ts = int(event_time.timestamp() * 1000) if isinstance(event_time, datetime) else None
        details = event.get('details') or {}
        metrics = pcs._extract_mem_metrics(event)
        if etype in ('kill', 'lmk') and not metrics:
            continue

        if etype in ('kill', 'lmk'):
            all_kill_events.append({
                'event_id': idx + 1,
                'process': base,
                'type': etype,
                'time': _format_event_time_ms(event_time),
                'time_ts': time_ts,
                'reason': pcs._extract_kill_reason(event),
                'mem_free': metrics.get('mem_free') if metrics else None,
                'file_pages': metrics.get('file_pages') if metrics else None,
                'anon_pages': metrics.get('anon_pages') if metrics else None,
                'swap_free': metrics.get('swap_free') if metrics else None,
            })

        if include_all or base in highlight_set:
            kill_info = details.get('kill_info') or {}
            if isinstance(kill_info, list):
                kill_info = kill_info[0] if kill_info else {}
            if etype == 'kill':
                kill_type = kill_info.get('killTypeDesc') or kill_info.get('killType') or 'KILL'
                adj = (details.get('proc_info') or {}).get('adj')
            else:
                kill_type = 'LMK'
                adj = details.get('adj')

            if etype in ('kill', 'lmk'):
                highlight_events.append({
                    'event_id': idx + 1,
                    'process': base,
                    'type': etype,
                    'time': _format_event_time_ms(event_time),
                    'time_ts': time_ts,
                    'kill_type': kill_type,
                    'adj': adj or '',
                    'reason': pcs._extract_kill_reason(event),
                    'mem_free': metrics.get('mem_free') if metrics else None,
                    'file_pages': metrics.get('file_pages') if metrics else None,
                    'anon_pages': metrics.get('anon_pages') if metrics else None,
                    'swap_free': metrics.get('swap_free') if metrics else None,
                })

                bucket = process_metrics[base]
                for metric_key, value in metrics.items():
                    if value is not None:
                        bucket[metric_key].append(value)

            highlight_timeline.append({
                'event_id': idx + 1,
                'process': base,
                'type': etype,
                'time': _format_event_time_ms(event_time),
                'time_ts': time_ts,
                'start_kind': details.get('start_kind') or '',
                'launch_source': details.get('launch_source') or '',
                'had_proc_start': bool(details.get('had_proc_start')),
                'kill_type': kill_type if etype in ('kill', 'lmk') else '',
                'adj': adj or '',
                'reason': pcs._extract_kill_reason(event) if etype in ('kill', 'lmk') else '',
            })

    if len(highlight_events) > max_events:
        highlight_events = highlight_events[:max_events]
    if len(all_kill_events) > max_events:
        all_kill_events = all_kill_events[:max_events]

    pcs_calc_stats = pcs._calc_stats
    process_stats = []
    for proc, metric_map in process_metrics.items():
        metrics_summary = {}
        sample_count = 0
        for metric_key, values in metric_map.items():
            stats = pcs_calc_stats(values)
            metrics_summary[metric_key] = stats
            sample_count = max(sample_count, stats.get('count', 0) or 0)
        process_stats.append({
            'process': proc,
            'sample_count': sample_count,
            'metrics': metrics_summary,
        })

    process_stats.sort(key=lambda item: (-item['sample_count'], item['process']))
    available_processes = [item['process'] for item in process_stats]
    if not highlight_list and available_processes:
        highlight_list = available_processes[:]

    low_memfree_rows = []
    for rec in summary.get('low_memfree_kills', []) or []:
        low_memfree_rows.append({
            'process': rec.get('process'),
            'mem_free': rec.get('mem_free'),
            'event_id': rec.get('event_id'),
            'type': rec.get('type'),
            'time': _format_event_time_ms(rec.get('time')),
        })

    summary_plain = pcs._to_plain({
        'mem_stats': summary.get('mem_stats', {}),
        'mem_avg': summary.get('mem_avg', {}),
    })
    mem_stats = summary_plain.get('mem_stats', {})
    mem_avg = summary_plain.get('mem_avg', {})
    counts = {
        'total_events': summary.get('total_events', 0),
        'kill': summary.get('kill_count', 0),
        'lmk': summary.get('lmk_count', 0),
        'highlight_main_kill': (summary.get('highlight_overall') or {}).get('main_kill', 0),
        'highlight_main_lmk': (summary.get('highlight_overall') or {}).get('main_lmk', 0),
    }

    return {
        'highlight_processes': highlight_list,
        'available_processes': available_processes,
        'highlight_events': highlight_events,
        'all_events': all_kill_events,
        'highlight_timeline': highlight_timeline,
        'process_stats': process_stats,
        'low_memfree_kills': low_memfree_rows,
        'mem_stats': mem_stats,
        'mem_avg': mem_avg,
        'counts': counts,
    }


def _run_cont_startup_analysis(
    file_path: str,
    result_dir: str,
    output_name: str,
    apps: Optional[list],
    *,
    start_time=None,
    end_time=None,
    auto_match_info: Optional[dict] = None,
    include_startup_section: bool = True,
):
    pcs = parse_cont_startup
    os.makedirs(result_dir, exist_ok=True)
    base = output_name or datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = os.path.join(result_dir, f"{base}.txt")
    output_file_html = os.path.join(result_dir, f"{base}.html")
    output_file_device_info = os.path.join(result_dir, f"{base}_device_info.txt")
    output_file_meminfo = os.path.join(result_dir, f"{base}_meminfo_summary.txt")

    effective_highlight = pcs._normalize_app_list(apps or [])
    effective_heatmap = pcs._normalize_app_list(apps or effective_highlight)

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path
    summary = None
    report_txt = ''
    memory_analysis = {}

    try:
        resolved_file_path, cleanup_path, source_desc = pcs._resolve_log_input_path(file_path)
        with pcs._temporary_highlight_processes(effective_highlight):
            events = pcs.parse_log_file(resolved_file_path, start_time=start_time, end_time=end_time)
            device_info = pcs._extract_device_info_from_bugreport(resolved_file_path)
            auto_match_payload = pcs._to_plain(auto_match_info or {})
            if auto_match_payload:
                device_info = dict(device_info or {})
                device_info['auto_match_info'] = auto_match_payload
            meminfo_bundle = pcs._build_meminfo_summary_bundle(resolved_file_path, source_desc)
            pcs.generate_report(events, output_file)
            summary = pcs.compute_summary_data(events)
            pcs.generate_report_html(
                events,
                summary,
                output_file_html,
                heatmap_apps=effective_heatmap,
                device_info=device_info,
                meminfo_bundle=meminfo_bundle,
                include_startup_section=include_startup_section,
            )
            pcs.save_device_info_report_text(device_info, output_file_device_info, source_desc=source_desc)
            report_txt = str(meminfo_bundle.get('report_txt', '') or '').strip()
            if report_txt:
                with open(output_file_meminfo, 'w', encoding='utf-8') as f:
                    f.write(report_txt)
            memory_analysis = _build_memory_analysis_bundle(events, summary, effective_highlight)
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    return {
        'text_file': output_file,
        'html_file': output_file_html,
        'device_info_file': output_file_device_info,
        'meminfo_file': output_file_meminfo if report_txt else None,
        'memory_analysis': memory_analysis,
        'summary': pcs._to_plain(summary) if summary else {},
    }


def _is_primary_analysis_txt_name(file_name: str) -> bool:
    name = str(file_name or '')
    if not name.endswith('.txt'):
        return False
    if 'device_info' in name or 'meminfo' in name:
        return False
    if 'bugreport_context' in name:
        return False
    if name.startswith('ai_interpret_'):
        return False
    return True


def _collect_kill_focus_candidates(file_path: str, package_name: str) -> Tuple[str, List[Tuple[int, dict]]]:
    pcs = parse_cont_startup
    pkg = str(package_name or '').strip()
    if not pkg:
        raise ValueError('包名不能为空')

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path
    try:
        resolved_file_path, cleanup_path, source_desc = pcs._resolve_log_input_path(file_path)
        events = pcs.parse_log_file(resolved_file_path)
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    if not events:
        raise ValueError('日志中未解析到任何事件')

    candidates = pcs._find_kill_candidates_for_package(events, pkg)
    return source_desc, candidates


def _serialize_kill_focus_candidate(event_idx: int, event: dict) -> Dict[str, Any]:
    pcs = parse_cont_startup
    mem_snapshot = pcs._extract_event_mem_snapshot(event)
    return {
        'event_idx': event_idx,
        'event_id': event_idx + 1,
        'time': pcs._fmt_event_time(event.get('time')),
        'type': str(event.get('type') or ''),
        'process': str(event.get('full_name') or event.get('process_name') or ''),
        'reason': pcs._extract_kill_reason(event),
        'mem_free_kb': str(mem_snapshot.get('mem_free_kb') or ''),
        'is_subprocess': bool(event.get('is_subprocess')),
    }


def _run_kill_focus_analysis(
    file_path: str,
    result_dir: str,
    output_name: str,
    package_name: str,
    *,
    target_event_idx: Optional[int] = None,
) -> Dict[str, Any]:
    pcs = parse_cont_startup
    pkg = str(package_name or '').strip()
    if not pkg:
        raise ValueError('kill_focus 模式下 package_name 不能为空')

    os.makedirs(result_dir, exist_ok=True)
    base = output_name or datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_base = pcs._sanitize_output_name(base, fallback='kill_focus')
    output_file = os.path.join(result_dir, f'{safe_base}.txt')
    output_file_html = os.path.join(result_dir, f'{safe_base}.html')
    output_file_context = os.path.join(result_dir, f'{safe_base}_bugreport_context.txt')

    resolved_file_path = file_path
    cleanup_path = None
    source_desc = file_path

    try:
        resolved_file_path, cleanup_path, source_desc = pcs._resolve_log_input_path(file_path)
        events = pcs.parse_log_file(resolved_file_path)

        if not events:
            raise ValueError('日志中未解析到任何事件')

        candidates = pcs._find_kill_candidates_for_package(events, pkg)
        if not candidates:
            raise ValueError(f'未找到包名 {pkg} 的 kill/lmk 事件')

        selected = None
        if target_event_idx is not None:
            try:
                selected_idx = int(target_event_idx)
            except Exception:
                raise ValueError('target_event_idx 必须是整数')
            for idx, event in candidates:
                if idx == selected_idx:
                    selected = (idx, event)
                    break
            if selected is None:
                raise ValueError('所选被杀事件不存在，请重新选择')
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            raise ValueError(f'包名 {pkg} 匹配到 {len(candidates)} 条被杀记录，请先选择目标记录')

        event_idx, target_event = selected
        report_text = pcs.build_kill_focus_report(
            events=events,
            source_desc=source_desc,
            package_name=pkg,
            target_event_idx=event_idx,
            target_event=target_event,
        )
        context_info = pcs.extract_kill_focus_bugreport_context(
            resolved_file_path,
            target_event=target_event,
            before_lines=300,
            after_lines=100,
        )
        context_text = pcs.format_kill_focus_bugreport_context(context_info, source_desc=source_desc)
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(report_text)
        f.write('\n\n')
        f.write(context_text)
        f.write('\n')
    with open(output_file_context, 'w', encoding='utf-8') as f:
        f.write(context_text)
    pcs.generate_kill_focus_report_html(report_text, output_file_html)

    return {
        'text_file': output_file,
        'html_file': output_file_html,
        'device_info_file': None,
        'meminfo_file': None,
        'bugreport_context_file': output_file_context,
        'memory_analysis': {},
        'selected_event_idx': event_idx,
        'candidate_count': len(candidates),
    }

def _feishu_request(method, url, token, payload=None):
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json; charset=utf-8',
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'飞书接口调用失败: HTTP {exc.code} {detail[:500]}')
    except Exception as exc:
        raise RuntimeError(f'飞书接口调用异常: {str(exc)}')


def _create_feishu_doc(token, title):
    result = _feishu_request(
        'POST',
        'https://open.feishu.cn/open-apis/docx/v1/documents',
        token,
        {'title': title},
    )
    document = result.get('data', {}).get('document', {}) if isinstance(result, dict) else {}
    doc_id = document.get('document_id')
    doc_url = document.get('url', '')
    if not doc_id:
        raise RuntimeError(f'创建飞书文档失败: {result}')
    return doc_id, doc_url


def _append_feishu_text(token, doc_id, content):
    lines = content.splitlines() or ['']
    chunks = []
    step = 40
    for i in range(0, len(lines), step):
        chunks.append(lines[i:i + step])

    for chunk in chunks:
        children = []
        for line in chunk:
            text = (line or ' ').strip('\r')
            if len(text) > 1200:
                text = text[:1200]
            children.append({
                'block_type': 2,
                'paragraph': {
                    'elements': [
                        {
                            'text_run': {
                                'content': text if text else ' '
                            }
                        }
                    ]
                }
            })

        _feishu_request(
            'POST',
            f'https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children',
            token,
            {'children': children},
        )

def clean_old_data():
    """清理超过7天的数据"""
    current_time = time.time()
    retention_seconds = DATA_RETENTION_DAYS * 24 * 60 * 60
    
    for user_folder in data_folder.iterdir():
        if not user_folder.is_dir():
            continue
            
        for folder_name in ['uploads', 'results']:
            folder = user_folder / folder_name
            if not folder.exists():
                continue
                
            for item in folder.iterdir():
                try:
                    item_time = item.stat().st_mtime
                    if current_time - item_time > retention_seconds:
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                except Exception as e:
                    print(f"清理文件失败 {item}: {e}")
        
        try:
            if user_folder.exists() and not any(user_folder.iterdir()):
                user_folder.rmdir()
        except:
            pass
    
    with tasks_lock:
        current_time_dt = datetime.now()
        expired_tasks = []
        for task_id, task in tasks.items():
            created_at = datetime.strptime(task['created_at'], '%Y%m%d_%H%M%S')
            if (current_time_dt - created_at).days > DATA_RETENTION_DAYS:
                expired_tasks.append(task_id)
        
        for task_id in expired_tasks:
            del tasks[task_id]

def start_cleanup_scheduler():
    """启动定时清理任务"""
    schedule.every().day.at("02:00").do(clean_old_data)
    
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(3600)
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()


def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_preset_apps(scene):
    """根据场景获取预设的应用列表"""
    config = parse_cont_startup._load_app_config()
    if not config:
        return []
    
    if scene == 'dynamic_performance':
        key = '动态性能模型(TOP20)'
    elif scene == 'nine_scene':
        key = '九大场景-驻留'
    else:
        return []
    
    apps = config.get(key, [])
    return apps if isinstance(apps, list) else []


def get_available_presets():
    """获取所有可用的预设配置"""
    config = parse_cont_startup._load_app_config()
    if not config:
        return []
    
    presets = []
    for key in config.keys():
        if isinstance(config[key], list) and len(config[key]) > 0:
            presets.append({
                'name': key,
                'apps': config[key][:5],  # 只显示前5个作为预览
                'count': len(config[key])
            })
    return presets


@bp.route('/')
def index():
    """主页"""
    presets = get_available_presets()
    return render_template('index.html', presets=presets)


def _safe_resolve_doc(path_text: str) -> Optional[Path]:
    """Resolve docs path safely under DOCS_DIR."""
    if not path_text or not path_text.endswith('.md'):
        return None
    candidate = (DOCS_DIR / path_text).resolve()
    if DOCS_DIR not in candidate.parents and candidate != DOCS_DIR:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


@bp.route('/api/docs')
def api_docs_list():
    docs = []
    if not DOCS_DIR.exists():
        return jsonify({'docs': docs})

    for path in sorted(DOCS_DIR.rglob('*.md')):
        rel = path.relative_to(DOCS_DIR).as_posix()
        preview_lines = []
        title = path.stem
        line_count = 0
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
            lines = content.replace('\r\n', '\n').split('\n')
            line_count = len(lines)
            preview_lines = lines[:6]
            for line in preview_lines:
                if line.startswith('# '):
                    title = line.lstrip('# ').strip() or title
                    break
        except Exception:
            preview_lines = []
        docs.append({
            'path': rel,
            'name': title,
            'preview_lines': preview_lines,
            'line_count': line_count,
        })

    return jsonify({'docs': docs})


@bp.route('/api/docs/<path:doc_path>')
def api_docs_content(doc_path: str):
    doc_file = _safe_resolve_doc(doc_path)
    if not doc_file:
        return jsonify({'error': '文档不存在'}), 404
    try:
        content = doc_file.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return jsonify({'error': '读取文档失败'}), 500
    return jsonify({
        'path': doc_path,
        'name': doc_file.stem,
        'content': content,
    })


@bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件类型，仅支持 .txt 和 .zip'}), 400
    
    client_ip = get_client_ip()
    user_uploads = get_user_uploads_folder(client_ip)
    
    task_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    filename = secure_filename(file.filename or 'unknown')
    upload_name = f"{task_id}_{filename}"
    upload_path = user_uploads / upload_name
    file.save(str(upload_path))
    
    user_results = get_user_results_folder(client_ip)
    result_dir = user_results / task_id
    result_dir.mkdir(exist_ok=True)
    
    with tasks_lock:
        tasks[task_id] = {
            'id': task_id,
            'ip': client_ip,
            'status': 'uploaded',
            'filename': filename,
            'upload_path': str(upload_path),
            'result_dir': str(result_dir),
            'created_at': timestamp,
            'scene': None,
            'progress': 0,
            'message': '文件已上传，等待分析'
        }
    
    return jsonify({
        'task_id': task_id,
        'filename': filename,
        'status': 'uploaded'
    })


@bp.route('/api/rd/mem-design/compare', methods=['POST'])
def rd_mem_design_compare():
    if 'file_a' not in request.files or 'file_b' not in request.files:
        return jsonify({'error': '请上传两份对比文件'}), 400

    file_a = request.files['file_a']
    file_b = request.files['file_b']
    if file_a.filename == '' or file_b.filename == '':
        return jsonify({'error': '文件名不能为空'}), 400

    if not _allowed_mem_design_file(file_a.filename) or not _allowed_mem_design_file(file_b.filename):
        return jsonify({'error': '仅支持 .txt 文件'}), 400

    client_ip = get_client_ip()
    compare_root = get_user_rd_mem_compare_folder(client_ip)

    job_id = str(uuid.uuid4())[:8]
    job_dir = (compare_root / job_id).resolve()
    job_dir.mkdir(exist_ok=True)

    filename_a = secure_filename(file_a.filename or 'device_a.txt')
    filename_b = secure_filename(file_b.filename or 'device_b.txt')
    path_a = job_dir / f"A_{filename_a}"
    path_b = job_dir / f"B_{filename_b}"
    file_a.save(str(path_a))
    file_b.save(str(path_b))

    try:
        report_text = _build_mem_design_compare_report(str(path_a), str(path_b))
    except Exception as exc:
        return jsonify({'error': f'对比失败: {exc}'}), 400

    base_a = os.path.splitext(filename_a)[0]
    base_b = os.path.splitext(filename_b)[0]
    output_name = f"mem_design_diff_{base_a}_vs_{base_b}.txt"
    output_path = job_dir / output_name
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    return jsonify({
        'job_id': job_id,
        'file_a': filename_a,
        'file_b': filename_b,
        'report_file': output_name,
        'report_text': report_text,
    })


@bp.route('/api/rd/mem-design/download/<job_id>/<path:filename>')
def rd_mem_design_download(job_id, filename):
    client_ip = get_client_ip()
    compare_root = get_user_rd_mem_compare_folder(client_ip).resolve()
    job_dir = (compare_root / str(job_id)).resolve()
    if not _is_within_dir(compare_root, job_dir):
        return jsonify({'error': '非法路径'}), 400
    file_path = (job_dir / str(filename)).resolve()
    if not _is_within_dir(job_dir, file_path):
        return jsonify({'error': '非法路径'}), 400
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404
    mimetype, _ = mimetypes.guess_type(str(file_path))
    return send_file(
        str(file_path),
        mimetype=mimetype or 'text/plain',
        as_attachment=True,
        download_name=file_path.name,
    )


@bp.route('/api/kill-focus/candidates', methods=['POST'])
def analyze_kill_focus_candidates():
    data = request.json or {}
    task_id = data.get('task_id')
    package_name = str(data.get('package_name', '')).strip()
    client_ip = get_client_ip()

    with tasks_lock:
        if not task_id or task_id not in tasks:
            return jsonify({'error': '无效的任务ID'}), 400
        task = tasks[task_id]
        if task['ip'] != client_ip:
            return jsonify({'error': '无权访问此任务'}), 403
        upload_path = task.get('upload_path') or ''

    if not package_name:
        return jsonify({'error': 'package_name 不能为空'}), 400
    if not upload_path or not os.path.isfile(upload_path):
        return jsonify({'error': '任务关联的上传文件不存在'}), 400

    try:
        source_desc, raw_candidates = _collect_kill_focus_candidates(upload_path, package_name)
        candidates = [_serialize_kill_focus_candidate(idx, event) for idx, event in raw_candidates]
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    return jsonify(
        {
            'task_id': task_id,
            'package_name': package_name,
            'source_desc': source_desc,
            'count': len(candidates),
            'candidates': candidates,
        }
    )


@bp.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    task_id = data.get('task_id')
    scene = data.get('scene', 'custom')
    custom_apps = data.get('custom_apps', [])
    mode = data.get('mode', 'quick')
    time_range = data.get('time_range')
    kill_focus = data.get('kill_focus') or {}
    if not isinstance(kill_focus, dict):
        return jsonify({'error': 'kill_focus 参数格式错误'}), 400

    if scene == 'kill_focus':
        package_name = str(kill_focus.get('package_name', '')).strip()
        if not package_name:
            return jsonify({'error': 'kill_focus 模式下必须提供 package_name'}), 400
        if 'target_event_idx' in kill_focus and kill_focus.get('target_event_idx') not in (None, ''):
            try:
                kill_focus['target_event_idx'] = int(kill_focus.get('target_event_idx'))
            except Exception:
                return jsonify({'error': 'kill_focus.target_event_idx 必须是整数'}), 400
    
    client_ip = get_client_ip()
    
    with tasks_lock:
        if not task_id or task_id not in tasks:
            return jsonify({'error': '无效的任务ID'}), 400
        
        task = tasks[task_id]
        
        if task['ip'] != client_ip:
            return jsonify({'error': '无权访问此任务'}), 403
        
        # 如果任务正在等待确认，更新参数并继续
        if task.get('needs_confirm'):
            task['mode'] = mode
            task['time_range'] = time_range
            task['kill_focus'] = kill_focus
            task['needs_confirm'] = False
            task['progress'] = 10
            task['message'] = '正在切换到手动模式...'
        elif task['status'] == 'analyzing':
            return jsonify({'error': '任务正在分析中'}), 400
        else:
            task['status'] = 'analyzing'
            task['scene'] = scene
            task['mode'] = mode
            task['time_range'] = time_range
            task['kill_focus'] = kill_focus
            task['message'] = '正在分析...'
            task['progress'] = 10
    
    thread = threading.Thread(
        target=run_analysis,
        args=(task_id, scene, custom_apps, client_ip, mode, time_range, kill_focus)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'task_id': task_id,
        'status': 'analyzing',
        'message': '分析已开始'
    })


def run_analysis(task_id, scene, custom_apps, client_ip, mode='quick', time_range=None, kill_focus=None):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task or task['ip'] != client_ip:
            return
    
    try:
        with tasks_lock:
            tasks[task_id]['progress'] = 5
            tasks[task_id]['message'] = '正在准备数据...'
        
        kill_focus = kill_focus if isinstance(kill_focus, dict) else {}
        kill_focus_package = str(kill_focus.get('package_name', '')).strip()
        kill_focus_target_idx = kill_focus.get('target_event_idx')

        if scene == 'dynamic_performance':
            apps = get_preset_apps('dynamic_performance')
            label = '动态性能模型(TOP20)'
            enable_auto_window = True
        elif scene == 'nine_scene':
            apps = get_preset_apps('nine_scene')
            label = '九大场景-驻留'
            enable_auto_window = True
        elif scene == 'kill_focus':
            apps = []
            label = '被杀应用定点分析'
            enable_auto_window = False
            if not kill_focus_package:
                raise ValueError('kill_focus 模式未提供 package_name')
        elif scene == 'custom':
            # 通用分析模式：使用自定义应用列表（用户输入），或使用默认 HighLight 列表
            apps = custom_apps if custom_apps else []
            label = '通用分析'
            enable_auto_window = False  # 通用模式不自动检测时间段
        else:
            apps = []
            label = '默认分析'
            enable_auto_window = False
        
        upload_path = task['upload_path']
        result_dir = task['result_dir']
        output_name = f"analysis_{task['created_at']}"
        if scene == 'kill_focus':
            pkg_safe = parse_cont_startup._sanitize_output_name(kill_focus_package, fallback='proc')
            output_name = f"analysis_{task['created_at']}_{pkg_safe}_kill_focus"
        
        start_time = None
        end_time = None
        confidence = None
        auto_match_info: Optional[Dict[str, Any]] = None
        
        # 检查是否需要继续分析（之前已确认过时间段）
        if task.get('needs_confirm') == False and mode == 'manual' and time_range:
            # 手动模式：使用指定的时间段
            try:
                start_str = time_range['start']
                end_str = time_range['end']
                
                from datetime import datetime
                current_year = datetime.now().year
                
                start_dt = datetime.strptime(f"{current_year}-{start_str}", "%Y-%m-%d %H:%M:%S.%f")
                end_dt = datetime.strptime(f"{current_year}-{end_str}", "%Y-%m-%d %H:%M:%S.%f")
                
                start_time = start_dt
                end_time = end_dt
                
                with tasks_lock:
                    tasks[task_id]['message'] = f'手动模式：{start_str} ~ {end_str}'
            except Exception as e:
                with tasks_lock:
                    tasks[task_id]['message'] = f'时间解析失败: {str(e)}'
        
        # 初始化 auto_match_info（根据不同模式）
        if mode == 'manual' or not enable_auto_window:
            auto_match_info = {
                "enabled": enable_auto_window and bool(apps),
                "target_app_count": len(apps) if apps else 0,
                "rounds": 2 if (enable_auto_window and apps) else 0,
                "detected": False,
                "used": False,
                "status": "手动指定时间段",
            }
            if start_time:
                auto_match_info["applied_start_time"] = start_time.strftime("%Y-%m-%d %H:%M:%S.%f")
            if end_time:
                auto_match_info["applied_end_time"] = end_time.strftime("%Y-%m-%d %H:%M:%S.%f")
        
        # 快速模式：先检测时间段和置信度（仅 dynamic_performance 和 nine_scene 支持）
        elif mode == 'quick' and enable_auto_window:
            # 检查是否需要继续（之前已检测过时间段）
            if task.get('needs_confirm') == True and task.get('status') == 'paused':
                with tasks_lock:
                    tasks[task_id]['status'] = 'analyzing'
                    tasks[task_id]['progress'] = 20
                    tasks[task_id]['message'] = '用户确认，继续分析...'
                # 继续执行下面的分析逻辑
            else:
                # 首次检测：自动定位时间段
                with tasks_lock:
                    tasks[task_id]['progress'] = 15
                    tasks[task_id]['message'] = f'正在自动定位连续启动窗口（目标 {len(apps)} 个应用 x 2次）...'
                
                auto_match_info = {
                    "enabled": bool(enable_auto_window and apps),
                    "target_app_count": len(apps) if apps else 0,
                    "rounds": 2 if (enable_auto_window and apps) else 0,
                    "detected": False,
                    "used": False,
                    "status": "",
                }
                
                try:
                    auto_windows = detect_cont_startup_windows(upload_path, apps, rounds=2)
                    auto_window = auto_windows[0] if auto_windows else None
                    if auto_window:
                        confidence = auto_window.get('confidence', 'UNKNOWN')
                        start_time = auto_window.get('window_start')
                        end_time = auto_window.get('window_end')

                        file_end_time = auto_window.get('file_end_time')
                        bugreport_time_hint = auto_window.get('bugreport_time_hint')
                        
                        match_score = auto_window.get('match_score', 0)
                        matched_count = auto_window.get('matched_start_count', 0)
                        expected_count = auto_window.get('expected_count', 0)
                        mismatch = auto_window.get('mismatch_count', 0)
                        tolerance = auto_window.get('tolerance', 10)
                        
                        msg = f'自动识别完成 - 匹配度: {match_score}%, 匹配 {matched_count}/{expected_count}, 误差 {mismatch}, 置信度: {confidence}'
                        
                        auto_match_info["detected"] = True
                        auto_match_info["used"] = True
                        auto_match_info["status"] = "已识别并采用自动匹配时间段"
                        auto_match_info["window_start"] = (
                            start_time.strftime("%Y-%m-%d %H:%M:%S.%f") if start_time else None
                        )
                        auto_match_info["window_end"] = (
                            end_time.strftime("%Y-%m-%d %H:%M:%S.%f") if end_time else None
                        )
                        auto_match_info["match_score"] = match_score
                        auto_match_info["matched_start_count"] = matched_count
                        auto_match_info["expected_count"] = expected_count
                        auto_match_info["mismatch_count"] = mismatch
                        auto_match_info["tolerance"] = tolerance
                        auto_match_info["observed_count"] = auto_window.get('observed_count', 0)
                        auto_match_info["match_variant"] = auto_window.get('match_variant', '')
                        auto_match_info["duration_sec"] = auto_window.get('duration_sec', 0)
                        auto_match_info["tail_gap_sec"] = auto_window.get('tail_gap_sec', 0)
                        auto_match_info["confidence"] = confidence
                        auto_match_info["file_end_time"] = (
                            file_end_time.strftime("%Y-%m-%d %H:%M:%S.%f") if file_end_time else None
                        )
                        auto_match_info["bugreport_time_hint"] = (
                            bugreport_time_hint.strftime("%Y-%m-%d %H:%M:%S.%f")
                            if bugreport_time_hint
                            else None
                        )
                        auto_match_info["bugreport_to_log_end_gap_sec"] = auto_window.get(
                            'bugreport_to_log_end_gap_sec',
                            0,
                        )
                        
                        with tasks_lock:
                            tasks[task_id]['message'] = msg
                            tasks[task_id]['confidence'] = confidence
                            tasks[task_id]['match_score'] = match_score
                            tasks[task_id]['matched_count'] = matched_count
                            tasks[task_id]['expected_count'] = expected_count
                            tasks[task_id]['mismatch_count'] = mismatch
                            tasks[task_id]['tolerance'] = tolerance
                            tasks[task_id]['auto_window'] = {
                                'start': start_time.isoformat() if start_time else None,
                                'end': end_time.isoformat() if end_time else None
                            }
                            tasks[task_id]['auto_windows'] = [
                                {
                                    'index': idx,
                                    'start': w.get('window_start').isoformat() if w.get('window_start') else None,
                                    'end': w.get('window_end').isoformat() if w.get('window_end') else None,
                                    'match_score': w.get('match_score', 0),
                                    'matched_start_count': w.get('matched_start_count', 0),
                                    'expected_count': w.get('expected_count', 0),
                                    'mismatch_count': w.get('mismatch_count', 0),
                                    'tolerance': w.get('tolerance', 0),
                                    'confidence': w.get('confidence', 'UNKNOWN'),
                                    'duration_sec': w.get('duration_sec', 0),
                                    'tail_gap_sec': w.get('tail_gap_sec', 0),
                                    'match_variant': w.get('match_variant', ''),
                                }
                                for idx, w in enumerate(auto_windows or [])
                            ]
                            tasks[task_id]['auto_window_default'] = max(len(auto_windows or []) - 1, 0)
                            if auto_windows and len(auto_windows) > 1:
                                tasks[task_id]['needs_confirm'] = True
                                tasks[task_id]['status'] = 'paused'
                                tasks[task_id]['message'] = '检测到多轮次自动匹配，请选择一个时间段继续分析'
                            else:
                                tasks[task_id]['needs_confirm'] = False
                    else:
                        auto_match_info["status"] = "未识别到满足顺序/数量要求的完整测试窗口"
                        with tasks_lock:
                            tasks[task_id]['message'] = '⚠️ 自动定位失败，将进行全量解析'
                            tasks[task_id]['needs_confirm'] = False
                except Exception as e:
                    auto_match_info["status"] = "自动定位失败"
                    auto_match_info["detection_error"] = str(e)
                    with tasks_lock:
                        tasks[task_id]['message'] = f'⚠️ 自动定位异常: {str(e)}，将进行全量解析'
                        tasks[task_id]['needs_confirm'] = False
                
                if start_time:
                    auto_match_info["applied_start_time"] = start_time.strftime("%Y-%m-%d %H:%M:%S.%f") if start_time else None
                if end_time:
                    auto_match_info["applied_end_time"] = end_time.strftime("%Y-%m-%d %H:%M:%S.%f") if end_time else None
        
        # 如果 auto_match_info 未定义（边界情况），则初始化默认空值
        if not auto_match_info:
            auto_match_info = {
                "enabled": enable_auto_window and bool(apps),
                "target_app_count": len(apps) if apps else 0,
                "rounds": 2 if (enable_auto_window and apps) else 0,
                "detected": False,
                "used": False,
                "status": "手动指定时间段",
            }
            if start_time:
                auto_match_info["applied_start_time"] = start_time.strftime("%Y-%m-%d %H:%M:%S.%f") if start_time else None
            if end_time:
                auto_match_info["applied_end_time"] = end_time.strftime("%Y-%m-%d %H:%M:%S.%f") if end_time else None
        
        # 手动模式：使用指定的时间段
        elif mode == 'manual' and time_range:
            try:
                # 解析时间格式 MM-DD HH:mm:ss.ms
                start_str = time_range['start']
                end_str = time_range['end']
                
                # 获取当前年份
                from datetime import datetime
                current_year = datetime.now().year
                
                # 解析时间
                start_dt = datetime.strptime(f"{current_year}-{start_str}", "%Y-%m-%d %H:%M:%S.%f")
                end_dt = datetime.strptime(f"{current_year}-{end_str}", "%Y-%m-%d %H:%M:%S.%f")
                
                start_time = start_dt
                end_time = end_dt
                
                with tasks_lock:
                    tasks[task_id]['message'] = f'手动模式：{start_str} ~ {end_str}'
            except Exception as e:
                with tasks_lock:
                    tasks[task_id]['message'] = f'时间解析失败: {str(e)}'
        
        # 显示自动识别成功信息
        if mode == 'quick' and enable_auto_window:
            with tasks_lock:
                if not tasks[task_id].get('needs_confirm'):
                    tasks[task_id]['progress'] = 25
                    tasks[task_id]['message'] = '✅ 时间段识别成功，正在开始解析...'
        
        mode_text = '快速模式' if mode == 'quick' else '手动模式'
        with tasks_lock:
            tasks[task_id]['progress'] = 30
            if scene == 'kill_focus':
                tasks[task_id]['message'] = f'正在解析 bugreport [被杀应用定点]，目标进程: {kill_focus_package}'
            else:
                tasks[task_id]['message'] = f'正在解析bugreport [{mode_text}]，应用列表: {len(apps)}个应用...'
        
        with tasks_lock:
            tasks[task_id]['progress'] = 50
            tasks[task_id]['message'] = '正在生成报告...'
        
        extra_kwargs = {}
        if auto_match_info is not None:
            extra_kwargs['auto_match_info'] = auto_match_info

        if scene == 'kill_focus':
            analysis_payload = _run_kill_focus_analysis(
                file_path=upload_path,
                result_dir=result_dir,
                output_name=output_name,
                package_name=kill_focus_package,
                target_event_idx=kill_focus_target_idx,
            )
        else:
            analysis_payload = _run_cont_startup_analysis(
                file_path=upload_path,
                result_dir=result_dir,
                output_name=output_name,
                apps=apps,
                start_time=start_time,
                end_time=end_time,
                auto_match_info=extra_kwargs.get('auto_match_info'),
            )

        if scene != 'kill_focus' and analysis_payload.get('memory_analysis'):
            mem_analysis_file_name = f"{output_name}_mem_analysis.json"
            mem_path = Path(result_dir) / mem_analysis_file_name
            with open(mem_path, 'w', encoding='utf-8') as f:
                json.dump(analysis_payload['memory_analysis'], f, ensure_ascii=False, indent=2)
        
        task['progress'] = 90
        task['message'] = '正在完成...'
        
        # 查找生成的文件
        result_path = Path(result_dir)
        html_file = (
            Path(str(analysis_payload.get('html_file') or '')).name
            if analysis_payload.get('html_file')
            else None
        )
        txt_file = (
            Path(str(analysis_payload.get('text_file') or '')).name
            if analysis_payload.get('text_file')
            else None
        )
        device_info_file = (
            Path(str(analysis_payload.get('device_info_file') or '')).name
            if analysis_payload.get('device_info_file')
            else None
        )
        mem_analysis_file = None
        bugreport_context_file = (
            Path(str(analysis_payload.get('bugreport_context_file') or '')).name
            if analysis_payload.get('bugreport_context_file')
            else None
        )
        
        for f in result_path.iterdir():
            if f.suffix == '.html' and not f.name.startswith('ai_interpret_') and not html_file:
                html_file = f.name
            elif _is_primary_analysis_txt_name(f.name) and not txt_file:
                txt_file = f.name
            elif 'device_info' in f.name:
                device_info_file = f.name
            elif f.name.endswith('_mem_analysis.json'):
                mem_analysis_file = f.name
            elif f.name.endswith('_bugreport_context.txt'):
                bugreport_context_file = f.name
        
        task['status'] = 'completed'
        task['progress'] = 100
        task['message'] = '分析完成'
        task['results'] = {
            'html_file': html_file,
            'txt_file': txt_file,
            'device_info_file': device_info_file,
            'label': label,
            'apps_count': len(apps) if scene != 'kill_focus' else 1,
            'mem_analysis_file': mem_analysis_file,
        }
        if scene == 'kill_focus':
            task['results']['kill_focus'] = {
                'package_name': kill_focus_package,
                'target_event_idx': analysis_payload.get('selected_event_idx'),
                'candidate_count': analysis_payload.get('candidate_count'),
            }
            if bugreport_context_file:
                task['results']['kill_focus']['bugreport_context_file'] = bugreport_context_file

        # 保存任务元数据到磁盘（包含 scene 信息）
        metadata_file = Path(result_path) / 'task_info.json'
        metadata = {
            'scene': task.get('scene', 'custom'),
            'filename': task.get('filename', ''),
            'created_at': task.get('created_at', ''),
            'label': label
        }
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
    except Exception as e:
        task['status'] = 'error'
        task['message'] = f'分析失败: {str(e)}'
        task['error'] = str(e)
        import traceback
        task['traceback'] = traceback.format_exc()


@bp.route('/api/status/<task_id>')
def get_status(task_id):
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    
    with tasks_lock:
        if task_id in tasks:
            task = tasks[task_id]
            
            if task['ip'] != client_ip:
                return jsonify({'error': '无权访问此任务'}), 403
            
            response = {
                'task_id': task_id,
                'status': task['status'],
                'progress': task['progress'],
                'message': task['message'],
                'filename': task['filename'],
                'created_at': task['created_at']
            }
            
            # 添加置信度信息（如果需要确认）
            if task.get('needs_confirm') and task['status'] in ('analyzing', 'paused'):
                response['needs_confirm'] = True
                response['confidence'] = task.get('confidence', 'UNKNOWN')
                response['match_score'] = task.get('match_score', 0)
            if task.get('auto_windows'):
                response['auto_windows'] = task.get('auto_windows', [])
                response['auto_window_default'] = task.get('auto_window_default', 0)
            
            if task['status'] == 'completed':
                response['results'] = task.get('results', {})
            elif task['status'] == 'error':
                response['error'] = task.get('error', '未知错误')
            
            return jsonify(response)
    
    results_folder = user_folder / 'results' / task_id
    if results_folder.exists():
        result_files = list(results_folder.glob('analysis_*'))
        if result_files:
            txt_file = None
            html_file = None
            meminfo_file = None
            device_info_file = None
            mem_analysis_file = None

            for f in result_files:
                name = f.name
                if 'meminfo' in name:
                    meminfo_file = name
                elif 'device_info' in name:
                    device_info_file = name
                elif name.endswith('_mem_analysis.json'):
                    mem_analysis_file = name
                elif name.endswith('.html') and not name.startswith('ai_interpret_'):
                    html_file = name
                elif _is_primary_analysis_txt_name(name):
                    txt_file = name

            return jsonify({
                'task_id': task_id,
                'status': 'completed',
                'progress': 100,
                    'message': '分析完成',
                    'filename': 'unknown',
                    'created_at': 'unknown',
                    'results': {
                        'txt_file': txt_file,
                        'html_file': html_file,
                        'meminfo_file': meminfo_file,
                        'device_info_file': device_info_file,
                        'mem_analysis_file': mem_analysis_file,
                    }
                })
    
    return jsonify({'error': '任务不存在'}), 404


@bp.route('/api/download/<task_id>/<path:filename>')
def download_result(task_id, filename):
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    result_dir = user_folder / 'results' / task_id
    
    task_in_memory = False
    with tasks_lock:
        if task_id in tasks and tasks[task_id]['ip'] == client_ip:
            task_in_memory = True
            result_dir = Path(tasks[task_id]['result_dir'])
    
    if not task_in_memory:
        if not result_dir.exists():
            return jsonify({'error': '任务不存在'}), 404
    
    file_path = (result_dir / filename).resolve()
    if not _is_within_dir(result_dir.resolve(), file_path):
        return jsonify({'error': '非法文件路径'}), 400
    
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404
    
    mimetype, _ = mimetypes.guess_type(str(file_path))
    if not mimetype:
        mimetype = 'application/octet-stream'
    
    return send_file(
        str(file_path),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename
    )


@bp.route('/api/download/zip/<task_id>')
def download_results_zip(task_id):
    """打包下载任务的所有结果文件"""
    import zipfile
    import io
    
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    
    with tasks_lock:
        if task_id in tasks and tasks[task_id]['ip'] == client_ip:
            result_dir = Path(tasks[task_id]['result_dir'])
        else:
            result_dir = user_folder / 'results' / task_id
    
    if not result_dir.exists():
        return jsonify({'error': '任务不存在'}), 404
    
    # 获取所有文件
    files = list(result_dir.iterdir())
    if not files:
        return jsonify({'error': '没有结果文件'}), 404
    
    # 创建内存 zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in files:
            if file_path.is_file():
                zipf.write(file_path, file_path.name)
    
    zip_buffer.seek(0)
    
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'analysis_{task_id}.zip'
    )


@bp.route('/api/preview/<task_id>')
def preview_html(task_id):
    """预览 HTML 报告"""
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    
    with tasks_lock:
        if task_id in tasks and tasks[task_id]['ip'] == client_ip:
            result_dir = Path(tasks[task_id]['result_dir'])
        else:
            result_dir = user_folder / 'results' / task_id
    
    if not result_dir.exists():
        return '任务不存在', 404
    
    # 查找 HTML 文件
    html_files = list(result_dir.glob('*.html'))
    if not html_files:
        return 'HTML 文件不存在', 404
    
    html_file = html_files[0]
    
    # 读取 HTML 内容
    with open(html_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 返回 HTML 内容，设置正确的 Content-Type
    from flask import make_response
    response = make_response(content)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/api/memory-analysis/<task_id>')
def get_memory_analysis(task_id):
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)

    mem_file = None
    result_dir = None

    with tasks_lock:
        task = tasks.get(task_id)
        if task and task.get('ip') == client_ip:
            result_dir = Path(task['result_dir'])
            mem_name = (task.get('results') or {}).get('mem_analysis_file')
            if mem_name:
                mem_file = result_dir / mem_name
        else:
            result_dir = user_folder / 'results' / task_id

    if (not mem_file or not mem_file.exists()) and result_dir and result_dir.exists():
        candidates = sorted(result_dir.glob('*_mem_analysis.json'))
        if candidates:
            mem_file = candidates[0]

    if not mem_file or not mem_file.exists():
        return jsonify({'error': '未找到内存分析数据'}), 404

    with open(mem_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@bp.route('/api/presets')
def get_presets():
    """获取所有预设配置"""
    return jsonify(get_available_presets())


@bp.route('/api/preset/<preset_name>')
def get_preset_detail(preset_name):
    """获取特定预设的详细信息"""
    config = parse_cont_startup._load_app_config()
    if not config or preset_name not in config:
        return jsonify({'error': '预设不存在'}), 404
    
    apps = config[preset_name]
    if not isinstance(apps, list):
        return jsonify({'error': '无效的预设'}), 400
    
    return jsonify({
        'name': preset_name,
        'apps': apps,
        'count': len(apps)
    })


@bp.route('/api/debug-ip')
def debug_ip():
    """调试：查看当前IP"""
    ip = get_client_ip()
    user_folder = get_user_folder(ip)
    results_folder = user_folder / 'results'
    return jsonify({
        'detected_ip': ip,
        'user_folder': str(user_folder),
        'results_exists': results_folder.exists(),
        'results_count': len(list(results_folder.iterdir())) if results_folder.exists() else 0
    })


@bp.route('/api/tasks')
def list_tasks():
    client_ip = get_client_ip()
    
    # 从磁盘加载任务信息
    user_folder = get_user_folder(client_ip)
    results_folder = user_folder / 'results'
    
    task_list = []
    
    if results_folder.exists():
        # 遍历所有任务目录
        for task_dir in results_folder.iterdir():
            if task_dir.is_dir() and task_dir.name != 'results':
                task_id = task_dir.name
                
                # 检查是否有分析结果文件
                has_results = False
                for f in task_dir.iterdir():
                    if f.suffix == '.txt' and 'device_info' not in f.name and 'meminfo' not in f.name:
                        has_results = True
                        break
                
                if has_results:
                    # 从内存中获取任务信息（如果存在）
                    task_info = tasks.get(task_id, {})

                    # 尝试从磁盘读取 task_info.json 获取 scene
                    scene_from_disk = None
                    metadata_file = task_dir / 'task_info.json'
                    if metadata_file.exists():
                        try:
                            with open(metadata_file, 'r', encoding='utf-8') as f:
                                metadata = json.load(f)
                                scene_from_disk = metadata.get('scene')
                        except Exception:
                            pass

                    # 如果内存中没有，尝试从文件名推断
                    if not task_info:
                        # 查找上传文件
                        uploads_folder = user_folder / 'uploads'
                        if uploads_folder.exists():
                            for upload_file in uploads_folder.iterdir():
                                if upload_file.name.startswith(f"{task_id}_"):
                                    # 从分析结果文件提取时间戳
                                    created_at = 'unknown'
                                    # 查找分析结果文件
                                    for result_file in task_dir.iterdir():
                                        if result_file.suffix == '.txt' and 'device_info' not in result_file.name and 'meminfo' not in result_file.name:
                                            # 从文件名提取时间戳：analysis_20260213_163628.txt
                                            filename = result_file.name
                                            if filename.startswith('analysis_') and filename.endswith('.txt'):
                                                timestamp = filename.replace('analysis_', '').replace('.txt', '')
                                                created_at = timestamp
                                                break

                                    task_info = {
                                        'task_id': task_id,
                                        'status': 'completed',
                                        'filename': upload_file.name.replace(f"{task_id}_", ''),
                                        'created_at': created_at,
                                        'scene': scene_from_disk or 'unknown'
                                    }
                                    break

                    if task_info:
                        normalized_task = dict(task_info)
                        normalized_task['task_id'] = (
                            normalized_task.get('task_id')
                            or normalized_task.get('id')
                            or task_id
                        )
                        normalized_task['status'] = normalized_task.get('status', 'completed')
                        normalized_task['filename'] = normalized_task.get('filename', 'unknown')
                        normalized_task['created_at'] = normalized_task.get('created_at', 'unknown')

                        if scene_from_disk:
                            normalized_task['scene'] = scene_from_disk
                        else:
                            normalized_task['scene'] = normalized_task.get('scene', 'unknown')

                        task_list.append(normalized_task)
    
    # 同时添加内存中的任务（确保不重复）
    with tasks_lock:
        for task_id, task in tasks.items():
            if task['ip'] == client_ip:
                # 检查是否已经在列表中
                if not any((t.get('task_id') or t.get('id')) == task_id for t in task_list):
                    task_list.append({
                        'task_id': task_id,
                        'status': task['status'],
                        'filename': task['filename'],
                        'created_at': task['created_at'],
                        'scene': task.get('scene', 'unknown')
                    })
    
    # 按创建时间排序，最新的在前
    task_list.sort(key=lambda x: str(x.get('created_at', '')), reverse=True)
    
    return jsonify(task_list)


@bp.route('/api/tasks/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    
    task = None
    task_in_memory = False
    
    with tasks_lock:
        if task_id in tasks:
            task = tasks[task_id]
            task_in_memory = True
            if task['ip'] != client_ip:
                return jsonify({'error': '无权访问此任务'}), 403
    
    # 如果任务不在内存中，尝试从磁盘获取路径
    if not task:
        result_dir = user_folder / 'results' / task_id
        uploads_folder = user_folder / 'uploads'
        
        # 查找上传文件
        upload_path = None
        if uploads_folder.exists():
            for f in uploads_folder.iterdir():
                if f.name.startswith(f"{task_id}_"):
                    upload_path = str(f)
                    break
        
        task = {
            'upload_path': upload_path,
            'result_dir': str(result_dir),
            'ip': client_ip
        }
    
    # 删除上传文件
    try:
        upload_path = task.get('upload_path')
        if upload_path and os.path.exists(upload_path):
            os.remove(upload_path)
    except Exception:
        pass
    
    # 删除结果目录
    try:
        result_dir = Path(str(task.get('result_dir') or ''))
        if result_dir.exists():
            shutil.rmtree(str(result_dir))
    except Exception:
        pass
    
    # 从内存中删除
    with tasks_lock:
        if task_id in tasks:
            del tasks[task_id]
    
    return jsonify({'message': '任务已删除'})


def _resolve_result_dir_for_task(task_id: str, client_ip: str, user_folder: Path) -> Tuple[Optional[dict], Optional[Path]]:
    task_obj = None
    result_dir = None

    with tasks_lock:
        task = tasks.get(task_id)
        if task:
            if task.get('ip') != client_ip:
                return None, None
            task_obj = task
            result_dir = Path(task.get('result_dir', ''))

    if result_dir is None:
        result_dir = user_folder / 'results' / task_id
    return task_obj, result_dir


def _locate_analysis_files(result_dir: Path) -> Dict[str, Optional[Path]]:
    html_file = None
    txt_file = None
    meminfo_file = None
    bugreport_context_file = None

    if not result_dir.exists():
        return {
            'html_file': None,
            'txt_file': None,
            'meminfo_file': None,
            'bugreport_context_file': None,
        }

    for f in result_dir.iterdir():
        if _is_primary_analysis_txt_name(f.name):
            txt_file = f
        elif f.name.endswith('.html') and not f.name.startswith('ai_interpret_'):
            html_file = f
        elif 'meminfo' in f.name:
            meminfo_file = f
        elif f.name.endswith('_bugreport_context.txt'):
            bugreport_context_file = f

    return {
        'html_file': html_file,
        'txt_file': txt_file,
        'meminfo_file': meminfo_file,
        'bugreport_context_file': bugreport_context_file,
    }


def _load_task_metadata_scene(result_dir: Path) -> str:
    metadata_file = result_dir / 'task_info.json'
    if not metadata_file.exists():
        return 'unknown'
    try:
        with open(metadata_file, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        return str(metadata.get('scene') or 'unknown')
    except Exception:
        return 'unknown'


def _build_interpret_preview_html(title: str, markdown_text: str) -> str:
    safe_title = html_lib.escape(str(title or 'AI智能解读'))
    safe_body = html_lib.escape(str(markdown_text or ''))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background: #f7f9fc;
      color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .card {{
      max-width: 1120px;
      margin: 0 auto;
      background: #ffffff;
      border-radius: 12px;
      border: 1px solid #dbe4f0;
      box-shadow: 0 8px 22px rgba(28, 39, 60, 0.08);
      overflow: hidden;
    }}
    .head {{
      padding: 16px 20px;
      background: linear-gradient(135deg, #edf3ff 0%, #f4f7ff 100%);
      border-bottom: 1px solid #dde6f4;
      font-size: 18px;
      font-weight: 700;
      color: #1f2a44;
    }}
    pre {{
      margin: 0;
      padding: 18px 20px 24px;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.65;
      font-size: 14px;
      color: #233252;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="head">{safe_title}</div>
    <pre>{safe_body}</pre>
  </div>
</body>
</html>
"""


@bp.route('/api/compare', methods=['POST'])
def compare_tasks():
    """对比两份分析报告"""
    from llm_client import llm_client  # pyright: ignore[reportImplicitRelativeImport]
    
    data = request.json
    task_id_1 = data.get('task_id_1')
    task_id_2 = data.get('task_id_2')
    
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)
    
    if not task_id_1 or not task_id_2:
        return jsonify({'error': '请选择两份报告进行对比'}), 400
    
    if task_id_1 == task_id_2:
        return jsonify({'error': '不能选择同一份报告进行对比'}), 400
    
    task_1 = None
    task_2 = None
    result_dir_1 = None
    result_dir_2 = None
    
    with tasks_lock:
        if task_id_1 in tasks and tasks[task_id_1]['ip'] == client_ip:
            task_1 = tasks[task_id_1]
            result_dir_1 = Path(task_1['result_dir'])
        if task_id_2 in tasks and tasks[task_id_2]['ip'] == client_ip:
            task_2 = tasks[task_id_2]
            result_dir_2 = Path(task_2['result_dir'])
    
    if not result_dir_1:
        result_dir_1 = user_folder / 'results' / task_id_1
    if not result_dir_2:
        result_dir_2 = user_folder / 'results' / task_id_2
    
    if not result_dir_1.exists() or not result_dir_2.exists():
        return jsonify({'error': '任务不存在'}), 404
    
    # 读取报告内容 - 使用 HTML 文件
    html_file_1 = None
    html_file_2 = None
    meminfo_file_1 = None
    meminfo_file_2 = None
    txt_file_1 = None
    txt_file_2 = None
    
    for f in result_dir_1.iterdir():
        if _is_primary_analysis_txt_name(f.name):
            txt_file_1 = f
        elif f.name.endswith('.html') and not f.name.startswith('ai_interpret_'):
            html_file_1 = f
        elif 'meminfo' in f.name:
            meminfo_file_1 = f

    for f in result_dir_2.iterdir():
        if _is_primary_analysis_txt_name(f.name):
            txt_file_2 = f
        elif f.name.endswith('.html') and not f.name.startswith('ai_interpret_'):
            html_file_2 = f
        elif 'meminfo' in f.name:
            meminfo_file_2 = f
    
    if not html_file_1 or not html_file_2:
        return jsonify({'error': '找不到 HTML 报告文件'}), 404
    
    report1_content = html_file_1.read_text(encoding='utf-8')
    report2_content = html_file_2.read_text(encoding='utf-8')
    
    # 读取 meminfo summary
    meminfo1 = meminfo_file_1.read_text(encoding='utf-8') if meminfo_file_1 else ""
    meminfo2 = meminfo_file_2.read_text(encoding='utf-8') if meminfo_file_2 else ""
    
    # 读取 txt 文件作为补充数据源
    txt_content1 = txt_file_1.read_text(encoding='utf-8') if txt_file_1 else ""
    txt_content2 = txt_file_2.read_text(encoding='utf-8') if txt_file_2 else ""
    
    # 准备元数据
    filename_1 = html_file_1.name.replace('analysis_', '').replace('.html', '')
    filename_2 = html_file_2.name.replace('analysis_', '').replace('.html', '')
    
    meta_1 = {
        'filename': filename_1,
        'created_at': filename_1.split('_')[1] if '_' in filename_1 else 'unknown',
        'scene': 'unknown'
    }
    meta_2 = {
        'filename': filename_2,
        'created_at': filename_2.split('_')[1] if '_' in filename_2 else 'unknown',
        'scene': 'unknown'
    }
    
    try:
        # 对比分析固定走 Mify，避免前端或环境变量切换导致不一致
        llm_client.provider = 'mify'
        
        # 调用 LLM 进行对比
        comparison_result = llm_client.compare_reports(
            report1_content, 
            report2_content,
            meta_1,
            meta_2,
            meminfo1,
            meminfo2,
            txt_content1,
            txt_content2
        )
        
        return jsonify({
            'status': 'success',
            'comparison': comparison_result,
            'task_1': {
                'task_id': task_id_1,
                'filename': meta_1['filename']
            },
            'task_2': {
                'task_id': task_id_2,
                'filename': meta_2['filename']
            }
        })
        
    except Exception as e:
        import traceback
        return jsonify({
            'error': f'对比分析失败: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500


@bp.route('/api/export/feishu', methods=['POST'])
def export_to_feishu_doc():
    data = request.json or {}
    title = (data.get('title') or '').strip() or f"Collie 对比分析_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    content = (data.get('content') or '').strip()
    token = (
        (data.get('feishu_token') or '').strip()
        or (request.headers.get('X-Feishu-Token') or '').strip()
        or os.getenv('FEISHU_USER_ACCESS_TOKEN', '').strip()
        or os.getenv('FEISHU_ACCESS_TOKEN', '').strip()
    )

    if not content:
        return jsonify({'error': '导出内容为空'}), 400
    if not token:
        return jsonify({'error': '未配置飞书访问令牌，请在页面输入或配置 FEISHU_USER_ACCESS_TOKEN'}), 400

    try:
        doc_id, doc_url = _create_feishu_doc(token, title)
        _append_feishu_text(token, doc_id, content)
        return jsonify({
            'status': 'success',
            'document_id': doc_id,
            'document_url': doc_url,
            'title': title,
        })
    except Exception as exc:
        return jsonify({'error': f'保存飞书文档失败: {str(exc)}'}), 500


@bp.route('/api/interpret', methods=['POST'])
def interpret_task():
    """对单份分析报告进行 AI 智能解读"""
    from llm_client import llm_client  # pyright: ignore[reportImplicitRelativeImport]

    data = request.json or {}
    task_id = str(data.get('task_id', '')).strip()
    if not task_id:
        return jsonify({'error': 'task_id 不能为空'}), 400

    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)

    task_obj, result_dir = _resolve_result_dir_for_task(task_id, client_ip, user_folder)
    if result_dir is None:
        return jsonify({'error': '无权访问此任务'}), 403
    if not result_dir.exists():
        return jsonify({'error': '任务不存在'}), 404

    files = _locate_analysis_files(result_dir)
    html_file = files.get('html_file')
    txt_file = files.get('txt_file')
    meminfo_file = files.get('meminfo_file')
    bugreport_context_file = files.get('bugreport_context_file')

    if not html_file and not txt_file:
        return jsonify({'error': '找不到可用于解读的报告文件（HTML/TXT）'}), 404

    report_html = html_file.read_text(encoding='utf-8') if html_file else ''
    report_txt = txt_file.read_text(encoding='utf-8') if txt_file else ''
    meminfo_text = meminfo_file.read_text(encoding='utf-8') if meminfo_file else ''
    bugreport_context_text = (
        bugreport_context_file.read_text(encoding='utf-8')
        if bugreport_context_file and bugreport_context_file.exists()
        else ''
    )

    filename_guess = ''
    if html_file:
        filename_guess = html_file.name.replace('analysis_', '').replace('.html', '')
    elif txt_file:
        filename_guess = txt_file.name.replace('analysis_', '').replace('.txt', '')

    scene = task_obj.get('scene') if isinstance(task_obj, dict) and task_obj.get('scene') else _load_task_metadata_scene(result_dir)
    meta = {
        'filename': filename_guess or task_id,
        'created_at': task_obj.get('created_at', 'unknown') if isinstance(task_obj, dict) else 'unknown',
        'scene': scene or 'unknown',
    }

    try:
        # 与对比分析保持一致，固定使用 mify
        llm_client.provider = 'mify'
        interpretation = llm_client.interpret_report(
            report_html=report_html,
            report_meta=meta,
            meminfo=meminfo_text,
            txt=report_txt,
            bugreport_context=bugreport_context_text,
        )
    except Exception as exc:
        import traceback
        return jsonify({
            'error': f'AI智能解读失败: {str(exc)}',
            'traceback': traceback.format_exc(),
        }), 500

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name = f'ai_interpret_{ts}'
    md_name = f'{base_name}.md'
    txt_name = f'{base_name}.txt'
    html_name = f'{base_name}.html'

    md_path = result_dir / md_name
    txt_path = result_dir / txt_name
    html_path = result_dir / html_name

    md_content = str(interpretation or '').strip()
    txt_content = md_content
    preview_html = _build_interpret_preview_html(
        title=f'AI智能解读 - {meta.get("filename", task_id)}',
        markdown_text=md_content,
    )

    md_path.write_text(md_content, encoding='utf-8')
    txt_path.write_text(txt_content, encoding='utf-8')
    html_path.write_text(preview_html, encoding='utf-8')

    return jsonify({
        'status': 'success',
        'task_id': task_id,
        'interpretation': md_content,
        'meta': meta,
        'used_bugreport_context': bool(bugreport_context_text.strip()),
        'files': {
            'md': md_name,
            'txt': txt_name,
            'html': html_name,
            'bugreport_context': bugreport_context_file.name if bugreport_context_file else None,
        },
        'preview_url': f'/api/interpret/preview/{task_id}/{html_name}',
        'download': {
            'md': f'/api/download/{task_id}/{md_name}',
            'txt': f'/api/download/{task_id}/{txt_name}',
            'html': f'/api/download/{task_id}/{html_name}',
        },
    })


@bp.route('/api/interpret/preview/<task_id>/<path:filename>')
def preview_interpretation(task_id, filename):
    client_ip = get_client_ip()
    user_folder = get_user_folder(client_ip)

    _, result_dir = _resolve_result_dir_for_task(task_id, client_ip, user_folder)
    if result_dir is None:
        return '无权访问此任务', 403
    if not result_dir.exists():
        return '任务不存在', 404

    file_name = str(filename or '').strip()
    if not file_name or not file_name.endswith('.html') or not file_name.startswith('ai_interpret_'):
        return '无效的预览文件', 400

    file_path = (result_dir / file_name).resolve()
    if not _is_within_dir(result_dir.resolve(), file_path):
        return '无效的预览文件', 400
    if not file_path.exists():
        return '文件不存在', 404

    from flask import make_response
    content = file_path.read_text(encoding='utf-8')
    response = make_response(content)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


if __name__ == '__main__':
    import socket
    import os
    from collie_package import config_loader

    app = create_app()
    
    start_cleanup_scheduler()
    
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = '127.0.0.1'
    
    print(f"\n{'='*60}")
    print(f"Collie Bugreport Web Analyzer 已启动")
    print(f"{'='*60}")
    server_cfg = APP_SETTINGS.get('server', {})
    host = str(server_cfg.get('host', '0.0.0.0'))
    port = int(server_cfg.get('port', 5000))
    debug = bool(server_cfg.get('debug', False))
    threaded = bool(server_cfg.get('threaded', True))

    display_host = host if host not in ('0.0.0.0', '::') else local_ip
    cfg_dir = config_loader.resolve_web_config_dir()
    yaml_available = getattr(config_loader, "yaml", None) is not None
    app_yaml_path = (cfg_dir / "app.yaml") if cfg_dir else None
    print("web 配置加载:")
    print(f"  resolve_web_config_dir: {cfg_dir or '-'}")
    print(f"  app.yaml exists: {bool(app_yaml_path and app_yaml_path.exists())}")
    print(f"  PyYAML available: {yaml_available}")
    simpleperf_root = os.getenv("SIMPLEPERF_ROOT", "").strip()
    llvm_readelf = os.getenv("LLVM_READELF", "").strip()
    llvm_readobj = os.getenv("LLVM_READOBJ", "").strip()
    simpleperf_cfg = APP_SETTINGS.get("utilities_webadb", {}).get("simpleperf", {})
    ndk_path = ""
    if isinstance(simpleperf_cfg, dict):
        ndk_path = str(simpleperf_cfg.get("ndk_path", "")).strip()
    print("simpleperf 配置（服务器端）:")
    print(f"  SIMPLEPERF_ROOT: {simpleperf_root or '-'}")
    print(f"  LLVM_READELF: {llvm_readelf or '-'}")
    print(f"  LLVM_READOBJ: {llvm_readobj or '-'}")
    print(f"  app.yaml ndk_path: {ndk_path or '-'}")
    print(f"本地访问: http://127.0.0.1:{port}")
    print(f"局域网访问: http://{display_host}:{port}")
    print(f"数据保留时间: {DATA_RETENTION_DAYS}天")
    print(f"{'='*60}\n")

    app.run(host=host, port=port, debug=debug, threaded=threaded)
