from datetime import datetime
from importlib import resources
from typing import List, Optional

import json
import logging
import os
import subprocess
import time

from . import state
from .config_loader import load_app_list_config, to_flat_app_config


def load_custom_config(file_path: str) -> list:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"加载自定义配置文件出错: {exc}")
        return None


def load_default_config() -> dict:
    yaml_cfg = load_app_list_config()
    if isinstance(yaml_cfg, dict) and yaml_cfg:
        return to_flat_app_config(yaml_cfg)
    try:
        with resources.open_text("collie_package", "app_config.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("警告: 默认配置文件不存在")
        return {}
    except Exception as exc:
        print(f"加载配置文件出错: {exc}")
        return {}


def _normalize_config(value, return_raw: bool):
    if return_raw:
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        merged = []
        for v in value.values():
            if isinstance(v, list):
                merged.extend(v)
        if merged:
            print("提示：选择的配置为场景字典，已合并其中所有列表项作为包列表。")
            seen = set()
            deduped = []
            for pkg in merged:
                if pkg not in seen:
                    seen.add(pkg)
                    deduped.append(pkg)
            return deduped
    print("警告：未能从配置中解析出包列表，请检查配置内容。")
    return []


def load_config_status(
    return_raw: bool = False,
    include_keys: Optional[List[str]] = None,
    exclude_keys: Optional[List[str]] = None,
):
    app_module_dict = load_default_config()

    def _key_allowed(k: str) -> bool:
        upper_k = k.upper()
        if include_keys is not None:
            if upper_k not in {ik.upper() for ik in include_keys}:
                return False
        if exclude_keys is not None and upper_k in {ek.upper() for ek in exclude_keys}:
            return False
        return True

    if isinstance(app_module_dict, dict) and 'app_presets' in app_module_dict:
        app_module_dict = app_module_dict.get('app_presets') or {}

    filtered_dict = {k: v for k, v in app_module_dict.items() if _key_allowed(k)}
    if not filtered_dict:
        print("警告：过滤后没有可用配置，将回退为全部配置。")
        filtered_dict = app_module_dict

    print("可用的配置文件选项:")
    print("\n0. 加载自定义JSON文件")
    for i, key in enumerate(filtered_dict.keys(), 1):
        print(f"{i}. {key}")

    while True:
        user_input = input("\n请选择要加载的配置(编号/名称)或输入文件路径: ").strip()

        if user_input == "q" or user_input == "exit":
            return -1

        if user_input == "0" or os.path.exists(user_input):
            file_path = user_input if user_input != "0" else input("请输入JSON文件路径: ").strip()
            config_list = load_custom_config(file_path)
            if config_list is not None:
                return config_list
            continue

        if user_input.isdigit():
            choice_idx = int(user_input) - 1
            keys = list(filtered_dict.keys())
            if 0 <= choice_idx < len(keys):
                selected_key = keys[choice_idx]
                selected_val = filtered_dict[selected_key]
                return _normalize_config(selected_val, return_raw)

        normalized_input = user_input.upper()
        for key in filtered_dict:
            if normalized_input == key.upper():
                selected_val = filtered_dict[key]
                return _normalize_config(selected_val, return_raw)

        print(f"错误: 无效选择或文件不存在 - '{user_input}'")
        print("请选择以下有效选项:")
        print("0. 加载自定义JSON文件")
        for i, key in enumerate(app_module_dict.keys(), 1):
            print(f"{i}. {key}")


def get_time_and_mkdir(out_dir=""):
    if out_dir == "":
        now = datetime.now()
        timestamp = now.strftime("%d_%H_%M")
        out_dir = f"log_{timestamp}"
    os.makedirs(out_dir, exist_ok=True)
    state.FILE_DIR = out_dir


def capture_bugreport(device_id=None, output_dir="bugreports", timeout=600):
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"bugreport_{timestamp}.zip")

    adb_cmd = ["adb"]
    if device_id:
        adb_cmd.extend(["-s", device_id])
    adb_cmd.extend(["bugreport", output_file])

    logging.info(f"开始捕获 bugreport: {' '.join(adb_cmd)}")

    try:
        start_time = time.time()
        process = subprocess.Popen(
            adb_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        while True:
            if process.stdout is None:
                break

            line = process.stdout.readline()
            if not line:
                break

            if "Generating" in line or "Dumping" in line:
                logging.info(line.strip())

            elapsed = time.time() - start_time
            if elapsed > timeout:
                logging.error(f"超时: 捕获时间超过 {timeout} 秒")
                process.terminate()
                return None

        process.wait(timeout=max(0, timeout - elapsed))

        if process.returncode != 0:
            error = process.stderr.read() if process.stderr else "未知错误"
            logging.error(f"捕获失败 (代码 {process.returncode}): {error}")
            return None

        if os.path.exists(output_file) and os.path.getsize(output_file) > 1024:
            logging.info(
                f"成功捕获 bugreport: {output_file} ({os.path.getsize(output_file)//1024} KB)"
            )
            return output_file
        logging.error("生成的 bugreport 文件无效或为空")
        return None
    except Exception as exc:
        logging.exception(f"捕获过程中发生异常: {str(exc)}")
        return None


def get_log_setting(tag):
    default_setting = 1

    while True:
        user_input = input(f"是否要记录{tag}? 输入1记录, 0不记录 (默认记录): ").strip()

        if user_input == "":
            return default_setting

        if user_input in ("0", "1"):
            return int(user_input)

        print("输入无效！请重新输入 (0 或 1)")


def is_process_alive(pid):
    if not pid:
        return False

    try:
        result = subprocess.run(
            ["adb", "shell", f"ps -p {pid}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def oomadj_log_enable(device_id: Optional[str] = None, delay_sec: int = 0):
    if delay_sec > 0:
        time.sleep(delay_sec)

    base_cmd = ["adb"]
    if device_id:
        base_cmd.extend(["-s", device_id])

    cmds = [
        base_cmd + ["shell", "am", "logging", "enable-text", "DEBUG_OOM_ADJ"],
        base_cmd + ["shell", "am", "logging", "enable-text", "DEBUG_OOM_ADJ_REASON"],
    ]
    for cmd in cmds:
        subprocess.run(cmd)


def run_pre_start_commands(commands, device_id=None, timeout=20):
    if not commands:
        return

    for raw_cmd in commands:
        if not raw_cmd:
            continue

        cmd = raw_cmd
        if device_id and raw_cmd.strip().startswith("adb ") and " -s " not in raw_cmd:
            parts = raw_cmd.split()
            if "-s" not in parts:
                parts.insert(1, "-s")
                parts.insert(2, device_id)
                cmd = " ".join(parts)

        print(f"执行预准备命令: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                print(f"⚠️ 命令失败({result.returncode}): {cmd}\nstderr: {result.stderr.strip()}")
            elif result.stdout:
                print(result.stdout.strip())
        except subprocess.TimeoutExpired:
            print(f"⚠️ 命令超时: {cmd}")
        except Exception as exc:
            print(f"⚠️ 命令异常: {cmd} -> {exc}")
