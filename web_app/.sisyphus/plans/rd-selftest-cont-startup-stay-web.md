# 研发自测：连续启动-驻留（cont_startup_stay）Web 迁移计划（全量复刻 + 可降级 + 系统性测试）

## TL;DR

> **Quick Summary**: 在现有 OpenCollies `web_app` 的“研发自测”Tab 下新增“连续启动-驻留”模块，复刻 collie CLI 的参数与产物/报告；后端复用 `utilities_webadb.py` 的长任务 job 基建，并补齐“设备绑定 ADB 执行器 + 能力探测/降级 + 产物 manifest + 离线可用 HTML 报告”。
>
> **Deliverables**:
> - RD 新子标签页：连续启动-驻留（完整参数表单 + 任务进度/日志/产物下载/预览）
> - 后端新 action：`cont_startup_stay`（服务端 ADB 跑机，支持 pause/resume/cancel）
> - 产物结构与 `artifacts_manifest.json`（可机读 + 人读，含降级原因）
> - HTML 报告离线化（移除 Chart.js CDN 依赖）
> - 系统性测试基建：pytest 单测 + API 集成测试（mock ADB），并保留真机 Agent-Executed QA
>
> **Estimated Effort**: XL
> **Parallel Execution**: YES - 3 waves
> **Critical Path**: 测试基建/可测试性改造 → ADB 设备绑定执行器 → cont_startup_stay pipeline（collect/run/parse/report）→ Web UI 接入

---

## Context

### Original Request
- 将 collie 的“自动化测试-连续启动-驻留”适配到该 web 项目中，位置在“研发自测模块”
- 需要完全理解原生代码实现与效果；尽可能保留原 collie 的全部效果
- 源目录只读：`/media/mi/ssd/安装包/collie`（可复制到当前项目再修改）

### Confirmed Decisions
- `.sisyphus` 根目录以 `.../OpenCollies/web_app/` 为准
- 首版范围：完全复刻 collie CLI（参数/交互项尽量完整搬到 Web）
- Root/权限策略：自动探测并降级（无 root/无节点时跳过对应采集器并在报告中标注）
- 验证策略：系统性测试基建（单测 + 集成测试），并辅以真机 Agent-Executed QA

### Research Findings (code map)

**Web 接入点（现状）**
- 单页前端：`web_app/templates/index.html` 已有“研发自测”主 Tab（RD 子标签包括：存活监控/存活检查/性能工具）
- 长任务 job 基建：`web_app/utilities_webadb.py`（`POST /api/utilities/run` + job status/logs/download/cancel/pause/resume；落盘 `user_data/{ip}/utilities/{job_id}/`）

**collie cont_startup_stay（源实现）**
- 入口：`/media/mi/ssd/安装包/collie/src/collie_package/automation/cont_startup_stay.py:main`
- 关键依赖：`collector_pipeline.py`, `log_collectors.py`, `startup_runner.py`, `post_run_parser.py`, `startup_reporting.py`, `pre_start.py`, `log_tools/log_analyzer.py`
- 关键交互：输出目录、包列表选择（app_config.json/自定义 JSON 路径）、采集开关逐项询问、预处理开关、开始前确认、bugreport 10s 可跳过
- 关键产物：console/logcat/memcat/meminfo/vmstat/greclaim/process_use_count/oomadj/ftrace + 归档目录 + `冷启动分析报告.html` + bugreport zip

### Metis Review (gaps addressed)
- 明确 guardrails：不改现有 `/api/utilities/run` 外部契约；新增能力尽量模块化
- 强制 ADB 设备绑定执行器：所有 adb 必须统一注入 `-s <serial>`；避免散落拼接
- 强制产物 manifest：`artifacts_manifest.json` 作为降级/验收/前端展示的稳定契约
- 报告离线化：禁止 Chart.js CDN 依赖

---

## Work Objectives

### Core Objective
在 OpenCollies Web 的“研发自测模块”中实现 `cont_startup_stay` 的可视化长任务运行与产物/报告输出，并在多设备/权限差异环境下可稳定降级，同时引入系统性测试保障回归。

