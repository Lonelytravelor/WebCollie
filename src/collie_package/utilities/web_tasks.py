import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class TaskHooks:
    progress: Callable[[int, str], None]
    log: Callable[[str], None]
    warn: Callable[[str], None]
    check_cancel: Callable[[], None]
    wait_if_paused: Callable[[], None]
    sleep_with_control: Callable[[float], None]
    is_cancelled: Callable[[], bool]
    add_monitor_detail: Optional[Callable[[bool, str, str], None]] = None
    update_monitor_summary: Optional[Callable[[dict], None]] = None
    set_paused: Optional[Callable[[str], None]] = None
    update_compile_summary: Optional[Callable[[dict], None]] = None
    update_compile_item: Optional[Callable[[int, dict], None]] = None


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


def _validate_positive_int(value, name, min_value=1, max_value=1000000):
    if not isinstance(value, int):
        raise RuntimeError(f"{name} 必须为整数")
    if value < min_value or value > max_value:
        raise RuntimeError(f"{name} 范围必须在 {min_value}~{max_value}")


def _extract_version_name(text: str) -> str:
    import re

    match = re.search(r"versionName=(\S+)", text or "")
    if match:
        return match.group(1)
    return "未获取到版本号"


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


def parse_app_versions_content(content: str) -> dict:
    result = {}
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("+") and line.endswith("+"):
            continue

        row_match = re.match(r"^\|\s*(?P<c1>[^|]+?)\s*\|\s*(?P<c2>[^|]+?)\s*\|$", line)
        if row_match:
            c1 = row_match.group("c1").strip()
            c2 = row_match.group("c2").strip()
            if c1.lower() == "package_name" and c2.lower() == "version_name":
                continue
            if c1:
                result[c1] = c2
            continue

        kv_match = re.match(
            r"^(?P<pkg>[A-Za-z0-9_\\.]+)\s*(?:versin|version|版本)?\s*[:：]\s*(?P<ver>.+)$",
            line,
            re.IGNORECASE,
        )
        if kv_match:
            result[kv_match.group("pkg").strip()] = kv_match.group("ver").strip()
    return result


