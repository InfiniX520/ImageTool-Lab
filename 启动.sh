#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

echo "[1/4] Resolve Python interpreter..."
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[ERROR] Python not found. Install Python 3.9+ first."
  exit 1
fi

echo "[OK] Using: $PYTHON_BIN"

echo "[2/4] Install requirements..."
"$PYTHON_BIN" -m pip --disable-pip-version-check install -r requirements.txt

echo "[3/4] Launch GUI..."
echo "[INFO] If no window appears, check the taskbar first."
"$PYTHON_BIN" main.py