### Concrete Deliverables
- 新 RD 子标签：连续启动-驻留（完整参数 UI + 任务控制 + 产物下载/预览）
- 新后端 action：`cont_startup_stay`（可暂停/继续/取消；明确错误与降级信息）
- 新模块代码（从 collie 复制/适配），但不修改源目录
- `artifacts_manifest.json` + 统一的产物目录结构与命名
- HTML 报告离线可打开（下载 zip 解压后本地打开也可用）
- pytest 测试基建：单测 + API 集成测试（mock ADB/capability）

### Must Have
- **CLI 参数/交互复刻**：collie CLI 的选择项（包列表、采集开关、预处理、bugreport 等）在 Web 里可配置并可复现实验
- **设备绑定**：任何 adb 调用都必须绑定到用户选择的 `device_id`
- **降级可解释**：root/节点/trace 不可用时，任务仍完成并在报告/manifest 明确标注缺失项与影响
- **可测试**：核心逻辑可在无真机环境下通过 mock ADB 跑通单测/集成测

### Must NOT Have (Guardrails)
- 不引入新的鉴权/登录系统（安全相关暂不修改）；但必须对 Web 入参做严格校验避免 shell 注入
- 不把 collie 其他 automation“顺手全搬”（只迁移 cont_startup_stay 所需链路）
- 不依赖公网资源（Chart.js CDN 等）

---

## Verification Strategy (MANDATORY)

> **UNIVERSAL RULE: ZERO HUMAN INTERVENTION**
>
> 本计划中的每个 TODO 都必须能被执行代理通过命令/工具验证；不得要求“用户手工确认/目测”。

### Test Decision
- **Infrastructure exists**: NO（仓库现状无 tests/、无 pytest.ini、无 CI）
- **Automated tests**: YES（系统性测试基建）
- **Framework**: pytest（单测 + Flask API 集成测试）

### Agent-Executed QA Scenarios (for real device)
除 pytest 外，仍要求真机 E2E（代理执行）：启动 web → 选择设备 → 启动作业 → 轮询状态 → 下载 zip → 校验 manifest/报告/关键产物存在与可打开。

---

## Execution Strategy

### Parallel Execution Waves

Wave 1 (Start Immediately):
├── Task 1: 测试基建与可测试性改造（pytest + app test client）
└── Task 2: 产物 manifest + 能力探测/降级契约定义（纯逻辑，可先行）

Wave 2 (After Wave 1):
├── Task 3: ADB 设备绑定执行器（生产 + mock）
└── Task 4: 复制/适配 collie cont_startup_stay pipeline（去交互 + 参数化）

Wave 3 (After Wave 2):
├── Task 5: Web 后端 action 接入 job 系统（pause/resume/cancel + 日志/产物）
└── Task 6: Web 前端 RD 子标签页与完整参数 UI + 预览/下载

Final:
└── Task 7: 端到端回归（pytest 全绿 + 真机 Agent QA + 报告离线化检查）

---

## TODOs

> Implementation + Test = ONE Task. Never separate.