def build_app_versions_compare(job_id_1: str, job_id_2: str, text_a: str, text_b: str) -> dict:
    data_1 = parse_app_versions_content(text_a)
    data_2 = parse_app_versions_content(text_b)
    if not data_1 and not data_2:
        raise RuntimeError("两份结果都无法解析版本信息")

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

    return {
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


def build_app_versions_report(job_id: str, content: str, updated_at: str) -> dict:
    parsed = parse_app_versions_content(content)
    rows = [{"package_name": pkg, "version_name": ver} for pkg, ver in sorted(parsed.items())]
    table_rows = [(row["package_name"], row["version_name"]) for row in rows]
    markdown = "\n".join(
        [
            "# 版本检查结果",
            "",
            f"- 任务ID: {job_id}",
            f"- 更新时间: {updated_at}",
            f"- 条目数: {len(rows)}",
            "",
            _render_markdown_table(("包名", "版本号"), table_rows),
            "",
        ]
    )
    return {
        "rows": rows,
        "rows_count": len(rows),
        "markdown": markdown,
    }


def build_check_app_history_item(
    job_id: str,
    job_status: str,
    created_at: str,
    updated_ts: float,
    result_file: Path,
    source: str,
    files: list,
) -> dict:
    rows_count = 0
    has_result = result_file.exists()
    if has_result:
        try:
            text = result_file.read_text(encoding="utf-8", errors="ignore")
            rows_count = len(parse_app_versions_content(text))
        except Exception:
            rows_count = 0
    updated_at = datetime.fromtimestamp(updated_ts).strftime("%Y%m%d_%H%M%S")
    return {
        "job_id": job_id,
        "status": job_status,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
        "result_file": "app_versions.txt" if has_result else None,
        "rows_count": rows_count,
        "has_result": has_result,
        "source": source,
        "files": files,
        "_updated_ts": updated_ts,
    }


def run_device_info(
    device_id: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
) -> None:
    hooks.progress(5, "获取设备信息")
    model = adb_runner(["shell", "getprop", "ro.product.model"], 30)
    android_ver = adb_runner(["shell", "getprop", "ro.build.version.release"], 30)
    sdk = adb_runner(["shell", "getprop", "ro.build.version.sdk"], 30)
    content = [
        f"device_id: {device_id}",
        f"model: {model.strip()}",
        f"android: {android_ver.strip()}",
        f"sdk: {sdk.strip()}",
    ]
    (out_dir / "device_info.txt").write_text("\n".join(content) + "\n", encoding="utf-8")
    hooks.progress(100, "设备信息获取完成")


def run_package_version(
    package_name: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
) -> None:
    hooks.progress(5, f"获取应用信息: {package_name}")
    output = adb_runner(["shell", "dumpsys", "package", package_name], 180)
    (out_dir / f"package_{package_name}.txt").write_text(output, encoding="utf-8")
    hooks.progress(100, "应用信息获取完成")


def run_check_app_versions(
    packages: list,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    validate_package: Callable[[str], None],
) -> None:
    pkg_list = [str(p).strip() for p in packages if str(p).strip()]
    if not pkg_list:
        raise RuntimeError("packages 必须是非空数组")
    for pkg in pkg_list:
        validate_package(pkg)

    rows = []
    total = len(pkg_list)
    hooks.progress(5, f"开始检查应用版本，共 {total} 个")
    for idx, pkg in enumerate(pkg_list):
        hooks.check_cancel()
        hooks.wait_if_paused()
        hooks.progress(5 + int(85 * (idx / max(1, total))), f"检查 {idx + 1}/{total}: {pkg}")
        output = adb_runner(["shell", "dumpsys", "package", pkg], 120)
        version = _extract_version_name(output)
        rows.append((pkg, version))

    table_text = "包名-版本号对照表\n" + _render_ascii_table(("package_name", "version_name"), rows)
    hooks.log(table_text)
    (out_dir / "app_versions.txt").write_text(table_text + "\n", encoding="utf-8")
    hooks.progress(100, "应用版本检查完成")


def run_meminfo_live(
    package_name: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    validate_package: Callable[[str], None],
) -> None:
    cmd = ["shell", "dumpsys", "meminfo"]
    if package_name:
        validate_package(package_name)
        cmd.append(package_name)
    output = adb_runner(cmd, 180)
    (out_dir / "meminfo_live.txt").write_text(output, encoding="utf-8")
    hooks.progress(100, "meminfo 抓取完成")


def run_meminfo_summary(
    device_id: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
) -> None:
    from collie_package.utilities import meminfo_summary

    raw = adb_runner(["shell", "dumpsys", "meminfo"], 180)
    report = meminfo_summary.generate_report(raw, f"adb shell dumpsys meminfo ({device_id})")
    (out_dir / "meminfo_summary.txt").write_text(report, encoding="utf-8")
    hooks.progress(100, "meminfo 摘要生成完成")


def run_collect_device_meminfo(
    device_id: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
) -> None:
    getprop = adb_runner(["shell", "getprop"], 180)
    meminfo = adb_runner(["shell", "dumpsys", "meminfo"], 180)
    text = f"# device_id: {device_id}\n\n## getprop\n{getprop}\n\n## dumpsys meminfo\n{meminfo}"
    (out_dir / "collect_device_meminfo.txt").write_text(text, encoding="utf-8")
    hooks.progress(100, "设备 meminfo 采集完成")


def run_killinfo_line_parse(
    line_text: str,
    out_dir: Path,
    hooks: TaskHooks,
) -> None:
    from collie_package.utilities.killinfo_line_parser import parse_kill_line_text

    text = str(line_text or "").strip()
    if not text:
        raise RuntimeError("输入内容为空")

    hooks.progress(10, "解析中")
    report = parse_kill_line_text(text)
    (out_dir / "killinfo_line_parse.txt").write_text(report + "\n", encoding="utf-8")
    hooks.progress(100, "解析完成")


def run_prepare_apps(
    packages: list,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    validate_package: Callable[[str], None],
) -> None:
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("packages 必须是非空数组")
    for pkg in packages:
        pkg = str(pkg).strip()
        validate_package(pkg)
        adb_runner(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], 120)


