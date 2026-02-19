# Issues

Append-only. Track blockers and failures with timestamps.

## 2026-02-14

- `pytest -q` 初次收集失败：`ModuleNotFoundError: rd_selftest`；通过在 `tests/conftest.py` 里插入项目根目录到 `sys.path` 解决。
- `rd_selftest/cont_startup_stay_contract.py` 运行时报 `NameError: asdict` 且缺少若干 typing 名称；补全 `dataclasses.asdict` 与 `typing` 导入后测试恢复。
- HTML 文件 `lsp_diagnostics` 在当前环境受 Biome/Node 版本影响初始化失败（`Unexpected token .`）；需升级 Node 运行时或切换可兼容的 Biome 版本后再做前端 LSP 级校验。
- 2026-02-14: Playwright MCP `browser_take_screenshot` 一直 timeout（等待元素稳定），无法产出 PNG；改用独立 Playwright CLI 才能捕获页面。
- 2026-02-14: `cont_startup_stay_artifacts.zip` 仍输出 `stdout.log` 而不是 manifest 中声明的 `console_{timestamp}.log`，需后续统一命名。
## 2026-02-14
- Playwright fullPage 截图 60s 超时，需要缩小为默认视口。

