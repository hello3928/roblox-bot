#!/bin/bash
# Run this once on a fresh EC2 Ubuntu instance to set everything up.
# Usage: bash setup.sh <your-github-repo-url>
# Example: bash setup.sh https://github.com/yourname/roblox-bot.git

set -e

REPO_URL=${1:?"Usage: bash setup.sh <github-repo-url>"}
BOT_DIR="$HOME/roblox-bot"

echo "==> Updating system packages..."
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

echo "==> Cloning repo..."
git clone "$REPO_URL" "$BOT_DIR"
cd "$BOT_DIR"

echo "==> Creating virtual environment..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet

echo "==> Creating .env file..."
cp .env.example .env
echo ""
echo "  --> Open .env and fill in your values before continuing:"
echo "      nano $BOT_DIR/.env"
echo ""
read -p "Press Enter once you have saved your .env file..."

echo "==> Installing systemd service..."
sudo cp roblox-bot.service /etc/systemd/system/roblox-bot.service
sudo systemctl daemon-reload
sudo systemctl enable roblox-bot
sudo systemctl start roblox-bot

echo ""
echo "==> Done! Bot is running. Check status with:"
echo "    sudo systemctl status roblox-bot"
echo "    journalctl -u roblox-bot -f"
