#!/usr/bin/env bash
# ============================================================
# Claude Trading Daemon - launch script (v9.2, llmspy gateway)
# ============================================================
# Configuration lives in a local `.env` file (copy .env.example -> .env and
# edit). Every variable below can also be provided by the calling
# environment; precedence is:
#     already-exported environment  >  .env  >  default in this script
# Secrets (API keys) are deliberately NOT given defaults here - put them in
# `.env` (git-ignored, chmod 600) or export them before running this script.
#
# Usage:
#   cp .env.example .env && chmod 600 .env    # then edit .env
#   chmod +x claude_bot.sh
#   ./claude_bot.sh
#
# Every variable claude_bot.py reads is documented in .env.example and in the
# README. DRY_RUN defaults to "true": no real orders are sent until you set
# DRY_RUN=false.

set -u
cd "$(dirname "$0")" || exit 1

# --- Load .env (variables already in the environment always win) ----------
# Minimal dotenv parser: KEY=VALUE lines, optional `export ` prefix, optional
# surrounding single/double quotes, '#' comment lines. No inline comments and
# no shell expansion - .env is treated as data, never executed.
load_dotenv() {
    local file="$1" line key val
    local perm
    perm="$(stat -c '%a' "$file" 2>/dev/null || echo '')"
    if [ -n "$perm" ] && [ "$perm" != "600" ] && [ "$perm" != "400" ]; then
        echo "[!] $file is mode $perm - it may contain API keys. Consider: chmod 600 $file"
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"                       # tolerate CRLF files
        case "$line" in ''|'#'*|' '*'#'*) continue ;; esac
        line="${line#export }"
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"; val="${line#*=}"
        key="${key//[[:space:]]/}"
        case "$key" in [A-Za-z_]*) ;; *) continue ;; esac
        if [ "${#val}" -ge 2 ]; then
            case "$val" in
                \"*\") val="${val:1:${#val}-2}" ;;
                \'*\') val="${val:1:${#val}-2}" ;;
            esac
        fi
        if [ -z "${!key+x}" ]; then export "$key=$val"; fi
    done < "$file"
}
[ -f .env ] && load_dotenv .env

# --- Defaults (only applied if not set by the environment / .env) ---------
# llmspy gateway (single OpenAI-chat-completions endpoint for every tier,
# local and cloud alike - no personal hostname defaulted here, set it in .env)
export LLMSPY_BASE_URL="${LLMSPY_BASE_URL:-http://localhost:8000/v1/chat/completions}"

# Instrument & timeframe
export TRADE_SYMBOL="${TRADE_SYMBOL:-EURUSD}"         # exactly as your broker lists it
export TRADE_TIMEFRAME="${TRADE_TIMEFRAME:-M15}"      # M1 | M5 | M15 | M30 | H1 | H4
export BROKER_UTC_OFFSET_HOURS="${BROKER_UTC_OFFSET_HOURS:-0}"   # display-only label shift
export MAGIC_NUMBER="${MAGIC_NUMBER:-260922}"         # changing it orphans open positions

# Position / risk
export MAX_POSITIONS_PER_DIRECTION="${MAX_POSITIONS_PER_DIRECTION:-1}"
export FIXED_LOT_SIZE="${FIXED_LOT_SIZE:-0.01}"       # 0 = risk-% based sizing
export DRY_RUN="${DRY_RUN:-true}"                     # true = never send orders
export MIN_CONFIDENCE="${MIN_CONFIDENCE:-0.75}"
export STARTING_RISK_PCT="${STARTING_RISK_PCT:-0.25}"
export MAX_RISK_PCT="${MAX_RISK_PCT:-1.5}"
export RISK_STEP_PCT="${RISK_STEP_PCT:-0.15}"
export DAILY_LOSS_LIMIT_PCT="${DAILY_LOSS_LIMIT_PCT:-3.0}"

# Candle / indicator lookback
export LOOKBACK_CANDLES="${LOOKBACK_CANDLES:-50}"     # must stay >= 22
export PROMPT_BARS="${PROMPT_BARS:-3}"

# Generation limits (num_ctx/num_thread are NOT settable here - llmspy
# doesn't forward Ollama's nested "options" object; configure those on the
# llmspy/Ollama side directly)
export LLMSPY_MAX_TOKENS="${LLMSPY_MAX_TOKENS:-128}"
export KEEP_LOCAL_ALIVE="${KEEP_LOCAL_ALIVE:-0}"         # minutes; 0 = Ollama's own default (~5m)
export LLM_MAX_LATENCY_SEC="${LLM_MAX_LATENCY_SEC:-0}"   # 0 = per-timeframe budget table

# Initial SL/TP (ATR multiples, set once at entry)
export ATR_SL_MULTIPLIER="${ATR_SL_MULTIPLIER:-2.0}"
export ATR_TP_MULTIPLIER="${ATR_TP_MULTIPLIER:-4.0}"
export EXTENDED_TP_ATR_MULT="${EXTENDED_TP_ATR_MULT:-6.0}"
export MAX_TP_EXTENSION_ATR_MULT="${MAX_TP_EXTENSION_ATR_MULT:-10.0}"

# Chandelier Exit trailing stop
export TRAIL_ACTIVATION_ATR_MULT="${TRAIL_ACTIVATION_ATR_MULT:-1.5}"
export CHANDELIER_ATR_MULT="${CHANDELIER_ATR_MULT:-3.0}"

# Escalation chain - ordered, comma-separated model names, tier 0 first.
# llmspy decides per-model whether that name is local or cloud; no API keys
# are needed here anymore, llmspy is the only thing that authenticates outward.
export LLMSPY_MODELS="${LLMSPY_MODELS:-phi4-mini,gpt-oss-20b}"
export ESCALATION_ENABLED="${ESCALATION_ENABLED:-true}"
export ESCALATION_CONFIDENCE_THRESHOLD="${ESCALATION_CONFIDENCE_THRESHOLD:-0.80}"
export ESCALATION_SHARE_LOCAL_ANSWER="${ESCALATION_SHARE_LOCAL_ANSWER:-false}"
export LLMSPY_TIMEOUT_SEC="${LLMSPY_TIMEOUT_SEC:-40}"    # same timeout applied to every tier call

# Wine / MT5 environment
export WINEDEBUG="${WINEDEBUG:-fixme-all}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"        # point at the prefix where MT5 + Python live

# Force UTF-8 under wine (stdout otherwise defaults to the host codepage and
# can crash on non-ASCII characters in LLM output).
export PYTHONUTF8="1"
export PYTHONIOENCODING="utf-8"

# --- Sanity checks ---------------------------------------------------------
if [ ! -f claude_bot.py ]; then
    echo "[!] claude_bot.py not found in $(pwd)"; exit 1
fi
if [ -f claude_bot.pid ] && kill -0 "$(cat claude_bot.pid)" 2>/dev/null; then
    echo "[!] Already running (PID $(cat claude_bot.pid)). Stop it first: kill $(cat claude_bot.pid)"
    exit 1
fi
if [ "$DRY_RUN" = "true" ]; then
    echo "[*] DRY_RUN=true - orders will be logged but NOT sent."
else
    echo "[!] DRY_RUN=false - LIVE ORDERS will be sent to the broker."
fi

# --- Launch ----------------------------------------------------------------
nohup wine python -u claude_bot.py > claude_bot.log 2>&1 &
echo $! > claude_bot.pid
echo "[*] claude_bot.py launched (PID $!). Tail claude_bot.log or trade_log.jsonl to watch it."