- [x] 1. 建立 pytest 测试基建 + Web 可测试性入口

  **What to do**:
  - 新增 `tests/` 目录与 pytest 依赖（建议：`web_app/requirements-dev.txt` 或在现有依赖体系中引入 dev 依赖）
  - 确保 Flask app 能在测试中创建 test client（可能需要为 `web_app/app.py` 增加 `create_app(test_config=...)` 入口，或提供等价的测试挂载方式）
  - 提供临时目录 fixture（用于 job 输出与 artifacts 写入），避免污染真实 `user_data/`
  - 产出 1 个最小 API 冒烟测试：启动 app → `GET /api/adb/devices`（mock）返回结构稳定

  **Must NOT do**:
  - 不引入/修改鉴权系统

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: `git-master`（仅用于后续提交时，不用于实现）、（无强依赖技能）

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: 3, 4, 5, 7

  **References**:
  - `web_app/app.py` - Flask 主入口与路由注册方式
  - `web_app/utilities_webadb.py` - 现有蓝图/长任务 job 结构，测试需能隔离
  - `AGENTS.md` - 当前项目未配测试框架的背景与建议

  **Acceptance Criteria**:
  - [ ] `pytest -q` 可运行（至少 1 条测试 PASS）
  - [ ] 测试运行不写入真实 `web_app/user_data/`

  **Agent-Executed QA Scenarios**:
  ```
  Scenario: Run pytest baseline
    Tool: Bash
    Preconditions: 在 web_app 目录
    Steps:
      1. pip3 install -r requirements.txt
      2. pip3 install -r requirements-dev.txt (或等价 dev 依赖安装方式)
      3. pytest -q
    Expected Result: pytest exit code 0
    Evidence: 保存 pytest 输出到 .sisyphus/evidence/task-1-pytest.txt
  ```

- [x] 2. 定义 cont_startup_stay Web Job 契约：参数 schema + capability 探测 + artifacts manifest

  **What to do**:
  - 定义 `ContStartupStayConfig`（等价于 collie CLI 交互项的“结构化参数”）：
    - device_id（必填）
    - 输出命名/时间戳策略
    - app list 选择：preset name + 自定义 JSON（上传内容）
    - collectors 开关：logcat/memcat/meminfo/vmstat/greclaim_parm/process_use_count/oomadj/ftrace
    - pre_start 开关
    - bugreport 开关 + 可选“10s 自动跳过/总是跳过/总是抓取”的等价策略
  - 定义 capability 探测输出：root 可用性、/sys 节点是否存在、/sys/kernel/tracing 是否可读写、simple adb 是否可用、设备状态（offline/unauthorized）
  - 定义降级规则（必须写入 manifest）：哪些 collectors 因何被跳过、影响是什么
  - 定义 `artifacts_manifest.json` 内容：
    - config（去敏/可复现）
    - capabilities + degrade reasons
    - artifacts 列表（文件名、描述、大小、生成状态、校验）
    - summary（冷/热启动统计、residency、kill/lmk 汇总是否可用）

  **Must NOT do**:
  - 不把“降级逻辑”做成静默跳过（必须可解释、可机读）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: （无）

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: 4, 5, 6, 7

  **References**:
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/cont_startup_stay.py` - 原交互项来源
  - `/media/mi/ssd/安装包/collie/src/collie_package/tools.py` - app_config 选择与 get_log_setting prompt
  - `web_app/utilities_webadb.py` - 现有 job 状态/产物落盘模型

  **Acceptance Criteria**:
  - [ ] 单测覆盖：capability 探测在“无 root/无节点”时输出可预测 degrade
  - [ ] `artifacts_manifest.json` 在 job 结束时必定存在（成功/失败都要写）

  **Agent-Executed QA Scenarios**:
  ```
  Scenario: Validate manifest schema with unit tests
    Tool: Bash
    Steps:
      1. pytest -q -k manifest
    Expected Result: exit code 0
  ```

- [x] 3. 实现 ADB 设备绑定执行器（生产 + mock），统一注入 `-s <device_id>` 并阻断注入风险

  **What to do**:
  - 新增一个“唯一入口”的 ADB 执行器（建议单独模块），所有 adb 调用走它：
    - 强制 `device_id`，并在命令中注入 `adb -s <id> ...`
    - 禁止 shell=True；所有 args 使用 list；对包名/路径做白名单校验
    - 支持超时、stdout/stderr 捕获、日志落盘
  - 提供 mock/fake ADB 层用于 pytest（可模拟：无设备、offline、unauthorized、root 失败、节点不存在、命令超时）

  **Must NOT do**:
  - 不允许前端透传任意 shell 片段到后端

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: （无）

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 2
  - **Blocked By**: 1, 2
  - **Blocks**: 4, 5, 7

  **References**:
  - `web_app/utilities_webadb.py` - 当前 adb 调用点与 device_id 传递方式（需要统一收敛）
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/log_collectors.py` - 多处未带 -s 的 adb 调用（迁移时必须改）

  **Acceptance Criteria**:
  - [ ] 单测：任意 adb 调用在缺 device_id 时直接拒绝（抛 4xx/异常）
  - [ ] 单测：构建命令时 `-s <device_id>` 必定存在
  - [ ] 集成测：mock ADB 返回 offline/unauthorized 时 job 进入 failed，并写 manifest

