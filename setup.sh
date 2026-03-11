#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────
#  Microwave AI – one-command setup
# ─────────────────────────────────────────────

REPO_URL="https://github.com/robot-time/Microwave.git"
REPO_DIR="Microwave"
CONFIG_FILE=".microwave.env"

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
if [ ! -f "pyproject.toml" ]; then
  if [ -d "$REPO_DIR/.git" ]; then
    echo "Updating repo ..."
    git -C "$REPO_DIR" pull --ff-only || true
  else
    echo "Cloning repo ..."
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  cd "$REPO_DIR"
  exec bash setup.sh
fi

# ── From here on, we are inside the repo ─────

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
echo -e "${DIM}github.com/robot-time/Microwave${RESET}"
echo ""

# ── detect LAN IP ────────────────────────────
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

NODE_IP=$(detect_ip)
if [ -z "$NODE_IP" ]; then
  echo -e "${RED}Could not auto-detect your LAN IP.${RESET}"
  read -rp "  Enter this machine's LAN IP: " NODE_IP
fi

# ── defaults (node, reverse mode, llama3.2) ──
ROLE="2"
REVERSE_MODE=true
GATEWAY_PORT=8000
NODE_PORT=9000
MODEL="llama3.2"
REGION="LAN"
GATEWAY_URL="${MICROWAVE_GATEWAY_URL:-https://electricity-guzzler.tail7917c7.ts.net}"

echo -e "  LAN IP:  ${CYAN}${NODE_IP}${RESET}"
echo -e "  Mode:    ${CYAN}Node (reverse WebSocket)${RESET}"
echo -e "  Gateway: ${CYAN}${GATEWAY_URL}${RESET}"
echo -e "  Model:   ${CYAN}${MODEL}${RESET}"
echo ""

# ── persist config for run.sh ────────────────
cat > "$CONFIG_FILE" <<EOF
MICROWAVE_ROLE="$ROLE"
MICROWAVE_GATEWAY_URL="$GATEWAY_URL"
MICROWAVE_GATEWAY_PORT="$GATEWAY_PORT"
MICROWAVE_NODE_IP="$NODE_IP"
MICROWAVE_NODE_PORT="$NODE_PORT"
MICROWAVE_REGION="$REGION"
MICROWAVE_MODEL="$MODEL"
MICROWAVE_REVERSE_MODE="$REVERSE_MODE"
EOF

# ── pull latest ──────────────────────────────
echo -e "${BOLD}[1/3] Pulling latest code${RESET}"
git pull --ff-only 2>/dev/null || true
echo ""

# ── python venv + install ────────────────────
echo -e "${BOLD}[2/3] Python environment${RESET}"
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
  echo -e "  ${RED}venv created but bin dir missing.${RESET}"
  echo "  Try: rm -rf .venv && bash setup.sh"
  exit 1
fi

export PATH="$VENV_BIN:$PATH"
pip install --upgrade pip >/dev/null 2>&1 || true
pip install -e . >/dev/null 2>&1 || {
  echo -e "  ${YELLOW}Retrying pip install ...${RESET}"
  pip install -e . 2>&1 || true
}
echo -e "  ${GREEN}Done.${RESET}"
echo ""

# ── ollama check + model pull ────────────────
echo -e "${BOLD}[3/3] Ollama + model${RESET}"
if ! command -v ollama >/dev/null 2>&1; then
  echo -e "  ${YELLOW}Ollama not found.${RESET} Installing ..."

  if command -v ipconfig.exe >/dev/null 2>&1; then
    echo "  Windows: open PowerShell and run:"
    echo "    irm https://ollama.com/install.ps1 | iex"
    read -rp "  Press Enter once Ollama is installed... " _
  else
    if bash -c "curl -fsSL https://ollama.com/install.sh | sh"; then
      echo -e "  ${GREEN}Ollama installed.${RESET}"
    else
      echo -e "  ${YELLOW}Auto-install failed.${RESET} Run manually:"
      echo "    curl -fsSL https://ollama.com/install.sh | sh"
      read -rp "  Press Enter once Ollama is installed... " _
    fi
  fi
fi

while ! command -v ollama >/dev/null 2>&1; do
  echo -e "  ${RED}Ollama still not in PATH.${RESET}"
  read -rp "  Press Enter to retry (or Ctrl+C to quit)... " _
done

if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
  echo "  Pulling '${MODEL}' (first time only) ..."
  ollama pull "$MODEL"
else
  echo -e "  ${GREEN}'${MODEL}' ready.${RESET}"
fi
echo ""

# ── start ────────────────────────────────────
echo -e "${GREEN}Setup complete. Starting node ...${RESET}"
echo ""
microwave-node \
  --gateway-url "$GATEWAY_URL" \
  --region "$REGION" \
  --model "$MODEL" \
  --reverse