def run_compile_apps(
    packages: list,
    mode: str,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    validate_package: Callable[[str], None],
) -> None:
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("packages 必须是非空数组")
    normalized = []
    for raw_pkg in packages:
        pkg = str(raw_pkg).strip()
        validate_package(pkg)
        normalized.append(pkg)
    if not normalized:
        raise RuntimeError("未找到可编译包名")

    total = len(normalized)
    if hooks.update_compile_summary:
        hooks.update_compile_summary({
            "total": total,
            "completed": 0,
            "current": "",
            "current_index": 0,
            "status": "running",
        })
    if hooks.update_compile_item:
        for idx, pkg in enumerate(normalized):
            hooks.update_compile_item(idx, {"package": pkg, "result": "待编译"})

    for idx, pkg in enumerate(normalized):
        if hooks.update_compile_summary:
            hooks.update_compile_summary({"current": pkg, "current_index": idx + 1, "status": "running"})
        if hooks.update_compile_item:
            hooks.update_compile_item(idx, {"result": "编译中"})
        hooks.progress(5 + int((idx * 90) / max(1, total)), f"编译中 {idx + 1}/{total}: {pkg}")
        try:
            adb_runner(["shell", "cmd", "package", "compile", "-m", mode, "-f", pkg], 300)
        except Exception:
            if hooks.update_compile_item:
                hooks.update_compile_item(idx, {"result": "已取消" if hooks.is_cancelled() else "失败"})
            if hooks.update_compile_summary:
                hooks.update_compile_summary({
                    "completed": idx,
                    "status": "cancelled" if hooks.is_cancelled() else "error",
                })
            raise

        if hooks.update_compile_item:
            hooks.update_compile_item(idx, {"result": "成功"})
        if hooks.update_compile_summary:
            hooks.update_compile_summary({"completed": idx + 1, "status": "running"})
        hooks.progress(5 + int(((idx + 1) * 90) / max(1, total)), f"已完成 {idx + 1}/{total}: {pkg}")

    if hooks.update_compile_summary:
        hooks.update_compile_summary({"current": "", "current_index": 0, "status": "completed"})
    hooks.progress(100, f"编译完成 {total}/{total}")


def run_app_install_apk(
    apk_path: Path,
    package_name: str,
    launch: bool,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    validate_package: Callable[[str], None],
) -> None:
    adb_runner(["install", "-r", str(apk_path)], 600)
    if launch and package_name:
        validate_package(package_name)
        adb_runner(["shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"], 120)
    hooks.progress(100, "APK 安装完成")


