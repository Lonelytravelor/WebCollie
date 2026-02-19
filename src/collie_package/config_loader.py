from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover - 运行环境未安装 PyYAML 时降级
    yaml = None


DEFAULT_APP_SETTINGS: Dict[str, Any] = {
    'server': {
        'host': '0.0.0.0',
        'port': 5000,
        'debug': True,
        'threaded': True,
    },
    'storage': {
        'data_folder': 'user_data',
        'data_retention_days': 7,
    },
    'upload': {
        'allowed_extensions': ['txt', 'zip'],
        'max_content_length_mb': 500,
    },
    'llm': {
        'default_provider': 'auto',
        'provider_fallback_order': ['mify', 'openai', 'azure'],
        'openai': {
            'api_key': '',
            'base_url': 'https://api.openai.com/v1',
            'model': 'gpt-4o-mini',
        },
        'azure': {
            'api_key': '',
            'endpoint': '',
            'model': 'gpt-4',
        },
        'mify': {
            'api_key': '',
            'base_url': 'https://mify.mioffice.cn/gateway',
            'model': 'mimo-v2-flash',
            'provider_id': 'xiaomi',
            'timeout_seconds': 60,
            'user_id': '',
            'conversation_id': '',
            'logging': '',
            'reasoning_content_enabled': None,
        },
    },
    'utilities_webadb': {
        'monitor_max_detail_rows': 800,
        'auto_skip_game_packages': [
            'com.tencent.tmgp.pubgmhd',
            'com.tencent.tmgp.sgame',
        ],
        'agent_ttl_default': 25,
        'proxy_timeout_floor': 5,
    },
}

