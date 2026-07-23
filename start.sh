#!/bin/bash
# ===== KYTC Bridge Map -> Excel : launcher (Linux) =====
# First run sets things up (a minute or two). Later runs start in seconds.
# Keep this terminal open while you use the app. Close it to stop the app.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3 is not installed. Install it with your package manager"
  echo "(e.g. sudo apt install python3 python3-venv) then run this again."
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
    echo "Something went wrong installing dependencies. Check your internet and try again."
    exit 1
  fi
fi

echo
echo "Starting the app... open http://localhost:8000 in your browser."
echo "Leave this terminal open. Close it when you're done to stop the app."
echo
[ -f .do_update ] && rm -f .do_update
( sleep 2; xdg-open "http://localhost:8000" >/dev/null 2>&1 ) &

while true; do
  python -m uvicorn app:app --host 127.0.0.1 --port 8000
  # If the app applied an update it leaves a .do_update flag and exits.
  if [ -f .do_update ]; then
    rm -f .do_update
    echo
    echo "Update downloaded - refreshing and restarting..."
    pip install -r requirements.txt
    continue
  fi
  break
done
