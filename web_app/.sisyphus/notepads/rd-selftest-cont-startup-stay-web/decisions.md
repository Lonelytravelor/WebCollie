# Decisions

Append-only. Record decisions that affect implementation.

## Confirmed
- .sisyphus root: /media/mi/ssd/安装包/OpenCollies/web_app
- Scope: fully replicate collie CLI parameters/interaction as much as possible
- Root strategy: auto-detect + degrade (skip collectors with clear reason)
- Verification: systematic test infrastructure (pytest unit + integration) + real-device agent QA

## 2026-02-14

- Contract 代码落地在 `rd_selftest/cont_startup_stay_contract.py`，只定义“参数 schema + capability 探测 + 降级规则 + artifacts_manifest”，不实现实际 runner（Task 4 再做）。
- capability 探测接口抽象为 `AdbLike.shell(cmd, timeout_sec)`，并通过 `sh -c "test -r/-d"` 做节点/目录探测，保证单测可用 FakeAdb 覆盖矩阵。
- artifacts manifest 采用 schema_version=1 的稳定结构：`config`/`capabilities`/`degradation`/`artifacts` + `status/result/error/traceback`，并提供 `run_and_write_manifest` 确保成功/失败都落盘。
- 采用 `FakeAdbExecutor` 覆盖 capability probe 与采集命令返回，避免真实 adb 依赖并保证 `run_cont_startup_stay` 测试完全可重复。
- 2026-02-14: 为保证证据可读性，截图阶段固定使用单独的 Playwright CLI 脚本（headless chromium），在脚本里复位 `utilityJobId` 并调用 `_pollUtilityJob()` 后再截全页图，绕过 MCP 超时限制。
