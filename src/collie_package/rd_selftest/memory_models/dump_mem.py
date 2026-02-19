"""简化版内存采集工具，供 rd_selftest 兼容调用。"""

from __future__ import annotations

import subprocess


def _run_adb_shell(cmd: str, timeout_sec: float = 20.0, device_id: str = '') -> str:
    adb_cmd = ['adb']
    if device_id:
        adb_cmd.extend(['-s', device_id])
    adb_cmd.extend(['shell', cmd])
    try:
        cp = subprocess.run(
            adb_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001
        return f'<error: {exc}>'

    out = cp.stdout or ''
    err = cp.stderr or ''
    if cp.returncode != 0 and err:
        return out + ('\n' if out else '') + err
    return out


def get_meminfo(device_id: str = '') -> str:
    return _run_adb_shell('dumpsys meminfo', device_id=device_id)


def get_vmstat(device_id: str = '') -> str:
    return _run_adb_shell('cat /proc/vmstat', device_id=device_id)
