import json
import os
import re
import shutil
import subprocess
import importlib
import threading
import time
import uuid
import zipfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from collie_package.config_loader import load_app_list_config, load_app_settings, resolve_app_config_path, to_flat_app_config
from collie_package.utilities.web_tasks import (
    TaskHooks,
    run_app_died_monitor,
    run_check_app_versions,
    run_collect_device_meminfo,
    run_cont_startup_stay,
    run_device_info,
    run_meminfo_live,
    run_meminfo_summary,
    run_monkey,
    run_package_version,
    run_prepare_apps,
    run_compile_apps,
    run_app_install_apk,
    run_store_install_apps,
    resolve_packages_from_preset,
    build_cont_startup_config,
    parse_wm_size,
    ratio_point,
    build_app_versions_compare,
    parse_app_versions_content,
    build_app_versions_report,
    build_check_app_history_item,
)


def _import_adb_executor_api():
    last_exc = None
    for name in (
        'collie_package.rd_selftest.adb_executor',
        'rd_selftest.adb_executor',
        'web_app.rd_selftest.adb_executor',
    ):
        try:
            return importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RuntimeError('无法导入 adb_executor 模块') from last_exc


_ADB_API = _import_adb_executor_api()
AdbExecutorLike = getattr(_ADB_API, 'AdbExecutorLike', object)
SubprocessAdbExecutor = getattr(_ADB_API, 'SubprocessAdbExecutor')
validate_compile_mode = getattr(_ADB_API, 'validate_compile_mode')
validate_package_name = getattr(_ADB_API, 'validate_package_name')


_APP_SETTINGS = load_app_settings()
_UTIL_SETTINGS = _APP_SETTINGS.get('utilities_webadb', {})

AUTO_SKIP_GAME_PACKAGES = set(
    _UTIL_SETTINGS.get(
        'auto_skip_game_packages',
        ["com.tencent.tmgp.pubgmhd", "com.tencent.tmgp.sgame"],
    )
)
MONITOR_MAX_DETAIL_ROWS = int(_UTIL_SETTINGS.get('monitor_max_detail_rows', 800))


def get_adb_executor():
    return SubprocessAdbExecutor()