def run_store_install_apps(
    packages: list,
    device_id: str,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    is_device_online: Callable[[], bool],
    load_install_coords: Callable[[], dict],
    parse_wm_size: Callable[[str], list],
    ratio_point: Callable[[tuple, tuple], tuple],
    auto_skip_game_packages: set,
    confirm_event: Any,
    set_manual_confirm: Callable[[list], None],
    clear_manual_confirm: Callable[[], None],
    set_message: Callable[[str], None],
    install_interval_sec: int,
    max_check_seconds: int,
) -> None:
    hooks.check_cancel()
    store_package = "com.xiaomi.market"

    packages = [str(p).strip() for p in packages if str(p).strip()]
    if not packages:
        raise RuntimeError("packages 必须是非空数组")

    hooks.progress(1, "检查设备分辨率")
    wm_size = adb_runner(["shell", "wm", "size"], 30)
    size_candidates = parse_wm_size(wm_size)
    screen_size = size_candidates[0] if size_candidates else None

    coords_map = load_install_coords()
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

    hooks.progress(2, "打开应用商店")
    adb_runner(["shell", "monkey", "-p", store_package, "-c", "android.intent.category.LAUNCHER", "1"], 30)
    time.sleep(2)

    hooks.progress(5, "读取已安装列表")
    pm_list = adb_runner(["shell", "pm", "list", "packages"], 60)
    installed = set()
    for line in pm_list.splitlines():
        if line.startswith("package:"):
            installed.add(line.replace("package:", "").strip())

    pending = [p for p in packages if p not in installed]
    if not pending:
        (out_dir / "store_install_summary.txt").write_text("所有应用已安装\n", encoding="utf-8")
        hooks.progress(100, "全部已安装")
        return

    hooks.log(f"待安装数量: {len(pending)}")

    def _tap_install_once():
        if known_point and isinstance(known_point, tuple):
            x, y = known_point
            adb_runner(["shell", "input", "tap", str(int(x)), str(int(y))], 10)
            return
        if ratios:
            x, y = ratio_point(ratios[0], screen_size)
            adb_runner(["shell", "input", "tap", str(int(x)), str(int(y))], 10)
            return
        adb_runner(["shell", "input", "tap", "540", "2100"], 10)

    hooks.progress(10, "依次触发安装")
    for idx, pkg in enumerate(pending):
        hooks.check_cancel()
        hooks.progress(10 + int(40 * (idx / max(1, len(pending)))), f"打开详情页并触发安装: {pkg}")
        adb_runner(
            [
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                f"market://details?id={pkg}",
            ],
            30,
        )
        hooks.sleep_with_control(max(1, install_interval_sec))
        _tap_install_once()
        hooks.sleep_with_control(2)
        adb_runner(["shell", "input", "keyevent", "KEYCODE_HOME"], 10)
        hooks.sleep_with_control(1)

    hooks.progress(55, f"本轮触发完成，等待手动确认后再校验（建议间隔 {install_interval_sec}s）")
    set_manual_confirm(pending)

    manual_confirm_deadline = time.time() + max_check_seconds
    auto_confirmed = False
    offline_warned = False
    while not confirm_event.is_set():
        hooks.check_cancel()
        if time.time() >= manual_confirm_deadline:
            if is_device_online():
                auto_confirmed = True
                confirm_event.set()
                set_message("等待确认超时，设备在线，自动继续并跳过游戏校验")
                hooks.warn("[auto-confirm] 等待 double check 超时，设备在线，自动继续。")
                break
            if not offline_warned:
                offline_warned = True
                set_message("等待确认超时，但设备当前离线，继续等待手动确认")
                hooks.warn("[warn] 等待 double check 超时，但设备当前离线，未自动继续。")
        time.sleep(1)

    clear_manual_confirm()
    if auto_confirmed:
        set_message("自动继续，开始校验安装结果（跳过游戏）")
    else:
        set_message("已确认，开始校验安装结果")

    deadline = time.time() + max_check_seconds
    still = set(pending)
    skipped_games = []
    if auto_confirmed:
        skipped_games = sorted(pkg for pkg in still if pkg in auto_skip_game_packages)
        if skipped_games:
            for pkg in skipped_games:
                still.discard(pkg)
            hooks.warn(f"[auto-confirm] 已跳过游戏校验: {skipped_games}")
    hooks.progress(65, "校验安装结果")
    while still and time.time() < deadline:
        hooks.check_cancel()
        pm_list_now = adb_runner(["shell", "pm", "list", "packages"], 60)
        installed_now = set()
        for line in pm_list_now.splitlines():
            if line.startswith("package:"):
                installed_now.add(line.replace("package:", "").strip())

        finished = [pkg for pkg in list(still) if pkg in installed_now]
        for pkg in finished:
            still.remove(pkg)
        if finished:
            hooks.log(f"校验通过: {finished}")
        done = len(pending) - len(still)
        hooks.progress(65 + int(30 * (done / max(1, len(pending)))), f"校验完成 {done}/{len(pending)}")
        if still:
            hooks.sleep_with_control(6)

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

    hooks.progress(100, "安装流程完成")


def parse_wm_size(output: str) -> list:
    candidates = []
    m = re.search(r"Override size:\s*(\d+)x(\d+)", output or "")
    if m:
        candidates.append((int(m.group(1)), int(m.group(2))))
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", output or "")
    if m:
        candidates.append((int(m.group(1)), int(m.group(2))))
    m = re.search(r"(\d+)x(\d+)", output or "")
    if m:
        any_size = (int(m.group(1)), int(m.group(2)))
        if any_size not in candidates:
            candidates.append(any_size)
    return candidates


def ratio_point(ratio: tuple, size: Optional[tuple]) -> tuple:
    if size:
        return int(size[0] * ratio[0]), int(size[1] * ratio[1])
    return int(1080 * ratio[0]), int(2340 * ratio[1])


