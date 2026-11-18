#!/usr/bin/env bash
# One-time setup on the VPS (Ubuntu/Debian). Run from inside the FrameMatcher_Bot folder:
#   bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
SERVICE=/etc/systemd/system/framematcher.service

echo "Folder: $DIR"
echo "User:   $USER_NAME"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 install kar raha hoon..."
  sudo apt-get update -y && sudo apt-get install -y python3
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ chahiye"'
echo "Python: $(python3 --version)"

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo ">> .env bana di hai. Isme BOT_TOKEN daalo:  nano $DIR/.env"
fi
sed -i 's/\r$//' "$DIR/.env" "$DIR/deploy/run.sh"   # Windows line endings hata do
chmod 600 "$DIR/.env"
mkdir -p "$DIR/data"

sed -e "s#__DIR__#$DIR#g" -e "s#__USER__#$USER_NAME#g" "$DIR/deploy/framematcher.service" \
  | sudo tee "$SERVICE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable framematcher >/dev/null

if grep -qE '^BOT_TOKEN=.+:.+' "$DIR/.env"; then
  sudo systemctl restart framematcher
  sleep 3
  sudo systemctl --no-pager --lines=5 status framematcher || true
  echo
  echo "Bot chal gaya. Logs dekhne ke liye:  sudo journalctl -u framematcher -f"
else
  echo ">> Token abhi khaali hai. nano $DIR/.env mein BOT_TOKEN daalo, phir:"
  echo "   sudo systemctl restart framematcher"
fi
