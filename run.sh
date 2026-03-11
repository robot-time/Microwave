#!/usr/bin/env bash
set -e

# Quick runner for already-installed setups.
# Assumes you're inside the Microwave repo and have already run setup.sh once.

BOLD="\033[1m"
CYAN="\033[1;36m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
DIM="\033[2m"
RESET="\033[0m"
CONFIG_FILE=".microwave.env"

if [ ! -f "pyproject.toml" ]; then
  echo -e "${RED}run.sh must be executed from the Microwave repo root.${RESET}"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo -e "${RED}No .venv found.${RESET} Run bash setup.sh first."
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo -e "${RED}No saved config found (${CONFIG_FILE}).${RESET}"
  echo "Run bash setup.sh once to save startup settings."
  exit 1
fi

VENV_BIN=""
if [ -d ".venv/Scripts" ]; then
  VENV_BIN="$(cd .venv/Scripts && pwd)"
elif [ -d ".venv/bin" ]; then
  VENV_BIN="$(cd .venv/bin && pwd)"
fi

if [ -z "$VENV_BIN" ]; then
  echo -e "${RED}Could not find venv bin directory.${RESET}"
  exit 1
fi

export PATH="$VENV_BIN:$PATH"
source "$CONFIG_FILE"

if ! command -v microwave-gateway >/dev/null 2>&1 || ! command -v microwave-node >/dev/null 2>&1; then
  echo -e "${YELLOW}Microwave commands not found in venv; reinstalling package...${RESET}"
  pip install -e . >/dev/null 2>&1 || pip install -e .
fi

detect_ip() {
  local ip=""
  if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ { print $7; exit }')
  fi
  if [ -z "$ip" ]; then
    local maybe_mac
    maybe_mac=$(ipconfig getifaddr en0 2>/dev/null || true)
    [ -n "$maybe_mac" ] && ip="$maybe_mac"
  fi
  if [ -z "$ip" ]; then
    local maybe_mac
    maybe_mac=$(ipconfig getifaddr en1 2>/dev/null || true)
    [ -n "$maybe_mac" ] && ip="$maybe_mac"
  fi
  if [ -z "$ip" ] && command -v ipconfig.exe >/dev/null 2>&1; then
    ip=$(ipconfig.exe 2>/dev/null | grep -i "IPv4" | head -1 | sed 's/.*: //' | tr -d '\r' || true)
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  fi
  echo "$ip"
}

LAN_IP=$(detect_ip)
[ -z "$LAN_IP" ] && LAN_IP="0.0.0.0"

echo -e "${BOLD}Microwave quick run${RESET} ${DIM}(using saved config)${RESET}"
echo -e "LAN IP:    ${CYAN}${LAN_IP}${RESET}"
echo -e "Gateway:   ${CYAN}${MICROWAVE_GATEWAY_URL}${RESET}"
echo -e "Model:     ${CYAN}${MICROWAVE_MODEL}${RESET}"
echo -e "Region:    ${CYAN}${MICROWAVE_REGION}${RESET}"
echo -e "Reverse:   ${CYAN}${MICROWAVE_REVERSE_MODE}${RESET}"
echo ""

if [[ "$MICROWAVE_REVERSE_MODE" == "true" ]]; then
  microwave-node \
    --gateway-url "$MICROWAVE_GATEWAY_URL" \
    --region "$MICROWAVE_REGION" \
    --model "$MICROWAVE_MODEL" \
    --reverse
else
  microwave-node \
    --gateway-url "$MICROWAVE_GATEWAY_URL" \
    --region "$MICROWAVE_REGION" \
    --model "$MICROWAVE_MODEL" \
    --host "${MICROWAVE_NODE_IP:-$LAN_IP}" \
    --port "${MICROWAVE_NODE_PORT:-9000}"
fi
