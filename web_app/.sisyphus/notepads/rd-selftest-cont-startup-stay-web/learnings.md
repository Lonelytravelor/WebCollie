# Learnings

Append-only. Capture conventions, gotchas, and verified behaviors.

## 2026-02-14

- Web Flask 提供 `create_app(test_config=None)`，测试通过传入 `DATA_FOLDER` 指向 `tmp_path` 来隔离 `user_data/` 写入。
- `utilities_webadb` 的 `/api/adb/devices` 依赖 `shutil.which('adb')` + `subprocess.run(['adb','devices','-l'])`；pytest 中可用 `monkeypatch` 替换这两处实现稳定返回。
- pytest 收集时不总能保证项目根目录在 `sys.path`（与 import 模式相关）；在 `tests/conftest.py` 里显式插入 `ROOT_DIR` 可避免本地包（如 `rd_selftest`）导入失败。

- Collie cont_startup_stay 使用 timestamp 格式 `%d_%H_%M`（示例：`01_02_03`）拼接输出文件名；Web contract 里用 `OutputDirStrategy(timestamp_format='%d_%H_%M')` 对齐。
- Collie 的 bugreport 抓取交互是“10 秒内输入 0 跳过，否则抓取”（见 `collie_package/log_class.py:BugReportHandler`）；Web contract 用 `BugreportPolicy(mode='capture'|'skip', cli_skip_window_sec=10)` 表达。
- ftrace 采集在 Collie 里写入 `/sys/kernel/tracing/...`；能力探测需兼容部分设备使用 `/sys/kernel/debug/tracing` 的情况，contract 探测按这两个 base 依次判断。
- 降级规则需要把 skip 原因写入 manifest，便于前端/调用方稳定展示（例如 `root_not_available`、`tracing_not_supported`、`missing_node:/sys/...`）。
- 新增 pipeline runner 集成测试时，固定 `when` 生成稳定 timestamp，可直接断言 `logcat_{timestamp}.txt` 等产物文件名。

- 冷启动/驻留 HTML 报告如果引用 Chart.js，必须保证离线：用相对路径 `<script src="chart.min.js"></script>`，并在生成报告时把 `chart.min.js` 写到同一输出目录。
- 基于 logcat 的可视化报告（`process_report.html`）同样要移除 CDN；测试里可以额外断言所有产出的 `.html` 不包含 `http(s)://`/`cdn`/`unpkg`。
- basedpyright/pyright 在本仓库里对 `# pyright: ignore`（不带规则）会报诊断；更稳妥是用文件头 `# pyright: reportXXX=false,...` 精确关闭触发的规则（例如 `reportAttributeAccessIssue`、`reportPossiblyUnboundVariable`）。

- Web utilities job 接入 `cont_startup_stay` 时，优先复用 `register_utilities_routes()` 作用域内的 `adb_exec`，并用包装器在每次 `adb_exec.run/run_host` 前调用 `_wait_if_paused()` + `_check_cancel()`，可让 pause/resume/cancel 至少在 ADB 调用边界生效。
- `rd_selftest.cont_startup_stay_runner.run_cont_startup_stay()` 会自己写入 `artifacts_manifest.json` + `冷启动分析报告.html`；若任务在执行中被取消，可在外层捕获异常后覆写 manifest 的 `result=cancelled` 来记录取消状态。
- RD 子标签可直接复用现有 utilities job 面板（status/progress/log/files），新增 `cont_startup_stay` 专属参数区与按钮即可无侵入接入现有轮询逻辑。
- 为实现“报告预览”且不影响下载语义，新增 `/api/utilities/preview/<job_id>/<filename>`（仅允许 html/txt/json/log），而下载继续走 `/api/utilities/download/...`。
- 2026-02-14: RD 工具的日志面板可通过在浏览器控制台内设置 `utilityJobId` + 调用 `_pollUtilityJob()` 来重新拉取指定 job 的历史状态；配合 `toolLogContainer` 强制展开可离线重现 UI 状态用于截图或调试。
## 2026-02-14
- Playwright 在该 Web 页截取 fullPage 屏幕会超时，改用默认视口截图即可。

