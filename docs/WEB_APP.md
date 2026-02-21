# Collie Bugreport Web Analyzer

基于 bugreport 的智能分析 Web 应用，从原 Collie 工具中提取并独立部署。

## 功能特性

- 📁 支持上传 `.txt` 或 `.zip` bugreport 文件
- 📊 三种预设分析场景：
  - 🚀 动态性能模型（TOP20 应用）
  - 📱 九大场景-驻留
  - ⚙️ 自定义应用列表
- 📈 实时显示分析进度
- 🌐 生成可视化 HTML 报告
- 📝 生成文本分析报告
- 📱 提取设备信息
- 📋 历史任务管理
- 🌐 支持局域网访问

## 快速开始

### 1. 安装依赖

```bash
cd web_app
pip3 install -r requirements.txt
```

### 2. 启动服务

```bash
# 方式一：使用启动脚本
./start.sh

# 方式二：直接运行
python3 app.py
```

### 3. 访问应用

启动后会显示访问地址：

```
本地访问: http://127.0.0.1:5000
局域网访问: http://<你的IP>:5000
```

## 使用说明

1. **上传文件**：点击上传区域或拖拽文件到上传区域，支持 `.txt` 或 `.zip` 格式的 bugreport
2. **选择场景**：
   - 动态性能模型：分析 TOP20 应用的启动性能
   - 九大场景-驻留：分析后台驻留能力
   - 自选应用：自定义需要分析的应用列表
3. **开始分析**：点击"开始分析"按钮，等待分析完成
4. **查看结果**：分析完成后，可以查看可视化报告、下载文本报告或查看设备信息

## 文件说明

```
web_app/
├── app.py                  # Flask 应用主文件
├── start.sh               # 启动脚本
├── requirements.txt       # Python 依赖
├── templates/
│   └── index.html        # 前端页面
├── uploads/              # 上传文件存储目录
└── results/              # 分析结果存储目录
```

## API 接口

- `POST /api/upload` - 上传 bugreport 文件
- `POST /api/analyze` - 启动分析任务
- `GET /api/status/<task_id>` - 获取任务状态
- `GET /api/presets` - 获取所有预设配置
- `GET /api/tasks` - 获取任务列表
- `DELETE /api/tasks/<task_id>` - 删除任务

## 技术栈

- 后端：Flask + Python 3.7+
- 前端：原生 HTML5 + CSS3 + JavaScript
- 解析引擎：复用 Collie 原有的 parse_cont_startup 模块

## 架构说明

- `web_app` 作为前端入口与 API 调度层
- 核心业务逻辑下沉到 `src/collie_package`（如 simpleperf、cont_startup_stay、监控与采集任务）
- Web 仅做参数校验、任务状态维护与结果展示

## 注意事项

1. 确保项目根目录的 `src/collie_package` 存在且可访问
2. 上传文件大小限制为 500MB
3. 分析过程中请勿关闭浏览器窗口
4. 局域网访问需要确保防火墙允许 5000 端口

## 配置说明

可通过修改 `app.py` 中的配置项来自定义：

```python
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 最大文件大小（默认500MB）
ALLOWED_EXTENSIONS = {'txt', 'zip'}      # 允许的文件类型
```

## simpleperf 配置说明（服务器端）

simpleperf 抓取与解析均发生在服务器端，需配置以下之一：

1. 使用服务器上的 Android NDK（推荐）

设置 `web_app/config/app.yaml`：

```yaml
utilities_webadb:
  simpleperf:
    ndk_path: "/path/to/ndk"
```

2. 使用服务器上的 simpleperf 资源目录（必须）

通过环境变量指定：

```bash
export SIMPLEPERF_ROOT=/path/to/simpleperf
```

目录需包含：

```
bin/android/<arch>/simpleperf
report_html.py
report_html.js
simpleperf_report_lib.py
simpleperf_utils.py
bin/linux/x86_64/libsimpleperf_report.so
```

3. （可选）指定 readelf/readobj 路径

```bash
export LLVM_READELF=/path/to/llvm-readelf
export LLVM_READOBJ=/path/to/llvm-readobj
```

> 以上配置均在服务器端生效，局域网用户仅通过浏览器访问，无需配置。

## 与原有 CLI 工具的关系

本 Web 应用完全复用了原 Collie 工具中的解析逻辑，只是将交互方式从命令行改为 Web 界面。分析结果与原工具完全一致。