- [x] 4. 复制并适配 collie cont_startup_stay pipeline：去交互 + 参数化 + 可降级 collectors

  **What to do**:
  - 从只读源复制所需模块到当前项目（禁止修改源目录）：
    - `/media/mi/ssd/安装包/collie/src/collie_package/automation/` 下相关文件
    - 相关 `log_tools/log_analyzer.py`、`parse_direct_reclaim.py`、`parse_kswapd.py`（按依赖最小集）
  - 改造为“非交互式函数调用”：所有 `input()`/prompt 改为读取 `ContStartupStayConfig`
  - 所有 collectors 通过 capability 探测决定启用/降级，并把跳过原因写入 manifest
  - 修复/统一 `kill_summary` 命名不一致：
    - 方案 A：固定 `log_analyzer.analyze_log_file(... output_name="process_report")`
    - 方案 B：改 `_read_process_summary()` 去读最新的 `{timestamp}.txt`
    - 二选一并加测试断言
  - 报告离线化：将 `startup_reporting.generate_html_report()` 的 Chart.js CDN 替换为本地资源或内嵌脚本
  - memcat 相关：若第三方 memcat 不可用，按“可降级”策略处理（报告中显示缺失原因）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: （无）

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 2
  - **Blocked By**: 1, 2, 3
  - **Blocks**: 5, 7

  **References**:
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/cont_startup_stay.py` - 主流程
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/collector_pipeline.py` - collectors 生命周期
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/startup_runner.py` - 两轮启动 + residency
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/post_run_parser.py` - 归档/解析/diff
  - `/media/mi/ssd/安装包/collie/src/collie_package/automation/startup_reporting.py` - HTML 报告（含 iframe memcat）
  - `/media/mi/ssd/安装包/collie/src/collie_package/log_tools/log_analyzer.py` - kill/lmk 分类与 highLight

  **Acceptance Criteria**:
  - [ ] pytest：无需真机即可跑通“pipeline dry-run”（mock ADB）并生成完整目录结构 + manifest + HTML
  - [ ] HTML 报告打开时不访问公网资源（无 CDN 依赖）

- [x] 5. 在现有 job 系统中接入新 action：`cont_startup_stay`（RD 自测长任务）

  **What to do**:
  - 在 `POST /api/utilities/run` 的 action 分发中新增 `cont_startup_stay`
  - job 状态机：queued/running/paused/completed/error/cancelled；确保 pause/resume/cancel 在 pipeline 各阶段可响应
  - 日志：阶段化日志（collect/run/parse/report/bugreport）写入 stdout/stderr，并可前端增量拉取
  - 产物：列出可下载文件；提供 zip 打包下载（含 manifest）
  - 清理策略：为 `user_data/{ip}/utilities/{job_id}` 增加可配置 TTL/空间上限的清理（默认可先只记录 TODO + 手动清理命令，但建议落地自动清理）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: （无）

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3
  - **Blocked By**: 4
  - **Blocks**: 6, 7

  **References**:
  - `web_app/utilities_webadb.py` - job 基建与 action 分发、日志与下载接口
  - `web_app/app.py` - 现有 schedule 清理逻辑（对齐 utilities 清理）

  **Acceptance Criteria**:
  - [ ] API 集成测试：POST run → 轮询 status → 下载 zip → unzip 后存在 manifest + HTML
  - [ ] cancel：中途取消后 job 状态为 cancelled，且 manifest 记录取消原因
  - [ ] pause/resume：paused 时不继续推进阶段；resume 后继续

