import os
import re
import subprocess
import time
from datetime import datetime

from memcat import MemcatTask

from .. import log_class, state, tools
from ..memory_models import dump_mem
from . import app_died_moniter

def get_running_processes():
    """获取当前所有运行中的进程包名"""
    try:
        # 使用ps命令获取所有进程
        result = subprocess.check_output(
            ['adb','shell','ps', '-A'],
            text=True,
            stderr=subprocess.DEVNULL
        )
        
        # 提取包名
        processes = set()
        for line in result.splitlines():
            # 跳过标题行
            if "USER" in line and "PID" in line:
                continue
            
            # 提取最后一列（进程名）
            parts = line.split()
            if parts:
                process_name = parts[-1]
                
                # 匹配包名格式 (com.xxx.xxx)
                if re.match(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$', process_name):
                    processes.add(process_name)
        
        return processes
    except Exception as e:
        print(f"获取进程信息失败: {e}")
        return set()

import os
import time
import subprocess

def get_oom_scores(packages):
    """获取指定包名的 OOM 分数"""
    if not packages:
        return {}
    
    # 获取所有进程信息
    try:
        ps_output = subprocess.check_output(["adb", "shell", "ps", "-A", "-o", "PID,NAME"]).decode().strip()
    except subprocess.CalledProcessError:
        return {}
    
    # 创建包名到 PID 列表的映射
    pkg_to_pids = {}
    for line in ps_output.split('\n')[1:]:  # 跳过标题行
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                pid, name = parts[0], ' '.join(parts[1:])
                # 检查进程名是否包含我们关心的包名
                for pkg in packages:
                    if pkg in name:
                        if pkg not in pkg_to_pids:
                            pkg_to_pids[pkg] = []
                        pkg_to_pids[pkg].append(pid)
    
    # 如果没有找到任何进程，直接返回
    if not pkg_to_pids:
        return {}
    
    # 构建一次性查询所有 PID 的 oom_score_adj 的命令
    oom_cmd = ["adb", "shell"]
    for pids in pkg_to_pids.values():
        for pid in pids:
            oom_cmd.extend(["echo", "-n", f"{pid}:"])
            oom_cmd.extend(["&&", "cat", f"/proc/{pid}/oom_score_adj"])
            oom_cmd.extend(["2>/dev/null", "||", "echo", "'N/A'", ";"])
    
    # 执行命令并获取输出
    try:
        oom_output = subprocess.check_output(oom_cmd, stderr=subprocess.DEVNULL).decode().strip()
    except subprocess.CalledProcessError:
        return {}
    
    # 解析 OOM 输出，创建 PID 到 OOM 值的映射
    pid_to_oom = {}
    for line in oom_output.split('\n'):
        if ':' in line:
            pid, oom_value = line.split(':', 1)
            pid_to_oom[pid] = oom_value.strip()
    
    # 创建包名到 OOM 值的映射
    pkg_to_oom = {}
    for pkg, pids in pkg_to_pids.items():
        oom_values = [pid_to_oom.get(pid, "N/A") for pid in pids]
        # 去重并排序 OOM 值
        unique_oom_values = sorted(set(oom_values))
        pkg_to_oom[pkg] = unique_oom_values
    
    return pkg_to_oom

def display_process_status(alive_packages, total_packages, oom_scores=None):
    """显示进程状态信息"""
    # 创建存活进程信息字符串
    alive_info = f"存活进程: {len(alive_packages)}/{total_packages}"
    print(alive_info + "\n")
    
    # 显示所有存活进程及其 OOM 值
    if alive_packages:
        print("当前存活进程列表及OOM_ADJ:")
        for pkg in sorted(alive_packages):
            oom_values = oom_scores.get(pkg, ["N/A"]) if oom_scores else ["N/A"]
            if len(oom_values) == 1:
                print(f"  • {pkg} (OOM_ADJ: {oom_values[0]})")
            else:
                print(f"  • {pkg} (OOM_ADJ: {', '.join(oom_values)})")
    else:
        print("没有存活的进程")
        print("可能原因:")
        print("1. 设备上没有运行这些应用")
        print("2. 需要root权限才能检测系统进程")
        print("3. 包名与实际进程名不匹配")

def monitor_processes(packages_list, log_path):
    """监控进程状态"""
    print(f"开始监控 {len(packages_list)} 个进程...")
    print("按 Ctrl+C 停止监控\n")
    
    # 记录前一次的状态用于检测变化
    prev_alive = set()
    
    # OOM 分数缓存和更新计数器
    oom_cache = {}
    oom_update_counter = 0
    OOM_UPDATE_INTERVAL = 5  # 每5秒更新一次OOM分数
    
    try:
        while True:
            # 获取当前所有运行中的进程
            running = get_running_processes()
            
            # 检查哪些包名对应的进程在运行
            alive_packages = [pkg for pkg in packages_list if pkg in running]
            alive_set = set(alive_packages)
            
            # 获取当前时间
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 检查状态变化
            new_alive = alive_set - prev_alive
            new_dead = prev_alive - alive_set
            
            # 更新前一次状态
            prev_alive = alive_set
            
            # 定期更新 OOM 分数
            oom_update_counter += 1
            if oom_update_counter >= OOM_UPDATE_INTERVAL:
                oom_cache = get_oom_scores(alive_packages)
                oom_update_counter = 0
            
            # 清屏并显示最新状态
            os.system('cls' if os.name == 'nt' else 'clear')
            print(f"=== 进程状态监控 [{current_time}] ===")
            
            # 写入日志（包含时间戳）
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{current_time}] 存活进程: {len(alive_packages)}/{len(packages_list)}\n")
            
            # 显示状态变化的进程
            if new_alive:
                print("新启动的进程:")
                for pkg in sorted(new_alive):
                    print(f"  ✓ {pkg}")
                print()
            
            if new_dead:
                print("新退出的进程:")
                for pkg in sorted(new_dead):
                    print(f"  ✗ {pkg}")
                print()
            
            # 显示所有存活进程及其 OOM 值
            display_process_status(alive_packages, len(packages_list), oom_cache)
            
            # 每秒更新一次
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n监控已停止")
        # 显示日志文件位置
        print(f"日志已保存至: {log_path}")

