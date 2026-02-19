#!/bin/bash

# Collie Bugreport Web Analyzer 启动脚本

cd "$(dirname "$0")"

echo "=========================================="
echo "Collie Bugreport Web Analyzer"
echo "=========================================="
echo ""

if [ -n "$MIFY_API_KEY" ] || [ -n "$OPENAI_API_KEY" ] || [ -n "$AZURE_OPENAI_API_KEY" ]; then
    echo "✓ 已检测到至少一个 LLM API Key（已隐藏）"
else
    echo "⚠ 未检测到 LLM API Key，请先在 web_app/config/app.yaml 填写配置或提供环境变量。"
fi
echo ""

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 Python3"
    exit 1
fi

# 检查Flask
if ! python3 -c "import flask" 2>/dev/null; then
    echo "正在安装依赖..."
    pip3 install -r requirements.txt
fi

echo "启动 Web 服务..."
echo ""

# 启动应用
python3 app.py
