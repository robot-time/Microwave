#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────
#  Microwave AI – interactive setup
# ─────────────────────────────────────────────

REPO_URL="https://github.com/robot-time/Microwave.git"
REPO_DIR="Microwave"

# ── find python binary ───────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    PYTHON="$cmd"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "ERROR: Python not found. Install Python 3.10+ and re-run."
  exit 1
fi

# ── colours ──────────────────────────────────
BOLD="\033[1m"
CYAN="\033[1;36m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
DIM="\033[2m"
RESET="\033[0m"

# ── are we already inside the repo? ──────────
# If pyproject.toml exists here, we're already in the repo – just pull.
# Otherwise clone/pull into a subdirectory, then re-exec from the new copy.
if [ ! -f "pyproject.toml" ]; then
  if [ -d "$REPO_DIR/.git" ]; then
    echo "Updating repo ..."
    git -C "$REPO_DIR" pull --ff-only || true
  else
    echo "Cloning repo ..."
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  cd "$REPO_DIR"
  # Re-exec the UPDATED setup.sh so all fixes take effect
  exec bash setup.sh
fi

# ── From here on, we are definitely inside the repo ──

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
  # Linux: ip route
  if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ { print $7; exit }')
  fi
  # macOS: ipconfig getifaddr (skip on Windows where ipconfig is a different tool)
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
  # Windows (Git Bash / MINGW): parse ipconfig.exe for IPv4
  if [ -z "$ip" ] && command -v ipconfig.exe >/dev/null 2>&1; then
    ip=$(ipconfig.exe 2>/dev/null | grep -i "IPv4" | head -1 | sed 's/.*: //' | tr -d '\r' || true)
  fi
  # Fallback: hostname -I (Linux)
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
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

# ── connection mode (node) ────────────────────
REVERSE_MODE=false
if [[ "$ROLE" == "2" ]]; then
  echo -e "  ${BOLD}Connection mode:${RESET}"
  echo -e "  ${CYAN}a)${RESET} Direct   ${DIM}(node listens on a port – use on LAN or when you control the firewall)${RESET}"
  echo -e "  ${CYAN}b)${RESET} Reverse  ${DIM}(node connects OUT to gateway – works behind NAT/firewall, no ports needed)${RESET}"
  echo ""
  read -rp "  Enter a or b [b]: " _connmode
  [[ "$_connmode" != "a" && "$_connmode" != "A" ]] && REVERSE_MODE=true
  echo ""
fi

# ── node config ───────────────────────────────
NODE_PORT=9000
MODEL="llama3.2"
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  if [[ "$REVERSE_MODE" == false ]]; then
    read -rp "  Node port [${NODE_PORT}]: " _nport
    [ -n "$_nport" ] && NODE_PORT="$_nport"
  fi

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
  if [[ "$REVERSE_MODE" == true ]]; then
    echo -e "  Connection:  ${CYAN}Reverse (WebSocket)${RESET}"
  else
    echo -e "  Node IP:     ${CYAN}${NODE_IP}:${NODE_PORT}${RESET}"
  fi
  echo -e "  Gateway:     ${CYAN}${GATEWAY_URL}${RESET}"
  echo -e "  Model:       ${CYAN}${MODEL}${RESET}"
  echo -e "  Region:      ${CYAN}${REGION}${RESET}"
fi
echo ""
read -rp "  Looks good? Start setup (y/n) [y]: " _confirm
[[ "$_confirm" == "n" || "$_confirm" == "N" ]] && echo "Aborted." && exit 0
echo ""

# ── pull latest (we're already in the repo) ──
echo -e "${BOLD}── Pulling latest code ─────────────────────${RESET}"
git pull --ff-only 2>/dev/null || true
echo ""

# ── python venv ───────────────────────────────
# We skip 'source activate' entirely – just prepend the venv dir to PATH.
# This works identically on Windows (Scripts/) and Unix (bin/).
echo -e "${BOLD}── Python environment ──────────────────────${RESET}"
if [ ! -d ".venv" ]; then
  echo "  Creating .venv ..."
  $PYTHON -m venv .venv
fi

VENV_BIN=""
if [ -d ".venv/Scripts" ]; then
  VENV_BIN="$(cd .venv/Scripts && pwd)"
elif [ -d ".venv/bin" ]; then
  VENV_BIN="$(cd .venv/bin && pwd)"
fi

if [ -z "$VENV_BIN" ]; then
  echo -e "  ${RED}venv created but Scripts/ and bin/ are both missing.${RESET}"
  echo "  Try deleting .venv and re-running:  rm -rf .venv && bash setup.sh"
  exit 1
fi

export PATH="$VENV_BIN:$PATH"
echo "  venv active ($VENV_BIN)"

echo "  Installing microwave-ai ..."
pip install --upgrade pip >/dev/null 2>&1 || true
pip install -e . >/dev/null 2>&1 || {
  echo -e "  ${YELLOW}pip install -e . had issues, retrying ...${RESET}"
  pip install -e . 2>&1 || true
}
echo -e "  ${GREEN}Done.${RESET}"
echo ""

# ── ollama check (node) ───────────────────────
if [[ "$ROLE" == "2" || "$ROLE" == "3" ]]; then
  echo -e "${BOLD}── Ollama check ────────────────────────────${RESET}"
  if ! command -v ollama >/dev/null 2>&1; then
    echo -e "  ${YELLOW}Ollama is not installed (or not in PATH).${RESET}"
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

# ── Windows firewall hint (only for direct HTTP mode) ──
if command -v ipconfig.exe >/dev/null 2>&1; then
  if [[ ("$ROLE" == "2" || "$ROLE" == "3") && "$REVERSE_MODE" == false ]]; then
    echo -e "${YELLOW}── Windows Firewall ────────────────────────${RESET}"
    echo -e "  The gateway needs to reach this node on port ${CYAN}${NODE_PORT}${RESET}."
    echo -e "  If health checks fail, allow the port through the firewall:"
    echo -e "  ${DIM}  (Run in an Admin PowerShell)${RESET}"
    echo -e "  ${CYAN}netsh advfirewall firewall add rule name=\"Microwave Node\" dir=in action=allow protocol=TCP localport=${NODE_PORT}${RESET}"
    echo ""
  fi
fi

# ── start services ────────────────────────────
echo -e "${BOLD}── Starting Microwave AI ───────────────────${RESET}"
echo ""

if [[ "$ROLE" == "3" ]]; then
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
elif [[ "$REVERSE_MODE" == true ]]; then
  echo "  Starting node in reverse mode (WebSocket) ..."
  microwave-node \
    --gateway-url "$GATEWAY_URL" \
    --region "$REGION" \
    --model "$MODEL" \
    --reverse
else
  echo "  Starting node on port ${NODE_PORT} ..."
  microwave-node \
    --gateway-url "$GATEWAY_URL" \
    --region "$REGION" \
    --model "$MODEL" \
    --host "$NODE_IP" \
    --port "$NODE_PORT"
fi