DEFAULT_RULES: Dict[str, Any] = {
    'parse_cont_startup': {
        'possible_anomaly_start_label': '可能为异常启动',
        'possible_anomaly_start_note': '需要视频/测试二次确认是否为异常',
        'kill_type_map': {
            '0': 'NPW',
            '1': 'EPW',
            '2': 'CPW',
            '3': 'LAUNCH',
            '4': 'SUB_PROC',
            '5': 'INVALID',
        },
        'min_score_map': {
            -1073741824: 'MAIN_PROC_FACTOR | SUB_MIN_SCORE',
            -536870912: 'LOWADJ_PROC_FACTOR',
            -268435456: 'FORCE_PROTECT_PROC_FACTOR',
            -134217728: 'LOCKED_PROC_FACTOR',
            -67108864: 'RECENT_PROC_FACTOR',
            -33554432: 'IMPORTANT_PROC_FACTOR',
            -1342177280: 'RECENT_MIN_SCORE',
            -1140850688: 'IMPORTANT_MIN_SCORE',
            -1107296256: 'NORMAL_MIN_SCORE',
        },
        'killinfo_field_mapping': {
            'compact': [
                'pid_or_comm',
                'pid_or_comm',
                'uid',
                'adj',
                'min_adj',
                'rss_kb',
                'proc_swap_kb',
                'kill_reason',
                'mem_total_kb',
                'mem_free_kb',
                'cached_kb',
                'swap_free_kb',
                'thrashing',
                'max_thrashing',
                'psi_mem_some',
                'psi_mem_full',
                'psi_io_some',
                'psi_io_full',
                'psi_cpu_some',
            ],
            'full': [
                'pid_or_comm',
                'pid_or_comm',
                'uid',
                'adj',
                'min_adj',
                'rss_kb',
                'kill_reason',
                'mem_total_kb',
                'mem_free_kb',
                'cached_kb',
                'swap_cached_kb',
                'buffers_kb',
                'shmem_kb',
                'unevictable_kb',
                'swap_total_kb',
                'swap_free_kb',
                'active_anon_kb',
                'inactive_anon_kb',
                'active_file_kb',
                'inactive_file_kb',
                'k_reclaimable_kb',
                's_reclaimable_kb',
                's_unreclaim_kb',
                'kernel_stack_kb',
                'page_tables_kb',
                'ion_heap_kb',
                'ion_heap_pool_kb',
                'cma_free_kb',
                'pressure_since_event_ms',
                'since_wakeup_ms',
                'wakeups_since_event',
                'skipped_wakeups',
                'proc_swap_kb',
                'gpu_kb',
                'thrashing',
                'max_thrashing',
                'psi_mem_some',
                'psi_mem_full',
                'psi_io_some',
                'psi_io_full',
                'psi_cpu_some',
            ],
        },
        'patterns': {
            'lmk': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
                r'.*?lowmemorykiller:\s*(?:Kill|Killing)\s*[\'"]?(?P<process>[^\s\'"(]+)[\'"]?'
                r'\s*(?:\((?:pid\s*)?(?P<pid>\d+)[^)]*\)|pid\s*(?P<pid_alt>\d+))?(?P<tail>.*)'
            ),
            'killinfo': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
                r'.*?killinfo:\s*\[(?P<payload>[^\]]+)\]'
            ),
            'am_kill': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
                r'.*?am_kill\s*:\s*\[(?P<payload>[^\]]+)\]'
            ),
            'am_proc_start': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*?am_proc_start:\s*'
                r'\[(?P<payload>[^\]]+)\]'
            ),
            'displayed': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
                r'.*?ActivityTaskManager:\s*Displayed\s+(?P<component>[^\s]+)\s+'
                r'for user \d+:\s+\+(?P<latency>.+)$'
            ),
            'wm_resumed': (
                r'(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)'
                r'.*?wm_set_resumed_activity:\s*\[(?P<payload>[^\]]+)\]'
            ),
        },
        'home_packages': [
            'com.miui.home',
            'com.android.launcher',
            'com.android.launcher3',
        ],
    },
    'app_install': {
        'store_package': 'com.xiaomi.market',
        'base_resolution': [1080, 2340],
        'search_box_coords': [840, 220],
        'install_button_ratios': [[0.50, 0.93], [0.50, 0.94]],
        'install_button_coords': [900, 1600],
        'use_ui_dump': False,
        'wait_time': 5,
        'delete_attempts': 30,
        'busy_pause_sec': 4,
        'install_poll_interval': 6,
        'install_poll_timeout': 240,
        'max_wait_per_app': 20,
        'page_load_wait': 4.0,
        'fallback_max_clicks': 1,
        'post_tap_wait': 2.5,
    },
    'app_died_monitor': {
        'trigger_commands': [
            'adb shell dumpsys activity',
            'adb shell dumpsys meminfo',
            'adb shell cmd greezer getUids 9999',
        ],
        'periodic_dumpsys_command': 'adb shell dumpsys activity',
        'periodic_dumpsys_count': 3,
        'periodic_dumpsys_interval': 10,
        'periodic_commands': [],
        'fallback_monitor_targets': [
            {'label': 'Demo App', 'packages': ['com.ss.android.ugc.aweme']},
        ],
    },
    'pre_start': {
        'commands': [
            'adb logcat -b all -c',
            'adb shell dmesg -C',
            'adb root',
            "adb shell 'echo 7 > /sys/kernel/mi_mempool/config'",
            "adb shell 'echo 2 > /sys/kernel/mem_limit/debug'",
            "adb shell 'echo 63 > /sys/kernel/mi_reclaim/greclaim_enable'",
            'adb shell setprop persist.sys.miui.integrated.memory.debug.enable true',
            'adb shell setprop debug.sys.spc true',
            'adb shell stop',
            'adb shell start',
        ],
        'post_commands': [
            'adb shell settings put system mmperf stat',
            'adb shell settings put system mmperf trace',
        ],
        'post_delay_sec': 10,
    },
    'compare_android_mem_design': {
        'page_size_kb': 4,
        'interesting_meminfo_keys': [
            'MemTotal',
            'Buffers',
            'Cached',
            'SwapCached',
            'SwapTotal',
            'Shmem',
            'Mapped',
            'AnonPages',
            'FilePages',
            'VmallocTotal',
            'CommitLimit',
            'Committed_AS',
            'HugePages_Total',
        ],
        'primary_prop_keys': [
            'ro.board.platform',
            'ro.build.version.release',
            'dalvik.vm.heapsize',
            'dalvik.vm.heapgrowthlimit',
            'ersist.sys.systemui.compress',
            'persist.sys.miui.integrated.memory.enable',
            'persist.sys.miui.integrated.memory.pr.enable',
            'persist.sys.imr.zramuserate.limit',
            'persist.sys.imr.cpuload.limit',
            'persist.sys.imr.memfree.limit',
            'persist.sys.imr.zramfree.limit',
            'persist.sys.imr.kill.memfree.limit',
            'persist.sys.imr.launchrecliam.num',
            'persist.sys.mmms.throttled.thread',
        ],
        'important_modules': [
            'mi_memory',
            'mi_mempool',
            'mi_mem_limit',
            'mi_mem_epoll',
            'mi_rmap_efficiency',
            'mi_async_reclaim',
            'kshrink_slabd',
            'unfairmem',
        ],
        'vm_tunable_keys': [
            'vm.min_free_kbytes',
            'vm.extra_free_kbytes',
            'vm.watermark_scale_factor',
            'vm.watermark_boost_factor',
            'vm.lowmem_reserve_ratio',
            'vm.swappiness',
            'vm.vfs_cache_pressure',
            'vm.dirty_background_ratio',
            'vm.dirty_ratio',
            'vm.dirty_background_bytes',
            'vm.dirty_bytes',
            'vm.overcommit_memory',
            'vm.overcommit_ratio',
            'vm.zone_reclaim_mode',
            'vm.min_slab_ratio',
            'vm.min_unmapped_ratio',
            'vm.percpu_pagelist_fraction',
            'vm.compact_unevictable_allowed',
            'vm.compact_defer_shift',
        ],
    },
    'meminfo_summary': {
        'kb_in_mb': 1024,
    },
    'parse_kswapd': {
        'line_re': (
            r'^(?P<comm>.+?)-(?P<pid>\d+)\s+'
            r'\[(?P<cpu>\d+)\]\s+'
            r'(?P<flags>[\.A-Z]+)\s+'
            r'(?P<ts>\d+\.\d+):\s+'
            r'(?P<event>\S+):\s+'
            r'(?P<args>.*)$'
        ),
        'nid_re': r'nid=(\d+)',
        'order_re': r'order=(\d+)',
        'gfp_re': r'gfp_flags=([^\s]+)',
        'events': [
            'mm_vmscan_wakeup_kswapd',
            'mm_vmscan_kswapd_wake',
            'mm_vmscan_kswapd_sleep',
        ],
    },
    'parse_direct_reclaim': {
        'line_re': (
            r'^(?P<comm>.+?)-(?P<pid>\d+)\s+'
            r'\[(?P<cpu>\d+)\]\s+'
            r'(?P<flags>[\.A-Z]+)\s+'
            r'(?P<ts>\d+\.\d+):\s+'
            r'(?P<event>\S+):\s+'
            r'(?P<args>.*)$'
        ),
        'order_re': r'order=(\d+)',
        'gfp_re': r'gfp_flags=([^\s]+)',
        'nr_reclaimed_re': r'nr_reclaimed=(\d+)',
        'prev_pid_re': r'prev_pid=(\d+)',
        'next_pid_re': r'next_pid=(\d+)',
        'events': [
            'mm_vmscan_direct_reclaim_begin',
            'mm_vmscan_direct_reclaim_end',
        ],
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(dict(base.get(key, {})), value)
        else:
            base[key] = value
    return base


def _resolve_dir_from_env(env_key: str) -> Optional[Path]:
    env_dir = os.getenv(env_key, '').strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return None


def resolve_web_config_dir() -> Optional[Path]:
    env_dir = _resolve_dir_from_env('COLLIE_WEB_CONFIG_DIR')
    if env_dir:
        return env_dir

    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    candidate = repo_root / 'web_app' / 'config'
    if candidate.exists():
        return candidate

    cwd = Path.cwd()
    candidate = cwd / 'web_app' / 'config'
    if candidate.exists():
        return candidate

    return None


def resolve_core_config_dir() -> Optional[Path]:
    env_dir = _resolve_dir_from_env('COLLIE_CORE_CONFIG_DIR')
    if env_dir:
        return env_dir

    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    candidate = repo_root / 'src' / 'collie_package' / 'config'
    if candidate.exists():
        return candidate

    cwd = Path.cwd()
    candidate = cwd / 'src' / 'collie_package' / 'config'
    if candidate.exists():
        return candidate

    return None


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        return {}
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_app_settings() -> Dict[str, Any]:
    cfg_dir = resolve_web_config_dir()
    base = dict(DEFAULT_APP_SETTINGS)
    if cfg_dir is None:
        return base
    data = _load_yaml(cfg_dir / 'app.yaml')
    return _deep_merge(base, data)


def load_app_list_config() -> Dict[str, Any]:
    cfg_dir = resolve_core_config_dir()
    if cfg_dir is not None:
        data = _load_yaml(cfg_dir / 'app_list.yaml')
        if data:
            return data
    fallback_dir = resolve_web_config_dir()
    if fallback_dir is None:
        return {}
    return _load_yaml(fallback_dir / 'app_list.yaml')


def resolve_app_config_path() -> Optional[Path]:
    cfg_dir = resolve_core_config_dir()
    if cfg_dir is not None:
        path = cfg_dir / 'app_list.yaml'
        if path.exists():
            return path
    fallback_dir = resolve_web_config_dir()
    if fallback_dir is not None:
        path = fallback_dir / 'app_list.yaml'
        if path.exists():
            return path
    return None


def load_rules_config() -> Dict[str, Any]:
    cfg_dir = resolve_core_config_dir()
    base = dict(DEFAULT_RULES)
    if cfg_dir is None:
        return base
    data = _load_yaml(cfg_dir / 'rules.yaml')
    if not data:
        return base
    return _deep_merge(base, data)


def to_flat_app_config(app_list_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(app_list_cfg, dict):
        return {}

    merged: Dict[str, Any] = {}
    if isinstance(app_list_cfg.get('app_presets'), dict):
        merged.update(app_list_cfg.get('app_presets') or {})

    for key in (
        'highlight_processes',
        'startup_sequence',
        'app_died_monitor_presets',
        'install_button_coords',
    ):
        if key in app_list_cfg:
            merged[key] = app_list_cfg.get(key)

    if isinstance(app_list_cfg.get('residency_test_presets'), dict):
        merged.update(app_list_cfg.get('residency_test_presets') or {})

    return merged
