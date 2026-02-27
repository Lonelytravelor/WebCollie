#!/usr/bin/env python3
"""
客户端 ADB 代理 Agent

用途：
1) 在客户端本机读取 `adb devices -l`
2) 定时上报到 Web 服务端 `/api/adb/agent/register`
3) 提供 `/adb/run` 供服务端转发执行 `adb -s <serial> <args...>`
"""

import argparse
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


def _run_local_adb(argv: List[str], timeout_sec: float) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_sec)),
        )
        return {
            "argv": argv,
            "returncode": int(proc.returncode),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\n[timeout]",
        }
    except Exception as exc:
        return {
            "argv": argv,
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
        }


def _list_local_devices() -> List[Dict[str, Any]]:
    result = _run_local_adb(["adb", "devices", "-l"], timeout_sec=20)
    if int(result.get("returncode", 1)) != 0:
        return []

    devices = []
    lines = str(result.get("stdout", "")).splitlines()
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        item = {
            "id": serial,
            "state": state,
            "raw": line,
        }
        for token in parts[2:]:
            if token.startswith("model:"):
                item["model"] = token.split(":", 1)[1]
            if token.startswith("transport_id:"):
                item["transport_id"] = token.split(":", 1)[1]
        devices.append(item)
    return devices


def _guess_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0] or "127.0.0.1")
    except Exception:
        return "127.0.0.1"


def _expand_path(raw_path: str) -> str:
    if not raw_path:
        raise ValueError("path 不能为空")
    expanded = os.path.expanduser(raw_path)
    return os.path.abspath(expanded)


def _ensure_dir(raw_path: str) -> str:
    path = _expand_path(raw_path)
    os.makedirs(path, exist_ok=True)
    if not os.path.isdir(path):
        raise ValueError("path 不是目录")
    return path


def _open_dir(raw_path: str) -> None:
    path = _expand_path(raw_path)
    if not os.path.isdir(path):
        raise ValueError("path 不是目录")
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", path], check=False)
        return
    if sys.platform.startswith("win"):
        subprocess.run(["explorer", path], check=False)
        return
    subprocess.run(["xdg-open", path], check=False)


class _State:
    def __init__(self, args: argparse.Namespace):
        self.server_url = str(args.server_url).rstrip("/")
        self.listen_host = str(args.listen_host)
        self.listen_port = int(args.listen_port)
        self.agent_id = str(args.agent_id)
        self.agent_name = str(args.agent_name or args.agent_id)
        self.token = str(args.token or "")
        self.heartbeat_sec = max(5, int(args.heartbeat_sec))
        self.ttl_sec = max(10, int(args.ttl_sec))
        if args.public_base_url:
            self.base_url = str(args.public_base_url).rstrip("/")
        else:
            ip = _guess_local_ip()
            self.base_url = f"http://{ip}:{self.listen_port}"

        self.stop_event = threading.Event()

    def heartbeat_payload(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "base_url": self.base_url,
            "devices": _list_local_devices(),
            "ttl_sec": self.ttl_sec,
            "token": self.token,
        }


def _post_json(url: str, payload: Dict[str, Any], timeout_sec: float = 10.0) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    return json.loads(text) if text else {}


def _heartbeat_loop(state: _State) -> None:
    url = f"{state.server_url}/api/adb/agent/register"
    while not state.stop_event.is_set():
        try:
            payload = state.heartbeat_payload()
            _ = _post_json(url, payload, timeout_sec=10.0)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            print(f"[heartbeat] HTTP {exc.code}: {detail[:200]}")
        except Exception as exc:
            print(f"[heartbeat] 失败: {exc}")
        state.stop_event.wait(state.heartbeat_sec)


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> Dict[str, Any]:
            try:
                raw_len = self.headers.get("Content-Length", "0")
                n = int(raw_len)
            except Exception:
                n = 0
            if n <= 0:
                return {}
            body = self.rfile.read(n).decode("utf-8", errors="ignore")
            try:
                data = json.loads(body)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "agent_id": state.agent_id,
                        "agent_name": state.agent_name,
                        "base_url": state.base_url,
                    },
                )
                return
            if self.path == "/adb/devices":
                self._json(200, {"devices": _list_local_devices()})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/host/ensure-dir":
                data = self._read_json()
                raw_path = str(data.get("path", "")).strip()
                try:
                    normalized = _ensure_dir(raw_path)
                except Exception as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"path": normalized})
                return

            if self.path == "/host/open-dir":
                data = self._read_json()
                raw_path = str(data.get("path", "")).strip()
                try:
                    _open_dir(raw_path)
                except Exception as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"ok": True})
                return

            if self.path != "/adb/run":
                self._json(404, {"error": "not found"})
                return

            data = self._read_json()
            serial = str(data.get("device_id", "")).strip()
            args = data.get("args")
            timeout_sec = data.get("timeout_sec", 20)
            try:
                timeout_sec = int(timeout_sec)
            except Exception:
                timeout_sec = 20
            timeout_sec = max(1, min(7200, timeout_sec))

            if not serial:
                self._json(400, {"error": "device_id 不能为空"})
                return
            if not isinstance(args, list) or not args:
                self._json(400, {"error": "args 必须是非空数组"})
                return

            argv = ["adb", "-s", serial, *[str(x) for x in args]]
            result = _run_local_adb(argv, timeout_sec=timeout_sec)
            self._json(200, result)

        def log_message(self, format, *args):  # noqa: A003
            return

    return Handler


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collie 客户端 ADB 代理 Agent")
    p.add_argument("--server-url", required=True, help="Web 服务地址，例如 http://192.168.1.10:5000")
    p.add_argument("--listen-host", default="0.0.0.0", help="本地监听地址，默认 0.0.0.0")
    p.add_argument("--listen-port", type=int, default=18765, help="本地监听端口，默认 18765")
    p.add_argument("--public-base-url", default="", help="服务端可访问的 Agent 地址（可选）")
    p.add_argument("--agent-id", default=socket.gethostname(), help="Agent 唯一ID，默认主机名")
    p.add_argument("--agent-name", default="", help="Agent 展示名称（可选）")
    p.add_argument("--token", default="", help="与服务端 ADB_AGENT_TOKEN 对应（可选）")
    p.add_argument("--heartbeat-sec", type=int, default=8, help="心跳周期秒，默认8")
    p.add_argument("--ttl-sec", type=int, default=25, help="服务端超时秒，默认25")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    state = _State(args)

    hb = threading.Thread(target=_heartbeat_loop, args=(state,), daemon=True)
    hb.start()

    server = ThreadingHTTPServer((state.listen_host, state.listen_port), _make_handler(state))
    print("=" * 68)
    print("Collie ADB Agent 已启动")
    print(f"Agent ID: {state.agent_id}")
    print(f"监听地址: http://{state.listen_host}:{state.listen_port}")
    print(f"上报地址: {state.base_url}")
    print(f"服务端: {state.server_url}")
    print("=" * 68)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
