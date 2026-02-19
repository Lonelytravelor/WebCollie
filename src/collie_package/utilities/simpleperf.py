#!/usr/bin/env python3
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from .. import state

class SimpleperfProfiler:
    def __init__(self):
        self.package_name = None
        self.report_script_path = None
        self.duration = 10
        self.output_dir = "./profiling_results"
        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        self.temp_dir = None
        self.simpleperf_path = None
        self.device_arch = None
        self.call_graph_option = "--call-graph fp"  # 默认值
        self._device_tmp_dir = "/data/local/tmp/"
        self._device_perf_data = "/sdcard/perf.data"
        self._device_perf_txt = "/sdcard/perf.txt"

    @property
    def device_simpleperf_path(self):
        """设备上 simpleperf 的路径"""
        return os.path.join(self._device_tmp_dir, "simpleperf")
    
    def print_header(self):
        """打印欢迎信息"""
        print("=" * 60)
        print("      Simpleperf 性能分析工具 (资源加载重构版)")
        print("=" * 60)
        print("本工具将帮助您分析Android应用程序的性能")
        print("使用包资源加载 simpleperf 工具，无需额外配置")
        print("请确保:")
        print("  1. 设备已通过USB连接并启用调试模式")
        print("  2. 已准备好 report_html.py 脚本")
        print("=" * 60)
    
    def extract_simpleperf_from_resource(self):
        """从包资源中提取 simpleperf 可执行文件"""
        print("从包资源中提取 simpleperf 工具...")
        
        # 1. 定义资源路径
        filepath = "simpleperf"  # 资源文件名
        
        # 2. 获取资源路径
        resource_path = os.path.join("resources/", filepath)
        
        # 3. 从包资源中读取可执行文件
        try:
            data = pkgutil.get_data("collie_package", resource_path)
            if data is None:
                raise FileNotFoundError(f"资源未找到: {resource_path}")
        except Exception as e:
            raise RuntimeError(f"加载可执行资源失败: {e}")
        
        # 创建临时目录
        self.temp_dir = tempfile.mkdtemp(prefix="simpleperf_")
        tmp_path = os.path.join(self.temp_dir, filepath)
        
        # 4. 创建临时文件
        with open(tmp_path, "wb") as tmp_file:
            tmp_file.write(data)
        
        # 5. 设置本地执行权限 (仅 Unix 系统)
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o755)
        
        self.simpleperf_path = tmp_path
        print(f"✅ simpleperf 已提取到: {self.simpleperf_path}")
        return True
    
    def push_simpleperf_to_device(self):
        """推送 simpleperf 到设备"""
        print("推送 simpleperf 到设备...")
        
        # 6. 推送到设备
        tmp_file_name = os.path.basename(self.simpleperf_path)
        full_file_path = os.path.join(self._device_tmp_dir, tmp_file_name)

        try:
            push_result = subprocess.run(
                ["adb", "push", self.simpleperf_path, self._device_tmp_dir],
                capture_output=True,
                text=True,
                timeout=30
            )
            if push_result.returncode != 0:
                raise RuntimeError(f"推送失败: {push_result.stderr}")
            
            # 设置设备上的执行权限
            chmod_cmd = f"adb shell chmod 777 {full_file_path}"
            chmod_result = subprocess.run(
                chmod_cmd, shell=True, capture_output=True, text=True, timeout=10
            )
            if chmod_result.returncode != 0:
                raise RuntimeError(f"设置权限失败: {chmod_result.stderr}")
            
            print("✅ simpleperf 已推送到设备并设置权限")
            return True
        except subprocess.TimeoutExpired:
            raise RuntimeError("推送超时")
        except Exception as e:
            raise RuntimeError(f"推送过程中出错: {e}")
    
    def detect_device_architecture(self):
        """检测设备架构"""
        print("检测设备架构...")
        result = self.run_adb_command("adb shell uname -m", description="检测设备架构")
        
        if not result or not result.stdout.strip():
            print("❌ 无法检测设备架构，使用默认设置")
            return False
        
        arch = result.stdout.strip().lower()
        print(f"设备架构: {arch}")
        
        # 根据架构设置调用图选项
        if 'arm' in arch and '64' not in arch:
            # 32位ARM设备
            self.call_graph_option = "-g"  # 使用 dwarf 格式
            print("使用 -g (dwarf) 作为调用图选项")
        else:
            # 64位设备或其他架构
            self.call_graph_option = "--call-graph fp"  # 使用帧指针
            print("使用 --call-graph fp 作为调用图选项")
        
        self.device_arch = arch
        return True
    
    def cleanup_temp_files(self):
        """清理临时文件"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                print(f"已清理临时文件: {self.temp_dir}")
            except Exception as e:
                print(f"清理临时文件时出错: {e}")
    
    def get_user_input(self, prompt, default=None, required=True):
        """获取用户输入，支持默认值"""
        while True:
            if default:
                user_input = input(f"{prompt} (默认: {default}): ").strip()
            else:
                user_input = input(f"{prompt}: ").strip()
            
            if not user_input and default:
                return default
            elif not user_input and required:
                print("此项为必填项，请重新输入")
            else:
                return user_input
    
    def setup_parameters(self):
        """设置分析参数"""
        print("\n步骤 1/4: 设置分析参数")
        print("-" * 40)
        
        self.package_name = self.get_user_input(
            "请输入要分析的应用程序包名", 
            required=True
        )
        
        self.report_script_path = self.get_user_input(
            "请输入 report_html.py 脚本路径", 
            required=True
        )
        
        duration_input = self.get_user_input(
            "请输入录制持续时间(秒)", 
            default="10"
        )
        self.duration = int(duration_input) if duration_input.isdigit() else 10
    
        self.output_dir = state.FILE_DIR
        
        # 确保输出目录存在
        self._ensure_output_dir()
        
        print("参数设置完成!")
    
    def confirm_parameters(self):
        """确认参数设置"""
        print("\n请确认以下参数:")
        print("-" * 40)
        print(f"应用包名: {self.package_name}")
        print(f"report脚本路径: {self.report_script_path}")
        print(f"录制时长: {self.duration}秒")
        print(f"输出目录: {self.output_dir}")
        print(f"设备架构: {self.device_arch or '未知'}")
        print(f"调用图选项: {self.call_graph_option}")
        print("-" * 40)
        
        input("按任意键开始抓取...")
        return True
    
    def run_adb_command(self, command, check_result=True, description=None):
        """运行ADB命令并返回结果"""
        if description:
            print(f"正在执行: {description}")
        
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
            if check_result and result.returncode != 0:
                print(f"命令执行失败: {command}")
                print(f"错误信息: {result.stderr}")
                return None
            return result
        except subprocess.TimeoutExpired:
            print(f"命令执行超时: {command}")
            return None
        except Exception as e:
            print(f"执行命令时发生异常: {e}")
            return None

    def _countdown(self, seconds=3):
        """简单倒计时，给予用户准备时间"""
        for i in range(seconds, 0, -1):
            print(f"{i}...")
            time.sleep(1)

    def _ensure_output_dir(self):
        """确保输出目录存在"""
        os.makedirs(self.output_dir, exist_ok=True)
    
    def check_device_connected(self):
        """检查设备是否连接"""
        print("\n步骤 2/4: 检查设备连接")
        print("-" * 40)
        
        result = self.run_adb_command("adb devices", description="检查设备连接")
        if not result:
            return False
            
        devices = [line.split('\t')[0] for line in result.stdout.split('\n') 
                  if line.strip() and 'device' in line and not line.startswith('List')]
        
        if not devices:
            print("❌ 未找到连接的设备")
            print("请确保:")
            print("  1. 设备已通过USB连接")
            print("  2. 已启用USB调试模式")
            print("  3. 设备已授权此电脑进行调试")
            return False
            
        print(f"✅ 找到设备: {', '.join(devices)}")
        return True
    
    def get_process_pid(self):
        """获取目标进程的PID"""
        print(f"查找进程 {self.package_name} 的PID...")
        ps_cmd = f"adb shell ps -e | grep {self.package_name}"
        result = self.run_adb_command(ps_cmd, description="查找目标进程")
        
        if not result or not result.stdout.strip():
            print(f"❌ 未找到包名为 {self.package_name} 的进程")
            print("请确保:")
            print("  1. 应用包名正确")
            print("  2. 应用正在运行")
            return None
            
        # 解析PID
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:
            print(f"找到多个进程，使用第一个:")
            for line in lines:
                print(f"  {line}")
        
        first_line = lines[0].split()
        pid = first_line[1] if len(first_line) > 1 else first_line[0]
        print(f"✅ 找到PID: {pid}")
        return pid
    
    def record_perf_data(self, pid):
        """录制性能数据"""
        print("\n步骤 3/4: 录制性能数据")
        print("-" * 40)
        print(f"将在 {self.duration} 秒内录制性能数据")
        print("请在此期间复现您要分析的问题...")
        print("倒计时开始:")
        self._countdown()
        print("开始录制!")

        for description, command in self._build_record_commands(pid):
            result = self.run_adb_command(command, description=description)
            if result:
                print("✅ 录制完成")
                return True
            print("尝试下一种录制方式...")

        print("❌ 录制失败")
        return False

    def _build_record_commands(self, pid):
        """构建录制命令序列，优先使用 cpu-clock，必要时回退"""
        common_args = f"--duration {self.duration} -o {self._device_perf_data} {self.call_graph_option}"
        base_cmd = f"adb shell {self.device_simpleperf_path} record"

        if pid:
            target = f"-p {pid}"
            yield ("录制性能数据", f"{base_cmd} {target} -e cpu-clock {common_args}")
            yield ("使用默认事件录制性能数据", f"{base_cmd} {target} {common_args}")
        else:
            target = f"--app {self.package_name}"
            yield ("按应用名录制性能数据", f"{base_cmd} {target} -e cpu-clock {common_args}")
            yield ("按应用名使用默认事件录制性能数据", f"{base_cmd} {target} {common_args}")
    
    def generate_reports(self):
        """生成报告文件"""
        print("\n步骤 4/4: 生成报告文件")
        print("-" * 40)
        
        # 生成txt报告
        report_txt_cmd = (
            f"adb shell {self.device_simpleperf_path} "
            f"report -i {self._device_perf_data} -o {self._device_perf_txt}"
        )
        if not self.run_adb_command(report_txt_cmd, description="生成文本报告"):
            print("生成txt报告失败")
            return False
        
        # 拉取报告文件
        perf_txt_file = os.path.join(self.output_dir, f"{self.timestamp}_perf.txt")
        perf_data_file = os.path.join(self.output_dir, f"{self.timestamp}_perf.data")
        
        if not self.run_adb_command(f"adb pull {self._device_perf_txt} {perf_txt_file}", description="拉取文本报告"):
            print("拉取perf.txt失败")
            return False
            
        if not self.run_adb_command(f"adb pull {self._device_perf_data} {perf_data_file}", description="拉取数据文件"):
            print("拉取perf.data失败")
            return False
        
        # 生成HTML报告
        html_file = os.path.join(self.output_dir, f"{self.package_name}_{self.timestamp}_perf.html")
        html_cmd = f"python3 {self.report_script_path} -i {perf_data_file} -o {html_file}"
        
        print("生成HTML报告...")
        result = self.run_adb_command(html_cmd, description="生成HTML报告")
        if not result:
            print("生成HTML报告失败")
            return False
        
        print(f"✅ 报告生成完成:")
        print(f"  📄 TXT报告: {perf_txt_file}")
        print(f"  🌐 HTML报告: {html_file}")
        print(f"  📊 原始数据: {perf_data_file}")
        
        return True
    
    def clean_device_files(self):
        """清理设备上的临时文件"""
        print("清理设备上的临时文件...")
        self.run_adb_command(f"adb shell rm -f {self._device_perf_data} {self._device_perf_txt}", 
                            check_result=False, 
                            description="清理设备临时文件")
    
    def run_profiling(self):
        """运行完整的性能分析流程"""
        self.print_header()
        
        try:
            # 检查设备连接
            if not self.check_device_connected():
                return False
                
            # 检测设备架构
            if not self.detect_device_architecture():
                return False
            
            # 从资源中提取 simpleperf
            if not self.extract_simpleperf_from_resource():
                return False
                
            # 推送 simpleperf 到设备
            if not self.push_simpleperf_to_device():
                return False
            
            self.setup_parameters()
            
            if not self.confirm_parameters():
                print("已取消分析")
                return True
            
            # 获取PID
            pid = self.get_process_pid()
            if not pid:
                print("未找到对应进程，将尝试使用 --app 方式启动抓取")
            
            # 录制性能数据
            if not self.record_perf_data(pid):
                return False
            
            # 生成报告
            if not self.generate_reports():
                return False
            
            # 清理设备文件
            self.clean_device_files()
            
            print("\n" + "=" * 60)
            print("✅ 性能分析完成!")
            print("=" * 60)
            return True
        except Exception as e:
            print(f"❌ 分析过程中出错: {e}")
            return False
        finally:
            # 确保清理临时文件
            self.cleanup_temp_files()
            print("如果发现打开的html文件加载不出来,请参考文档:https://xiaomi.f.mioffice.cn/docx/doxk4daTvQ5yfRzYq6nb9qYaQkg")

def main():
    profiler = SimpleperfProfiler()
    
    try:
        success = profiler.run_profiling()
    except KeyboardInterrupt:
        print("\n\n用户中断了操作")
    except Exception as e:
        print(f"\n\n发生未预期错误: {e}")

if __name__ == "__main__":
    main()
