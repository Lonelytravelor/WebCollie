import json
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from importlib import resources
from typing import Dict, Iterable, List, Optional, Tuple

from .. import tools
from ..config_loader import load_rules_config

# 配置参数（支持 rules.yaml）
_RULES = load_rules_config()
_INSTALL_RULES = _RULES.get('app_install', {}) if isinstance(_RULES, dict) else {}

STORE_PACKAGE = _INSTALL_RULES.get('store_package', "com.xiaomi.market")
BASE_RESOLUTION = tuple(_INSTALL_RULES.get('base_resolution', (1080, 2340)))  # 仅用于坐标缩放基准
SEARCH_BOX_COORDS = tuple(_INSTALL_RULES.get('search_box_coords', (840, 220)))  # 搜索框坐标
# 使用比例兜底点击位置，来源于用户提供的样本设备：
# - 1880x3008, 按钮 (935, 2800) -> (0.498, 0.931)
# - 1080x2400(override), 按钮 (530, 2275) -> (0.491, 0.948)
# 默认比例兜底（可被配置文件动态补充/覆盖）
_ratio_list = _INSTALL_RULES.get('install_button_ratios', [(0.50, 0.93), (0.50, 0.94)])
INSTALL_BUTTON_RATIOS = [tuple(x) for x in _ratio_list if isinstance(x, (list, tuple)) and len(x) == 2]
if not INSTALL_BUTTON_RATIOS:
    INSTALL_BUTTON_RATIOS = [(0.50, 0.93), (0.50, 0.94)]

INSTALL_BUTTON_COORDS = tuple(_INSTALL_RULES.get('install_button_coords', (900, 1600)))  # 仅用于极端兜底
USE_UI_DUMP = bool(_INSTALL_RULES.get('use_ui_dump', False))  # 关闭 UI dump，直接使用比例/兜底点击
WAIT_TIME = int(_INSTALL_RULES.get('wait_time', 5))
DELETE_ATTEMPTS = int(_INSTALL_RULES.get('delete_attempts', 30))
BUSY_PAUSE_SEC = int(_INSTALL_RULES.get('busy_pause_sec', 4))
INSTALL_POLL_INTERVAL = int(_INSTALL_RULES.get('install_poll_interval', 6))
INSTALL_POLL_TIMEOUT = int(_INSTALL_RULES.get('install_poll_timeout', 240))  # 总等待时间（秒）
MAX_WAIT_PER_APP = int(_INSTALL_RULES.get('max_wait_per_app', 20))  # 每个待安装应用最大等待时间
PAGE_LOAD_WAIT = float(_INSTALL_RULES.get('page_load_wait', 4.0))  # 详情页加载等待时间
FALLBACK_MAX_CLICKS = int(_INSTALL_RULES.get('fallback_max_clicks', 1))  # 兜底点击次数
POST_TAP_WAIT = float(_INSTALL_RULES.get('post_tap_wait', 2.5))  # 点击后等待 UI 响应
COORD_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "app_install_coords.json")
APP_CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app_config.json"))
SIZE_CANDIDATES: List[Tuple[int, int]] = []


