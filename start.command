#!/bin/bash
# ===== KYTC Bridge Map -> Excel : double-click launcher (macOS) =====
# First run sets things up (a minute or two). Later runs start in seconds.
# Keep this window open while you use the app. Close it to stop the app.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3 is not installed."
  echo "Get it from https://www.python.org/downloads/ then double-click this again."
  echo
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating environment (first run only)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f ".venv/.installed" ]; then
  echo "Installing dependencies (first run only, may take a minute)..."
  pip install --upgrade pip >/dev/null 2>&1
  if pip install -r requirements.txt; then
    touch .venv/.installed
  else
    echo
    echo "Something went wrong installing dependencies. Check your internet and try again."
    read -r -p "Press Enter to close."
    exit 1
  fi
fi

echo
echo "Starting the app... your browser will open at http://localhost:8000"
echo "Leave this window open. Close it when you're done to stop the app."
echo
[ -f .do_update ] && rm -f .do_update
( sleep 2; open "http://localhost:8000" ) &

while true; do
  python -m uvicorn app:app --host 127.0.0.1 --port 8000
  # If the app applied an update it leaves a .do_update flag and exits.
  if [ -f .do_update ]; then
    rm -f .do_update
    echo
    echo "Update downloaded — refreshing and restarting..."
    pip install -r requirements.txt
    continue
  fi
  break
done
