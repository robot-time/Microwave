#!/usr/bin/env bash
set -e

# Quick re-run using saved config from setup.sh.
# Just: bash run.sh

CONFIG_FILE=".microwave.env"
C="\033[1;36m"; G="\033[1;32m"; R="\033[1;31m"; B="\033[1m"; X="\033[0m"

[ ! -f "pyproject.toml" ] && { echo -e "${R}Run from the Microwave repo root.${X}"; exit 1; }
[ ! -d ".venv" ] && { echo -e "${R}No .venv. Run: bash setup.sh${X}"; exit 1; }
[ ! -f "$CONFIG_FILE" ] && { echo -e "${R}No config. Run: bash setup.sh${X}"; exit 1; }

VENV_BIN=""
[ -d ".venv/Scripts" ] && VENV_BIN="$(cd .venv/Scripts && pwd)"
[ -d ".venv/bin" ] && VENV_BIN="$(cd .venv/bin && pwd)"
[ -z "$VENV_BIN" ] && { echo -e "${R}venv bin missing.${X}"; exit 1; }
export PATH="$VENV_BIN:$PATH"
source "$CONFIG_FILE"

command -v microwave >/dev/null 2>&1 || { pip install -e . -q 2>/dev/null || pip install -e .; }

echo -e "${B}Microwave${X}  ${C}$MICROWAVE_MODEL${X}  ->  ${C}$MICROWAVE_GATEWAY_URL${X}"

ARGS="--gateway-url $MICROWAVE_GATEWAY_URL --region $MICROWAVE_REGION --model $MICROWAVE_MODEL"
ARGS="$ARGS --engine ${MICROWAVE_ENGINE:-ollama}"
ARGS="$ARGS --latitude ${MICROWAVE_LAT:-0.0} --longitude ${MICROWAVE_LON:-0.0}"
ARGS="$ARGS --reverse"
ARGS="$ARGS --expert-domains ${MICROWAVE_EXPERT_DOMAINS:-general}"
[ -n "$MICROWAVE_DRAFT_MODELS" ] && ARGS="$ARGS --draft-models $MICROWAVE_DRAFT_MODELS"

exec microwave run $ARGS
