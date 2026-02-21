import os
import subprocess
import re
from datetime import datetime

from .. import state, tools

# 执行adb shell命令并获取输出结果
def execute_adb_shell_command(command, timeout: int = 20) -> str:
    if isinstance(command, str):
        cmd = command.strip().split()
    else:
        cmd = list(command)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""

# 从给定的文本中提取版本号（通过匹配versionName后的版本字符串）
def extract_version_name(text):
    match = re.search(r'versionName=(\S+)', text)
    if match:
        return match.group(1)
    return "未获取到版本号"

def check_app_version():

    app_list = tools.load_config_status()
    
    if app_list == -1:
        return

    output_file = os.path.join(state.FILE_DIR, f"app_versions.txt")

    # 创建用于保存版本信息的文本文件
    with open(output_file, 'w', encoding='utf-8') as file:
        # 循环获取各应用版本信息
        print("\n======================================")
        for app_package in app_list:

            command = ["adb", "shell", "dumpsys", "package", app_package]
            output = execute_adb_shell_command(command)
            version_name = extract_version_name(output)

            info_line = f"{app_package} versin：{version_name}\n"
            print(info_line.strip())  # 在控制台打印信息
            file.write(info_line)  # 将信息写入文本文件
        print("======================================")
