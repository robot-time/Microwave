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

if [ ! -f "pyproject.toml" ]; then
  echo -e "${RED}run.sh must be executed from the Microwave repo root.${RESET}"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo -e "${RED}No .venv found.${RESET} Run bash setup.sh first."
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

echo -e "${BOLD}Microwave quick run${RESET} ${DIM}(already installed mode)${RESET}"
echo -e "LAN IP: ${CYAN}${LAN_IP}${RESET}"
echo ""
echo -e "  ${CYAN}1)${RESET} Gateway"
echo -e "  ${CYAN}2)${RESET} Node"
echo -e "  ${CYAN}3)${RESET} Both"
echo ""
read -rp "Choose 1, 2, or 3: " ROLE
echo ""

GATEWAY_PORT=8000
NODE_PORT=9000
REGION="LAN"
MODEL="llama3.2"
REVERSE_MODE=false
GATEWAY_URL="http://${LAN_IP}:${GATEWAY_PORT}"

if [[ "$ROLE" == "1" || "$ROLE" == "3" ]]; then
  read -rp "Gateway port [8000]: " _gp
  [ -n "$_gp" ] && GATEWAY_PORT="$_gp"
  GATEWAY_URL="http://${LAN_IP}:${GATEWAY_PORT}"
fi

if [[ "$ROLE" == "2" ]]; then
  read -rp "Gateway URL [http://SERVER_IP:8000]: " _gw
  [ -n "$_gw" ] && GATEWAY_URL="$_gw"
  echo ""
  read -rp "Use reverse mode (works behind NAT/firewall)? [Y/n]: " _r
  [[ "$_r" != "n" && "$_r" != "N" ]] && REVERSE_MODE=true
fi

if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  if [[ "$REVERSE_MODE" == false ]]; then
    read -rp "Node port [9000]: " _np
    [ -n "$_np" ] && NODE_PORT="$_np"
  fi
  read -rp "Model [llama3.2]: " _m
  [ -n "$_m" ] && MODEL="$_m"
  read -rp "Region [LAN]: " _rgn
  [ -n "$_rgn" ] && REGION="$_rgn"
fi

echo ""
echo -e "${BOLD}Starting...${RESET}"

if [[ "$ROLE" == "1" ]]; then
  echo -e "${DIM}Gateway: http://${LAN_IP}:${GATEWAY_PORT}${RESET}"
  microwave-gateway --host 0.0.0.0 --port "$GATEWAY_PORT"
elif [[ "$ROLE" == "2" ]]; then
  if [[ "$REVERSE_MODE" == true ]]; then
    microwave-node \
      --gateway-url "$GATEWAY_URL" \
      --region "$REGION" \
      --model "$MODEL" \
      --reverse
  else
    microwave-node \
      --gateway-url "$GATEWAY_URL" \
      --region "$REGION" \
      --model "$MODEL" \
      --host "$LAN_IP" \
      --port "$NODE_PORT"
  fi
else
  echo -e "${DIM}Gateway: http://${LAN_IP}:${GATEWAY_PORT}${RESET}"
  microwave-gateway --host 0.0.0.0 --port "$GATEWAY_PORT" &
  GATEWAY_PID=$!
  echo -e "${DIM}Gateway PID: ${GATEWAY_PID}${RESET}"
  sleep 1
  microwave-node \
    --gateway-url "http://${LAN_IP}:${GATEWAY_PORT}" \
    --region "$REGION" \
    --model "$MODEL" \
    --host "$LAN_IP" \
    --port "$NODE_PORT"
fi
