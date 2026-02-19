# Collie 项目目录结构说明

本文档用于说明当前仓库的目录结构与作用。

## 顶层目录

- `AGENTS.md`：项目协作规范（保留在根目录，便于工具识别）。
- `docs/`：统一文档目录（README、部署、配置说明等）。
- `src/`：Collie CLI/核心逻辑源码。
- `web_app/`：Collie Web 应用（Flask）。

## docs/（文档）

- `README.md`：项目总体说明与目录结构（本文件）。
- `WEB_APP.md`：Web 端使用说明。
- `DEPLOY.md`：部署说明。
- `LLM_CONFIG.md`：LLM 配置说明。
- `MIFY_CONFIG.md` / `MIFY_QUICK_START.md`：Mify 相关配置与快速开始。
- `DATA_STRUCTURE.md`：数据结构与输出说明。

## src/（核心包）

- `src/collie_package/`：Collie 主要功能模块（CLI、解析、ADB 等）。
  - `log_tools/`：日志解析与报告生成。
  - `rd_selftest/`：连续启动/驻留测试相关逻辑。
  - `utilities/`：ADB 相关工具、设备信息等辅助工具。
  - `memory_models/`：内存相关模型与数据采集。
- `config/`：核心脚本配置目录（`app_list.yaml`、`rules.yaml`）。
- `config_loader.py`：统一配置加载器（读取 `src/collie_package/config/` 与 `web_app/config/`）。
- `app_config.json`：历史 JSON 配置（兼容回退，建议逐步废弃）。

## web_app/（Web 应用）

- `app.py`：Flask 入口，读取 `web_app/config/app.yaml`。
- `config/`：Web 配置目录（`app.yaml`）。
- `templates/`：前端模板（HTML）。
- `static/`：静态资源。
- `rd_selftest/`：Web 端复用的连续启动/驻留测试逻辑。
- `utilities_webadb.py`：Web 端 ADB 工具与设备 API。
- `results/`：分析结果输出目录（运行时生成）。
- `uploads/`：上传文件暂存目录（运行时生成）。
- `user_data/`：按 IP 隔离的数据目录（运行时生成）。
- `.sisyphus/`：运行态目录（工具内部使用）。
