#!/usr/bin/env bash
# setup.sh — 1-click setup and launcher for the M365 Audit Portal (Linux/macOS)
#
# Usage:
#   ./setup.sh              # install everything + launch portal
#   ./setup.sh --setup      # install only, don't launch
#   ./setup.sh --skip-deps  # skip dependency install (faster restart)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

check_mark() { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn_mark()  { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail_mark()  { echo -e "  ${RED}[FAIL]${NC} $1"; }
info_mark()  { echo -e "  ${CYAN}[..]${NC} $1"; }

SETUP_ONLY=false
SKIP_DEPS=false
for arg in "$@"; do
  case "$arg" in
    --setup)     SETUP_ONLY=true ;;
    --setup-only) SETUP_ONLY=true ;;
    --skip-deps) SKIP_DEPS=true ;;
  esac
done

echo ""
echo -e "${BLUE}${BOLD}  ┌─────────────────────────────────────────┐${NC}"
echo -e "${BLUE}${BOLD}  │     M365 Audit Portal — Quick Start     │${NC}"
echo -e "${BLUE}${BOLD}  └─────────────────────────────────────────┘${NC}"
echo ""

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

if [ "$SKIP_DEPS" = false ]; then
  # ---- Install Python dependencies ----
  info_mark "Installing Python dependencies..."
  $PYTHON -m pip install -r requirements.txt -q 2>&1 | tail -1
  check_mark "Python dependencies installed"

  # ---- Check PowerShell + modules ----
  PWSH_BIN=""
  if [ -x "$SCRIPT_DIR/bin/pwsh/pwsh" ]; then
    PWSH_BIN="$SCRIPT_DIR/bin/pwsh/pwsh"
    check_mark "Bundled pwsh found"
  elif command -v pwsh &>/dev/null; then
    PWSH_VER=$(pwsh -NoProfile -Command '$PSVersionTable.PSVersion.Major' 2>/dev/null || echo "0")
    if [ "$PWSH_VER" -ge 7 ]; then
      PWSH_BIN="pwsh"
      check_mark "PowerShell $PWSH_VER (system)"
    else
      warn_mark "PowerShell 7+ recommended (found v$PWSH_VER)"
    fi
  else
    warn_mark "PowerShell 7 not found — portal will work but audit scripts need pwsh"
    echo -e "         Install from: ${CYAN}https://aka.ms/install-powershell${NC}"
  fi

  # Auto-download PS modules if pwsh is available and modules are missing
  if [ -n "$PWSH_BIN" ]; then
    if [ -d "$SCRIPT_DIR/modules" ] && [ "$(find "$SCRIPT_DIR/modules" -name '*.psm1' 2>/dev/null | head -1)" ]; then
      check_mark "PowerShell modules found"
    elif [ -f "$SCRIPT_DIR/Build-Toolkit.ps1" ]; then
      echo ""
      info_mark "Downloading PowerShell modules (first run only)..."
      "$PWSH_BIN" -NoProfile -NonInteractive -ExecutionPolicy Bypass \
        -File "$SCRIPT_DIR/Build-Toolkit.ps1" && \
        check_mark "PowerShell modules downloaded" || \
        warn_mark "Some modules may have failed — retry with: pwsh -File Build-Toolkit.ps1"
    fi
  fi
fi

# ---- Create runtime directories ----
mkdir -p data logs tenants config

# ---- Done setup ----
if [ "$SETUP_ONLY" = true ]; then
  echo ""
  echo -e "${GREEN}${BOLD}  Setup complete!${NC}"
  echo ""
  echo -e "  To start the portal:"
  echo -e "    ${CYAN}./setup.sh${NC}"
  echo ""
  exit 0
fi

# ---- Launch the portal ----
echo ""
echo -e "${GREEN}${BOLD}  Starting M365 Audit Portal...${NC}"
echo -e "  Open ${CYAN}http://127.0.0.1:5000${NC} in your browser."
echo -e "  Press Ctrl+C to stop."
echo ""

$PYTHON portal/app.py
