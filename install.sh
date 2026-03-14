#!/bin/sh
# ─────────────────────────────────────────────────────
#  Microwave AI – one-line installer
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/robot-time/Microwave/main/install.sh | sh
#
#  Or with options:
#    curl -fsSL https://raw.githubusercontent.com/robot-time/Microwave/main/install.sh | MICROWAVE_EXPERT_DOMAINS=code,math sh
#
#  This script is designed to be piped from curl.
#  It clones the repo, installs deps, and starts the node.
# ─────────────────────────────────────────────────────
set -e

REPO_URL="https://github.com/robot-time/Microwave.git"
INSTALL_DIR="${MICROWAVE_DIR:-$HOME/Microwave}"

C="\033[1;36m"; G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"
B="\033[1m"; D="\033[2m"; X="\033[0m"

printf '     ________________\n'
printf '    |.-----------.   |\n'
printf '    ||   _____   |ooo|\n'
printf '    ||  |     |  |ooo|\n'
printf '    ||  |     |  | = |\n'
printf "    ||  '-----'  | _ |\n"
printf '    ||___________|[_]|\n'
printf "    '----------------'\n"
printf "${B}Microwave AI${X} – one-line installer\n\n"

# ── check prerequisites ──────────────────────────
check_cmd() {
  command -v "$1" >/dev/null 2>&1
}

PYTHON=""
for cmd in python3 python; do
  check_cmd "$cmd" && { PYTHON="$cmd"; break; }
done
if [ -z "$PYTHON" ]; then
  printf "${R}Python not found.${X} Install Python 3.10+: https://python.org\n"
  exit 1
fi

if ! check_cmd git; then
  printf "${R}Git not found.${X} Install git: https://git-scm.com\n"
  exit 1
fi

if ! check_cmd curl; then
  printf "${R}curl not found.${X}\n"
  exit 1
fi

# ── clone or update ──────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
  printf "${D}Updating existing install at ${INSTALL_DIR}...${X}\n"
  git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || true
else
  printf "Cloning Microwave into ${C}${INSTALL_DIR}${X}...\n"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── python venv ──────────────────────────────────
printf "\n${B}[1/3]${X} Python environment\n"
if [ ! -d ".venv" ]; then
  $PYTHON -m venv .venv
fi

VENV_BIN=""
[ -d ".venv/Scripts" ] && VENV_BIN=".venv/Scripts"
[ -d ".venv/bin" ] && VENV_BIN=".venv/bin"
if [ -z "$VENV_BIN" ]; then
  printf "${R}venv creation failed. Delete .venv and retry.${X}\n"
  exit 1
fi

export PATH="$(cd "$VENV_BIN" && pwd):$PATH"
pip install --upgrade pip -q 2>/dev/null || true
pip install -e . -q 2>&1 || pip install -e . 2>&1
printf "  ${G}done${X}\n"

# ── ollama ───────────────────────────────────────
printf "${B}[2/3]${X} Ollama + models\n"
MODEL="${MICROWAVE_MODEL:-llama3.2}"

if ! check_cmd ollama; then
  printf "  ${Y}Installing Ollama...${X}\n"
  curl -fsSL https://ollama.com/install.sh | sh 2>/dev/null || {
    printf "  ${Y}Auto-install failed.${X} Install manually: https://ollama.com\n"
    printf "  Then re-run: ${C}cd %s && bash setup.sh${X}\n" "$INSTALL_DIR"
    exit 1
  }
fi

ollama list 2>/dev/null | grep -q "$MODEL" || { printf "  Pulling %s ...\n" "$MODEL"; ollama pull "$MODEL"; }
printf "  ${G}%s ready${X}\n" "$MODEL"

# ── auto-detect location ────────────────────────
LAT="${MICROWAVE_LAT:-0.0}"
LON="${MICROWAVE_LON:-0.0}"
if [ "$LAT" = "0.0" ] && [ "$LON" = "0.0" ]; then
  GEO=$(curl -sf "http://ip-api.com/json/?fields=lat,lon,status" 2>/dev/null || printf '{}')
  LAT=$($PYTHON -c "import json,sys;d=json.loads(sys.argv[1]);print(d.get('lat',0.0))" "$GEO" 2>/dev/null || printf "0.0")
  LON=$($PYTHON -c "import json,sys;d=json.loads(sys.argv[1]);print(d.get('lon',0.0))" "$GEO" 2>/dev/null || printf "0.0")
fi

# ── build launch args ───────────────────────────
GATEWAY_URL="${MICROWAVE_GATEWAY_URL:-https://electricity-guzzler.tail7917c7.ts.net}"
REGION="${MICROWAVE_REGION:-LAN}"
EXPERT_DOMAINS="${MICROWAVE_EXPERT_DOMAINS:-general}"
ENGINE_TYPE="${MICROWAVE_ENGINE:-ollama}"

printf "\n${B}[3/3]${X} Starting expert node\n"
printf "  ${C}Model${X}    %s\n" "$MODEL"
printf "  ${C}Domains${X}  %s\n" "$EXPERT_DOMAINS"
printf "  ${C}Gateway${X}  %s\n" "$GATEWAY_URL"
[ "$LAT" != "0.0" ] && printf "  ${C}Location${X} %s, %s\n" "$LAT" "$LON"
printf "\n${G}Setup complete.${X} Connecting to network ...\n"
printf "  Next time, just run: ${C}microwave run${X}\n"
printf "  Check the network:   ${C}microwave status${X}\n\n"

exec microwave run \
  --gateway-url "$GATEWAY_URL" \
  --region "$REGION" \
  --model "$MODEL" \
  --engine "$ENGINE_TYPE" \
  --latitude "$LAT" \
  --longitude "$LON" \
  --expert-domains "$EXPERT_DOMAINS" \
  --reverse
