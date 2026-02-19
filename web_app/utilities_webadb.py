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


TABLE_ROW_RE = re.compile(r"^\|\s*(?P<c1>[^|]+?)\s*\|\s*(?P<c2>[^|]+?)\s*\|$")
KV_LINE_RE = re.compile(
    r"^(?P<pkg>[A-Za-z0-9_\.]+)\s*(?:versin|version|版本)?\s*[:：]\s*(?P<ver>.+)$",
    re.IGNORECASE,
)
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

    def _extract_version_name(text):
        match = re.search(r"versionName=(\S+)", text or "")
        if match:
            return match.group(1)
        return "未获取到版本号"

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
        for f in sorted(job_dir.iterdir()):
            if f.is_file() and f.name not in {"stdout.log", "stderr.log"}:
                files.append({"name": f.name, "size": f.stat().st_size})
        return files

    def _parse_app_versions_content(content):
        result = {}
        for raw_line in (content or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("+") and line.endswith("+"):
                continue

            row_match = TABLE_ROW_RE.match(line)
            if row_match:
                c1 = row_match.group("c1").strip()
                c2 = row_match.group("c2").strip()
                if c1.lower() == "package_name" and c2.lower() == "version_name":
                    continue
                if c1:
                    result[c1] = c2
                continue

            kv_match = KV_LINE_RE.match(line)
            if kv_match:
                result[kv_match.group("pkg").strip()] = kv_match.group("ver").strip()
        return result

    def _escape_markdown_cell(value):
        text = str(value if value is not None else "")
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()

    def _render_markdown_table(headers, rows):
        header_line = "| " + " | ".join(_escape_markdown_cell(h) for h in headers) + " |"
        split_line = "| " + " | ".join("---" for _ in headers) + " |"
        body_lines = []
        for row in rows:
            body_lines.append("| " + " | ".join(_escape_markdown_cell(c) for c in row) + " |")
        if not body_lines:
            body_lines.append("| " + " | ".join("-" for _ in headers) + " |")
        return "\n".join([header_line, split_line, *body_lines])

    def _run_action(job):
        action = job["action"]
        params = job["params"]
        device_id = job["device_id"]
        out_dir = Path(job["job_dir"])

        def _coerce_bool(value, name, default=False):
            if value is None:
                return bool(default)
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in {0, 1}:
                return bool(value)
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"true", "1", "yes", "y"}:
                    return True
                if text in {"false", "0", "no", "n"}:
                    return False
            raise RuntimeError(f"{name} 必须为 bool")

        if action == "device_info":
            model = _run_cmd(job, _adb_command(device_id, ["shell", "getprop", "ro.product.model"]))
            android_ver = _run_cmd(job, _adb_command(device_id, ["shell", "getprop", "ro.build.version.release"]))
            sdk = _run_cmd(job, _adb_command(device_id, ["shell", "getprop", "ro.build.version.sdk"]))
            content = [
                f"device_id: {device_id}",
                f"model: {model.strip()}",
                f"android: {android_ver.strip()}",
                f"sdk: {sdk.strip()}",
            ]
            (out_dir / "device_info.txt").write_text("\n".join(content) + "\n", encoding="utf-8")
            return

        if action == "package_version":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            output = _run_cmd(job, _adb_command(device_id, ["shell", "dumpsys", "package", package_name]), timeout=180)
            (out_dir / f"package_{package_name}.txt").write_text(output, encoding="utf-8")
            return

        if action == "check_app_versions":
            preset_name = str(params.get("preset_name", "")).strip()
            packages = params.get("packages")
            if preset_name:
                cfg = _load_app_config()
                preset = cfg.get(preset_name)
                if not isinstance(preset, list) or not preset:
                    raise RuntimeError("preset_name 无效或为空")
                packages = preset

            if not isinstance(packages, list) or not packages:
                raise RuntimeError("packages 必须是非空数组，或提供 preset_name")

            pkg_list = [str(p).strip() for p in packages if str(p).strip()]
            for pkg in pkg_list:
                _validate_package(pkg)

            rows = []
            total = len(pkg_list)
            _job_set_progress(job, 5, f"开始检查应用版本，共 {total} 个")
            for idx, pkg in enumerate(pkg_list):
                _check_cancel(job)
                _wait_if_paused(job)
                _job_set_progress(job, 5 + int(85 * (idx / max(1, total))), f"检查 {idx + 1}/{total}: {pkg}")
                output = _run_cmd(
                    job,
                    _adb_command(device_id, ["shell", "dumpsys", "package", pkg]),
                    timeout=120,
                    log_stdout=False,
                )
                version = _extract_version_name(output)
                rows.append((pkg, version))

            table_text = "包名-版本号对照表\n" + _render_ascii_table(("package_name", "version_name"), rows)
            _append_log(job["stdout_path"], table_text + "\n")
            (out_dir / "app_versions.txt").write_text(table_text + "\n", encoding="utf-8")
            _job_set_progress(job, 100, "应用版本检查完成")
            return

        if action == "meminfo_live":
            package_name = str(params.get("package", "")).strip()
            cmd = ["shell", "dumpsys", "meminfo"]
            if package_name:
                _validate_package(package_name)
                cmd.append(package_name)
            output = _run_cmd(job, _adb_command(device_id, cmd), timeout=180)
            (out_dir / "meminfo_live.txt").write_text(output, encoding="utf-8")
            return

        if action == "meminfo_summary_live":
            from collie_package.utilities import meminfo_summary

            raw = _run_cmd(job, _adb_command(device_id, ["shell", "dumpsys", "meminfo"]), timeout=180)
            report = meminfo_summary.generate_report(raw, f"adb shell dumpsys meminfo ({device_id})")
            (out_dir / "meminfo_summary.txt").write_text(report, encoding="utf-8")
            return

        if action == "collect_device_meminfo_live":
            getprop = _run_cmd(job, _adb_command(device_id, ["shell", "getprop"]), timeout=180)
            meminfo = _run_cmd(job, _adb_command(device_id, ["shell", "dumpsys", "meminfo"]), timeout=180)
            text = f"# device_id: {device_id}\n\n## getprop\n{getprop}\n\n## dumpsys meminfo\n{meminfo}"
            (out_dir / "collect_device_meminfo.txt").write_text(text, encoding="utf-8")
            return

        if action == "store_install_apps":
            _check_cancel(job)
            store_package = "com.xiaomi.market"
            preset_name = str(params.get("preset_name", "")).strip()
            packages = params.get("packages")
            install_interval_sec = params.get("install_interval_sec", 5)
            max_check_seconds = params.get("max_check_seconds", 1200)

            _validate_positive_int(int(install_interval_sec), "install_interval_sec", 1, 120)
            _validate_positive_int(int(max_check_seconds), "max_check_seconds", 10, 7200)
            install_interval_sec = int(install_interval_sec)
            max_check_seconds = int(max_check_seconds)

            if preset_name:
                cfg = _load_app_config()
                preset = cfg.get(preset_name)
                if not isinstance(preset, list) or not preset:
                    raise RuntimeError("preset_name 无效或为空")
                packages = preset

            if not isinstance(packages, list) or not packages:
                raise RuntimeError("packages 必须是非空数组，或提供 preset_name")

            packages = [str(p).strip() for p in packages if str(p).strip()]
            for pkg in packages:
                _validate_package(pkg)

            _job_set_progress(job, 1, "检查设备分辨率")
            wm_size = _run_cmd(job, _adb_command(device_id, ["shell", "wm", "size"]), timeout=30)
            size_candidates = _parse_wm_size(wm_size)
            screen_size = size_candidates[0] if size_candidates else None

            coords_map = _load_install_coords()
            known_point = None
            if screen_size:
                key = f"{screen_size[0]}x{screen_size[1]}".lower()
                known_point = coords_map.get(key)

            ratios = []
            for k, (x, y) in coords_map.items():
                m = re.match(r"(\d+)x(\d+)", k)
                if not m:
                    continue
                w, h = int(m.group(1)), int(m.group(2))
                if w and h:
                    ratios.append((x / w, y / h))
            ratios += [(0.50, 0.93), (0.50, 0.94)]

            _job_set_progress(job, 2, "打开应用商店")
            _run_cmd(
                job,
                _adb_command(device_id, ["shell", "monkey", "-p", store_package, "-c", "android.intent.category.LAUNCHER", "1"]),
                timeout=30,
            )
            time.sleep(2)

            _job_set_progress(job, 5, "读取已安装列表")
            pm_list = _run_cmd(job, _adb_command(device_id, ["shell", "pm", "list", "packages"]), timeout=60)
            installed = set()
            for line in pm_list.splitlines():
                if line.startswith("package:"):
                    installed.add(line.replace("package:", "").strip())

            pending = [p for p in packages if p not in installed]
            if not pending:
                (out_dir / "store_install_summary.txt").write_text("所有应用已安装\n", encoding="utf-8")
                _job_set_progress(job, 100, "全部已安装")
                return

            _append_log(job["stdout_path"], f"\n待安装数量: {len(pending)}\n")

            def _tap_install_once():
                if known_point and isinstance(known_point, tuple):
                    x, y = known_point
                    _run_cmd(job, _adb_command(device_id, ["shell", "input", "tap", str(int(x)), str(int(y))]), timeout=10)
                    return
                if ratios:
                    x, y = _ratio_point(ratios[0], screen_size)
                    _run_cmd(job, _adb_command(device_id, ["shell", "input", "tap", str(int(x)), str(int(y))]), timeout=10)
                    return
                _run_cmd(job, _adb_command(device_id, ["shell", "input", "tap", "540", "2100"]), timeout=10)

            _job_set_progress(job, 10, "依次触发安装")
            for idx, pkg in enumerate(pending):
                _check_cancel(job)
                _job_set_progress(job, 10 + int(40 * (idx / max(1, len(pending)))), f"打开详情页并触发安装: {pkg}")
                _run_cmd(
                    job,
                    _adb_command(
                        device_id,
                        [
                            "shell",
                            "am",
                            "start",
                            "-a",
                            "android.intent.action.VIEW",
                            "-d",
                            f"market://details?id={pkg}",
                        ],
                    ),
                    timeout=30,
                )
                _sleep_with_control(job, max(1, install_interval_sec))
                _tap_install_once()
                _sleep_with_control(job, 2)
                _run_cmd(job, _adb_command(device_id, ["shell", "input", "keyevent", "KEYCODE_HOME"]), timeout=10)
                _sleep_with_control(job, 1)

            _job_set_progress(job, 55, f"本轮触发完成，等待手动确认后再校验（建议间隔 {install_interval_sec}s）")
            with utilities_jobs_lock:
                job["requires_manual_confirm"] = True
                job["pending_packages"] = pending

            manual_confirm_deadline = time.time() + max_check_seconds
            auto_confirmed = False
            offline_warned = False
            while not job.get("confirm_event").is_set():
                _check_cancel(job)
                if time.time() >= manual_confirm_deadline and job.get("requires_manual_confirm"):
                    if _is_device_online(device_id, owner_ip=job.get("ip")):
                        auto_confirmed = True
                        with utilities_jobs_lock:
                            job.get("confirm_event").set()
                            job["message"] = "等待确认超时，设备在线，自动继续并跳过游戏校验"
                        _append_log(
                            job["stdout_path"],
                            "\n[auto-confirm] 等待 double check 超时，设备在线，自动继续。\n",
                        )
                        break
                    if not offline_warned:
                        offline_warned = True
                        with utilities_jobs_lock:
                            job["message"] = "等待确认超时，但设备当前离线，继续等待手动确认"
                        _append_log(
                            job["stderr_path"],
                            "\n[warn] 等待 double check 超时，但设备当前离线，未自动继续。\n",
                        )
                time.sleep(1)

            with utilities_jobs_lock:
                job["requires_manual_confirm"] = False
                if auto_confirmed:
                    job["message"] = "自动继续，开始校验安装结果（跳过游戏）"
                else:
                    job["message"] = "已确认，开始校验安装结果"

            deadline = time.time() + max_check_seconds
            still = set(pending)
            skipped_games = []
            if auto_confirmed:
                skipped_games = sorted(pkg for pkg in still if pkg in AUTO_SKIP_GAME_PACKAGES)
                if skipped_games:
                    for pkg in skipped_games:
                        still.discard(pkg)
                    _append_log(
                        job["stdout_path"],
                        f"\n[auto-confirm] 已跳过游戏校验: {skipped_games}\n",
                    )
            _job_set_progress(job, 65, "校验安装结果")
            while still and time.time() < deadline:
                _check_cancel(job)
                pm_list_now = _run_cmd(job, _adb_command(device_id, ["shell", "pm", "list", "packages"]), timeout=60)
                installed_now = set()
                for line in pm_list_now.splitlines():
                    if line.startswith("package:"):
                        installed_now.add(line.replace("package:", "").strip())

                finished = [pkg for pkg in list(still) if pkg in installed_now]
                for pkg in finished:
                    still.remove(pkg)
                if finished:
                    _append_log(job["stdout_path"], f"\n校验通过: {finished}\n")
                done = len(pending) - len(still)
                _job_set_progress(job, 65 + int(30 * (done / max(1, len(pending)))), f"校验完成 {done}/{len(pending)}")
                if still:
                    _sleep_with_control(job, 6)

            summary_lines = [
                f"device_id: {device_id}",
                f"screen_size_candidates: {size_candidates}",
                f"known_point: {known_point}",
                f"pending_total: {len(pending)}",
                f"install_interval_sec: {install_interval_sec}",
                f"max_check_seconds: {max_check_seconds}",
                f"auto_confirmed: {auto_confirmed}",
                f"skipped_games_in_check: {skipped_games}",
                f"not_finished: {sorted(list(still))}",
            ]
            (out_dir / "store_install_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

            if still:
                raise RuntimeError(f"部分应用未确认安装完成: {sorted(list(still))}")

            _job_set_progress(job, 100, "安装流程完成")
            return

        if action == "app_install_apk":
            apk_path = str(params.get("apk_path", "")).strip()
            if not apk_path:
                raise RuntimeError("缺少 apk_path")
            apk_file = Path(apk_path)
            if not apk_file.exists() or not apk_file.is_file() or apk_file.suffix.lower() != ".apk":
                raise RuntimeError("apk_path 无效，必须是存在的 .apk 文件")
            _run_cmd(job, _adb_command(device_id, ["install", "-r", str(apk_file)]), timeout=600)
            package_name = str(params.get("package", "")).strip()
            launch = bool(params.get("launch", False))
            if launch and package_name:
                _validate_package(package_name)
                _run_cmd(
                    job,
                    _adb_command(device_id, ["shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"]),
                    timeout=120,
                )
            return

        if action == "compile_apps":
            packages = params.get("packages")
            if (not isinstance(packages, list) or not packages) and isinstance(params.get("preset_name"), str):
                preset_name = params.get("preset_name")
                preset = _load_app_config().get(preset_name, [])
                if isinstance(preset, list):
                    packages = preset
            if not isinstance(packages, list) or not packages:
                raise RuntimeError("packages 必须是非空数组，或提供 preset_name")

            normalized_packages = []
            for raw_pkg in packages:
                pkg = str(raw_pkg).strip()
                _validate_package(pkg)
                normalized_packages.append(pkg)
            if not normalized_packages:
                raise RuntimeError("未找到可编译包名")

            mode = str(params.get("mode", "speed-profile")).strip() or "speed-profile"
            mode = validate_compile_mode(mode)
            total = len(normalized_packages)
            with utilities_jobs_lock:
                job["compile_items"] = [{"package": pkg, "result": "待编译"} for pkg in normalized_packages]
                job["compile_summary"] = {
                    "total": total,
                    "completed": 0,
                    "current": "",
                    "current_index": 0,
                    "status": "running",
                }

            for idx, pkg in enumerate(normalized_packages):
                with utilities_jobs_lock:
                    summary = job.setdefault("compile_summary", {})
                    summary["current"] = pkg
                    summary["current_index"] = idx + 1
                    summary["status"] = "running"
                    items = job.setdefault("compile_items", [])
                    if idx < len(items):
                        items[idx]["result"] = "编译中"
                _job_set_progress(job, 5 + int((idx * 90) / max(1, total)), f"编译中 {idx + 1}/{total}: {pkg}")
                try:
                    _run_cmd(
                        job,
                        _adb_command(device_id, ["shell", "cmd", "package", "compile", "-m", mode, "-f", pkg]),
                        timeout=300,
                    )
                except Exception:
                    with utilities_jobs_lock:
                        items = job.setdefault("compile_items", [])
                        if idx < len(items):
                            items[idx]["result"] = "已取消" if job.get("cancel_requested") else "失败"
                        summary = job.setdefault("compile_summary", {})
                        summary["completed"] = idx
                        summary["status"] = "cancelled" if job.get("cancel_requested") else "error"
                    raise

                with utilities_jobs_lock:
                    items = job.setdefault("compile_items", [])
                    if idx < len(items):
                        items[idx]["result"] = "成功"
                    summary = job.setdefault("compile_summary", {})
                    summary["completed"] = idx + 1
                    summary["status"] = "running"
                _job_set_progress(job, 5 + int(((idx + 1) * 90) / max(1, total)), f"已完成 {idx + 1}/{total}: {pkg}")

            with utilities_jobs_lock:
                summary = job.setdefault("compile_summary", {})
                summary["current"] = ""
                summary["current_index"] = 0
                summary["status"] = "completed"
            _job_set_progress(job, 100, f"编译完成 {total}/{total}")
            return

        if action == "prepare_apps":
            packages = params.get("packages")
            if (not isinstance(packages, list) or not packages) and isinstance(params.get("preset_name"), str):
                preset_name = params.get("preset_name")
                preset = _load_app_config().get(preset_name, [])
                if isinstance(preset, list):
                    packages = preset
            if not isinstance(packages, list) or not packages:
                raise RuntimeError("packages 必须是非空数组，或提供 preset_name")
            for pkg in packages:
                pkg = str(pkg).strip()
                _validate_package(pkg)
                _run_cmd(
                    job,
                    _adb_command(device_id, ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]),
                    timeout=120,
                )
            return

        if action == "app_died_monitor":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            interval_sec = params.get("interval_sec", 1)
            _validate_positive_int(int(interval_sec), "interval_sec", 1, 30)
            interval_sec = int(interval_sec)

            with utilities_jobs_lock:
                summary = job.setdefault("monitor_summary", {})
                summary["package"] = package_name
                summary["interval_sec"] = interval_sec
                summary["checks"] = 0
                summary["alive_checks"] = 0
                summary["dead_checks"] = 0
                summary["first_alive_time"] = ""
                summary["first_kill_time"] = ""
                summary["last_state"] = "init"
                summary["last_note"] = "监控已启动"

            _job_set_progress(job, 5, f"开始监控 {package_name}（每 {interval_sec}s）")
            _job_add_monitor_detail(job, False, "monitor_started", "开始监控，等待应用启动")

            first_alive_seen = False
            first_kill_captured = False
            last_alive = None
            while True:
                _check_cancel(job)
                _wait_if_paused(job)

                if not _is_device_online(device_id, owner_ip=job.get("ip")):
                    _job_add_monitor_detail(job, False, "device_offline", "设备离线，等待重连")
                    _job_set_progress(job, 8, "设备离线，等待重连")
                    _sleep_with_control(job, interval_sec)
                    continue
                try:
                    probe_out = _run_cmd(
                        job,
                        _adb_command(device_id, ["shell", "pidof", package_name]),
                        timeout=15,
                        log_stdout=False,
                        log_stderr=False,
                        allow_returncodes={0, 1},
                    )
                    alive_now = bool((probe_out or "").strip())
                except Exception as exc:
                    _job_add_monitor_detail(job, False, "probe_error", f"探测失败: {exc}")
                    _job_set_progress(job, 8, "探测失败，重试中")
                    _sleep_with_control(job, interval_sec)
                    continue

                if not first_alive_seen:
                    if alive_now:
                        first_alive_seen = True
                        with utilities_jobs_lock:
                            job.setdefault("monitor_summary", {})["first_alive_time"] = datetime.now().strftime("%Y%m%d_%H%M%S")
                        _job_add_monitor_detail(job, True, "first_alive", "检测到应用首次存活，开始等待首次查杀")
                        _job_set_progress(job, 45, "已检测到应用启动，等待首次查杀")
                    else:
                        _job_add_monitor_detail(job, False, "waiting_start", "应用未启动，继续等待")
                        _job_set_progress(job, 20, "等待应用首次启动")
                    last_alive = alive_now
                    _sleep_with_control(job, interval_sec)
                    continue

                if (not first_kill_captured) and last_alive is True and alive_now is False:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    _job_add_monitor_detail(job, False, "first_killed", "检测到首次查杀，开始抓取全局 dumpsys")
                    _job_set_progress(job, 80, "检测到首次查杀，抓取全局 dumpsys 中")

                    meminfo_out = _run_cmd(
                        job,
                        _adb_command(device_id, ["shell", "dumpsys", "meminfo"]),
                        timeout=180,
                    )
                    activity_out = _run_cmd(
                        job,
                        _adb_command(device_id, ["shell", "dumpsys", "activity"]),
                        timeout=180,
                    )

                    meminfo_file = out_dir / f"monitor_meminfo_global_{timestamp}.txt"
                    activity_file = out_dir / f"monitor_activity_{package_name}_{timestamp}.txt"
                    meminfo_file.write_text(meminfo_out, encoding="utf-8")
                    activity_file.write_text(activity_out, encoding="utf-8")

                    with utilities_jobs_lock:
                        summary = job.setdefault("monitor_summary", {})
                        summary["first_kill_time"] = datetime.now().strftime("%Y%m%d_%H%M%S")
                        summary["capture_files"] = [meminfo_file.name, activity_file.name]
                        job["paused"] = True
                        job["status"] = "paused"
                        job["message"] = "首次查杀抓取完成，已自动暂停"
                    _job_add_monitor_detail(
                        job,
                        False,
                        "auto_paused_after_capture",
                        f"抓取完成并自动暂停: {meminfo_file.name}, {activity_file.name}",
                    )
                    _job_set_progress(job, 90, "首次查杀抓取完成，任务已自动暂停")
                    first_kill_captured = True
                    last_alive = alive_now
                    continue

                if alive_now:
                    _job_add_monitor_detail(job, True, "alive", "监控中")
                    _job_set_progress(job, 60, "应用存活，持续监控中")
                else:
                    _job_add_monitor_detail(job, False, "dead_waiting_restart", "应用当前未存活，等待再次启动后继续监控")
                    _job_set_progress(job, 55, "应用当前未存活，等待再次启动")

                last_alive = alive_now
                _sleep_with_control(job, interval_sec)

        if action == "monkey_run":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            events = params.get("events", 200)
            throttle = params.get("throttle_ms", 300)
            seed = params.get("seed")
            _validate_positive_int(events, "events", 1, 2000000)
            _validate_positive_int(throttle, "throttle_ms", 0, 10000)

            cmd = [
                "shell",
                "monkey",
                "-p",
                package_name,
                "--throttle",
                str(throttle),
            ]
            if seed is not None:
                _validate_positive_int(seed, "seed", 1, 2147483647)
                cmd += ["-s", str(seed)]
            cmd += [str(events)]
            output = _run_cmd(job, _adb_command(device_id, cmd), timeout=max(300, events * max(throttle, 1) // 1000 + 120))
            (out_dir / "monkey_output.txt").write_text(output, encoding="utf-8")
            return

        if action == "simpleperf_record":
            package_name = str(params.get("package", "")).strip()
            _validate_package(package_name)
            duration = params.get("duration_s", 10)
            _validate_positive_int(duration, "duration_s", 1, 600)

            remote_data = f"/data/local/tmp/{job['id']}_simpleperf.data"
            local_data = out_dir / "simpleperf.data"
            local_report = out_dir / "simpleperf_report.txt"

            _run_cmd(
                job,
                _adb_command(
                    device_id,
                    [
                        "shell",
                        "simpleperf",
                        "record",
                        "--app",
                        package_name,
                        "--duration",
                        str(duration),
                        "-o",
                        remote_data,
                    ],
                ),
                timeout=duration + 180,
            )
            _run_cmd(job, _adb_command(device_id, ["pull", remote_data, str(local_data)]), timeout=180)
            report = _run_cmd(job, _adb_command(device_id, ["shell", "simpleperf", "report", "-i", remote_data]), timeout=180)
            local_report.write_text(report, encoding="utf-8")
            _run_cmd(job, _adb_command(device_id, ["shell", "rm", "-f", remote_data]), timeout=60)
            return

        if action == "cont_startup_stay":
            runner = None
            contract = None
            for base in ("collie_package.rd_selftest", "rd_selftest", "web_app.rd_selftest"):
                try:
                    runner = importlib.import_module(f"{base}.cont_startup_stay_runner")
                    contract = importlib.import_module(f"{base}.cont_startup_stay_contract")
                    break
                except Exception:
                    continue
            if runner is None or contract is None:
                raise RuntimeError("无法导入 cont_startup_stay 模块")

            ContStartupStayConfig = getattr(contract, "ContStartupStayConfig")
            CollectorsConfig = getattr(contract, "CollectorsConfig")
            BugreportPolicy = getattr(contract, "BugreportPolicy")
            AppListSelection = getattr(contract, "AppListSelection")
            OutputDirStrategy = getattr(contract, "OutputDirStrategy")

            collectors_raw = params.get("collectors")
            if collectors_raw is None:
                collectors_raw = {}
            if not isinstance(collectors_raw, dict):
                raise RuntimeError("collectors 必须是对象")

            bugreport_raw = params.get("bugreport")
            if bugreport_raw is None:
                bugreport_raw = {}
            if not isinstance(bugreport_raw, dict):
                raise RuntimeError("bugreport 必须是对象")

            app_list_raw = params.get("app_list")
            if app_list_raw is None:
                app_list_raw = {}
            if not isinstance(app_list_raw, dict):
                raise RuntimeError("app_list 必须是对象")

            output_dir_raw = params.get("output_dir_strategy")
            if output_dir_raw is None:
                output_dir_raw = {}
            if not isinstance(output_dir_raw, dict):
                raise RuntimeError("output_dir_strategy 必须是对象")

            mode = str(bugreport_raw.get("mode", "capture")).strip() or "capture"
            if mode not in {"capture", "skip"}:
                raise RuntimeError("bugreport.mode 只能为 capture/skip")

            cli_skip_window_sec = int(bugreport_raw.get("cli_skip_window_sec", 10))
            capture_timeout_sec = int(bugreport_raw.get("capture_timeout_sec", 1200))
            _validate_positive_int(cli_skip_window_sec, "bugreport.cli_skip_window_sec", 1, 600)
            _validate_positive_int(capture_timeout_sec, "bugreport.capture_timeout_sec", 30, 7200)

            preset_name = app_list_raw.get("preset_name")
            if preset_name is not None:
                preset_name = str(preset_name).strip() or None

            custom_json = app_list_raw.get("custom_json")
            if custom_json is not None and not isinstance(custom_json, (dict, list, str)):
                raise RuntimeError("app_list.custom_json 仅支持对象/数组/字符串")

            dir_prefix = str(output_dir_raw.get("dir_prefix", "log_")).strip() or "log_"
            timestamp_format = str(output_dir_raw.get("timestamp_format", "%d_%H_%M")).strip() or "%d_%H_%M"

            config = ContStartupStayConfig(
                device_id=device_id,
                output_dir_strategy=OutputDirStrategy(
                    dir_prefix=dir_prefix,
                    timestamp_format=timestamp_format,
                ),
                app_list=AppListSelection(
                    preset_name=preset_name,
                    custom_json=custom_json,
                ),
                collectors=CollectorsConfig(
                    logcat=_coerce_bool(collectors_raw.get("logcat"), "collectors.logcat", default=True),
                    memcat=_coerce_bool(collectors_raw.get("memcat"), "collectors.memcat", default=False),
                    meminfo=_coerce_bool(collectors_raw.get("meminfo"), "collectors.meminfo", default=True),
                    vmstat=_coerce_bool(collectors_raw.get("vmstat"), "collectors.vmstat", default=True),
                    greclaim_parm=_coerce_bool(
                        collectors_raw.get("greclaim_parm"),
                        "collectors.greclaim_parm",
                        default=False,
                    ),
                    process_use_count=_coerce_bool(
                        collectors_raw.get("process_use_count"),
                        "collectors.process_use_count",
                        default=False,
                    ),
                    oomadj=_coerce_bool(collectors_raw.get("oomadj"), "collectors.oomadj", default=False),
                    ftrace=_coerce_bool(collectors_raw.get("ftrace"), "collectors.ftrace", default=False),
                    ftrace_include_sched_switch=_coerce_bool(
                        collectors_raw.get("ftrace_include_sched_switch"),
                        "collectors.ftrace_include_sched_switch",
                        default=False,
                    ),
                ),
                run_pre_start=_coerce_bool(params.get("run_pre_start"), "run_pre_start", default=False),
                bugreport=BugreportPolicy(
                    mode=mode,
                    cli_skip_window_sec=cli_skip_window_sec,
                    capture_timeout_sec=capture_timeout_sec,
                ),
            )

            class _ControlledAdbExecutor:
                def __init__(self, base_exec):
                    self._base = base_exec

                def build_argv(self, device_id, args):
                    return self._base.build_argv(device_id=device_id, args=args)

                def build_host_argv(self, args):
                    return self._base.build_host_argv(args=args)

                def run(self, device_id, args, timeout_sec=20.0):
                    _wait_if_paused(job)
                    _check_cancel(job)
                    return self._base.run(device_id=device_id, args=args, timeout_sec=timeout_sec)

                def run_host(self, args, timeout_sec=20.0):
                    _wait_if_paused(job)
                    _check_cancel(job)
                    return self._base.run_host(args=args, timeout_sec=timeout_sec)

            def _mark_manifest_cancelled():
                manifest_path = out_dir / "artifacts_manifest.json"
                if not manifest_path.exists():
                    return
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore") or "{}")
                except Exception:
                    return
                if not isinstance(data, dict):
                    return
                data["status"] = "completed"
                data["result"] = "cancelled"
                data["error"] = "cancelled"
                data["traceback"] = None
                manifest_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            def _zip_artifacts(zip_name: str = "cont_startup_stay_artifacts.zip"):
                zip_path = out_dir / zip_name
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(out_dir):
                        for fname in files:
                            path = Path(root) / fname
                            if path.resolve() == zip_path.resolve():
                                continue
                            rel = os.path.relpath(str(path), str(out_dir))
                            zf.write(str(path), arcname=rel)
                return zip_path

            _job_set_progress(job, 1, "准备 cont_startup_stay 配置")
            _wait_if_paused(job)
            _check_cancel(job)

            _job_set_progress(job, 5, "执行 cont_startup_stay")
            controlled_exec = _ControlledAdbExecutor(adb_exec)
            try:
                out = runner.run_cont_startup_stay(job_dir=out_dir, config=config, adb_exec=controlled_exec)
                _append_log(job["stdout_path"], f"\n[cont_startup_stay] {json.dumps(out, ensure_ascii=False)}\n")
            except Exception:
                if job.get("cancel_requested"):
                    _mark_manifest_cancelled()
                raise
            finally:
                try:
                    _job_set_progress(job, 90, "打包 cont_startup_stay 产物")
                    _ = _zip_artifacts()
                except Exception as exc:  # noqa: BLE001
                    _append_log(job["stderr_path"], f"\n[zip_error] {exc}\n")

            _job_set_progress(job, 100, "cont_startup_stay 完成")
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
                rows_count = 0
                if has_result:
                    try:
                        text = result_file.read_text(encoding="utf-8", errors="ignore")
                        rows_count = len(_parse_app_versions_content(text))
                    except Exception:
                        rows_count = 0

                items_by_id[job_id] = {
                    "job_id": job_id,
                    "status": job.get("status", "unknown"),
                    "created_at": job.get("created_at") or datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S"),
                    "updated_at": datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S"),
                    "result_file": "app_versions.txt" if has_result else None,
                    "rows_count": rows_count,
                    "has_result": has_result,
                    "source": "memory",
                    "files": _list_job_files(job_dir),
                    "_updated_ts": updated_ts,
                }

        for item in utilities_root.iterdir():
            if not item.is_dir():
                continue
            job_id = item.name
            result_file = item / "app_versions.txt"
            if not result_file.exists():
                continue
            updated_ts = result_file.stat().st_mtime
            rows_count = 0
            try:
                text = result_file.read_text(encoding="utf-8", errors="ignore")
                rows_count = len(_parse_app_versions_content(text))
            except Exception:
                rows_count = 0

            if job_id in items_by_id:
                current = items_by_id[job_id]
                current["has_result"] = True
                current["result_file"] = "app_versions.txt"
                current["rows_count"] = max(current.get("rows_count", 0), rows_count)
                current["files"] = _list_job_files(item)
                if updated_ts > current["_updated_ts"]:
                    current["_updated_ts"] = updated_ts
                    current["updated_at"] = datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S")
                continue

            items_by_id[job_id] = {
                "job_id": job_id,
                "status": "completed",
                "created_at": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y%m%d_%H%M%S"),
                "updated_at": datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S"),
                "result_file": "app_versions.txt",
                "rows_count": rows_count,
                "has_result": True,
                "source": "disk",
                "files": _list_job_files(item),
                "_updated_ts": updated_ts,
            }

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
        parsed = _parse_app_versions_content(content)
        rows = [{"package_name": pkg, "version_name": ver} for pkg, ver in sorted(parsed.items())]
        table_rows = [(row["package_name"], row["version_name"]) for row in rows]
        markdown = "\n".join(
            [
                "# 版本检查结果",
                "",
                f"- 任务ID: {job_id}",
                f"- 更新时间: {datetime.fromtimestamp(result_file.stat().st_mtime).strftime('%Y%m%d_%H%M%S')}",
                f"- 条目数: {len(rows)}",
                "",
                _render_markdown_table(("包名", "版本号"), table_rows),
                "",
            ]
        )
        return jsonify(
            {
                "job_id": job_id,
                "file": "app_versions.txt",
                "updated_at": datetime.fromtimestamp(result_file.stat().st_mtime).strftime("%Y%m%d_%H%M%S"),
                "rows_count": len(rows),
                "rows": rows,
                "content": content,
                "markdown": markdown,
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

        data_1 = _parse_app_versions_content(file_1.read_text(encoding="utf-8", errors="ignore"))
        data_2 = _parse_app_versions_content(file_2.read_text(encoding="utf-8", errors="ignore"))
        if not data_1 and not data_2:
            return jsonify({"error": "两份结果都无法解析版本信息"}), 400

        all_packages = sorted(set(data_1.keys()) | set(data_2.keys()))
        rows = []
        same_count = 0
        changed_count = 0
        only_1_count = 0
        only_2_count = 0
        diff_rows = []

        for pkg in all_packages:
            version_1 = data_1.get(pkg)
            version_2 = data_2.get(pkg)
            if version_1 is None:
                diff_type = "仅结果B存在"
                only_2_count += 1
            elif version_2 is None:
                diff_type = "仅结果A存在"
                only_1_count += 1
            elif version_1 == version_2:
                diff_type = "一致"
                same_count += 1
            else:
                diff_type = "版本变化"
                changed_count += 1

            row = {
                "package_name": pkg,
                "version_a": version_1 or "-",
                "version_b": version_2 or "-",
                "diff_type": diff_type,
            }
            rows.append(row)
            if diff_type != "一致":
                diff_rows.append((row["package_name"], row["version_a"], row["version_b"], row["diff_type"]))

        if diff_rows:
            comparison_text = _render_ascii_table(
                ("package_name", "version_a", "version_b", "diff_type"),
                diff_rows,
            )
        else:
            comparison_text = "两份结果版本完全一致。"

        markdown_rows = [
            (row["package_name"], row["version_a"], row["version_b"], row["diff_type"])
            for row in rows
        ]
        markdown = "\n".join(
            [
                "# 应用版本差异对比",
                "",
                f"- 结果A: {job_id_1}",
                f"- 结果B: {job_id_2}",
                f"- 总包数: {len(all_packages)}",
                f"- 一致: {same_count}",
                f"- 版本变化: {changed_count}",
                f"- 仅A存在: {only_1_count}",
                f"- 仅B存在: {only_2_count}",
                "",
                _render_markdown_table(("包名", "结果A版本", "结果B版本", "差异类型"), markdown_rows),
                "",
            ]
        )

        return jsonify(
            {
                "status": "success",
                "job_id_1": job_id_1,
                "job_id_2": job_id_2,
                "summary": {
                    "total_packages": len(all_packages),
                    "same_count": same_count,
                    "changed_count": changed_count,
                    "only_a_count": only_1_count,
                    "only_b_count": only_2_count,
                },
                "rows": rows,
                "comparison_text": comparison_text,
                "markdown": markdown,
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

    def _parse_wm_size(output):
        candidates = []
        m = re.search(r"Override size:\s*(\d+)x(\d+)", output)
        if m:
            candidates.append((int(m.group(1)), int(m.group(2))))
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if m:
            ph = (int(m.group(1)), int(m.group(2)))
            if ph not in candidates:
                candidates.append(ph)
        m = re.search(r"(\d+)x(\d+)", output)
        if m:
            any_size = (int(m.group(1)), int(m.group(2)))
            if any_size not in candidates:
                candidates.append(any_size)
        return candidates

    def _ratio_point(ratio, size):
        if size:
            return int(size[0] * ratio[0]), int(size[1] * ratio[1])
        return int(1080 * ratio[0]), int(2340 * ratio[1])

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