def run_app_died_monitor(
    package_name: str,
    interval_sec: int,
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
    hooks: TaskHooks,
    device_online_checker: Callable[[], bool],
) -> None:
    if hooks.update_monitor_summary:
        hooks.update_monitor_summary({
            "package": package_name,
            "interval_sec": interval_sec,
            "checks": 0,
            "alive_checks": 0,
            "dead_checks": 0,
            "first_alive_time": "",
            "first_kill_time": "",
            "last_state": "init",
            "last_note": "监控已启动",
        })

    hooks.progress(5, f"开始监控 {package_name}（每 {interval_sec}s）")
    if hooks.add_monitor_detail:
        hooks.add_monitor_detail(False, "monitor_started", "开始监控，等待应用启动")

    first_alive_seen = False
    first_kill_captured = False
    last_alive = None

    while True:
        hooks.check_cancel()
        hooks.wait_if_paused()

        if not device_online_checker():
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(False, "device_offline", "设备离线，等待重连")
            hooks.progress(8, "设备离线，等待重连")
            hooks.sleep_with_control(interval_sec)
            continue

        try:
            probe_out = adb_runner(["shell", "pidof", package_name], 15)
            alive_now = bool((probe_out or "").strip())
        except Exception as exc:  # noqa: BLE001
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(False, "probe_error", f"探测失败: {exc}")
            hooks.progress(8, "探测失败，重试中")
            hooks.sleep_with_control(interval_sec)
            continue

        if not first_alive_seen:
            if alive_now:
                first_alive_seen = True
                if hooks.update_monitor_summary:
                    hooks.update_monitor_summary({"first_alive_time": datetime.now().strftime("%Y%m%d_%H%M%S")})
                if hooks.add_monitor_detail:
                    hooks.add_monitor_detail(True, "first_alive", "检测到应用首次存活，开始等待首次查杀")
                hooks.progress(45, "已检测到应用启动，等待首次查杀")
            else:
                if hooks.add_monitor_detail:
                    hooks.add_monitor_detail(False, "waiting_start", "应用未启动，继续等待")
                hooks.progress(20, "等待应用首次启动")
            last_alive = alive_now
            hooks.sleep_with_control(interval_sec)
            continue

        if (not first_kill_captured) and last_alive is True and alive_now is False:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(False, "first_killed", "检测到首次查杀，开始抓取全局 dumpsys")
            hooks.progress(80, "检测到首次查杀，抓取全局 dumpsys 中")

            meminfo_out = adb_runner(["shell", "dumpsys", "meminfo"], 180)
            activity_out = adb_runner(["shell", "dumpsys", "activity"], 180)

            meminfo_file = out_dir / f"monitor_meminfo_global_{timestamp}.txt"
            activity_file = out_dir / f"monitor_activity_{package_name}_{timestamp}.txt"
            meminfo_file.write_text(meminfo_out, encoding="utf-8")
            activity_file.write_text(activity_out, encoding="utf-8")

            if hooks.update_monitor_summary:
                hooks.update_monitor_summary({
                    "first_kill_time": datetime.now().strftime("%Y%m%d_%H%M%S"),
                    "capture_files": [meminfo_file.name, activity_file.name],
                })
            if hooks.set_paused:
                hooks.set_paused("首次查杀抓取完成，已自动暂停")
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(
                    False,
                    "auto_paused_after_capture",
                    f"抓取完成并自动暂停: {meminfo_file.name}, {activity_file.name}",
                )
            hooks.progress(90, "首次查杀抓取完成，任务已自动暂停")
            first_kill_captured = True
            last_alive = alive_now
            continue

        if alive_now:
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(True, "alive", "监控中")
            hooks.progress(60, "应用存活，持续监控中")
        else:
            if hooks.add_monitor_detail:
                hooks.add_monitor_detail(False, "dead_waiting_restart", "应用当前未存活，等待再次启动后继续监控")
            hooks.progress(55, "应用当前未存活，等待再次启动")

        last_alive = alive_now
    hooks.sleep_with_control(interval_sec)


