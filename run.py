#!/usr/bin/env python3
"""
run.py — 1-click launcher for Office Sentry.

Handles everything automatically:
  1. Installs Python dependencies (if missing)
  2. Downloads PowerShell modules (if pwsh is available and modules are missing)
  3. Creates runtime directories
  4. Launches the portal

Usage:
    python run.py              # setup + launch
    python run.py --setup      # setup only, don't launch
    python run.py --skip-deps  # skip dependency install (faster restart)
"""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
REQUIREMENTS = ROOT / "requirements.txt"
MODULES_DIR = ROOT / "modules"
BUILD_SCRIPT = ROOT / "Build-Toolkit.ps1"

# ANSI colors (disabled on Windows without VT support)
_COLOR = sys.stdout.isatty() and os.name != "nt"
GREEN = "\033[0;32m" if _COLOR else ""
YELLOW = "\033[1;33m" if _COLOR else ""
CYAN = "\033[0;36m" if _COLOR else ""
RED = "\033[0;31m" if _COLOR else ""
BOLD = "\033[1m" if _COLOR else ""
NC = "\033[0m" if _COLOR else ""


def ok(msg):
    print(f"  {GREEN}[OK]{NC} {msg}")


def warn(msg):
    print(f"  {YELLOW}[!!]{NC} {msg}")


def fail(msg):
    print(f"  {RED}[FAIL]{NC} {msg}")


def info(msg):
    print(f"  {CYAN}[..]{NC} {msg}")


def banner():
    print()
    print(f"  {BOLD}========================================{NC}")
    print(f"  {BOLD}    Office Sentry — Quick Start{NC}")
    print(f"  {BOLD}========================================{NC}")
    print()


# --------------------------------------------------------------------------
# Step 1: Python dependencies
# --------------------------------------------------------------------------

def _packages_missing() -> list[str]:
    """Check which required packages are not importable."""
    # Map pip package names to importable module names
    import_map = {
        "Flask": "flask",
        "cryptography": "cryptography",
        "Flask-WTF": "flask_wtf",
        "Flask-Limiter": "flask_limiter",
    }
    missing = []
    for line in REQUIREMENTS.read_text().splitlines():
        pkg = line.strip().split("==")[0].split(">=")[0].split("<=")[0]
        if not pkg or pkg.startswith("#"):
            continue
        mod = import_map.get(pkg, pkg.lower().replace("-", "_"))
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def install_python_deps():
    """Install Python dependencies from requirements.txt if any are missing."""
    info("Checking Python dependencies...")
    missing = _packages_missing()
    if not missing:
        ok("All Python dependencies installed")
        return

    info(f"Installing: {', '.join(missing)}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail("Failed to install Python dependencies")
        print(f"         {result.stderr.strip()}")
        print(f"         Try manually: {sys.executable} -m pip install -r requirements.txt")
        sys.exit(1)
    ok("Python dependencies installed")


# --------------------------------------------------------------------------
# Step 2: PowerShell modules
# --------------------------------------------------------------------------

def _has_ps_modules() -> bool:
    """Check if PowerShell modules directory has content."""
    if not MODULES_DIR.exists():
        return False
    # Check for at least one module directory with content
    for child in MODULES_DIR.iterdir():
        if child.is_dir() and any(child.rglob("*.psm1")):
            return True
    return False


def _find_pwsh() -> str | None:
    """Find PowerShell 7+ executable."""
    # Check bundled first
    bundled = ROOT / "bin" / "pwsh"
    for name in ("pwsh", "pwsh.exe"):
        p = bundled / name
        if p.exists():
            return str(p)
    # Check system PATH
    return shutil.which("pwsh")


def install_ps_modules():
    """Download PowerShell modules via Build-Toolkit.ps1 if pwsh is available."""
    info("Checking PowerShell modules...")
    if _has_ps_modules():
        ok("PowerShell modules found")
        return

    pwsh = _find_pwsh()
    if not pwsh:
        warn("PowerShell 7 not found — skipping module download")
        print(f"         The portal will run, but audit scripts require pwsh + modules.")
        print(f"         Install pwsh from: {CYAN}https://aka.ms/install-powershell{NC}")
        return

    if not BUILD_SCRIPT.exists():
        warn("Build-Toolkit.ps1 not found — skipping module download")
        return

    info("Downloading PowerShell modules (this may take a few minutes on first run)...")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(BUILD_SCRIPT)],
        cwd=str(ROOT),
    )
    if result.returncode == 0:
        ok("PowerShell modules downloaded")
    else:
        warn("Some PowerShell modules may have failed to download")
        print(f"         You can retry later: pwsh -File Build-Toolkit.ps1")


# --------------------------------------------------------------------------
# Step 3: Runtime directories
# --------------------------------------------------------------------------

def create_dirs():
    """Create runtime directories if they don't exist."""
    for d in ("data", "logs", "tenants", "config"):
        (ROOT / d).mkdir(exist_ok=True)


# --------------------------------------------------------------------------
# Step 4: Launch
# --------------------------------------------------------------------------

def launch_portal():
    """Launch the Flask portal."""
    print()
    print(f"  {BOLD}Starting Office Sentry...{NC}")
    print(f"  Open {CYAN}http://127.0.0.1:5000{NC} in your browser.")
    print(f"  Press Ctrl+C to stop.")
    print()

    app_py = ROOT / "portal" / "app.py"
    try:
        subprocess.run([sys.executable, str(app_py)], cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n  Portal stopped.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    setup_only = "--setup" in args
    skip_deps = "--skip-deps" in args

    banner()

    # Check Python version
    info("Checking Python version...")
    v = sys.version_info
    if v < (3, 9):
        fail(f"Python 3.9+ required (found {v.major}.{v.minor})")
        sys.exit(1)
    ok(f"Python {v.major}.{v.minor}.{v.micro}")

    # Install dependencies
    if not skip_deps:
        install_python_deps()
        install_ps_modules()

    # Create dirs
    create_dirs()

    if setup_only:
        print()
        print(f"  {GREEN}{BOLD}Setup complete!{NC}")
        print(f"  Run {CYAN}python run.py{NC} to start the portal.")
        print()
        return

    # Launch
    launch_portal()


if __name__ == "__main__":
    main()
