#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────
#  Microwave AI – interactive setup
# ─────────────────────────────────────────────

REPO_URL="https://github.com/robot-time/Microwave.git"
REPO_DIR="Microwave"

# ── colours ──────────────────────────────────
BOLD="\033[1m"
CYAN="\033[1;36m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
DIM="\033[2m"
RESET="\033[0m"

if [ -z "$_SETUP_REEXECED" ]; then

clear

cat << 'EOF'
     ________________
    |.-----------.   |
    ||   _____   |ooo|
    ||  |     |  |ooo|
    ||  |     |  | = |
    ||  '-----'  | _ |
    ||___________|[_]|
    '----------------'
------------------------------------------------
EOF

echo -e "${BOLD}Microwave Network${RESET} – decentralised AI inference"
echo -e "${DIM}BitTorrent for AI  ·  github.com/robot-time/Microwave${RESET}"
echo ""

# ── detect LAN IP ─────────────────────────────
detect_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ { print $7; exit }')
  fi
  if [ -z "$ip" ] && command -v ipconfig >/dev/null 2>&1; then
    ip=$(ipconfig getifaddr en0 2>/dev/null || true)
    [ -z "$ip" ] && ip=$(ipconfig getifaddr en1 2>/dev/null || true)
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  fi
  echo "$ip"
}

NODE_IP=$(detect_ip)

if [ -z "$NODE_IP" ]; then
  echo -e "${RED}Could not auto-detect your LAN IP.${RESET}"
  read -rp "  Enter this machine's LAN IP manually: " NODE_IP
fi

echo -e "  Detected LAN IP: ${CYAN}${NODE_IP}${RESET}"
echo ""

# ── role selection ────────────────────────────
echo -e "${BOLD}What do you want to run on this machine?${RESET}"
echo ""
echo -e "  ${CYAN}1)${RESET} Gateway  ${DIM}(central coordinator – routes requests to nodes)${RESET}"
echo -e "  ${CYAN}2)${RESET} Node     ${DIM}(volunteer compute – runs the AI model locally)${RESET}"
echo -e "  ${CYAN}3)${RESET} Both     ${DIM}(gateway + node on the same machine)${RESET}"
echo ""
read -rp "  Enter 1, 2, or 3: " ROLE
echo ""

# ── gateway port ─────────────────────────────
GATEWAY_PORT=8000
if [[ "$ROLE" == "1" || "$ROLE" == "3" ]]; then
  read -rp "  Gateway port [${GATEWAY_PORT}]: " _port
  [ -n "$_port" ] && GATEWAY_PORT="$_port"
fi

# ── gateway URL (for node) ────────────────────
GATEWAY_URL="http://${NODE_IP}:${GATEWAY_PORT}"
if [[ "$ROLE" == "2" ]]; then
  echo -e "  ${YELLOW}You need the gateway's IP to register this node.${RESET}"
  read -rp "  Enter gateway URL [e.g. http://192.168.20.30:8000]: " GATEWAY_URL
  echo ""
fi

# ── node config ───────────────────────────────
NODE_PORT=9000
MODEL="llama3.2"
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  read -rp "  Node port [${NODE_PORT}]: " _nport
  [ -n "$_nport" ] && NODE_PORT="$_nport"

  echo ""
  echo -e "  ${BOLD}Which model should this node serve?${RESET}"
  echo -e "  ${DIM}(must be pulled in Ollama on this machine)${RESET}"
  echo ""
  echo -e "  ${CYAN}1)${RESET} llama3.2    ${DIM}(small, fast, general)${RESET}"
  echo -e "  ${CYAN}2)${RESET} llama3      ${DIM}(larger general model)${RESET}"
  echo -e "  ${CYAN}3)${RESET} phi3        ${DIM}(tiny, very fast)${RESET}"
  echo -e "  ${CYAN}4)${RESET} deepseek-coder:6.7b  ${DIM}(coding specialist)${RESET}"
  echo -e "  ${CYAN}5)${RESET} Custom      ${DIM}(type your own)${RESET}"
  echo ""
  read -rp "  Enter 1-5 [1]: " _mchoice
  case "$_mchoice" in
    2) MODEL="llama3" ;;
    3) MODEL="phi3" ;;
    4) MODEL="deepseek-coder:6.7b" ;;
    5) read -rp "  Model name: " MODEL ;;
    *) MODEL="llama3.2" ;;
  esac
  echo ""
fi

# ── region ────────────────────────────────────
REGION="LAN"
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  read -rp "  Region tag [LAN]: " _region
  [ -n "$_region" ] && REGION="$_region"
  echo ""
fi

# ── summary ───────────────────────────────────
echo -e "${BOLD}── Setup summary ──────────────────────────${RESET}"
if [[ "$ROLE" == "1" || "$ROLE" == "3" ]]; then
  echo -e "  Role:        ${CYAN}Gateway${RESET}"
  echo -e "  Gateway URL: ${CYAN}http://${NODE_IP}:${GATEWAY_PORT}${RESET}"
  echo -e "  Control:     ${CYAN}http://${NODE_IP}:${GATEWAY_PORT}/${RESET}"
  echo -e "  Chat UI:     ${CYAN}http://${NODE_IP}:${GATEWAY_PORT}/chat-ui${RESET}"
