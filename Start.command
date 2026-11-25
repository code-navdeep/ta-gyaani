#!/bin/bash
#
# TA Gyaani - One-Click Starter for macOS
# Double-click this file to start the application
#

# Change to script directory
cd "$(dirname "$0")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Banner
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                        TA Gyaani                         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Function to check if Docker is installed
check_docker_installed() {
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}[ERROR] Docker is not installed.${NC}"
        echo ""
        echo "Please install Docker Desktop from:"
        echo "  https://www.docker.com/products/docker-desktop/"
        echo ""
        echo "After installation:"
        echo "  1. Open Docker Desktop"
        echo "  2. Wait for it to start (whale icon in menu bar)"
        echo "  3. Double-click this file again"
        echo ""
        read -p "Press Enter to open Docker download page..."
        open "https://www.docker.com/products/docker-desktop/"
        exit 1
    fi
}

# Function to check if Docker daemon is running
check_docker_running() {
    if ! docker info &> /dev/null; then
        echo -e "${YELLOW}[INFO] Docker Desktop is not running. Starting it now...${NC}"
        open -a Docker

        echo -n "Waiting for Docker to start"
        for i in {1..60}; do
            if docker info &> /dev/null; then
                echo ""
                echo -e "${GREEN}[OK] Docker is now running.${NC}"
                return 0
            fi
            echo -n "."
            sleep 2
        done

        echo ""
        echo -e "${RED}[ERROR] Docker failed to start within 2 minutes.${NC}"
        echo "Please start Docker Desktop manually and try again."
        read -p "Press Enter to exit..."
        exit 1
    fi
}

# Function to check the settings file. The app refuses to start without the
# Dhan login and a username and password for the dashboard.
check_env() {
    local missing=""
    if [ ! -f .env ]; then
        missing="DHAN_CLIENT_ID DHAN_ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD"
    else
        for key in DHAN_CLIENT_ID DHAN_ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD; do
            if ! grep -Eq "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]#]" .env; then
                missing="$missing $key"
            fi
        done
    fi
    if [ -n "$missing" ]; then
        echo -e "${RED}[ERROR] The settings file .env is missing or incomplete.${NC}"
        echo ""
        echo "Copy .env.example to a file named .env in this folder and fill in:"
        for key in $missing; do
            echo "  $key"
        done
        echo ""
        echo "See README.md, 'Setup'."
        read -p "Press Enter to exit..."
        exit 1
    fi
}

# Function to start containers.
# --build on EVERY start: without it, a code change keeps running the old image
# until someone rebuilds by hand. When nothing changed, Docker's layer cache
# makes the build take seconds.
start_containers() {
    echo -e "${YELLOW}[INFO] Building (if the code changed) and starting TA Gyaani...${NC}"
    echo "The first run takes a few minutes."

    if docker compose up -d --build; then
        echo ""
        echo -e "${GREEN}[OK] All services started successfully.${NC}"
        return 0
    else
        echo ""
        echo -e "${RED}[ERROR] Failed to start services.${NC}"
        read -p "Press Enter to exit..."
        exit 1
    fi
}

# Function to show status
show_status() {
    echo ""
    echo -e "${BLUE}Service Status:${NC}"
    docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
    echo ""
}

# Function to open dashboard
open_dashboard() {
    echo -e "${YELLOW}[INFO] Opening dashboard in browser...${NC}"
    sleep 10  # Give dashboard time to start
    open "http://localhost:8054"
}

# ============================================
# MAIN SCRIPT
# ============================================

# Step 1: Check Docker installation
echo -e "${BLUE}[1/5] Checking Docker installation...${NC}"
check_docker_installed
echo -e "${GREEN}[OK] Docker is installed.${NC}"

# Step 2: Check Docker is running
echo -e "${BLUE}[2/5] Checking Docker status...${NC}"
check_docker_running
echo -e "${GREEN}[OK] Docker is running.${NC}"

# Step 3: Check the settings file
echo -e "${BLUE}[3/5] Checking the settings file (.env)...${NC}"
check_env
echo -e "${GREEN}[OK] Settings found.${NC}"

# Step 4: Build and start containers
echo -e "${BLUE}[4/5] Preparing containers...${NC}"
start_containers

# Step 5: Show status and open dashboard
echo -e "${BLUE}[5/5] Launching application...${NC}"
show_status

echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Application Started Successfully!           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Dashboard URL: ${BLUE}http://localhost:8054${NC}"
echo ""
echo "To stop the application:"
echo "  - Double-click Stop.command"
echo "  - Or, in Docker Desktop, find 'ta-trading-system' in the Containers"
echo "    list and click its Stop button"
echo ""
echo "Or run: docker compose down"
echo ""

# Open dashboard in browser
open_dashboard

echo -e "${GREEN}Done! You can close this window.${NC}"
echo ""
read -p "Press Enter to close..."
