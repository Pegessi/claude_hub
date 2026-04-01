#!/bin/bash
# Claude Hub 一键启动临时隧道脚本
# 自动启动前后端 + Cloudflare Tunnel 临时域名
# 自动更新 .env 文件中的 URL

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "========================================="
echo "  Claude Hub - 一键启动临时隧道"
echo "========================================="
echo ""

# 检查 cloudflared
if ! command -v cloudflared &> /dev/null; then
    echo "❌ cloudflared 未安装"
    echo ""
    echo "安装方式："
    echo "  macOS:   brew install cloudflare/cloudflare/cloudflared"
    echo "  其他:    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    echo ""
    exit 1
fi

# 检查端口是否被占用
if lsof -ti:5173 &> /dev/null; then
    echo "⚠️  端口 5173 已被占用，请先停止前端服务"
    exit 1
fi

if lsof -ti:8000 &> /dev/null; then
    echo "⚠️  端口 8000 已被占用，请先停止后端服务"
    exit 1
fi

# 创建临时日志目录
mkdir -p "$PROJECT_ROOT/tmp"
BACKEND_LOG="$PROJECT_ROOT/tmp/backend.log"
FRONTEND_LOG="$PROJECT_ROOT/tmp/frontend.log"
TUNNEL_LOG="$PROJECT_ROOT/tmp/tunnel.log"

# 清理旧日志
rm -f "$BACKEND_LOG" "$FRONTEND_LOG" "$TUNNEL_LOG"

echo "🚀 启动后端..."
cd "$PROJECT_ROOT/backend"
if command -v uv &> /dev/null; then
    uv run uvicorn claude_hub.main:app --reload > "$BACKEND_LOG" 2>&1 &
    BACKEND_PID=$!
else
    echo "❌ uv 未安装，请先安装 uv"
    exit 1
fi

# 等待后端启动
sleep 3
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo "❌ 后端启动失败"
    echo "日志:"
    cat "$BACKEND_LOG"
    exit 1
fi
echo "✅ 后端已启动 (PID: $BACKEND_PID)"

echo ""
echo "🚀 启动前端..."
cd "$PROJECT_ROOT/frontend"
if command -v pnpm &> /dev/null; then
    pnpm dev > "$FRONTEND_LOG" 2>&1 &
    FRONTEND_PID=$!
else
    echo "❌ pnpm 未安装，请先安装 pnpm"
    kill $BACKEND_PID 2>/dev/null
    exit 1
fi

# 等待前端启动
sleep 5
if ! kill -0 $FRONTEND_PID 2>/dev/null; then
    echo "❌ 前端启动失败"
    echo "日志:"
    cat "$FRONTEND_LOG"
    kill $BACKEND_PID 2>/dev/null
    exit 1
fi
echo "✅ 前端已启动 (PID: $FRONTEND_PID)"

echo ""
echo "🚀 启动 Cloudflare Tunnel..."
cd "$PROJECT_ROOT"

# 启动 cloudflared 并捕获输出
cloudflared tunnel --url http://localhost:5173 > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# 等待隧道启动并获取 URL
echo "⏳ 等待隧道建立..."
sleep 5

# 提取 URL
TUNNEL_URL=$(grep -o "https://[^ ]*trycloudflare.com" "$TUNNEL_LOG" | head -1)

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ 未能获取隧道 URL"
    echo "日志:"
    cat "$TUNNEL_LOG"
    kill $BACKEND_PID $FRONTEND_PID $TUNNEL_PID 2>/dev/null
    exit 1
fi

# 自动更新 .env 文件
echo ""
echo "🔄 自动更新 .env 文件..."

update_env_file() {
    local env_file="$1"
    if [ ! -f "$env_file" ]; then
        echo "⚠️  $env_file 不存在，跳过"
        return
    fi

    # 更新 FRONTEND_URL
    if grep -q "^FRONTEND_URL=" "$env_file"; then
        sed -i.bak "s|^FRONTEND_URL=.*|FRONTEND_URL=$TUNNEL_URL|" "$env_file"
        rm -f "$env_file.bak"
    fi

    # 更新 FEISHU_REDIRECT_URI（如果配置了飞书）
    if grep -q "^FEISHU_REDIRECT_URI=" "$env_file"; then
        sed -i.bak "s|^FEISHU_REDIRECT_URI=.*|FEISHU_REDIRECT_URI=$TUNNEL_URL/api/auth/callback|" "$env_file"
        rm -f "$env_file.bak"
    fi

    echo "✅ 已更新 $env_file"
}

# 更新项目根目录的 .env
update_env_file "$PROJECT_ROOT/.env"

# 更新 backend 目录的 .env
update_env_file "$PROJECT_ROOT/backend/.env"

echo ""
echo "========================================="
echo "  🎉 全部启动成功！"
echo "========================================="
echo ""
echo "🌐 公网访问地址: $TUNNEL_URL"
echo ""
echo "📝 本地访问:"
echo "   前端: http://localhost:5173"
echo "   后端: http://localhost:8000"
echo ""
echo "📋 进程信息:"
echo "   后端 PID: $BACKEND_PID"
echo "   前端 PID: $FRONTEND_PID"
echo "   隧道 PID: $TUNNEL_PID"
echo ""
echo "📂 日志文件:"
echo "   后端: $BACKEND_LOG"
echo "   前端: $FRONTEND_LOG"
echo "   隧道: $TUNNEL_LOG"
echo ""
echo "⚠️  重要提示："
echo "   1. .env 文件已自动更新为新的临时域名"
echo "   2. 请在飞书开放平台更新重定向 URL 为："
echo "      $TUNNEL_URL/api/auth/callback"
echo "   3. 如果启用了飞书鉴权，需要重启后端服务"
echo ""
echo "⏹  按 Ctrl+C 停止所有服务"
echo ""

# 保存 PID 以便清理
echo "$BACKEND_PID" > "$PROJECT_ROOT/tmp/backend.pid"
echo "$FRONTEND_PID" > "$PROJECT_ROOT/tmp/frontend.pid"
echo "$TUNNEL_PID" > "$PROJECT_ROOT/tmp/tunnel.pid"

# 清理函数
cleanup() {
    echo ""
    echo "🛑 正在停止所有服务..."
    kill $BACKEND_PID $FRONTEND_PID $TUNNEL_PID 2>/dev/null
    wait $BACKEND_PID $FRONTEND_PID $TUNNEL_PID 2>/dev/null
    rm -f "$PROJECT_ROOT/tmp/backend.pid" "$PROJECT_ROOT/tmp/frontend.pid" "$PROJECT_ROOT/tmp/tunnel.pid"
    echo "✅ 所有服务已停止"
    exit 0
}

# 捕获中断信号
trap cleanup SIGINT SIGTERM

# 等待用户中断
wait
