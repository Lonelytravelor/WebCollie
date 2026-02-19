from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from .cont_startup_stay_contract import (
    AdbLike,
    AdbShellResult,
    ContStartupStayConfig,
    JsonValue,
    build_execution_plan,
    detect_device_capabilities,
    run_and_write_manifest,
)
from ..config_loader import load_app_list_config, to_flat_app_config

if TYPE_CHECKING:
    from .adb_executor import AdbExecutorLike


_PACKAGE_RE = re.compile(r'^[A-Za-z0-9_.]+$')


def _dedup_packages(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        pkg = str(item or '').strip()
        if not pkg or not _PACKAGE_RE.fullmatch(pkg):
            continue
        if pkg in seen:
            continue
        seen.add(pkg)
        out.append(pkg)
    return out


def _flatten_package_values(value) -> list[str]:  # type: ignore[no-untyped-def]
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [x for x in re.split(r'[\s,;]+', text) if x]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_package_values(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_package_values(item))
        return out
    return []


def _load_app_config_dict() -> dict:
    yaml_cfg = load_app_list_config()
    if isinstance(yaml_cfg, dict) and yaml_cfg:
        return to_flat_app_config(yaml_cfg)
    try:
        with resources.open_text('collie_package', 'app_config.json', encoding='utf-8') as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_app_packages(config: ContStartupStayConfig) -> list[str]:
    # 1) custom_json 优先
    custom = config.app_list.custom_json
    custom_parsed = custom
    if isinstance(custom, str):
        raw = custom.strip()
        if raw:
            try:
                custom_parsed = json.loads(raw)
            except Exception:
                custom_parsed = raw
    packages = _dedup_packages(_flatten_package_values(custom_parsed))
    if packages:
        return packages

    cfg = _load_app_config_dict()
    if isinstance(cfg, dict) and 'app_presets' in cfg:
        cfg = cfg.get('app_presets') or {}

    # 2) 指定 preset_name
    preset_name = str(config.app_list.preset_name or '').strip()
    if preset_name and isinstance(cfg.get(preset_name), (list, dict)):
        packages = _dedup_packages(_flatten_package_values(cfg.get(preset_name)))
        if packages:
            return packages

    # 3) 回退优选 key
    for key in ('动态性能模型(TOP20)', '动态性能模型', '九大场景-驻留'):
        if isinstance(cfg.get(key), (list, dict)):
            packages = _dedup_packages(_flatten_package_values(cfg.get(key)))
            if packages:
                return packages

    # 4) 再回退第一个可解析列表
    for value in cfg.values():
        packages = _dedup_packages(_flatten_package_values(value))
        if packages:
            return packages
    return []


def _ensure_archive_dirs(job_dir: Path) -> dict[str, Path]:
    dirs = {
        'residency_results': job_dir / 'residency_results',
        'ftrace_logs': job_dir / 'ftrace_logs',
        'node_logs': job_dir / 'node_logs',
        'memory_info': job_dir / 'memory_info',
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def _offline_html(
    *,
    title: str,
    timestamp: str,
    capabilities_wire: dict[str, JsonValue],
    plan_wire: dict[str, JsonValue],
    created_at: str,
) -> str:
    safe_caps = json.dumps(capabilities_wire, ensure_ascii=False, indent=2, sort_keys=True)
    safe_plan = json.dumps(plan_wire, ensure_ascii=False, indent=2, sort_keys=True)
    safe_title = json.dumps(title, ensure_ascii=False)
    safe_created = json.dumps(created_at, ensure_ascii=False)
    safe_ts = json.dumps(timestamp, ensure_ascii=False)

    return (
        '<!doctype html>\n'
        '<html>\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        f'  <title>{title}</title>\n'
        '  <style>\n'
        '    body{font-family:Arial,sans-serif;margin:0;background:#f6f7fb;color:#111827;}\n'
        '    .page{max-width:1100px;margin:0 auto;padding:24px;}\n'
        '    .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:16px 0;}\n'
        '    h1{margin:0 0 8px 0;font-size:22px;}\n'
        '    h2{margin:0 0 8px 0;font-size:16px;}\n'
        '    pre{background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:12px;overflow:auto;}\n'
        '    .meta{display:flex;gap:12px;flex-wrap:wrap;color:#6b7280;font-size:12px;}\n'
        '    .pill{background:#f8fafc;border:1px solid #e5e7eb;border-radius:999px;padding:4px 10px;}\n'
        '  </style>\n'
        '</head>\n'
        '<body>\n'
        '  <div class="page">\n'
        '    <div class="card">\n'
        f'      <h1>{title}</h1>\n'
        '      <div class="meta">\n'
        f'        <div class="pill">created_at: <span id="created"></span></div>\n'
        f'        <div class="pill">timestamp: <span id="ts"></span></div>\n'
        '        <div class="pill">mode: dry-run</div>\n'
        '      </div>\n'
        '    </div>\n'
        '    <div class="card">\n'
        '      <h2>Capabilities</h2>\n'
        '      <pre id="caps"></pre>\n'
        '    </div>\n'
        '    <div class="card">\n'
        '      <h2>Degradation Plan</h2>\n'
        '      <pre id="plan"></pre>\n'
        '    </div>\n'
        '  </div>\n'
        '  <script>\n'
        f'    const title = {safe_title};\n'
        f'    const createdAt = {safe_created};\n'
        f'    const timestamp = {safe_ts};\n'
        f'    const caps = {json.dumps(safe_caps, ensure_ascii=False)};\n'
        f'    const plan = {json.dumps(safe_plan, ensure_ascii=False)};\n'
        '    document.getElementById("created").textContent = createdAt;\n'
        '    document.getElementById("ts").textContent = timestamp;\n'
        '    document.getElementById("caps").textContent = caps;\n'
        '    document.getElementById("plan").textContent = plan;\n'
        '  </script>\n'
        '</body>\n'
        '</html>\n'
    )


def run_cont_startup_stay_dryrun(
    *,
    job_dir: Path,
    config: ContStartupStayConfig,
    adb: AdbLike,
    when: datetime | None = None,
) -> Path:
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    def _work() -> Path:
        _ = _ensure_archive_dirs(job_dir)
        timestamp = config.output_dir_strategy.format_timestamp(when)
        created_at = datetime.now().isoformat(timespec='seconds')
        html_text = _offline_html(
            title='冷启动分析报告',
            timestamp=timestamp,
            created_at=created_at,
            capabilities_wire=caps.to_wire(),
            plan_wire=plan.to_wire(),
        )
        html_path = job_dir / '冷启动分析报告.html'
        _ = html_path.write_text(html_text, encoding='utf-8')
        return html_path

    caps = detect_device_capabilities(adb)
    plan = build_execution_plan(config, caps)
    out = run_and_write_manifest(
        job_dir=job_dir,
        config=config,
        adb=adb,
        fn=_work,
        when=when,
        capabilities=caps,
        plan=plan,
    )
    assert out is not None
    return out


class _AdbLikeAdapter:
    def __init__(self, *, device_id: str, adb_exec: 'AdbExecutorLike') -> None:
        self._device_id = device_id
        self._adb_exec = adb_exec

    def shell(self, cmd: str, timeout_sec: float = 20.0) -> AdbShellResult:
        res = self._adb_exec.run(self._device_id, ['shell', cmd], timeout_sec=timeout_sec)
        return AdbShellResult(
            cmd=cmd,
            returncode=res.returncode,
            stdout=res.stdout or '',
            stderr=res.stderr or '',
        )


def _plan_enabled(plan_wire: dict[str, JsonValue], collector_id: str) -> bool:
    collectors = plan_wire.get('collectors')
    if not isinstance(collectors, list):
        return False
    for item in collectors:
        if not isinstance(item, dict):
            continue
        if item.get('collector_id') != collector_id:
            continue
        return item.get('status') == 'enabled'
    return False


def _plan_step_enabled(plan_wire: dict[str, JsonValue], step_id: str) -> bool:
    step = plan_wire.get(step_id)
    if not isinstance(step, dict):
        return False
    return step.get('status') == 'enabled'


def run_cont_startup_stay(
    *,
    job_dir: Path,
    config: ContStartupStayConfig,
    adb_exec: 'AdbExecutorLike',
    when: datetime | None = None,
) -> dict[str, str]:
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    dirs = _ensure_archive_dirs(job_dir)

    timestamp = config.output_dir_strategy.format_timestamp(when)
    adapter: AdbLike = _AdbLikeAdapter(device_id=config.device_id, adb_exec=adb_exec)
    caps = detect_device_capabilities(adapter)
    plan = build_execution_plan(config, caps)
    plan_wire = plan.to_wire()
    caps_wire = caps.to_wire()

    def _work() -> dict[str, str]:
        from . import state as rd_state

        rd_state.FILE_DIR = str(job_dir)
        for mod_name in (
            'collie_package.rd_selftest.collie_automation.state',
            'rd_selftest.collie_automation.state',
            'web_app.rd_selftest.collie_automation.state',
        ):
            sys.modules.setdefault(mod_name, rd_state)

        def _res_text(res) -> str:  # type: ignore[no-untyped-def]
            out = (res.stdout or '')
            err = (res.stderr or '')
            if err:
                if out:
                    out = out + '\n' + err
                else:
                    out = err
            return out

        def _write_before_after(path: Path, before_res, after_res) -> None:  # type: ignore[no-untyped-def]
            sep = '=' * 50
            text = (
                '测试前 - \n'
                + sep
                + '\n'
                + _res_text(before_res)
                + '\n\n'
                + '测试后 - \n'
                + sep
                + '\n'
                + _res_text(after_res)
                + '\n'
            )
            _ = path.write_text(text, encoding='utf-8')

        selected_packages = _resolve_app_packages(config)
        if not selected_packages:
            raise RuntimeError('未解析到有效 APP 列表，请检查 app_list 配置')
        selected_pkg_path = job_dir / 'selected_packages.txt'
        _ = selected_pkg_path.write_text('\n'.join(selected_packages) + '\n', encoding='utf-8')

        if config.run_pre_start and _plan_step_enabled(plan_wire, 'pre_start'):
            try:
                pre_start_mod = importlib.import_module(
                    'collie_package.rd_selftest.collie_automation.pre_start'
                )
                run_pre_start = getattr(pre_start_mod, 'run_pre_start')
                run_pre_start(device_id=config.device_id)
            except Exception as exc:  # noqa: BLE001
                _ = (job_dir / 'pre_start_error.txt').write_text(
                    f'pre_start failed: {exc}\n',
                    encoding='utf-8',
                )

        logcat_path = job_dir / f'logcat_{timestamp}.txt'
        if _plan_enabled(plan_wire, 'logcat'):
            _ = adb_exec.run(config.device_id, ['logcat', '-c'], timeout_sec=10)
            res = adb_exec.run(config.device_id, ['logcat', '-b', 'all', '-d'], timeout_sec=20)
            text = (res.stdout or '')
            if res.stderr:
                text = text + ('\n' if text else '') + (res.stderr or '')
            _ = logcat_path.write_text(text, encoding='utf-8')
        else:
            _ = logcat_path.write_text('logcat collector not enabled\n', encoding='utf-8')

        if _plan_enabled(plan_wire, 'meminfo'):
            before = adb_exec.run(config.device_id, ['shell', 'dumpsys meminfo'], timeout_sec=20)
            after = adb_exec.run(config.device_id, ['shell', 'dumpsys meminfo'], timeout_sec=20)
            _write_before_after(job_dir / f'meminfo{timestamp}.txt', before, after)

        if _plan_enabled(plan_wire, 'vmstat'):
            before = adb_exec.run(config.device_id, ['shell', 'cat', '/proc/vmstat'], timeout_sec=20)
            after = adb_exec.run(config.device_id, ['shell', 'cat', '/proc/vmstat'], timeout_sec=20)
            _write_before_after(job_dir / f'vmstat{timestamp}.txt', before, after)

        if _plan_enabled(plan_wire, 'greclaim_parm'):
            before = adb_exec.run(
                config.device_id,
                ['shell', 'cat', '/sys/kernel/mi_reclaim/greclaim_parm'],
                timeout_sec=10,
            )
            after = adb_exec.run(
                config.device_id,
                ['shell', 'cat', '/sys/kernel/mi_reclaim/greclaim_parm'],
                timeout_sec=10,
            )
            _write_before_after(job_dir / f'greclaim_parm{timestamp}.txt', before, after)

        if _plan_enabled(plan_wire, 'process_use_count'):
            before = adb_exec.run(
                config.device_id,
                ['shell', 'cat', '/sys/kernel/mi_mempool/process_use_count'],
                timeout_sec=10,
            )
            after = adb_exec.run(
                config.device_id,
                ['shell', 'cat', '/sys/kernel/mi_mempool/process_use_count'],
                timeout_sec=10,
            )
            _write_before_after(job_dir / f'process_use_count{timestamp}.txt', before, after)

        process_report_txt = dirs['residency_results'] / 'process_report.txt'
        try:
            analyzer = None
            for mod_name in (
                'collie_package.rd_selftest.collie_automation.log_tools.log_analyzer',
                'rd_selftest.collie_automation.log_tools.log_analyzer',
                'web_app.rd_selftest.collie_automation.log_tools.log_analyzer',
            ):
                try:
                    analyzer = importlib.import_module(mod_name)
                    break
                except Exception:
                    continue
            if analyzer is None:
                raise RuntimeError('无法导入 log_analyzer')
            analyze_log_file = getattr(analyzer, 'analyze_log_file')
            old_highlight = getattr(analyzer, 'HIGHLIGHT_PROCESSES', None)
            try:
                setattr(analyzer, 'HIGHLIGHT_PROCESSES', list(selected_packages))
                _ = analyze_log_file(
                    str(logcat_path),
                    output_dir=str(dirs['residency_results']),
                    output_name='process_report',
                )
            finally:
                if old_highlight is not None:
                    setattr(analyzer, 'HIGHLIGHT_PROCESSES', old_highlight)
        except Exception as exc:  # noqa: BLE001
            _ = process_report_txt.write_text(f'process report generation failed: {exc}\n', encoding='utf-8')

        created_at = datetime.now().isoformat(timespec='seconds')
        html_text = _offline_html(
            title='冷启动分析报告',
            timestamp=timestamp,
            created_at=created_at,
            capabilities_wire=caps_wire,
            plan_wire=plan_wire,
        )
        html_path = job_dir / '冷启动分析报告.html'
        _ = html_path.write_text(html_text, encoding='utf-8')

        bugreport_path: str | None = None
        if config.bugreport.mode == 'capture' and _plan_step_enabled(plan_wire, 'bugreport'):
            bugreport_file = job_dir / f'bugreport_{timestamp}.zip'
            try:
                res = adb_exec.run(
                    config.device_id,
                    ['bugreport', str(bugreport_file)],
                    timeout_sec=max(30, int(config.bugreport.capture_timeout_sec)),
                )
                if res.returncode == 0 and bugreport_file.exists() and bugreport_file.stat().st_size > 0:
                    bugreport_path = str(bugreport_file)
                else:
                    _ = (job_dir / 'bugreport_capture_error.txt').write_text(
                        f'bugreport capture failed: returncode={res.returncode}\n'
                        f'stdout={res.stdout or ""}\n'
                        f'stderr={res.stderr or ""}\n',
                        encoding='utf-8',
                    )
            except Exception as exc:  # noqa: BLE001
                _ = (job_dir / 'bugreport_capture_error.txt').write_text(
                    f'bugreport capture exception: {exc}\n',
                    encoding='utf-8',
                )

        manifest_path = job_dir / 'artifacts_manifest.json'
        return {
            'manifest_path': str(manifest_path),
            'html_path': str(html_path),
            'process_report_path': str(process_report_txt),
            'logcat_path': str(logcat_path),
            'selected_packages_path': str(selected_pkg_path),
            'bugreport_path': bugreport_path or '',
        }

    out = run_and_write_manifest(
        job_dir=job_dir,
        config=config,
        adb=adapter,
        fn=_work,
        when=when,
        capabilities=caps,
        plan=plan,
    )
    assert out is not None
    return out