def adb_command(cmd: str, timeout: int = 30) -> str:
    """执行 ADB 命令并返回输出"""
    args = shlex.split(cmd)
    result = subprocess.run(
        ["adb"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        print(f"命令执行失败: adb {cmd}")
        if result.stderr:
            print(f"错误信息: {result.stderr.strip()}")
    return result.stdout.strip()


def tap(x: int, y: int) -> None:
    adb_command(f"shell input tap {x} {y}")


def input_text(text: str) -> None:
    adb_command(f'shell input text "{text}"')


def get_screen_size() -> Optional[Tuple[int, int]]:
    """
    获取屏幕分辨率，并记录 Override / Physical 两组候选。
    返回优先 Override 结果；若不存在 Override 则返回 Physical。
    """
    global SIZE_CANDIDATES
    SIZE_CANDIDATES = []
    size_output = adb_command("shell wm size")
    # 解析 override / physical
    override_match = re.search(r"Override size:\s*(\d+)x(\d+)", size_output)
    physical_match = re.search(r"Physical size:\s*(\d+)x(\d+)", size_output)
    if override_match:
        ov = (int(override_match.group(1)), int(override_match.group(2)))
        SIZE_CANDIDATES.append(ov)
    if physical_match:
        ph = (int(physical_match.group(1)), int(physical_match.group(2)))
        if ph not in SIZE_CANDIDATES:
            SIZE_CANDIDATES.append(ph)
    # 回退匹配任意数字
    match = re.search(r"(\d+)x(\d+)", size_output)
    if match:
        any_size = (int(match.group(1)), int(match.group(2)))
        if any_size not in SIZE_CANDIDATES:
            SIZE_CANDIDATES.append(any_size)
    if SIZE_CANDIDATES:
        return SIZE_CANDIDATES[0]
    return None


def scale_point(point: Tuple[int, int], screen_size: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    if not screen_size:
        return point
    base_w, base_h = BASE_RESOLUTION
    x = int(point[0] * screen_size[0] / base_w)
    y = int(point[1] * screen_size[1] / base_h)
    return x, y


def ratio_point(ratio: Tuple[float, float], screen_size: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """根据比例生成坐标，适配不同分辨率"""
    if screen_size:
        return int(screen_size[0] * ratio[0]), int(screen_size[1] * ratio[1])
    # 兜底到基准分辨率
    base_w, base_h = BASE_RESOLUTION
    return int(base_w * ratio[0]), int(base_h * ratio[1])


def _candidate_coord_paths(primary_path: str) -> List[str]:
    """生成可能的坐标文件路径（包内、源码、当前工作目录）"""
    candidates = [primary_path]
    cwd = os.getcwd()
    candidates.append(os.path.join(cwd, "src", "collie_package", "utilities", "app_install_coords.json"))
    candidates.append(os.path.join(cwd, "collie_package", "utilities", "app_install_coords.json"))
    candidates.append(os.path.join(cwd, "app_install_coords.json"))
    # 去重保持顺序
    seen = set()
    uniq = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _candidate_app_config_paths() -> List[str]:
    candidates = [APP_CONFIG_PATH]
    cwd = os.getcwd()
    candidates.append(os.path.join(cwd, "src", "collie_package", "app_config.json"))
    candidates.append(os.path.join(cwd, "collie_package", "app_config.json"))
    candidates.append(os.path.join(cwd, "app_config.json"))
    # 去重
    seen = set()
    uniq = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_known_coords(path: str = COORD_CONFIG_PATH) -> Dict[str, Tuple[int, int]]:
    """
    从配置文件读取分辨率对应的点击坐标映射。
    格式示例:
    {
        "1080x2400": [530, 2275],
        "1880x3008": [935, 2800]
    }
    """
    def _parse(data: Dict[str, Iterable[int]]) -> Dict[str, Tuple[int, int]]:
        coords: Dict[str, Tuple[int, int]] = {}
        for key, val in data.items():
            if (
                isinstance(key, str)
                and isinstance(val, (list, tuple))
                and len(val) == 2
                and all(isinstance(v, (int, float)) for v in val)
            ):
                coords[key.lower()] = (int(val[0]), int(val[1]))
        return coords

    # 1) 尝试路径列表（当前目录/源码目录/包目录）
    for candidate in _candidate_coord_paths(path):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as fp:
                    coords = _parse(json.load(fp))
                if coords:
                    print(f"从文件加载坐标表: {candidate}")
                    return coords
            except Exception as exc:  # noqa: BLE001
                print(f"加载坐标配置失败（文件路径读取 {candidate}）: {exc}")

    # 2) 尝试从 app_config.json 中的 install_button_coords
    for cfg_path in _candidate_app_config_paths():
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                coords_section = data.get("install_button_coords", {})
                coords = _parse(coords_section) if isinstance(coords_section, dict) else {}
                if coords:
                    # print(f"从 app_config.json 加载坐标表: {cfg_path}")
                    return coords
            except Exception as exc:  # noqa: BLE001
                print(f"加载 app_config.json 中坐标失败（{cfg_path}）: {exc}")

    # 3) 尝试从包资源加载，适配安装为包的场景
    try:
        with resources.open_text(__package__, "app_install_coords.json", encoding="utf-8") as fp:
            coords = _parse(json.load(fp))
            if coords:
                print("从包资源加载坐标表: app_install_coords.json")
                return coords
    except Exception as exc:  # noqa: BLE001
        print(f"加载坐标配置失败（包资源读取）: {exc}")
    return {}


KNOWN_COORDS = load_known_coords()


def get_known_point(screen_size: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    candidates: List[Tuple[int, int]] = []
    if screen_size:
        candidates.append(screen_size)
    candidates.extend([s for s in SIZE_CANDIDATES if s not in candidates])
    for size in candidates:
        key = f"{size[0]}x{size[1]}".lower()
        if key in KNOWN_COORDS:
            return KNOWN_COORDS[key]
    return None


def refresh_known_coords() -> None:
    global KNOWN_COORDS
    KNOWN_COORDS = load_known_coords()
    if KNOWN_COORDS:
        print(f"已加载坐标表: {list(KNOWN_COORDS.keys())}")
    else:
        print("坐标表为空或未找到 app_install_coords.json，继续使用比例兜底。")


def derived_ratios_from_known() -> List[Tuple[float, float]]:
    """
    将已知坐标转换为比例，用于未匹配到 exact 分辨率时的兜底。
    """
    ratios: List[Tuple[float, float]] = []
    for key, (px, py) in KNOWN_COORDS.items():
        match = re.match(r"(\\d+)x(\\d+)", key)
        if not match:
            continue
        w, h = int(match.group(1)), int(match.group(2))
        if w and h:
            ratios.append((px / w, py / h))
    return ratios


def clear_text() -> None:
    """
    更稳健地清空文本框：移动到末尾 + 长按删除 + 补充多次删除
    防止点击到中间导致残留。
    """
    adb_command("shell input keyevent KEYCODE_MOVE_END")
    adb_command("shell input keyevent --longpress KEYCODE_DEL")
    time.sleep(0.1)
    for _ in range(DELETE_ATTEMPTS):
        adb_command("shell input keyevent KEYCODE_DEL")
        time.sleep(0.02)


def dump_ui_xml() -> Optional[str]:
    """抓取当前 UI 层级，返回 XML 字符串"""
    dump_result = subprocess.run(
        ["adb", "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
        capture_output=True,
        text=True,
    )
    if dump_result.returncode != 0:
        return None
    cat_result = subprocess.run(
        ["adb", "shell", "cat", "/sdcard/window_dump.xml"],
        capture_output=True,
        text=True,
    )
    if cat_result.returncode != 0:
        return None
    return cat_result.stdout


def _node_center(bounds: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_clickable_by_text(
    keywords: Iterable[str],
    package_filter: Optional[str] = STORE_PACKAGE,
    relax_package: bool = True,
) -> Optional[Tuple[int, int]]:
    """
    在当前界面查找包含关键词的可点击节点并返回中心坐标。
    默认只匹配商店包名；如果 relax_package=True 则会在未命中时取消包过滤再查一次。
    """
    xml_content = dump_ui_xml()
    if not xml_content:
        return None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    lowered = [k.lower() for k in keywords]
    def _scan(filter_pkg: Optional[str]) -> Optional[Tuple[int, int]]:
        for node in root.iter("node"):
            text = node.attrib.get("text", "").lower()
            res_id = node.attrib.get("resource-id", "").lower()
            pkg = node.attrib.get("package", "")
            if filter_pkg and pkg != filter_pkg:
                continue
            if any(k in text for k in lowered) or any(k in res_id for k in lowered):
                if node.attrib.get("clickable", "false") == "true":
                    bounds = node.attrib.get("bounds", "")
                    center = _node_center(bounds)
                    if center:
                        return center
        return None

    found = _scan(package_filter)
    if found:
        return found
    if relax_package:
        return _scan(None)
    return None


def open_app_store() -> None:
    print("正在打开应用商店...")
    subprocess.run(
        ["adb", "shell", "monkey", "-p", STORE_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def open_app_detail(package_name: str) -> None:
    """直接跳转到应用详情页，避免手动输入搜索框"""
    adb_command(
        f'shell am start -a android.intent.action.VIEW -d "market://details?id={package_name}"'
    )
    time.sleep(3)


def wait_and_tap_install(
    screen_size: Optional[Tuple[int, int]], attempts: int = 8, sleep_sec: float = 1.5
) -> bool:
    """
    尝试点击安装按钮。
    若开启 USE_UI_DUMP，则先查 UI；否则直接按比例/兜底坐标点击，避免依赖 UI dump。
    """
    keywords = ("安装", "下载", "更新", "获取", "继续")
    if USE_UI_DUMP:
        for i in range(attempts):
            coords = find_clickable_by_text(keywords)
            if coords:
                tap(*coords)
                return True
            if i >= 2:
                print("界面可能卡顿，稍等片刻再尝试...")
                time.sleep(BUSY_PAUSE_SEC)
            time.sleep(sleep_sec)
        # 放宽包名限制再查一次
        coords = find_clickable_by_text(keywords, package_filter=None, relax_package=False)
        if coords:
            tap(*coords)
            return True

    # fallback：优先已知分辨率的精确坐标，其次比例/基准缩放；只点击有限次数，避免取消下载
    known_point = get_known_point(screen_size)
    if known_point:
        print(f"使用已知分辨率坐标点击: {known_point}")
        tap(*known_point)
        time.sleep(POST_TAP_WAIT)
        return True

    print("未匹配到已知分辨率坐标，使用比例兜底。")
    # 动态补充由已知分辨率计算出的比例（若有）
    dynamic_ratios = derived_ratios_from_known()
    all_ratios = dynamic_ratios + INSTALL_BUTTON_RATIOS
    fallback_points = [ratio_point(r, screen_size) for r in all_ratios]
    fallback_points.append(scale_point(INSTALL_BUTTON_COORDS, screen_size))  # 极端兜底

    for idx, point in enumerate(fallback_points):
        if idx >= FALLBACK_MAX_CLICKS:
            break
        print(f"兜底点击: {point}")
        tap(*point)
        time.sleep(POST_TAP_WAIT)
        return True
    return False


def wait_for_installation(package_name: str, timeout: int = 180, interval: int = 5) -> bool:
    """轮询安装结果，网络差时等待更久"""
    waited = 0
    while waited < timeout:
        output = adb_command(f"shell pm path {package_name}", timeout=timeout)
        if output and "package:" in output:
            return True
        time.sleep(interval)
        waited += interval
    return False


def is_installed(package_name: str) -> bool:
    output = adb_command(f"shell pm path {package_name}", timeout=10)
    return bool(output and "package:" in output)


def search_and_install(
    package_name: str, screen_size: Optional[Tuple[int, int]], wait_for_finish: bool = True
) -> None:
    """
    打开详情页 -> 点击安装。
    wait_for_finish=True 时等待安装完成；否则仅触发下载/安装后立即返回，便于并行发起。
    """
    print(f"处理应用: {package_name}")

    open_app_detail(package_name)
    time.sleep(PAGE_LOAD_WAIT)  # 给页面加载留足时间，防止未加载就点击导致误触

    tapped = wait_and_tap_install(screen_size)
    if tapped:
        print("已找到并点击安装按钮。")
    else:
        print("未通过 UI 定位到安装按钮，已尝试兜底坐标，请确认是否已触发安装。")

    if wait_for_finish:
        print("等待安装完成中...")
        if wait_for_installation(package_name):
            print(f"[完成] {package_name} 安装成功")
        else:
            print(f"[警告] 等待超时，未确认 {package_name} 是否安装成功，请手动检查")
    adb_command("shell input keyevent KEYCODE_HOME")
    time.sleep(POST_TAP_WAIT)


def filter_installed_packages(package_list: List[str]) -> List[str]:
    """过滤掉设备中已安装的应用"""
    try:
        result = subprocess.run(
            ["adb", "shell", "pm", "list", "packages"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print("ADB命令执行失败，请确保设备已连接且ADB可用")
            return package_list

        installed_packages = set()
        for line in result.stdout.splitlines():
            if line.startswith("package:"):
                installed_packages.add(line.strip().replace("package:", ""))

        return [package for package in package_list if package not in installed_packages]
    except subprocess.TimeoutExpired:
        print("ADB命令执行超时")
        return package_list
    except Exception as exc:  # noqa: BLE001
        print(f"执行过程中发生错误: {exc}")
        return package_list


def ensure_device_connected() -> bool:
    devices = adb_command("devices").splitlines()
    return len(devices) >= 2 and "device" in devices[1]


def wait_for_installations(
    packages: List[str], max_wait_seconds: Optional[int] = None
) -> Tuple[List[str], bool]:
    """
    并行等待一组应用完成安装，返回 (仍未完成的列表, 是否达到最大等待时间)。
    会在系统卡顿时适当延长轮询间隔，并在达到最大等待时间后提前结束。
    """
    pending = set(packages)
    if not pending:
        return [], False

    effective_timeout = max_wait_seconds if max_wait_seconds is not None else max(INSTALL_POLL_TIMEOUT, len(pending) * 60)
    deadline = time.time() + effective_timeout
    interval = INSTALL_POLL_INTERVAL

    while pending and time.time() < deadline:
        finished = []
        for pkg in list(pending):
            if is_installed(pkg):
                finished.append(pkg)
                pending.remove(pkg)
        if finished:
            print(f"已完成: {finished}")
            # 恢复正常轮询速度
            interval = INSTALL_POLL_INTERVAL
        else:
            print("安装中，设备可能繁忙，稍等再检查...")
            interval = min(interval + 2, 15)
        time.sleep(interval)

    timed_out = bool(pending)
    return list(pending), timed_out


def install_apps() -> None:
    if not ensure_device_connected():
        print("未找到连接的Android设备")
        return

    original_list = tools.load_config_status()
    if original_list == -1:
        return

    refresh_known_coords()

    screen_size = get_screen_size()
    if screen_size:
        print(f"设备分辨率: {screen_size[0]}x{screen_size[1]}")
        if SIZE_CANDIDATES:
            print(f"解析到的分辨率候选: {[f'{w}x{h}' for w, h in SIZE_CANDIDATES]}")
        # 显示可用的已知坐标，便于核对匹配
        if KNOWN_COORDS:
            print(f"已知坐标表: {KNOWN_COORDS}")
    else:
        print("未获取到分辨率，将使用默认坐标作为兜底。")

    open_app_store()

    pending = filter_installed_packages(original_list)
    if not pending:
        print("当前配置的应用均已安装。")
        return

    print("未安装的包名列表:", pending)

    # 第一阶段：快速发起下载/安装，不等待完成，加速多应用场景
    print("\n先依次发起下载/安装，不等待完成，以便并行下载...")
    for idx, package in enumerate(pending):
        print(f"[触发 {idx + 1}/{len(pending)}] {package}")
        search_and_install(package, screen_size, wait_for_finish=False)

    # 第二阶段：统一等待安装完成，可根据设备繁忙程度自动延长间隔
    max_wait_seconds = len(pending) * MAX_WAIT_PER_APP

    print(f"\n已触发全部下载，开始后台轮询安装进度（最长等待 {max_wait_seconds}s）...")
    still_pending, timed_out = wait_for_installations(pending, max_wait_seconds=max_wait_seconds)
    if timed_out and still_pending:
        print(f"\n已达到最大等待时间（{max_wait_seconds}s），仍未确认安装完成: {still_pending}")

    if not still_pending:
        print("\n所有应用安装流程已完成!")
        return

    # 收尾：再次确认是否还有遗漏
    remaining = filter_installed_packages(original_list) if not still_pending else still_pending
    if not remaining:
        print("\n所有应用安装流程已完成!")
        return

    print("\n仍有未安装的应用:", remaining)
    prompt = (
        "已达到最大等待时间，是否再次检查并尝试安装遗漏的应用? 输入 y 继续，其他键退出: "
        if timed_out
        else "是否再次检查并尝试安装遗漏的应用? 输入 y 继续，其他键退出: "
    )
    retry = input(prompt).strip().lower()
    if retry == "y":
        for idx, package in enumerate(remaining):
            print(f"\n[补装 {idx + 1}/{len(remaining)}] 开始安装: {package}")
            search_and_install(package, screen_size)
        print("\n补装流程结束，请再次确认是否全部安装完毕。")
    else:
        print("已选择不再补装，请手动确认剩余应用。")


if __name__ == "__main__":
    install_apps()
