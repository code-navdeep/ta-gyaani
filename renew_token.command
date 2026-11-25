#!/bin/bash
cd "$(dirname "$0")"

if command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    DC="docker compose"
fi

clear
echo "================================================"
echo "  TA Gyaani - Dhan Token Renewal"
echo "================================================"
echo ""

if ! docker info >/dev/null 2>&1; then
    echo "FAILED: Docker is not running."
    echo "Please open Docker Desktop and try again."
    echo ""
    read -n 1 -s -r -p "Press any key to close..."
    echo ""
    exit 1
fi

if [ -z "$($DC images -q trading_system 2>/dev/null)" ]; then
    echo "First-time setup: building image (takes ~1 minute)..."
    echo ""
    if ! $DC build trading_system; then
        echo ""
        echo "FAILED: Image build failed."
        echo ""
        read -n 1 -s -r -p "Press any key to close..."
        echo ""
        exit 1
    fi
    echo ""
fi

$DC run --rm trading_system python scripts/renew_token.py
EXIT_CODE=$?

echo ""
read -n 1 -s -r -p "Press any key to close..."
echo ""
exit $EXIT_CODE
