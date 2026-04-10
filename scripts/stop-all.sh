#!/bin/bash
# 停止所有 Claude Hub 相关服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🛑 正在停止 Claude Hub 相关服务..."

# 从 PID 文件停止
if [ -f "$PROJECT_ROOT/tmp/backend.pid" ]; then
    PID=$(cat "$PROJECT_ROOT/tmp/backend.pid" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 $PID 2>/dev/null; then
        kill $PID 2>/dev/null
        echo "✅ 已停止后端 (PID: $PID)"
    fi
    rm -f "$PROJECT_ROOT/tmp/backend.pid"
fi

if [ -f "$PROJECT_ROOT/tmp/frontend.pid" ]; then
    PID=$(cat "$PROJECT_ROOT/tmp/frontend.pid" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 $PID 2>/dev/null; then
        kill $PID 2>/dev/null
        echo "✅ 已停止前端 (PID: $PID)"
    fi
    rm -f "$PROJECT_ROOT/tmp/frontend.pid"
fi

if [ -f "$PROJECT_ROOT/tmp/tunnel.pid" ]; then
    PID=$(cat "$PROJECT_ROOT/tmp/tunnel.pid" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 $PID 2>/dev/null; then
        kill $PID 2>/dev/null
        echo "✅ 已停止隧道 (PID: $PID)"
    fi
    rm -f "$PROJECT_ROOT/tmp/tunnel.pid"
fi

# 强制停止占用端口的进程
for PORT in 5173 8173; do
    PIDS=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "⚠️  强制停止端口 $PORT 上的进程: $PIDS"
        kill -9 $PIDS 2>/dev/null
    fi
done

# 停止 cloudflared 进程
CLOUDFLARED_PIDS=$(pgrep -f "cloudflared tunnel" 2>/dev/null)
if [ -n "$CLOUDFLARED_PIDS" ]; then
    echo "⚠️  停止 cloudflared 进程: $CLOUDFLARED_PIDS"
    kill $CLOUDFLARED_PIDS 2>/dev/null
    sleep 1
    pkill -9 -f "cloudflared tunnel" 2>/dev/null
fi

echo ""
echo "✅ 清理完成！"
