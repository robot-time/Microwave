#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────
#  Microwave AI – zero-config setup
#  Just run: bash setup.sh
#
#  Optional env overrides (set before running):
#    MICROWAVE_GATEWAY_URL   gateway address
#    MICROWAVE_MODEL         model to pull (default: llama3.2)
#    MICROWAVE_REGION        region label (default: LAN)
#    MICROWAVE_MODE          "simple" or "pipeline"
#    MICROWAVE_DRAFT_MODELS  comma-separated draft models
#    MICROWAVE_EXPERT_DOMAINS comma-separated domains (code,math,general)
#    MICROWAVE_LAT / LON     manual coordinates
# ─────────────────────────────────────────────

REPO_URL="https://github.com/robot-time/Microwave.git"
REPO_DIR="Microwave"
CONFIG_FILE=".microwave.env"

C="\033[1;36m"; G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"
B="\033[1m"; D="\033[2m"; X="\033[0m"

# ── find python ──────────────────────────────
PYTHON=""
for cmd in python3 python; do
  command -v "$cmd" >/dev/null 2>&1 && { PYTHON="$cmd"; break; }
done
[ -z "$PYTHON" ] && { echo -e "${R}Python not found. Install Python 3.10+.${X}"; exit 1; }

# ── clone if needed ──────────────────────────
if [ ! -f "pyproject.toml" ]; then
  [ -d "$REPO_DIR/.git" ] && git -C "$REPO_DIR" pull --ff-only 2>/dev/null || git clone "$REPO_URL" "$REPO_DIR"
  cd "$REPO_DIR" && exec bash setup.sh
fi

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
EOF
echo -e "${B}Microwave Network${X} v0.3.0"
echo ""

# ── auto-detect everything ───────────────────
detect_ip() {
  local ip=""
  command -v ip >/dev/null 2>&1 && ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7;exit}')
  [ -z "$ip" ] && ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  [ -z "$ip" ] && command -v hostname >/dev/null 2>&1 && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "${ip:-0.0.0.0}"
}

NODE_IP=$(detect_ip)
MODEL="${MICROWAVE_MODEL:-llama3.2}"
REGION="${MICROWAVE_REGION:-LAN}"
GATEWAY_URL="${MICROWAVE_GATEWAY_URL:-https://electricity-guzzler.tail7917c7.ts.net}"
SETUP_MODE="${MICROWAVE_MODE:-simple}"
ENGINE_TYPE="ollama"
DRAFT_MODELS="${MICROWAVE_DRAFT_MODELS:-}"
EXPERT_DOMAINS="${MICROWAVE_EXPERT_DOMAINS:-general}"
LAT="${MICROWAVE_LAT:-0.0}"
LON="${MICROWAVE_LON:-0.0}"

[ "$SETUP_MODE" = "pipeline" ] && ENGINE_TYPE="llamacpp"

# auto-geolocate if no manual coords
if [ "$LAT" = "0.0" ] && [ "$LON" = "0.0" ]; then
  GEO=$(curl -sf "http://ip-api.com/json/?fields=lat,lon,status" 2>/dev/null || echo '{}')
  LAT=$($PYTHON -c "import sys,json;d=json.load(sys.stdin);print(d.get('lat',0.0))" <<< "$GEO" 2>/dev/null || echo "0.0")
  LON=$($PYTHON -c "import sys,json;d=json.load(sys.stdin);print(d.get('lon',0.0))" <<< "$GEO" 2>/dev/null || echo "0.0")
fi

echo -e "  ${C}IP${X}  $NODE_IP   ${C}Model${X}  $MODEL   ${C}Gateway${X}  $GATEWAY_URL"
echo -e "  ${C}Domains${X}  $EXPERT_DOMAINS"
[ "$LAT" != "0.0" ] && echo -e "  ${C}Location${X}  $LAT, $LON"
echo ""

# ── save config ──────────────────────────────
cat > "$CONFIG_FILE" <<EOF
MICROWAVE_GATEWAY_URL="$GATEWAY_URL"
MICROWAVE_NODE_IP="$NODE_IP"
MICROWAVE_REGION="$REGION"
MICROWAVE_MODEL="$MODEL"
MICROWAVE_ENGINE="$ENGINE_TYPE"
MICROWAVE_DRAFT_MODELS="$DRAFT_MODELS"
MICROWAVE_EXPERT_DOMAINS="$EXPERT_DOMAINS"
MICROWAVE_LAT="$LAT"
MICROWAVE_LON="$LON"
MICROWAVE_SETUP_MODE="$SETUP_MODE"
EOF

# ── [1/3] python ─────────────────────────────
echo -e "${B}[1/3]${X} Python environment"
[ ! -d ".venv" ] && $PYTHON -m venv .venv
VENV_BIN=""
[ -d ".venv/Scripts" ] && VENV_BIN="$(cd .venv/Scripts && pwd)"
[ -d ".venv/bin" ] && VENV_BIN="$(cd .venv/bin && pwd)"
[ -z "$VENV_BIN" ] && { echo -e "${R}venv bin missing. rm -rf .venv && retry.${X}"; exit 1; }
export PATH="$VENV_BIN:$PATH"
pip install --upgrade pip -q 2>/dev/null || true
if [ "$SETUP_MODE" = "pipeline" ]; then
  pip install -e ".[pipeline]" -q 2>&1 || pip install -e ".[pipeline]" 2>&1
else
  pip install -e . -q 2>&1 || pip install -e . 2>&1
fi
echo -e "  ${G}done${X}"

# ── [2/3] ollama ─────────────────────────────
echo -e "${B}[2/3]${X} Ollama + models"
if ! command -v ollama >/dev/null 2>&1; then
  echo -e "  ${Y}Installing Ollama...${X}"
  curl -fsSL https://ollama.com/install.sh | sh 2>/dev/null || {
    echo -e "  ${Y}Auto-install failed.${X} Install manually: https://ollama.com"
    echo -e "  Then re-run: ${C}bash setup.sh${X}"
    exit 1
  }
fi
ollama list 2>/dev/null | grep -q "$MODEL" || { echo "  Pulling $MODEL ..."; ollama pull "$MODEL"; }
echo -e "  ${G}$MODEL ready${X}"

if [ -n "$DRAFT_MODELS" ]; then
  IFS=',' read -ra DA <<< "$DRAFT_MODELS"
  for dm in "${DA[@]}"; do
    dm=$(echo "$dm" | xargs)
    [ -n "$dm" ] && { ollama list 2>/dev/null | grep -q "$dm" || ollama pull "$dm"; }
  done
fi

# ── [3/3] start ──────────────────────────────
echo -e "${B}[3/3]${X} Starting node"
echo ""

ARGS="--gateway-url $GATEWAY_URL --region $REGION --model $MODEL"
ARGS="$ARGS --engine $ENGINE_TYPE --latitude $LAT --longitude $LON --reverse"
ARGS="$ARGS --expert-domains $EXPERT_DOMAINS"
[ -n "$DRAFT_MODELS" ] && ARGS="$ARGS --draft-models $DRAFT_MODELS"

echo -e "${G}Setup complete.${X} Connecting to network ..."
echo -e "  Next time, just run: ${C}microwave run${X}"
echo ""
exec microwave run $ARGS
