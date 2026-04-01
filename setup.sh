#!/bin/bash

# Claude Hub Setup Script
# Installs uv and other dependencies if needed

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Setting up Claude Hub${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check for uv
if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}uv not found, installing...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo -e "${GREEN}✓ uv installed${NC}"
    echo ""
    echo -e "${YELLOW}Please reload your shell or run:${NC}"
    echo -e "${YELLOW}  source $HOME/.local/bin/env${NC}"
    echo ""
else
    echo -e "${GREEN}✓ uv is already installed${NC}"
fi

# Check for ttyd
if ! command -v ttyd &> /dev/null; then
    echo -e "${RED}⚠️  ttyd not found${NC}"
    echo "Please install ttyd from https://github.com/tsl0922/ttyd"
    echo ""
else
    echo -e "${GREEN}✓ ttyd is already installed${NC}"
fi

# Check for pnpm
if ! command -v pnpm &> /dev/null; then
    echo -e "${RED}⚠️  pnpm not found${NC}"
    echo "Please install pnpm from https://pnpm.io/"
    echo ""
else
    echo -e "${GREEN}✓ pnpm is already installed${NC}"
fi

echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo -e "${GREEN}Next, run ./start.sh to start the application${NC}"
