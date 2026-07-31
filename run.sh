#!/bin/bash
# Market Advisor Web 启动脚本
cd "$(dirname "$0")"
echo "=================================="
echo "  Market Advisor Web 分析系统"
echo "=================================="
echo "启动中..."
echo "访问地址: http://localhost:5000"
echo "按 Ctrl+C 停止"
echo ""
python3 app.py