def run_monkey(
    package_name: str,
    events: int,
    throttle_ms: int,
    seed: Optional[int],
    out_dir: Path,
    adb_runner: Callable[[list, int], str],
) -> None:
    cmd = [
        "shell",
        "monkey",
        "-p",
        package_name,
        "--throttle",
        str(throttle_ms),
    ]
    if seed is not None:
        cmd += ["-s", str(seed)]
    cmd += [str(events)]
    timeout = max(300, events * max(throttle_ms, 1) // 1000 + 120)
    output = adb_runner(cmd, timeout)
    (out_dir / "monkey_output.txt").write_text(output, encoding="utf-8")


def run_cont_startup_stay(
    config: Any,
    job_dir: Path,
    adb_exec: Any,
    hooks: TaskHooks,
) -> None:
    from collie_package.rd_selftest import cont_startup_stay_runner as runner

    class _ControlledAdbExecutor:
        def __init__(self, base_exec):
            self._base = base_exec

        def build_argv(self, device_id, args):
            return self._base.build_argv(device_id=device_id, args=args)

        def build_host_argv(self, args):
            return self._base.build_host_argv(args=args)

        def run(self, device_id, args, timeout_sec=20.0):
            hooks.wait_if_paused()
            hooks.check_cancel()
            return self._base.run(device_id=device_id, args=args, timeout_sec=timeout_sec)

        def run_host(self, args, timeout_sec=20.0):
            hooks.wait_if_paused()
            hooks.check_cancel()
            return self._base.run_host(args=args, timeout_sec=timeout_sec)

    def _mark_manifest_cancelled():
        manifest_path = job_dir / "artifacts_manifest.json"
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
        zip_path = job_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(job_dir):
                for fname in files:
                    path = Path(root) / fname
                    if path.resolve() == zip_path.resolve():
                        continue
                    rel = os.path.relpath(str(path), str(job_dir))
                    zf.write(str(path), arcname=rel)
        return zip_path

    hooks.progress(1, "准备 cont_startup_stay 配置")
    hooks.wait_if_paused()
    hooks.check_cancel()

    hooks.progress(5, "执行 cont_startup_stay")
    controlled_exec = _ControlledAdbExecutor(adb_exec)
    try:
        out = runner.run_cont_startup_stay(job_dir=job_dir, config=config, adb_exec=controlled_exec)
        hooks.log(f"[cont_startup_stay] {json.dumps(out, ensure_ascii=False)}")
    except Exception:
        if hooks.is_cancelled():
            _mark_manifest_cancelled()
        raise
    finally:
        try:
            hooks.progress(90, "打包 cont_startup_stay 产物")
            _ = _zip_artifacts()
        except Exception as exc:  # noqa: BLE001
            hooks.warn(f"[zip_error] {exc}")

    hooks.progress(100, "cont_startup_stay 完成")


def build_cont_startup_config(device_id: str, params: dict):
    from collie_package.rd_selftest import cont_startup_stay_contract as contract

    ContStartupStayConfig = getattr(contract, "ContStartupStayConfig")
    CollectorsConfig = getattr(contract, "CollectorsConfig")
    BugreportPolicy = getattr(contract, "BugreportPolicy")
    AppListSelection = getattr(contract, "AppListSelection")
    OutputDirStrategy = getattr(contract, "OutputDirStrategy")

    collectors_raw = params.get("collectors") or {}
    if not isinstance(collectors_raw, dict):
        raise RuntimeError("collectors 必须是对象")

    bugreport_raw = params.get("bugreport") or {}
    if not isinstance(bugreport_raw, dict):
        raise RuntimeError("bugreport 必须是对象")

    app_list_raw = params.get("app_list") or {}
    if not isinstance(app_list_raw, dict):
        raise RuntimeError("app_list 必须是对象")

    output_dir_raw = params.get("output_dir_strategy") or {}
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

    return ContStartupStayConfig(
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


def resolve_packages_from_preset(
    preset_name: str,
    packages: Optional[list],
    load_app_config: Callable[[], dict],
) -> list:
    preset_name = str(preset_name or "").strip()
    if preset_name:
        cfg = load_app_config()
        preset = cfg.get(preset_name)
        if not isinstance(preset, list) or not preset:
            raise RuntimeError("preset_name 无效或为空")
        packages = preset
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("packages 必须是非空数组，或提供 preset_name")
    return packages
