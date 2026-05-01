#!/bin/bash
# Install Upstox Token Refresh Scheduler
# Run this script to set up automatic daily token refresh at 8 AM

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_NAME="com.bala.upstox-token-refresh.plist"
PLIST_TEMPLATE="$SCRIPT_DIR/$PLIST_NAME"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
REFRESH_SCRIPT="$REPO_ROOT/apps/journaling/upstox_token_refresh.py"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "=== Upstox Token Refresh Scheduler Installation ==="
echo ""

# Step 1: Check virtual environment and dependencies
echo "1. Checking Python dependencies..."
if [ ! -d "$REPO_ROOT/.venv" ]; then
    echo "   Creating virtual environment with Python 3.13..."
    /opt/homebrew/bin/python3.13 -m venv "$REPO_ROOT/.venv"
fi
"$PYTHON_BIN" -m pip install --quiet -r "$REPO_ROOT/apps/journaling/requirements.txt"
echo "   ✓ Dependencies installed"

# Step 2: Make the refresh script executable
echo "2. Making refresh script executable..."
chmod +x "$REFRESH_SCRIPT"
echo "   ✓ Script is executable"

# Step 3: Create LaunchAgents directory if needed
echo "3. Setting up LaunchAgents..."
mkdir -p "$LAUNCH_AGENTS_DIR"

# Step 4: Render plist to LaunchAgents
sed \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  -e "s|__REFRESH_SCRIPT__|$REFRESH_SCRIPT|g" \
  -e "s|__STDOUT_LOG__|$HOME/Library/Logs/upstox_token_refresh_stdout.log|g" \
  -e "s|__STDERR_LOG__|$HOME/Library/Logs/upstox_token_refresh_stderr.log|g" \
  "$PLIST_TEMPLATE" > "$LAUNCH_AGENTS_DIR/$PLIST_NAME"
echo "   ✓ Plist rendered to $LAUNCH_AGENTS_DIR"

# Step 5: Unload existing job if present
echo "4. Loading scheduler..."
launchctl unload "$LAUNCH_AGENTS_DIR/$PLIST_NAME" 2>/dev/null || true

# Step 6: Load the new job
launchctl load "$LAUNCH_AGENTS_DIR/$PLIST_NAME"
echo "   ✓ Scheduler loaded"

# Step 7: Verify
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Schedule: Daily at 8:00 AM"
echo "Script:   $REFRESH_SCRIPT"
echo "Logs:     ~/Library/Logs/upstox_token_refresh.log"
echo ""
echo "Commands:"
echo "  Test now:     $PYTHON_BIN $REFRESH_SCRIPT --notify"
echo "  View logs:    tail -f ~/Library/Logs/upstox_token_refresh.log"
echo "  Stop:         launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
echo "  Start:        launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
echo ""
