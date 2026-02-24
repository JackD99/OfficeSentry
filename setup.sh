#!/usr/bin/env bash
# setup.sh — Cross-platform setup and launcher for the M365 Audit Portal
# Works on Linux, macOS, and Windows (Git Bash / WSL)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

banner() {
  echo ""
  echo -e "${BLUE}${BOLD}  ┌─────────────────────────────────────────┐${NC}"
  echo -e "${BLUE}${BOLD}  │       M365 Audit Portal — Setup         │${NC}"
  echo -e "${BLUE}${BOLD}  └─────────────────────────────────────────┘${NC}"
  echo ""
}

check_mark() { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn_mark()  { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail_mark()  { echo -e "  ${RED}[FAIL]${NC} $1"; }
info_mark()  { echo -e "  ${CYAN}[..]${NC} $1"; }

banner

# ---- Check Python ----
info_mark "Checking Python..."
if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  fail_mark "Python not found. Install Python 3.9+ from https://python.org"
  exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
  fail_mark "Python 3.9+ required (found $PY_VERSION)"
  exit 1
fi
check_mark "Python $PY_VERSION"

# ---- Check pip ----
info_mark "Checking pip..."
if ! $PYTHON -m pip --version &>/dev/null; then
  fail_mark "pip not found. Install pip: $PYTHON -m ensurepip --upgrade"
  exit 1
fi
check_mark "pip available"

# ---- Check PowerShell (optional, for audit scripts) ----
info_mark "Checking PowerShell 7..."
if [ -x "$SCRIPT_DIR/bin/pwsh/pwsh" ]; then
  check_mark "Bundled pwsh found"
elif command -v pwsh &>/dev/null; then
  PWSH_VER=$(pwsh -NoProfile -Command '$PSVersionTable.PSVersion.Major' 2>/dev/null || echo "0")
  if [ "$PWSH_VER" -ge 7 ]; then
    check_mark "PowerShell $PWSH_VER (system)"
  else
    warn_mark "PowerShell 7+ recommended for audit scripts (found v$PWSH_VER)"
  fi
else
  warn_mark "PowerShell 7 not found — portal will work but audit scripts require pwsh"
  echo -e "         Install from: ${CYAN}https://aka.ms/install-powershell${NC}"
fi

# ---- Install Python dependencies ----
echo ""
info_mark "Installing Python dependencies..."
$PYTHON -m pip install -r requirements.txt -q 2>&1 | tail -1
check_mark "Dependencies installed"

# ---- Check PowerShell modules ----
if [ -d "$SCRIPT_DIR/modules" ] && [ "$(ls -A "$SCRIPT_DIR/modules" 2>/dev/null)" ]; then
  check_mark "PowerShell modules found in ./modules/"
else
  warn_mark "PowerShell modules not downloaded yet"
  echo -e "         Run: ${CYAN}pwsh -File Build-Toolkit.ps1${NC}"
fi

# ---- Create runtime directories ----
mkdir -p data logs tenants

# ---- Done ----
echo ""
echo -e "${GREEN}${BOLD}  Setup complete!${NC}"
echo ""
echo -e "  To start the portal:"
echo -e "    ${CYAN}$PYTHON portal/app.py${NC}"
echo ""
echo -e "  Then open ${CYAN}http://127.0.0.1:5000${NC} in your browser."
echo -e "  On first visit, you'll create an admin account."
echo ""

# ---- Optional: auto-launch ----
if [ "${1:-}" = "--run" ] || [ "${1:-}" = "-r" ]; then
  echo -e "${BLUE}Starting portal...${NC}"
  echo ""
  $PYTHON portal/app.py
fi
