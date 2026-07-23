#!/bin/bash

# Longbridge Quant Console 启动脚本
# 使用方法：./start.sh

set -e  # 遇到错误立即退出

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "🚀 启动 Longbridge Quant Console..."
echo "================================================"

# 检查系统环境
check_requirements() {
    echo "📋 检查系统环境..."

    # 检查 Python
    if ! command -v python3 &> /dev/null; then
        echo "❌ 错误: 未找到 Python3"
        exit 1
    fi
    echo "✅ Python3: $(python3 --version)"

    # 检查 Node.js
    if ! command -v node &> /dev/null; then
        echo "❌ 错误: 未找到 Node.js"
        exit 1
    fi
    echo "✅ Node.js: $(node --version)"

    # 检查 npm
    if ! command -v npm &> /dev/null; then
        echo "❌ 错误: 未找到 npm"
        exit 1
    fi
    echo "✅ npm: $(npm --version)"

    echo ""
}

# 检查后端环境
check_backend() {
    echo "📊 检查后端环境..."

    if [ ! -d "backend/.venv" ]; then
        echo "⚠️  警告: 未找到虚拟环境，将自动创建: backend/.venv"
        python3 -m venv backend/.venv
    fi
    echo "✅ 虚拟环境存在"

    if [ ! -f "backend/.venv/.deps_installed" ] || [ "backend/pyproject.toml" -nt "backend/.venv/.deps_installed" ]; then
        echo "📦 安装后端依赖..."

        backend/.venv/bin/python -m pip install -U pip

        if ! backend/.venv/bin/python -m pip install -e backend; then
            echo "❌ 错误: 后端依赖安装失败，请检查 backend/pyproject.toml"
            exit 1
        fi

        touch backend/.venv/.deps_installed
        echo "✅ 后端依赖已安装"
        echo ""
    fi

    echo ""
}

# 检查前端环境
check_frontend() {
    echo "🎨 检查前端环境..."

    if [ ! -d "frontend/node_modules" ]; then
        echo "⚠️  警告: 未找到 node_modules，将自动安装依赖..."
        cd frontend
        npm install
        cd ..
    else
        echo "✅ 前端依赖已安装"
    fi

    echo ""
}

# 启动后端服务
start_backend() {
    echo "📊 启动后端服务器..."
    cd backend

    # 激活虚拟环境
    source .venv/bin/activate

    # 检查端口是否被占用
    if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null; then
        echo "❌ 端口 8000 已被占用，请先运行 ./stop.sh 或检查占用进程"
        exit 1
    fi

    # 启动后端（后台运行）
    echo "🔄 启动 FastAPI 服务器..."
    UVICORN_ENV_FILE=()
    if [ -f ".longbridge.env" ]; then
        UVICORN_ENV_FILE=(--env-file .longbridge.env)
    elif [ -f ".longport.env" ]; then
        UVICORN_ENV_FILE=(--env-file .longport.env)
    fi
    nohup uvicorn app.main:app --host 127.0.0.1 --port 8000 "${UVICORN_ENV_FILE[@]}" > ../logs/backend.log 2>&1 &
    BACKEND_PID=$!
    echo "$BACKEND_PID" > ../logs/backend.pid

    # 等待服务启动
    echo "⏳ 等待后端服务启动..."
    for i in {1..15}; do
        if curl --fail --silent --show-error --max-time 2 http://localhost:8000/health > /dev/null; then
            echo "✅ 后端服务启动成功 (PID: $BACKEND_PID)"
            break
        fi
        if [ $i -eq 15 ]; then
            echo "❌ 后端服务启动超时"
            kill "$BACKEND_PID" 2>/dev/null || true
            exit 1
        fi
        sleep 1
    done

    cd ..
}

# 启动前端服务
start_frontend() {
    echo "🎨 启动前端界面..."
    cd frontend

    # 检查端口是否被占用
    if lsof -Pi :5173 -sTCP:LISTEN -t >/dev/null; then
        echo "❌ 端口 5173 已被占用，请先运行 ./stop.sh 或检查占用进程"
        exit 1
    fi

    # 设置后端地址
    export VITE_API_BASE=http://127.0.0.1:8000

    # 启动前端（后台运行）
    echo "🔄 启动 Vite 开发服务器..."
    nohup ./node_modules/.bin/vite > ../logs/frontend.log 2>&1 &
    FRONTEND_PID=$!
    echo "$FRONTEND_PID" > ../logs/frontend.pid

    # 等待服务启动
    echo "⏳ 等待前端服务启动..."
    for i in {1..15}; do
        if curl --fail --silent --show-error --max-time 2 http://localhost:5173 > /dev/null; then
            echo "✅ 前端服务启动成功 (PID: $FRONTEND_PID)"
            break
        fi
        if [ $i -eq 15 ]; then
            echo "❌ 前端服务启动超时"
            kill "$FRONTEND_PID" 2>/dev/null || true
            exit 1
        fi
        sleep 1
    done

    cd ..
}

# 显示启动信息
show_info() {
    echo ""
    echo "🎉 系统启动完成！"
    echo "================================================"
    echo "📊 后端服务: http://localhost:8000"
    echo "🎨 前端界面: http://localhost:5173"
    echo "📖 API文档: http://localhost:8000/docs"
    echo "❤️  健康检查: http://localhost:8000/health"
    echo ""
    echo "💡 使用说明:"
    echo "1. 打开浏览器访问 http://localhost:5173"
    echo "2. 进入「基础配置」页面配置 Longbridge API 凭据"
    echo "3. 添加要监控的股票代码"
    echo "4. 按需使用「AI 交易」「智能选股」等功能"
    echo ""
    echo "📝 日志文件:"
    echo "   后端: logs/backend.log"
    echo "   前端: logs/frontend.log"
    echo ""
    echo "🛑 停止服务: 按 Ctrl+C 或运行 ./stop.sh"
    echo "================================================"
}

# 优雅关闭函数
cleanup() {
    local exit_code=$?
    trap - EXIT SIGINT SIGTERM
    cd "$ROOT_DIR"
    echo ""
    echo "🛑 正在停止服务..."

    # 停止后端
    if [ -n "${BACKEND_PID:-}" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "📊 停止后端服务 (PID: $BACKEND_PID)"
        kill "$BACKEND_PID"
    fi

    # 停止前端
    if [ -n "${FRONTEND_PID:-}" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "🎨 停止前端服务 (PID: $FRONTEND_PID)"
        kill "$FRONTEND_PID"
    fi

    rm -f logs/backend.pid logs/frontend.pid

    echo "✅ 服务已停止"
    exit "$exit_code"
}

# 主执行流程
main() {
    # 创建日志目录
    mkdir -p logs

    # 设置信号处理
    trap cleanup EXIT SIGINT SIGTERM

    # 执行检查和启动
    check_requirements
    check_backend
    check_frontend
    start_backend
    start_frontend
    show_info

    # 保持脚本运行
    echo "🔄 服务正在运行中... (按 Ctrl+C 停止)"
    while true; do
        # 检查进程是否还在运行
        if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
            echo "❌ 后端进程异常退出"
            cleanup
        fi

        if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
            echo "❌ 前端进程异常退出"
            cleanup
        fi

        sleep 5
    done
}

# 检查是否在正确的目录
if [ ! -f "backend/app/main.py" ] || [ ! -f "frontend/package.json" ]; then
    echo "❌ 错误: 请在 longbridge-quant-console 根目录下运行此脚本"
    exit 1
fi

# 执行主函数
main