fi
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  echo -e "  Role:        ${CYAN}Node${RESET}"
  echo -e "  Node IP:     ${CYAN}${NODE_IP}:${NODE_PORT}${RESET}"
  echo -e "  Gateway:     ${CYAN}${GATEWAY_URL}${RESET}"
  echo -e "  Model:       ${CYAN}${MODEL}${RESET}"
  echo -e "  Region:      ${CYAN}${REGION}${RESET}"
fi
echo ""
read -rp "  Looks good? Start setup (y/n) [y]: " _confirm
[[ "$_confirm" == "n" || "$_confirm" == "N" ]] && echo "Aborted." && exit 0
echo ""

# ── clone / update repo ───────────────────────
echo -e "${BOLD}── Cloning / updating repo ─────────────────${RESET}"
if [ -d "$REPO_DIR/.git" ]; then
  echo "  Repo already exists – pulling latest..."
  git -C "$REPO_DIR" pull --ff-only
else
  echo "  Cloning from ${REPO_URL} ..."
  git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
echo ""

# ── self-update ───────────────────────────────
# If this script was launched from outside the repo (e.g. an extracted
# zip), re-exec from the freshly-pulled repo copy so any fixes in the
# latest version are always applied.
if [ -f "setup.sh" ]; then
  _setup_self="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/$(basename "${BASH_SOURCE[0]}")"
  _setup_repo="$(pwd)/setup.sh"
  if [ "$_setup_self" != "$_setup_repo" ]; then
    export _SETUP_REEXECED=1
    export ROLE NODE_IP GATEWAY_PORT GATEWAY_URL NODE_PORT MODEL REGION
    exec bash "$_setup_repo"
  fi
fi

fi  # end _SETUP_REEXECED guard

# ── python venv ───────────────────────────────
echo -e "${BOLD}── Python environment ──────────────────────${RESET}"
if [ ! -d ".venv" ]; then
  echo "  Creating .venv ..."
  if command -v python3 >/dev/null 2>&1; then
    python3 -m venv .venv
  else
    python -m venv .venv
  fi
fi
# shellcheck disable=SC1091
# Windows (MINGW/Git Bash) uses Scripts/activate; Unix uses bin/activate
if [ -f ".venv/Scripts/activate" ]; then
  source .venv/Scripts/activate
else
  source .venv/bin/activate
fi
echo "  Installing microwave-ai ..."
# Use 'python -m pip' so pip can upgrade itself on Windows
python -m pip install --upgrade pip --quiet 2>/dev/null || true
python -m pip install -e . --quiet
echo -e "  ${GREEN}Done.${RESET}"
echo ""

# ── ollama check (node) ───────────────────────
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  echo -e "${BOLD}── Ollama check ────────────────────────────${RESET}"
  if ! command -v ollama >/dev/null 2>&1; then
    echo -e "  ${YELLOW}Ollama is not installed.${RESET}"
    echo "  Install it from https://ollama.com and re-run this script."
    echo "  Or install it now and press Enter to continue."
    read -rp "  Press Enter once Ollama is installed... " _
  fi

  echo "  Checking if '${MODEL}' is available locally ..."
  if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
    echo "  Model '${MODEL}' not found. Pulling now (this may take a while) ..."
    ollama pull "$MODEL"
  else
    echo -e "  ${GREEN}Model '${MODEL}' already present.${RESET}"
  fi
  echo ""
fi

# ── start services ────────────────────────────
echo -e "${BOLD}── Starting Microwave AI ───────────────────${RESET}"
echo ""

if [[ "$ROLE" == "3" ]]; then
  # Both: start gateway in background, then node in foreground
  echo "  Starting gateway in background on port ${GATEWAY_PORT} ..."
  microwave-gateway --host 0.0.0.0 --port "$GATEWAY_PORT" &
  GATEWAY_PID=$!
  echo "  Gateway PID: ${GATEWAY_PID}"
  sleep 1
  echo ""
  echo "  Starting node on port ${NODE_PORT} ..."
  microwave-node \
    --gateway-url "$GATEWAY_URL" \
    --region "$REGION" \
    --model "$MODEL" \
    --host "$NODE_IP" \
    --port "$NODE_PORT"
elif [[ "$ROLE" == "1" ]]; then
  echo "  Starting gateway on port ${GATEWAY_PORT} ..."
  echo ""
  echo -e "  ${DIM}Control plane: http://${NODE_IP}:${GATEWAY_PORT}/${RESET}"
  echo -e "  ${DIM}Chat UI:       http://${NODE_IP}:${GATEWAY_PORT}/chat-ui${RESET}"
  echo ""
  microwave-gateway --host 0.0.0.0 --port "$GATEWAY_PORT"
else
  echo "  Starting node on port ${NODE_PORT} ..."
  microwave-node \
    --gateway-url "$GATEWAY_URL" \
    --region "$REGION" \
    --model "$MODEL" \
    --host "$NODE_IP" \
    --port "$NODE_PORT"
fi
