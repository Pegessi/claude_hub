#!/bin/bash
# Cloudflare Tunnel 运行脚本
# 用于启动 Cloudflare Tunnel 以实现公网访问

set -e

SCRIPT_DIR="$(dirname "$0")"
CONFIG_FILE="$SCRIPT_DIR/../docker/cloudflared/config.yml"

echo "========================================="
echo "  Claude Hub - Cloudflare Tunnel"
echo "========================================="
echo ""

# 检查配置文件
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 配置文件不存在: $CONFIG_FILE"
    echo ""
    echo "请先运行设置脚本: ./scripts/cloudflared-setup.sh"
    echo ""
    exit 1
fi

# 检查 cloudflared 是否安装
if ! command -v cloudflared &> /dev/null; then
    echo "❌ cloudflared 未安装"
    echo ""
    echo "安装方式："
    echo "  macOS:   brew install cloudflare/cloudflare/cloudflared"
    echo "  其他:    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    echo ""
    exit 1
fi

echo "✅ 配置文件: $CONFIG_FILE"
echo ""
echo "正在启动 Cloudflare Tunnel..."
echo "按 Ctrl+C 停止"
echo ""

# 启动隧道
cloudflared tunnel --config "$CONFIG_FILE" run
