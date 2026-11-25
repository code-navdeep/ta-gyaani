#!/bin/bash
#
# TA Gyaani - One-Click Stop for macOS
# Double-click this file to stop the application
#

# Change to script directory
cd "$(dirname "$0")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                  TA Gyaani - Stopping                    ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo -e "${RED}[ERROR] Docker is not installed.${NC}"
    read -p "Press Enter to exit..."
    exit 1
fi

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo -e "${YELLOW}[INFO] Docker is not running. Nothing to stop.${NC}"
    read -p "Press Enter to exit..."
    exit 0
fi

# Stop containers
echo -e "${YELLOW}[INFO] Stopping TA Gyaani...${NC}"

if docker compose down; then
    echo ""
    echo -e "${GREEN}[OK] All services stopped successfully.${NC}"
else
    echo ""
    echo -e "${RED}[ERROR] Failed to stop some services.${NC}"
fi

echo ""
echo -e "${GREEN}Application stopped.${NC}"
echo ""
read -p "Press Enter to close..."
