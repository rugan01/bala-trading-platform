#!/bin/bash
# Install Upstox Token Refresh Scheduler
# Run this script to set up automatic daily token refresh at 8 AM

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.bala.upstox-token-refresh.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "=== Upstox Token Refresh Scheduler Installation ==="
echo ""

# Step 1: Check virtual environment and dependencies
echo "1. Checking Python dependencies..."
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "   Creating virtual environment with Python 3.13..."
    /opt/homebrew/bin/python3.13 -m venv "$SCRIPT_DIR/.venv"
fi
"$SCRIPT_DIR/.venv/bin/pip" install --quiet upstox-totp python-dotenv
echo "   ✓ Dependencies installed"

# Step 2: Make the refresh script executable
echo "2. Making refresh script executable..."
chmod +x "$SCRIPT_DIR/upstox_token_refresh.py"
echo "   ✓ Script is executable"

# Step 3: Create LaunchAgents directory if needed
echo "3. Setting up LaunchAgents..."
mkdir -p "$LAUNCH_AGENTS_DIR"

# Step 4: Copy plist to LaunchAgents
cp "$SCRIPT_DIR/$PLIST_NAME" "$LAUNCH_AGENTS_DIR/"
echo "   ✓ Plist copied to $LAUNCH_AGENTS_DIR"

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
echo "Script:   $SCRIPT_DIR/upstox_token_refresh.py"
echo "Logs:     ~/Library/Logs/upstox_token_refresh.log"
echo ""
echo "Commands:"
echo "  Test now:     python3 $SCRIPT_DIR/upstox_token_refresh.py --notify"
echo "  View logs:    tail -f ~/Library/Logs/upstox_token_refresh.log"
echo "  Stop:         launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
echo "  Start:        launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
echo ""