- [x] 6. 前端新增 RD 子标签“连续启动-驻留”：完整参数表单 + 任务控制 + 报告预览/下载

  **What to do**:
  - 在 `web_app/templates/index.html` 的 RD tab 增加一个 subtab（例如 `cont_startup_stay`）
  - 复刻 CLI 参数项的 UI：
    - 设备选择（复用现有 device strip）
    - app list：preset 下拉 + 自定义 JSON 上传/粘贴
    - collectors 勾选项（默认全开，按 capability 降级结果提示）
    - pre_start/bugreport 开关
    - “开始前确认”用 confirm-check 流程复用（代理可通过 API 自动 confirm 以满足无人工验收）
  - 任务面板：实时进度/状态、stdout/stderr tail、产物列表（含 `冷启动分析报告.html` 一键预览/下载）

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: `frontend-ui-ux`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3（在后端 action 稳定后联调）
  - **Blocked By**: 5
  - **Blocks**: 7

  **References**:
  - `web_app/templates/index.html` - 现有 RD 子标签结构与 `runUtilityActionWithPayload(...)` 调用方式
  - `web_app/utilities_webadb.py` - jobs API（status/logs/download）

  **Acceptance Criteria**:
  - [x] 前端可启动 `cont_startup_stay` job，并能看到状态从 running → completed/error
  - [x] 前端可预览/下载 HTML 报告与 zip

  **Agent-Executed QA Scenarios**:
  ```
  Scenario: Start cont_startup_stay from RD UI
    Tool: Playwright
    Preconditions: web_app running on http://127.0.0.1:5000 ; adb devices 至少 1 台可用
    Steps:
      1. Navigate to http://127.0.0.1:5000/
      2. Click top tab for RD (研发自测)
      3. Click subtab 连续启动-驻留
      4. Select device from #toolDeviceSelect
      5. Select preset app list
      6. Click Start
      7. Wait for job status to show running
      8. Wait for job status to show completed (timeout 30m)
      9. Click Download zip; assert file downloaded
      10. Screenshot evidence
    Evidence: .sisyphus/evidence/task-6-rd-cont-startup.png
  ```

- [ ] 7. 全链路回归：pytest 全绿 + 真机跑通 + 离线报告检查

  **What to do**:
  - pytest：单测覆盖核心降级矩阵、命令构建、manifest、输出命名稳定性
  - API 集成测：mock ADB 端到端（run→status→download→unzip 断言）
  - 真机 Agent QA：跑 1 个小 app list（比如 2-3 个包）验证产物齐全、HTML 可打开、manifest 合理
  - 离线检查：断网环境（或模拟）打开 `冷启动分析报告.html` 不报错、不请求外网

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: `playwright`

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Final
  - **Blocked By**: 1, 2, 3, 4, 5, 6

  **Acceptance Criteria**:
  - [ ] `pytest -q` → PASS
  - [ ] 真机跑通 1 次：job completed；zip 中至少包含 manifest + HTML + logcat + console
  - [ ] 无 root 环境：job 仍 completed（degraded），manifest 标注跳过 ftrace/pre_start

---

## Commit Strategy

（执行阶段由实施代理决定是否拆分提交；建议按波次提交，避免大杂烩）

---

## Success Criteria

### Verification Commands
```bash
# 在 web_app 目录
pytest -q

# 启动服务后（示例）
curl -s http://127.0.0.1:5000/api/adb/devices
```

### Final Checklist
- [ ] RD Tab 下出现“连续启动-驻留”，可配置完整参数并启动任务
- [ ] 任务产物目录结构稳定，且 zip 下载包含 `artifacts_manifest.json`
- [ ] HTML 报告离线可用（无 CDN 依赖）
- [ ] pytest 单测 + API 集成测试可在无真机环境下运行
- [ ] 真机 Agent QA 可复现 collie 关键效果（冷/热判定、residency 统计、归档目录、bugreport 可选）