def register_utilities_routes(app, get_client_ip, get_user_folder):
    bp = Blueprint("utilities_webadb", __name__)

    adb_exec = get_adb_executor()

    project_root = Path(__file__).resolve().parent.parent
    app_config_path = project_root / "src" / "collie_package" / "app_config.json"
    app_config_yaml = resolve_app_config_path()
    app_install_coords_path = project_root / "src" / "collie_package" / "utilities" / "app_install_coords.json"

    utilities_jobs = {}
    utilities_jobs_lock = threading.Lock()
    device_locks = {}
    device_locks_lock = threading.Lock()
    agent_registry = {}
    agent_registry_lock = threading.Lock()
    agent_token = os.getenv("ADB_AGENT_TOKEN", "").strip()
    agent_ttl_default = int(_UTIL_SETTINGS.get('agent_ttl_default', 25))
    proxy_prefix = "agent:"
    proxy_timeout_floor = int(_UTIL_SETTINGS.get('proxy_timeout_floor', 5))

    def _adb_exists():
        return shutil.which("adb") is not None

    def _cleanup_stale_agents():
        now = time.time()
        expired = []
        with agent_registry_lock:
            for agent_id, info in agent_registry.items():
                if float(info.get("expires_at", 0.0)) < now:
                    expired.append(agent_id)
            for agent_id in expired:
                agent_registry.pop(agent_id, None)
        return expired

    def _normalize_owner_ip(owner_ip):
        value = str(owner_ip or "").strip()
        return value or "unknown"

    def _agent_registry_key(owner_ip, agent_id):
        return f"{_normalize_owner_ip(owner_ip)}::{str(agent_id or '').strip()}"

    def _list_agents_snapshot(owner_ip=None):
        owner_norm = None if owner_ip is None else _normalize_owner_ip(owner_ip)
        _cleanup_stale_agents()
        snapshots = {}
        with agent_registry_lock:
            for item in agent_registry.values():
                if not isinstance(item, dict):
                    continue
                if owner_norm is not None and str(item.get("owner_ip", "")) != owner_norm:
                    continue
                agent_id = str(item.get("agent_id", "")).strip()
                if not agent_id:
                    continue
                snapshots[agent_id] = dict(item)
        return snapshots

    def _normalize_agent_devices(devices):
        normalized = []
        if not isinstance(devices, list):
            return normalized
        for item in devices:
            if not isinstance(item, dict):
                continue
            serial = str(item.get("id", "")).strip()
            if not serial:
                continue
            normalized.append({
                "id": serial,
                "state": str(item.get("state", "unknown")).strip() or "unknown",
                "raw": str(item.get("raw", "")).strip(),
                "model": str(item.get("model", "")).strip(),
                "transport_id": str(item.get("transport_id", "")).strip(),
            })
        return normalized

    def _upsert_agent(payload, owner_ip):
        agent_id = str(payload.get("agent_id", "")).strip()
        if not agent_id:
            raise RuntimeError("agent_id 不能为空")
        if re.search(r"[^A-Za-z0-9._-]", agent_id):
            raise RuntimeError("agent_id 只允许字母/数字/._-")

        base_url = str(payload.get("base_url", "")).strip().rstrip("/")
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            raise RuntimeError("base_url 必须以 http:// 或 https:// 开头")

        ttl_sec = payload.get("ttl_sec", agent_ttl_default)
        try:
            ttl_sec = int(ttl_sec)
        except Exception:
            ttl_sec = agent_ttl_default
        ttl_sec = max(10, min(120, ttl_sec))

        devices = _normalize_agent_devices(payload.get("devices"))
        now = time.time()

        info = {
            "agent_id": agent_id,
            "agent_name": str(payload.get("agent_name", "")).strip() or agent_id,
            "owner_ip": _normalize_owner_ip(owner_ip),
            "base_url": base_url,
            "devices": devices,
            "ttl_sec": ttl_sec,
            "last_seen": now,
            "expires_at": now + ttl_sec,
        }
        reg_key = _agent_registry_key(info["owner_ip"], agent_id)
        with agent_registry_lock:
            agent_registry[reg_key] = info
        return info

    def _list_local_devices_raw():
        if not _adb_exists():
            return []

        result = adb_exec.run_host(["devices", "-l"], timeout_sec=20)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "").strip() or "adb devices 执行失败")
        devices = []
        for line in (result.stdout or "").splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            device_id = parts[0]
            state = parts[1] if len(parts) > 1 else "unknown"
            info = {
                "id": device_id,
                "state": state,
                "raw": line,
            }
            for item in parts[2:]:
                if item.startswith("model:"):
                    info["model"] = item.split(":", 1)[1]
                if item.startswith("transport_id:"):
                    info["transport_id"] = item.split(":", 1)[1]
            devices.append(info)
        return devices

    def _list_agent_devices_raw(owner_ip=None):
        devices = []
        snapshots = _list_agents_snapshot(owner_ip=owner_ip)
        for agent_id, agent in snapshots.items():
            for item in agent.get("devices", []):
                serial = str(item.get("id", "")).strip()
                if not serial:
                    continue
                devices.append({
                    "id": f"{proxy_prefix}{agent_id}:{serial}",
                    "state": str(item.get("state", "unknown")).strip() or "unknown",
                    "raw": str(item.get("raw", "")).strip() or serial,
                    "model": str(item.get("model", "")).strip(),
                    "transport_id": str(item.get("transport_id", "")).strip(),
                    "via": "agent",
                    "agent_id": agent_id,
                    "agent_name": agent.get("agent_name", agent_id),
                    "serial": serial,
                })
        return devices

    def _list_devices_raw(owner_ip=None):
        local_error = ""
        local_devices = []
        try:
            local_devices = _list_local_devices_raw()
        except Exception as exc:
            local_error = str(exc)

        proxy_devices = _list_agent_devices_raw(owner_ip=owner_ip)
        devices = [*local_devices, *proxy_devices]
        if not devices and local_error:
            raise RuntimeError(local_error)
        return devices

    def _is_proxy_device_id(device_id):
        text = str(device_id or "").strip()
        return text.startswith(proxy_prefix) and ":" in text[len(proxy_prefix):]

    def _parse_proxy_device_id(device_id):
        text = str(device_id or "").strip()
        if not _is_proxy_device_id(text):
            raise RuntimeError("非法代理设备ID")
        tail = text[len(proxy_prefix):]
        agent_id, serial = tail.split(":", 1)
        agent_id = agent_id.strip()
        serial = serial.strip()
        if not agent_id or not serial:
            raise RuntimeError("非法代理设备ID")
        return agent_id, serial

    def _call_agent_api(agent_id, path, payload, timeout_sec, owner_ip=None):
        owner_norm = None if owner_ip is None else _normalize_owner_ip(owner_ip)
        snapshots = _list_agents_snapshot(owner_ip=owner_norm)
        agent = snapshots.get(agent_id)
        if not agent:
            raise RuntimeError(f"代理 {agent_id} 不在线或未注册")

        url = f"{str(agent.get('base_url', '')).rstrip('/')}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"代理调用失败: HTTP {exc.code} {detail[:300]}")
        except Exception as exc:
            raise RuntimeError(f"代理调用异常: {str(exc)}")

        with agent_registry_lock:
            reg_key = _agent_registry_key(agent.get("owner_ip", owner_norm), agent_id)
            info = agent_registry.get(reg_key)
            if info:
                now = time.time()
                info["last_seen"] = now
                info["expires_at"] = now + int(info.get("ttl_sec", agent_ttl_default))
        return data

    def _cmd_to_text(cmd):
        if isinstance(cmd, dict):
            if cmd.get("mode") == "agent":
                args = [str(x) for x in (cmd.get("args") or [])]
                return f"agent:{cmd.get('agent_id')}:{cmd.get('serial')} adb {' '.join(args)}"
            return json.dumps(cmd, ensure_ascii=False)
        return " ".join(str(x) for x in (cmd or []))

    def _resolve_device(device_id, owner_ip=None):
        devices = _list_devices_raw(owner_ip=owner_ip)
        online = [d for d in devices if d.get("state") == "device"]
        if not online:
            raise RuntimeError("未检测到在线设备，请先连接设备并授权 USB 调试")

        if device_id:
            chosen = next((d for d in online if d["id"] == device_id), None)
            if not chosen:
                raise RuntimeError(f"设备 {device_id} 不存在或未就绪")
            return chosen["id"]

        if len(online) > 1:
            raise RuntimeError("检测到多台设备，请先选择 device_id")
        return online[0]["id"]

    def _is_device_online(device_id, owner_ip=None):
        if not device_id:
            return False
        try:
            devices = _list_devices_raw(owner_ip=owner_ip)
        except Exception:
            return False
        for dev in devices:
            if dev.get("id") == device_id and dev.get("state") == "device":
                return True
        return False

    def _adb_command(device_id, parts):
        if _is_proxy_device_id(device_id):
            agent_id, serial = _parse_proxy_device_id(device_id)
            return {
                "mode": "agent",
                "agent_id": agent_id,
                "serial": serial,
                "args": [str(x) for x in (parts or [])],
            }
        return adb_exec.build_argv(device_id=device_id, args=parts)

    def _get_device_lock(device_id):
        key = device_id or "default"
        with device_locks_lock:
            if key not in device_locks:
                device_locks[key] = threading.Lock()
            return device_locks[key]

    def _append_log(path, text):
        with open(path, "a", encoding="utf-8", errors="ignore") as f:
            f.write(text)

    def _wait_if_paused(job):
        while job.get("paused", False):
            if job.get("cancel_requested"):
                raise RuntimeError("任务已取消")
            with utilities_jobs_lock:
                if job.get("status") == "running":
                    job["status"] = "paused"
                if not job.get("message") or job.get("message") in {"running", "paused"}:
                    job["message"] = "paused"
            time.sleep(0.3)

        with utilities_jobs_lock:
            if job.get("status") == "paused":
                job["status"] = "running"
                if not job.get("message") or job.get("message") == "paused":
                    job["message"] = "running"

    def _sleep_with_control(job, seconds):
        remain = max(0.0, float(seconds))
        while remain > 0:
            _wait_if_paused(job)
            if job.get("cancel_requested"):
                raise RuntimeError("任务已取消")
            step = 0.3 if remain > 0.3 else remain
            time.sleep(step)
            remain -= step

    def _run_cmd(job, cmd, timeout=120, log_stdout=True, log_stderr=True, allow_returncodes=None):
        _wait_if_paused(job)
        if job.get("cancel_requested"):
            raise RuntimeError("任务已取消")

        cmd_text = _cmd_to_text(cmd)

        if isinstance(cmd, dict) and cmd.get("mode") == "agent":
            agent_id = str(cmd.get("agent_id", "")).strip()
            serial = str(cmd.get("serial", "")).strip()
            args = [str(x) for x in (cmd.get("args") or [])]
            if not agent_id or not serial:
                raise RuntimeError("代理命令参数缺失")

            payload = {
                "device_id": serial,
                "args": args,
                "timeout_sec": int(max(proxy_timeout_floor, timeout)),
            }
            result = _call_agent_api(
                agent_id=agent_id,
                path="/adb/run",
                payload=payload,
                timeout_sec=float(timeout) + 8.0,
                owner_ip=job.get("ip"),
            )
            out = str(result.get("stdout", "") or "")
            err = str(result.get("stderr", "") or "")
            rc_raw = result.get("returncode", 1)
            try:
                returncode = int(rc_raw)
            except Exception:
                returncode = 1

            if out and log_stdout:
                _append_log(job["stdout_path"], out)
            if err and log_stderr:
                _append_log(job["stderr_path"], err)
            if allow_returncodes is not None and returncode in set(allow_returncodes):
                return out
            if returncode != 0:
                raise RuntimeError(f"命令失败({returncode}): {cmd_text}")
            return out

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with utilities_jobs_lock:
            job["process"] = process

        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            _append_log(job["stderr_path"], f"\n[timeout] {cmd_text}\n")
            raise RuntimeError(f"命令超时: {cmd_text}")
        finally:
            with utilities_jobs_lock:
                job["process"] = None

        if out and log_stdout:
            _append_log(job["stdout_path"], out)
        if err and log_stderr:
            _append_log(job["stderr_path"], err)
        if allow_returncodes is not None and process.returncode in set(allow_returncodes):
            return out
        if process.returncode != 0:
            raise RuntimeError(f"命令失败({process.returncode}): {cmd_text}")
        return out

    def _validate_package(package_name):
        try:
            validate_package_name(package_name)
        except Exception:
            raise RuntimeError("无效包名")

    def _validate_positive_int(value, name, min_value=1, max_value=1000000):
        if not isinstance(value, int):
            raise RuntimeError(f"{name} 必须为整数")
        if value < min_value or value > max_value:
            raise RuntimeError(f"{name} 范围必须在 {min_value}~{max_value}")

    def _render_ascii_table(headers, rows):
        headers = [str(h) for h in headers]
        normalized_rows = [[str(c) for c in row] for row in rows]
        if not headers:
            return ""

        col_count = len(headers)
        widths = [len(h) for h in headers]
        for row in normalized_rows:
            for idx in range(min(col_count, len(row))):
                widths[idx] = max(widths[idx], len(row[idx]))

        border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        header_line = "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(col_count)) + " |"
        body_lines = []
        for row in normalized_rows:
            padded = []
            for idx in range(col_count):
                cell = row[idx] if idx < len(row) else ""
                padded.append(cell.ljust(widths[idx]))
            body_lines.append("| " + " | ".join(padded) + " |")

        return "\n".join([border, header_line, border, *body_lines, border])

    def _make_task_hooks(job):
        return TaskHooks(
            progress=lambda p, m: _job_set_progress(job, p, m),
            log=lambda text: _append_log(job["stdout_path"], f"{text}\n"),
            warn=lambda text: _append_log(job["stderr_path"], f"{text}\n"),
            check_cancel=lambda: _check_cancel(job),
            wait_if_paused=lambda: _wait_if_paused(job),
            sleep_with_control=lambda sec: _sleep_with_control(job, sec),
            is_cancelled=lambda: bool(job.get("cancel_requested")),
        )

    def _get_user_utilities_root(client_ip=None):
        current_ip = client_ip or get_client_ip()
        user_folder = get_user_folder(current_ip)
        utilities_root = user_folder / "utilities"
        utilities_root.mkdir(parents=True, exist_ok=True)
        return utilities_root.resolve()

    def _resolve_job_dir_for_client(job_id, client_ip):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if job and job.get("ip") == client_ip:
                return Path(job["job_dir"]).resolve()

        utilities_root = _get_user_utilities_root(client_ip)
        candidate = (utilities_root / job_id).resolve()
        if os.path.commonpath([str(utilities_root), str(candidate)]) != str(utilities_root):
            return None
        if candidate.exists() and candidate.is_dir():
            return candidate
        return None

    def _list_job_files(job_dir):
        files = []
        if not job_dir.exists():
            return files
        skip_names = {"stdout.log", "stderr.log", "simpleperf", "report_html.py"}
        for f in sorted(job_dir.iterdir()):
            if f.is_file() and f.name not in skip_names:
                files.append({"name": f.name, "size": f.stat().st_size})
        return files

    def _run_action(job):
        action = job["action"]
        params = job["params"]
        device_id = job["device_id"]
        out_dir = Path(job["job_dir"])

        if action == "device_info":
            hooks = _make_task_hooks(job)
            run_device_info(
                device_id=device_id,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
            )
            return

        if action == "package_version":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            hooks = _make_task_hooks(job)
            run_package_version(
                package_name=package_name,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
            )
            return

        if action == "check_app_versions":
            preset_name = str(params.get("preset_name", "")).strip()
            packages = resolve_packages_from_preset(preset_name, params.get("packages"), _load_app_config)
            hooks = _make_task_hooks(job)
            run_check_app_versions(
                packages=packages,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(
                    job,
                    _adb_command(device_id, list(args)),
                    timeout=timeout,
                    log_stdout=False,
                ),
                hooks=hooks,
                validate_package=_validate_package,
            )
            return

        if action == "meminfo_live":
            package_name = str(params.get("package", "")).strip()
            hooks = _make_task_hooks(job)
            run_meminfo_live(
                package_name=package_name,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
                validate_package=_validate_package,
            )
            return

        if action == "meminfo_summary_live":
            hooks = _make_task_hooks(job)
            run_meminfo_summary(
                device_id=device_id,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
            )
            return

        if action == "collect_device_meminfo_live":
            hooks = _make_task_hooks(job)
            run_collect_device_meminfo(
                device_id=device_id,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
            )
            return

        if action == "store_install_apps":
            _check_cancel(job)
            preset_name = str(params.get("preset_name", "")).strip()
            install_interval_sec = params.get("install_interval_sec", 5)
            max_check_seconds = params.get("max_check_seconds", 1200)

            _validate_positive_int(int(install_interval_sec), "install_interval_sec", 1, 120)
            _validate_positive_int(int(max_check_seconds), "max_check_seconds", 10, 7200)
            install_interval_sec = int(install_interval_sec)
            max_check_seconds = int(max_check_seconds)

            packages = resolve_packages_from_preset(preset_name, params.get("packages"), _load_app_config)
            for pkg in packages:
                _validate_package(str(pkg).strip())

            hooks = _make_task_hooks(job)

            def _set_manual_confirm(pending):
                with utilities_jobs_lock:
                    job["requires_manual_confirm"] = True
                    job["pending_packages"] = pending

            def _clear_manual_confirm():
                with utilities_jobs_lock:
                    job["requires_manual_confirm"] = False

            def _set_message(message):
                with utilities_jobs_lock:
                    job["message"] = message

            run_store_install_apps(
                packages=packages,
                device_id=device_id,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
                is_device_online=lambda: _is_device_online(device_id, owner_ip=job.get("ip")),
                load_install_coords=_load_install_coords,
                parse_wm_size=parse_wm_size,
                ratio_point=ratio_point,
                auto_skip_game_packages=AUTO_SKIP_GAME_PACKAGES,
                confirm_event=job.get("confirm_event"),
                set_manual_confirm=_set_manual_confirm,
                clear_manual_confirm=_clear_manual_confirm,
                set_message=_set_message,
                install_interval_sec=install_interval_sec,
                max_check_seconds=max_check_seconds,
            )
            return

        if action == "app_install_apk":
            apk_path = str(params.get("apk_path", "")).strip()
            if not apk_path:
                raise RuntimeError("缺少 apk_path")
            apk_file = Path(apk_path)
            if not apk_file.exists() or not apk_file.is_file() or apk_file.suffix.lower() != ".apk":
                raise RuntimeError("apk_path 无效，必须是存在的 .apk 文件")
            package_name = str(params.get("package", "")).strip()
            launch = bool(params.get("launch", False))
            hooks = _make_task_hooks(job)
            run_app_install_apk(
                apk_path=apk_file,
                package_name=package_name,
                launch=launch,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
                validate_package=_validate_package,
            )
            return

        if action == "compile_apps":
            preset_name = str(params.get("preset_name", "")).strip()
            packages = resolve_packages_from_preset(preset_name, params.get("packages"), _load_app_config)
            mode = str(params.get("mode", "speed-profile")).strip() or "speed-profile"
            mode = validate_compile_mode(mode)
            with utilities_jobs_lock:
                job["compile_items"] = []
                job["compile_summary"] = {}

            def _update_compile_summary(patch):
                with utilities_jobs_lock:
                    summary = job.setdefault("compile_summary", {})
                    summary.update(patch or {})

            def _update_compile_item(idx, patch):
                with utilities_jobs_lock:
                    items = job.setdefault("compile_items", [])
                    while len(items) <= idx:
                        items.append({"package": "", "result": "待编译"})
                    items[idx].update(patch or {})

            hooks = _make_task_hooks(job)
            hooks.update_compile_summary = _update_compile_summary
            hooks.update_compile_item = _update_compile_item

            run_compile_apps(
                packages=packages,
                mode=mode,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
                validate_package=_validate_package,
            )
            return

        if action == "prepare_apps":
            preset_name = str(params.get("preset_name", "")).strip()
            packages = resolve_packages_from_preset(preset_name, params.get("packages"), _load_app_config)
            hooks = _make_task_hooks(job)
            run_prepare_apps(
                packages=packages,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
                hooks=hooks,
                validate_package=_validate_package,
            )
            return

        if action == "app_died_monitor":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            interval_sec = params.get("interval_sec", 1)
            _validate_positive_int(int(interval_sec), "interval_sec", 1, 30)
            interval_sec = int(interval_sec)

            def _update_summary(patch):
                with utilities_jobs_lock:
                    summary = job.setdefault("monitor_summary", {})
                    summary.update(patch or {})

            def _set_paused(message):
                with utilities_jobs_lock:
                    job["paused"] = True
                    job["status"] = "paused"
                    job["message"] = message

            hooks = TaskHooks(
                progress=lambda p, m: _job_set_progress(job, p, m),
                log=lambda text: _append_log(job["stdout_path"], f"{text}\n"),
                warn=lambda text: _append_log(job["stderr_path"], f"{text}\n"),
                check_cancel=lambda: _check_cancel(job),
                wait_if_paused=lambda: _wait_if_paused(job),
                sleep_with_control=lambda sec: _sleep_with_control(job, sec),
                is_cancelled=lambda: bool(job.get("cancel_requested")),
                add_monitor_detail=lambda alive, state, note="": _job_add_monitor_detail(job, alive, state, note),
                update_monitor_summary=_update_summary,
                set_paused=_set_paused,
            )

            run_app_died_monitor(
                package_name=package_name,
                interval_sec=interval_sec,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(
                    job,
                    _adb_command(device_id, list(args)),
                    timeout=timeout,
                    log_stdout=False if args[:2] == ["shell", "pidof"] else True,
                    log_stderr=False if args[:2] == ["shell", "pidof"] else True,
                    allow_returncodes={0, 1} if args[:2] == ["shell", "pidof"] else None,
                ),
                hooks=hooks,
                device_online_checker=lambda: _is_device_online(device_id, owner_ip=job.get("ip")),
            )

        if action == "monkey_run":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            events = params.get("events", 200)
            throttle = params.get("throttle_ms", 300)
            seed = params.get("seed")
            _validate_positive_int(events, "events", 1, 2000000)
            _validate_positive_int(throttle, "throttle_ms", 0, 10000)

            if seed is not None:
                _validate_positive_int(seed, "seed", 1, 2147483647)
            run_monkey(
                package_name=package_name,
                events=int(events),
                throttle_ms=int(throttle),
                seed=seed,
                out_dir=out_dir,
                adb_runner=lambda args, timeout: _run_cmd(job, _adb_command(device_id, list(args)), timeout=timeout),
            )
            return

        if action == "simpleperf_record":
            from collie_package.utilities.simpleperf_pipeline import run_simpleperf_pipeline

            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            duration = params.get("duration_s", 10)
            _validate_positive_int(duration, "duration_s", 1, 600)
            try:
                run_simpleperf_pipeline(
                    package_name=package_name,
                    duration_s=int(duration),
                    out_dir=out_dir,
                    adb_command_builder=lambda parts: _adb_command(device_id, list(parts)),
                    run_cmd=lambda cmd, timeout: _run_cmd(job, cmd, timeout=timeout),
                    progress=lambda p, m: _job_set_progress(job, p, m),
                    log=lambda text: _append_log(job["stdout_path"], f"{text}\n"),
                )
            finally:
                pass
            return

        if action == "cont_startup_stay":
            config = build_cont_startup_config(device_id=device_id, params=params)

            hooks = _make_task_hooks(job)

            run_cont_startup_stay(
                config=config,
                job_dir=out_dir,
                adb_exec=adb_exec,
                hooks=hooks,
            )
            return

        raise RuntimeError(f"不支持的 action: {action}")

    def _run_job_thread(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
            job["started_at"] = datetime.now().strftime("%Y%m%d_%H%M%S")
            if not isinstance(job.get("progress"), int) or job.get("progress", 0) <= 0:
                job["progress"] = 1
            job["message"] = "running"

        lock = _get_device_lock(job.get("device_id"))
        with lock:
            try:
                if job.get("cancel_requested"):
                    raise RuntimeError("任务已取消")
                _run_action(job)
                with utilities_jobs_lock:
                    job["status"] = "completed"
                    job["exit_code"] = 0
                    job["progress"] = 100
                    job["message"] = "completed"
            except Exception as exc:
                with utilities_jobs_lock:
                    if job.get("cancel_requested"):
                        job["status"] = "cancelled"
                    else:
                        job["status"] = "error"
                    job["exit_code"] = 1
                    job["error"] = str(exc)
                    if job["status"] == "cancelled":
                        job["message"] = "cancelled"
                    else:
                        job["message"] = "error"
                _append_log(job["stderr_path"], f"\n[error] {exc}\n")
            finally:
                with utilities_jobs_lock:
                    job["finished_at"] = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _job_to_response(job):
        job_dir = Path(job["job_dir"])
        files = []
        if job_dir.exists():
            for f in sorted(job_dir.iterdir()):
                if f.is_file() and f.name not in {"stdout.log", "stderr.log"}:
                    files.append({"name": f.name, "size": f.stat().st_size})
        return {
            "job_id": job["id"],
            "action": job["action"],
            "device_id": job.get("device_id"),
            "status": job["status"],
            "params": job.get("params", {}),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "exit_code": job.get("exit_code"),
            "error": job.get("error"),
            "paused": bool(job.get("paused", False)),
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "requires_manual_confirm": bool(job.get("requires_manual_confirm", False)),
            "pending_packages": job.get("pending_packages", []),
            "compile_summary": job.get("compile_summary", {}),
            "compile_items": job.get("compile_items", []),
            "monitor_summary": job.get("monitor_summary", {}),
            "monitor_details": job.get("monitor_details", []),
            "files": files,
        }

    @bp.route("/api/adb/agent/register", methods=["POST"])
    def api_adb_agent_register():
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求体必须是对象"}), 400

        if agent_token:
            req_token = str(data.get("token", "")).strip()
            if req_token != agent_token:
                return jsonify({"error": "token 校验失败"}), 403

        try:
            info = _upsert_agent(data, owner_ip=get_client_ip())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({
            "status": "ok",
            "agent_id": info.get("agent_id"),
            "expires_at": info.get("expires_at"),
            "device_count": len(info.get("devices", [])),
        })

    @bp.route("/api/adb/agents")
    def api_adb_agents():
        snapshots = _list_agents_snapshot(owner_ip=get_client_ip())
        agents = []
        for agent_id, item in sorted(snapshots.items(), key=lambda x: x[0]):
            agents.append({
                "agent_id": agent_id,
                "agent_name": item.get("agent_name", agent_id),
                "base_url": item.get("base_url", ""),
                "device_count": len(item.get("devices", [])),
                "last_seen": item.get("last_seen"),
                "expires_at": item.get("expires_at"),
            })
        return jsonify({"agents": agents})

    @bp.route("/api/adb/agent/script")
    def api_adb_agent_script():
        agent_script = Path(__file__).resolve().parent / "adb_agent.py"
        if not agent_script.exists():
            return jsonify({"error": "adb_agent.py 不存在"}), 404
        return send_file(
            str(agent_script),
            mimetype="text/x-python; charset=utf-8",
            as_attachment=True,
            download_name="adb_agent.py",
        )

    @bp.route("/api/adb/devices")
    def api_adb_devices():
        owner_ip = get_client_ip()
        try:
            devices = _list_devices_raw(owner_ip=owner_ip)
            adb_found = bool(_adb_exists()) or bool(_list_agents_snapshot(owner_ip=owner_ip))
            return jsonify({"devices": devices, "adb_found": adb_found})
        except Exception as exc:
            adb_found = bool(_adb_exists()) or bool(_list_agents_snapshot(owner_ip=owner_ip))
            return jsonify({"devices": [], "adb_found": adb_found, "error": str(exc)}), 500

    @bp.route("/api/utilities/presets")
    def api_utilities_presets():
        try:
            cfg = _load_app_config()
            presets = []
            for key, value in cfg.items():
                if isinstance(value, list):
                    presets.append({"name": key, "count": len(value)})
            presets.sort(key=lambda x: x["name"])
            return jsonify({"presets": presets})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/utilities/presets/<path:preset_name>")
    def api_utilities_preset_detail(preset_name):
        try:
            cfg = _load_app_config()
            value = cfg.get(preset_name)
            if not isinstance(value, list):
                return jsonify({"error": "预设不存在或不是列表"}), 404
            limit_raw = request.args.get("limit")
            packages = [str(x) for x in value]
            if limit_raw:
                try:
                    limit = max(1, min(500, int(limit_raw)))
                    packages = packages[:limit]
                except ValueError:
                    pass
            return jsonify({
                "name": preset_name,
                "count": len(value),
                "packages": packages,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/utilities/run", methods=["POST"])
    def api_utilities_run():
        data = request.json or {}
        action = str(data.get("action", "")).strip()
        params = data.get("params") or {}
        req_device = str(data.get("device_id", "")).strip() or None

        if not action:
            return jsonify({"error": "缺少 action"}), 400
        if not isinstance(params, dict):
            return jsonify({"error": "params 必须是对象"}), 400

        try:
            device_id = _resolve_device(req_device, owner_ip=get_client_ip())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

        client_ip = get_client_ip()
        user_folder = get_user_folder(client_ip)
        utilities_root = user_folder / "utilities"
        utilities_root.mkdir(parents=True, exist_ok=True)

        job_id = str(uuid.uuid4())[:8]
        job_dir = utilities_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        job = {
            "id": job_id,
            "ip": client_ip,
            "action": action,
            "device_id": device_id,
            "params": params,
            "status": "queued",
            "created_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "process": None,
            "cancel_requested": False,
            "paused": False,
            "progress": 0,
            "message": "queued",
            "requires_manual_confirm": False,
            "pending_packages": [],
            "compile_summary": {},
            "compile_items": [],
            "monitor_summary": {},
            "monitor_details": [],
            "confirm_event": threading.Event(),
            "job_dir": str(job_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }

        with utilities_jobs_lock:
            utilities_jobs[job_id] = job

        worker = threading.Thread(target=_run_job_thread, args=(job_id,), daemon=True)
        worker.start()

        return jsonify({"job_id": job_id, "status": "queued"})

    @bp.route("/api/utilities/check-app/history")
    def api_check_app_history():
        client_ip = get_client_ip()
        utilities_root = _get_user_utilities_root(client_ip)
        items_by_id = {}

        with utilities_jobs_lock:
            for job_id, job in utilities_jobs.items():
                if job.get("ip") != client_ip:
                    continue
                if job.get("action") != "check_app_versions":
                    continue

                job_dir = Path(job["job_dir"]).resolve()
                result_file = job_dir / "app_versions.txt"
                has_result = result_file.exists()
                updated_ts = result_file.stat().st_mtime if has_result else (
                    job_dir.stat().st_mtime if job_dir.exists() else time.time()
                )
                items_by_id[job_id] = build_check_app_history_item(
                    job_id=job_id,
                    job_status=job.get("status", "unknown"),
                    created_at=job.get("created_at") or "",
                    updated_ts=updated_ts,
                    result_file=result_file,
                    source="memory",
                    files=_list_job_files(job_dir),
                )

        for item in utilities_root.iterdir():
            if not item.is_dir():
                continue
            job_id = item.name
            result_file = item / "app_versions.txt"
            if not result_file.exists():
                continue
            updated_ts = result_file.stat().st_mtime

            if job_id in items_by_id:
                current = items_by_id[job_id]
                current["has_result"] = True
                current["result_file"] = "app_versions.txt"
                current["files"] = _list_job_files(item)
                if updated_ts > current["_updated_ts"]:
                    current["_updated_ts"] = updated_ts
                    current["updated_at"] = datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S")
                continue

            items_by_id[job_id] = build_check_app_history_item(
                job_id=job_id,
                job_status="completed",
                created_at=datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y%m%d_%H%M%S"),
                updated_ts=updated_ts,
                result_file=result_file,
                source="disk",
                files=_list_job_files(item),
            )

        items = list(items_by_id.values())
        items.sort(key=lambda x: x["_updated_ts"], reverse=True)
        for item in items:
            item.pop("_updated_ts", None)

        return jsonify({"items": items})

    @bp.route("/api/utilities/check-app/result/<job_id>")
    def api_check_app_result(job_id):
        client_ip = get_client_ip()
        job_dir = _resolve_job_dir_for_client(job_id, client_ip)
        if not job_dir:
            return jsonify({"error": "任务不存在"}), 404

        result_file = job_dir / "app_versions.txt"
        if not result_file.exists():
            return jsonify({"error": "该任务没有 app_versions.txt 结果"}), 404

        content = result_file.read_text(encoding="utf-8", errors="ignore")
        updated_at = datetime.fromtimestamp(result_file.stat().st_mtime).strftime('%Y%m%d_%H%M%S')
        report = build_app_versions_report(job_id=job_id, content=content, updated_at=updated_at)
        return jsonify(
            {
                "job_id": job_id,
                "file": "app_versions.txt",
                "updated_at": updated_at,
                "rows_count": report.get("rows_count", 0),
                "rows": report.get("rows", []),
                "content": content,
                "markdown": report.get("markdown", ""),
            }
        )

    @bp.route("/api/utilities/check-app/history/<job_id>", methods=["DELETE"])
    def api_check_app_history_delete(job_id):
        client_ip = get_client_ip()
        job_dir = _resolve_job_dir_for_client(job_id, client_ip)
        if not job_dir:
            return jsonify({"error": "任务不存在"}), 404

        has_app_versions = (job_dir / "app_versions.txt").exists()
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            action = job.get("action") if job and job.get("ip") == client_ip else None
        if not has_app_versions and action != "check_app_versions":
            return jsonify({"error": "该任务不是版本检查历史记录"}), 400

        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if job and job.get("ip") == client_ip:
                proc = job.get("process")
                if proc is not None:
                    try:
                        proc.terminate()
                        time.sleep(0.2)
                        if proc.poll() is None:
                            proc.kill()
                    except Exception:
                        pass
                utilities_jobs.pop(job_id, None)

        try:
            if job_dir.exists():
                shutil.rmtree(job_dir)
        except Exception as exc:
            return jsonify({"error": f"删除失败: {str(exc)}"}), 500

        return jsonify({"status": "deleted", "job_id": job_id})

    @bp.route("/api/utilities/check-app/compare", methods=["POST"])
    def api_check_app_compare():
        data = request.json or {}
        job_id_1 = str(data.get("job_id_1", "")).strip()
        job_id_2 = str(data.get("job_id_2", "")).strip()
        if not job_id_1 or not job_id_2:
            return jsonify({"error": "请提供两份历史结果任务ID"}), 400
        if job_id_1 == job_id_2:
            return jsonify({"error": "请选择两份不同的结果"}), 400

        client_ip = get_client_ip()
        job_dir_1 = _resolve_job_dir_for_client(job_id_1, client_ip)
        job_dir_2 = _resolve_job_dir_for_client(job_id_2, client_ip)
        if not job_dir_1 or not job_dir_2:
            return jsonify({"error": "任务不存在或无权限访问"}), 404

        file_1 = job_dir_1 / "app_versions.txt"
        file_2 = job_dir_2 / "app_versions.txt"
        if not file_1.exists() or not file_2.exists():
            return jsonify({"error": "待对比任务缺少 app_versions.txt"}), 404

        try:
            compare_result = build_app_versions_compare(
                job_id_1=job_id_1,
                job_id_2=job_id_2,
                text_a=file_1.read_text(encoding="utf-8", errors="ignore"),
                text_b=file_2.read_text(encoding="utf-8", errors="ignore"),
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "status": "success",
                "job_id_1": job_id_1,
                "job_id_2": job_id_2,
                "summary": compare_result.get("summary", {}),
                "rows": compare_result.get("rows", []),
                "comparison_text": compare_result.get("comparison_text", ""),
                "markdown": compare_result.get("markdown", ""),
            }
        )

    @bp.route("/api/utilities/jobs/<job_id>")
    def api_utilities_job_status(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            payload = _job_to_response(job)
        return jsonify(payload)

    @bp.route("/api/utilities/jobs/<job_id>/logs")
    def api_utilities_job_logs(job_id):
        stream = (request.args.get("stream") or "stdout").strip().lower()
        offset_raw = request.args.get("offset", "0")
        limit_raw = request.args.get("limit", "200")

        if stream not in {"stdout", "stderr"}:
            return jsonify({"error": "stream 只能是 stdout 或 stderr"}), 400
        try:
            offset = max(0, int(offset_raw))
            limit = max(1, min(1000, int(limit_raw)))
        except ValueError:
            return jsonify({"error": "offset/limit 必须是整数"}), 400

        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            log_path = Path(job[f"{stream}_path"])

        if not log_path.exists():
            return jsonify({"offset": offset, "next_offset": offset, "lines": []})

        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        sliced = lines[offset: offset + limit]
        return jsonify({
            "offset": offset,
            "next_offset": offset + len(sliced),
            "total": len(lines),
            "lines": sliced,
        })

    @bp.route("/api/utilities/jobs/<job_id>/cancel", methods=["POST"])
    def api_utilities_job_cancel(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            job["cancel_requested"] = True
            job["paused"] = False
            job.get("confirm_event").set()
            proc = job.get("process")

        if proc is not None:
            try:
                proc.terminate()
                time.sleep(0.3)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        return jsonify({"status": "cancel_requested"})

    @bp.route("/api/utilities/jobs/<job_id>/confirm-check", methods=["POST"])
    def api_utilities_job_confirm_check(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            if not job.get("requires_manual_confirm"):
                return jsonify({"error": "当前任务不需要手动确认"}), 400
            job.get("confirm_event").set()
            job["message"] = "已收到确认，开始校验"
        return jsonify({"status": "confirm_received"})

    @bp.route("/api/utilities/jobs/<job_id>/pause", methods=["POST"])
    def api_utilities_job_pause(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            if job.get("status") not in {"queued", "running", "paused"}:
                return jsonify({"error": f"当前状态不支持暂停: {job.get('status')}"}), 400
            job["paused"] = True
            if job.get("status") == "running":
                job["status"] = "paused"
            job["message"] = "paused"
        return jsonify({"status": "paused"})

    @bp.route("/api/utilities/jobs/<job_id>/resume", methods=["POST"])
    def api_utilities_job_resume(job_id):
        with utilities_jobs_lock:
            job = utilities_jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务不存在"}), 404
            if job.get("status") not in {"queued", "running", "paused"}:
                return jsonify({"error": f"当前状态不支持恢复: {job.get('status')}"}), 400
            job["paused"] = False
            if job.get("status") == "paused":
                job["status"] = "running"
            if job.get("message") == "paused":
                job["message"] = "running"
        return jsonify({"status": "running"})

    @bp.route("/api/utilities/download/<job_id>/<path:filename>")
    def api_utilities_download(job_id, filename):
        client_ip = get_client_ip()
        job_dir = _resolve_job_dir_for_client(job_id, client_ip)
        if not job_dir:
            return jsonify({"error": "任务不存在"}), 404

        target = (job_dir / filename).resolve()
        if os.path.commonpath([str(job_dir), str(target)]) != str(job_dir):
            return jsonify({"error": "非法文件路径"}), 400
        if not target.exists() or not target.is_file():
            return jsonify({"error": "文件不存在"}), 404

        return send_file(str(target), as_attachment=True, download_name=target.name)

    @bp.route("/api/utilities/preview/<job_id>/<path:filename>")
    def api_utilities_preview(job_id, filename):
        client_ip = get_client_ip()
        job_dir = _resolve_job_dir_for_client(job_id, client_ip)
        if not job_dir:
            return jsonify({"error": "任务不存在"}), 404

        target = (job_dir / filename).resolve()
        if os.path.commonpath([str(job_dir), str(target)]) != str(job_dir):
            return jsonify({"error": "非法文件路径"}), 400
        if not target.exists() or not target.is_file():
            return jsonify({"error": "文件不存在"}), 404

        suffix = target.suffix.lower()
        if suffix not in {".html", ".htm", ".txt", ".json", ".log"}:
            return jsonify({"error": "该文件类型不支持在线预览"}), 400

        if suffix in {".html", ".htm"}:
            mimetype = "text/html"
        elif suffix == ".json":
            mimetype = "application/json"
        else:
            mimetype = "text/plain"
        return send_file(str(target), mimetype=f"{mimetype}; charset=utf-8", as_attachment=False)

    app.register_blueprint(bp)
    def _load_app_config():
        yaml_cfg = load_app_list_config()
        if isinstance(yaml_cfg, dict) and yaml_cfg:
            return to_flat_app_config(yaml_cfg)
        if app_config_yaml is not None and app_config_yaml.exists():
            yaml_cfg = load_app_list_config()
            if isinstance(yaml_cfg, dict) and yaml_cfg:
                return to_flat_app_config(yaml_cfg)
        if not app_config_path.exists():
            return {}
        with open(app_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    def _load_install_coords():
        if app_install_coords_path.exists():
            try:
                with open(app_install_coords_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    coords = {}
                    for k, v in data.items():
                        if isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) == 2:
                            coords[k.lower()] = (int(v[0]), int(v[1]))
                    return coords
            except Exception:
                return {}
        return {}

    def _job_set_progress(job, progress, message):
        with utilities_jobs_lock:
            job["progress"] = max(0, min(100, int(progress)))
            job["message"] = str(message) if message is not None else ""

    def _job_add_monitor_detail(job, alive, state, note=""):
        now_hms = datetime.now().strftime("%H:%M:%S")
        entry = {
            "time": now_hms,
            "alive": bool(alive),
            "state": str(state),
            "note": str(note or ""),
        }
        log_line = f"[monitor][{now_hms}] state={entry['state']} alive={entry['alive']} note={entry['note']}\n"
        _append_log(job["stdout_path"], log_line)
        with utilities_jobs_lock:
            details = job.setdefault("monitor_details", [])
            details.clear()
            details.append(entry)

            summary = job.setdefault("monitor_summary", {})
            summary["package"] = str(job.get("params", {}).get("package", ""))
            summary["checks"] = int(summary.get("checks", 0)) + 1
            if alive:
                summary["alive_checks"] = int(summary.get("alive_checks", 0)) + 1
            else:
                summary["dead_checks"] = int(summary.get("dead_checks", 0)) + 1
            summary["last_state"] = str(state)
            summary["last_note"] = str(note or "")
            summary["last_time"] = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _check_cancel(job):
        if job.get("cancel_requested"):
            raise RuntimeError("任务已取消")
