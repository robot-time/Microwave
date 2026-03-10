#!/usr/bin/env bash
set -e

echo "Microwave AI – gateway quickstart"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -e . >/dev/null

echo
echo "Starting gateway on 0.0.0.0:8000 ..."
echo
microwave-gateway --host 0.0.0.0 --port 8000

