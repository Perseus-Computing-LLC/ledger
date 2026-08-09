#!/usr/bin/env bash
# Ledger — the billing layer for AI agents. One-line install.
#   curl -fsSL https://raw.githubusercontent.com/Perseus-Computing-LLC/ledger/main/install.sh | bash
set -euo pipefail

YELLOW='\033[33m'; GREEN='\033[32m'; DIM='\033[2m'; NC='\033[0m'
echo -e "${YELLOW}◆ Installing Ledger — the billing layer for AI agents${NC}"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "Python 3.9+ is required." >&2; exit 1; fi

if command -v pipx >/dev/null 2>&1; then
  pipx install "ledger-agent[all]" || pipx install ledger-agent
else
  "$PY" -m pip install --user --upgrade "ledger-agent[all]" || "$PY" -m pip install --user --upgrade ledger-agent
fi

echo -e "${GREEN}✓ installed${NC}"
echo
echo -e "  ${DIM}Try the demo dashboard:${NC}  ledger demo        ${DIM}→ http://localhost:8420${NC}"
echo -e "  ${DIM}Meter Claude Code spend:${NC} ledger install-claude-hook"
echo -e "  ${DIM}Set up real billing:${NC}     see BILLING.md"
