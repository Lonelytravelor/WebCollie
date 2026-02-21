import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence


class SimpleperfPipelineError(RuntimeError):
    pass


def _normalize_android_arch(raw_arch: str) -> Optional[str]:
    arch = str(raw_arch or '').strip().lower()
    if 'aarch64' in arch or 'arm64' in arch:
        return 'arm64'
    if 'arm' in arch:
        return 'arm'
    if 'x86_64' in arch:
        return 'x86_64'
    if 'x86' in arch:
        return 'x86'
    return None


def _resolve_simpleperf_root() -> Path:
    base = Path(__file__).resolve().parents[1] / 'resources' / 'simpleperf'
    if not base.exists():
        raise SimpleperfPipelineError('未找到 simpleperf 资源目录: src/collie_package/resources/simpleperf')
    return base


def _assert_simpleperf_resources() -> None:
    root = _resolve_simpleperf_root()
    required = [
        root / 'report_html.py',
        root / 'report_html.js',
        root / 'simpleperf_report_lib.py',
        root / 'simpleperf_utils.py',
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SimpleperfPipelineError(f'缺少 simpleperf 依赖文件: {", ".join(missing)}')

    host_lib = root / 'bin' / 'linux' / 'x86_64' / 'libsimpleperf_report.so'
    if not host_lib.exists():
        raise SimpleperfPipelineError(f'缺少主机端解析库: {host_lib}')


def _resolve_device_simpleperf(arch_raw: str) -> Path:
    simpleperf_root = _resolve_simpleperf_root()
    arch = _normalize_android_arch(arch_raw)
    if not arch:
        raise SimpleperfPipelineError(f'无法识别设备架构: {arch_raw}')
    candidate = simpleperf_root / 'bin' / 'android' / arch / 'simpleperf'
    if not candidate.exists():
        raise SimpleperfPipelineError(f'未找到设备 simpleperf: {candidate}')
    return candidate


def _resolve_report_script() -> Path:
    simpleperf_root = _resolve_simpleperf_root()
    candidate = simpleperf_root / 'report_html.py'
    if not candidate.exists():
        raise SimpleperfPipelineError('未找到 report_html.py')
    return candidate


def run_simpleperf_pipeline(
    package_name: str,
    duration_s: int,
    out_dir: Path,
    adb_command_builder: Callable[[Sequence[str]], object],
    run_cmd: Callable[[object, int], str],
    progress: Callable[[int, str], None],
    log: Callable[[str], None],
) -> Dict[str, Path]:
    if not package_name:
        raise SimpleperfPipelineError('包名不能为空')
    if duration_s < 1:
        raise SimpleperfPipelineError('duration_s 不能小于 1')

    out_dir.mkdir(parents=True, exist_ok=True)
    _assert_simpleperf_resources()
    progress(10, '检测设备架构')
    arch_raw = run_cmd(adb_command_builder(['shell', 'uname', '-m']), 30)
    call_graph_option = '--call-graph fp'
    if 'arm' in str(arch_raw).lower() and '64' not in str(arch_raw).lower():
        call_graph_option = '-g'

    progress(15, '推送 simpleperf 到设备')
    local_simpleperf = _resolve_device_simpleperf(arch_raw)
    report_script = _resolve_report_script()

    remote_simpleperf = '/data/local/tmp/simpleperf'
    remote_data = f'/data/local/tmp/{out_dir.name}_simpleperf.data'
    local_data = out_dir / 'simpleperf.data'
    local_report = out_dir / 'simpleperf_report.txt'
    local_html = out_dir / 'simpleperf_flamegraph.html'

    run_cmd(adb_command_builder(['shell', 'rm', '-f', remote_simpleperf]), 60)
    run_cmd(adb_command_builder(['push', str(local_simpleperf), remote_simpleperf]), 120)
    run_cmd(adb_command_builder(['shell', 'chmod', '755', remote_simpleperf]), 60)

    progress(25, '准备抓取（请在设备上复现问题）')
    log('已开始抓取，请在设备上复现需要分析的场景。')

    progress(28, '检查应用是否已启动')
    start_wait = time.time()
    pid = ""
    while time.time() - start_wait < 15:
        try:
            pid = (run_cmd(adb_command_builder(['shell', 'pidof', package_name]), 10) or "").strip()
        except Exception:
            pid = ""
        if pid:
            break
        time.sleep(1.0)
    if not pid:
        raise SimpleperfPipelineError("未检测到应用进程，请先启动应用后再抓取")

    progress(35, '抓取中（simpleperf 录制进行中）')
    base_cmd = [
        'shell',
        remote_simpleperf,
        'record',
        '--app',
        package_name,
        '--duration',
        str(duration_s),
        '-o',
        remote_data,
    ]
    record_cmds = [
        base_cmd + ['-e', 'cpu-clock'] + call_graph_option.split(),
        base_cmd + call_graph_option.split(),
    ]
    last_error: Optional[Exception] = None
    stop_event = threading.Event()

    def _progress_ticker():
        start_ts = time.time()
        while not stop_event.is_set():
            elapsed = int(time.time() - start_ts)
            if duration_s > 0:
                ratio = min(1.0, elapsed / float(duration_s))
                current = 35 + int(ratio * 18)
            else:
                current = 35
            progress(current, f"抓取中 {min(elapsed, duration_s)}/{duration_s}s")
            time.sleep(1.0)

    ticker = threading.Thread(target=_progress_ticker, daemon=True)
    ticker.start()

    for record_cmd in record_cmds:
        try:
            run_cmd(adb_command_builder(record_cmd), duration_s + 180)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log(f'[warn] 录制失败，尝试备用方案: {exc}')
    stop_event.set()
    ticker.join(timeout=1.0)
    if last_error:
        raise SimpleperfPipelineError(f'录制失败: {last_error}')

    progress(55, '抓取完成，拉取 perf.data')
    run_cmd(adb_command_builder(['pull', remote_data, str(local_data)]), 180)

    progress(60, '抓取完成，开始解析')
    log('抓取完成，开始解析并生成报告。')

    progress(70, '生成文本报告')
    report = run_cmd(adb_command_builder(['shell', remote_simpleperf, 'report', '-i', remote_data]), 180)
    local_report.write_text(str(report or ''), encoding='utf-8')

    progress(85, '生成 HTML 火焰图')
    html_cmd = [
        sys.executable,
        str(report_script),
        '-i',
        str(local_data),
        '-o',
        str(local_html),
        '--no_browser',
    ]
    run_cmd(html_cmd, 240)

    progress(95, '清理设备临时文件')
    try:
        run_cmd(adb_command_builder(['shell', 'rm', '-f', remote_data]), 60)
    except Exception as exc:  # noqa: BLE001
        log(f'[warn] 清理设备临时文件失败: {exc}')

    progress(100, '完成')
    return {
        'perf_data': local_data,
        'report_txt': local_report,
        'report_html': local_html,
    }
