#!/bin/bash
# Ledger — provider credit monitor refresh (watchdog pattern: silent on success).
# Regenerates the HTML dashboard and appends a burn-rate history snapshot.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

LEDGER_DIR=/opt/data/webui/minions/.minions-data/workspace/ledger
HTML="$LEDGER_DIR/ledger.html"

if [ ! -f "$LEDGER_DIR/ledger.py" ]; then
    echo "Ledger not found at $LEDGER_DIR/ledger.py"
    exit 1
fi

python3 "$LEDGER_DIR/ledger.py" --html "$HTML" --snapshot >/dev/null 2>>"$LEDGER_DIR/ledger.err"
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "Ledger refresh FAILED (exit $exit_code) — see $LEDGER_DIR/ledger.err"
    exit 1
fi

# Balancing arm: rebalance model routing by runway. Self-verifies + backs up
# config before writing; refuses the write if any provider/key would be lost.
python3 "$LEDGER_DIR/ledger_route.py" --apply >>"$LEDGER_DIR/ledger.routing.log" 2>>"$LEDGER_DIR/ledger.err"
route_code=$?
if [ $route_code -ne 0 ]; then
    echo "Ledger routing FAILED (exit $route_code) — see $LEDGER_DIR/ledger.err"
    exit 1
fi
exit 0
