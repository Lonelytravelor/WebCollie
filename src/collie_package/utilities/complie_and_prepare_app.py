import subprocess
import time

from .. import tools
from ..automation import cont_startup_stay

def compile_apps():
    package_app_list = tools.load_config_status()
    if package_app_list == -1 :
        return
    
    for package in package_app_list:
        try:
            cmd = [
                    'adb',
                    'shell',
                    'pm',
                    'compile',
                    '-r',
                    'bg-dexopt',
                    package  # 包名作为单独参数
            ]
            print(f"正在执行命令：{' '.join(cmd)}")
            result = subprocess.run(cmd, 
                                    capture_output=True,  # 捕获 stdout 和 stderr
                                    text=True,            # 将输出解码为字符串
                                    timeout=60              # 设置超时时间
                )
            if result.returncode != 0:
                print(f"编译失败：{result.stderr}")
            else:
                print(f"编译成功：{result.stdout}")
        except subprocess.TimeoutExpired:
            print(f"⏰ {package} 编译超时")
        except FileNotFoundError:
            print("🔍 找不到 adb 命令，请检查环境变量")
        except Exception as e:
            print(f"⚠️ {package} 编译异常: {str(e)}")

def app_prepare():
    package_app_list = tools.load_config_status()
    if package_app_list == -1:
        return
    
    for idx, package in enumerate(package_app_list, 1):
        if cont_startup_stay.launch_app(package,app_wait=9):
            pid = None
            retry = 3
            while retry > 0 and not pid:
                pid = cont_startup_stay.get_pid(package)
                retry -= 1
                time.sleep(3)
            
            status = "成功" if pid else "失败"
            print(f"应用 {idx}/{len(package_app_list)}: {package.ljust(25)} PID: {str(pid).ljust(8)} {status}")