def pre_logcat():
    for _ in range(10):
        # 使用ps命令获取所有进程
        result = subprocess.check_output(
            ['adb','logcat','-c'],
        )
        time.sleep(0.1)

def monitor_log():
    now = datetime.now()
    timestamp = now.strftime("%d_%H_%M")  # 格式: 日期_小时_分钟

    package_list = tools.load_config_status()
    if package_list == -1:
        return

    should_logcat = tools.get_log_setting('logcat')
    should_memcat = tools.get_log_setting('memcat')
    should_meminfo = tools.get_log_setting('meminfo')
    should_oomadj = tools.get_log_setting('oomadj')
    should_monitor_died = tools.get_log_setting('monitor for specified app')
    if should_monitor_died :
        monitor_package = input("请输入 Android 包名 (默认: com.example.app): ").strip() or "com.example.app"
    
    pre_logcat()
    
    input("\n请确保手机处于初始状态,按 Enter 开始测试...")

    try:
        if should_logcat:                
            recorder = log_class.LogcatRecorder() 
            # 启动logcat记录
            recorder.start()
            # 等待logcat启动
            time.sleep(1)

        if should_memcat:
            output_file = os.path.join(state.FILE_DIR, f"memcat_{timestamp}")
            memcat_task = MemcatTask(sample_period=[1, 180], outfile=output_file)
            memcat_task.start_capture() # 开始抓取
            time.sleep(1)

        if should_meminfo:
            meminfo_output = dump_mem.get_meminfo()
            meminfo_file = os.path.join(state.FILE_DIR, f"meminfo{timestamp}.txt")
        with open(meminfo_file, 'a', encoding='utf-8') as f:
                f.write(f"测试前 - \n{'='*50}\n")
                f.write(meminfo_output + "\n")
        if should_monitor_died:
            monitor_for_died = app_died_moniter.ProcessMonitor(
                monitor_package,
                command=app_died_moniter.DEFAULT_TRIGGER_COMMANDS,
                output_dir=state.FILE_DIR,
                interval=1,
                duration=0,
            )
            monitor_for_died.start()
        if should_oomadj:
            oomadj_file = os.path.join(state.FILE_DIR, f"oomadj_{timestamp}.csv")
            monitor = log_class.OOMAdjLogger(package_list, oomadj_file)
            monitor.start()
        
        while True:
            time.sleep(1)
        
    except Exception as e:
        print(f"⚠️ 主程序出错: {str(e)}")
    finally:
        # 确保停止logcat记录
        if should_logcat:
            recorder.stop()
        if should_memcat:
            memcat_task.stop_capture()
        if should_meminfo:
            meminfo_output = dump_mem.get_meminfo()
            meminfo_file = os.path.join(state.FILE_DIR, f"meminfo{timestamp}.txt")
        with open(meminfo_file, 'a', encoding='utf-8') as f:
                f.write(f"\n测试后 - \n{'='*50}\n")
                f.write(meminfo_output + "\n")
        if should_monitor_died:
            monitor_for_died.stop()
        if should_oomadj:
            monitor.stop()
            # 使用示例
            # 分析之前生成的CSV文件
            oomadj_summary_file = os.path.join(state.FILE_DIR, f"oomadj_summary_report_{timestamp}.txt")
            oomadj_analysis_file = os.path.join(state.FILE_DIR, f"oomadj_analysis_plots_{timestamp}.png")
            log_class.analyze_oomadj_csv(oomadj_file, oomadj_summary_file, oomadj_analysis_file)

    bugreport_handler = log_class.BugReportHandler()
    bugreport_handler.handle_bugreport()

def run_check_app_alive():
    package_app_list = tools.load_config_status()
    if package_app_list == -1:
        return
    now = datetime.now()
    timestamp = now.strftime("%d_%H_%M")  # 格式: 日期_小时_分钟
    output_file = os.path.join(state.FILE_DIR, f"memcat_{timestamp}")
    monitor_processes(package_app_list, output_file)

if __name__ == "__main__":
    run_check_app_alive()
