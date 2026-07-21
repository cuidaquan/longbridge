#!/bin/bash

# Longbridge Quant Console 停止脚本
# 使用方法：./stop.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "🛑 停止 Longbridge Quant Console..."
echo "================================================"

terminate_pid() {
    local pid="$1"
    [ -z "$pid" ] && return
    kill -0 "$pid" 2>/dev/null || return

    local children
    children=$(pgrep -P "$pid" 2>/dev/null || true)
    kill "$pid" 2>/dev/null || true

    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 1
    done

    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    for child in $children; do
        if kill -0 "$child" 2>/dev/null; then
            kill -9 "$child" 2>/dev/null || true
        fi
    done
}

stop_pid_file() {
    local file="$1"
    local expected="$2"
    if [ -f "$file" ]; then
        local pid
        pid=$(cat "$file")
        local command
        command=$(ps -p "$pid" -o command= 2>/dev/null || true)
        if [ -z "$command" ] || [[ "$command" != *"$expected"* ]]; then
            echo "   忽略过期 PID 文件: $file"
            rm -f "$file"
            return 1
        fi
        echo "   停止进程 PID: $pid"
        terminate_pid "$pid"
        rm -f "$file"
        return 0
    fi
    return 1
}

# 停止后端进程
stop_backend() {
    echo "📊 停止后端服务..."

    if stop_pid_file "logs/backend.pid" "uvicorn app.main:app"; then
        echo "✅ 后端服务已停止"
        return
    fi

    BACKEND_PIDS=$(pgrep -f "[u]vicorn app.main:app" || true)
    if [ -n "$BACKEND_PIDS" ]; then
        for pid in $BACKEND_PIDS; do
            echo "   停止进程 PID: $pid"
            terminate_pid "$pid"
        done
        echo "✅ 后端服务已停止"
    else
        echo "ℹ️  后端服务未运行"
    fi
}

# 停止前端进程
stop_frontend() {
    echo "🎨 停止前端服务..."

    if stop_pid_file "logs/frontend.pid" "node_modules/.bin/vite"; then
        echo "✅ 前端服务已停止"
        return
    fi

    FRONTEND_PIDS=$(pgrep -f "[n]ode .*node_modules/.bin/vite" || true)
    if [ -n "$FRONTEND_PIDS" ]; then
        for pid in $FRONTEND_PIDS; do
            echo "   停止进程 PID: $pid"
            terminate_pid "$pid"
        done
        echo "✅ 前端服务已停止"
    else
        echo "ℹ️  前端服务未运行"
    fi
}

# 检查端口占用
check_ports() {
    echo "🔍 检查端口占用情况..."

    # 检查后端端口 8000
    if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "⚠️  端口 8000 仍被占用:"
        lsof -Pi :8000 -sTCP:LISTEN
        echo "   运行以下命令手动清理: sudo lsof -ti :8000 | xargs kill -9"
    else
        echo "✅ 端口 8000 已释放"
    fi

    # 检查前端端口 5173
    if lsof -Pi :5173 -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "⚠️  端口 5173 仍被占用:"
        lsof -Pi :5173 -sTCP:LISTEN
        echo "   运行以下命令手动清理: sudo lsof -ti :5173 | xargs kill -9"
    else
        echo "✅ 端口 5173 已释放"
    fi
}

# 清理临时文件
cleanup_temp() {
    echo "🧹 清理临时文件..."

    # 清理 Python 缓存
    find backend -name "*.pyc" -delete 2>/dev/null || true
    find backend -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    # 清理日志文件（可选）
    if [ "$1" = "--clean-logs" ]; then
        echo "   清理日志文件..."
        rm -f logs/backend.log logs/frontend.log
    fi

    echo "✅ 临时文件清理完成"
}

# 显示状态
show_status() {
    echo ""
    echo "📊 服务状态检查:"
    echo "================================================"

    # 检查后端服务
    if curl -s http://localhost:8000/health >/dev/null 2>&1; then
        echo "❌ 后端服务仍在运行: http://localhost:8000"
    else
        echo "✅ 后端服务已停止"
    fi

    # 检查前端服务
    if curl -s http://localhost:5173 >/dev/null 2>&1; then
        echo "❌ 前端服务仍在运行: http://localhost:5173"
    else
        echo "✅ 前端服务已停止"
    fi

    echo ""
}

# 主执行函数
main() {
    stop_backend
    stop_frontend
    check_ports
    cleanup_temp "$1"
    show_status

    echo "🎉 Longbridge Quant Console 已完全停止"
    echo "================================================"
    echo ""
    echo "💡 重新启动请运行: ./start.sh"
    echo "🧹 完全清理请运行: ./stop.sh --clean-logs"
    echo ""
}

# 显示帮助信息
show_help() {
    echo "用法: ./stop.sh [选项]"
    echo ""
    echo "选项:"
    echo "  --clean-logs    同时清理日志文件"
    echo "  --help         显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  ./stop.sh                # 停止所有服务"
    echo "  ./stop.sh --clean-logs   # 停止服务并清理日志"
    echo ""
}

# 检查命令行参数
case "$1" in
    --help)
        show_help
        exit 0
        ;;
    *)
        main "$1"
        ;;
esac
