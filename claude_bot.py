"""
Claude Bot (v9.0)

v8 changes from v7 (folded into this version - see below for v9):
  1. TIMESTAMP FIX: the "Recent bars" shown to the LLM used
     time.strftime('%H:%M', time.localtime(bar['time'])) - time.localtime()
     converts using whatever OS timezone the Wine/Python HOST process
     happens to be running under, which has no relationship to the
     broker's server timezone OR the operator's real local time. This
     produced bar timestamps that looked "off by a couple hours" with no
     clean explanation. Fixed by formatting bar times as explicit UTC
     (dt.datetime.fromtimestamp(..., tz=timezone.utc)), with an optional
     BROKER_UTC_OFFSET_HOURS env var if you want the displayed time
     shifted to match your broker's actual server clock. The label now
     always states which timezone/offset it is, so it can't be silently
     ambiguous again. Also added an explicit note that the most recent
     bar shown is always the last FULLY CLOSED candle (copy_rates_from_pos
     is called with start index 1, deliberately skipping the still-forming
     current candle) - not a bug, but easy to mistake for one when the
     clock was already wrong.

  2. TRAILING STOP: replaced the "lock in a fraction of profit-from-entry"
     trail with a Chandelier Exit. The old method anchored the stop to
     pos.price_open, so in a strong trend it lagged further and further
     behind the current price the longer the trade ran (giving back a
     growing share of *recent* gains even though "total" gains stayed
     protected). Chandelier Exit anchors the stop to the highest price
     reached since entry (for a BUY) / lowest price reached (for a SELL),
     at a fixed ATR-multiple distance behind that peak - so it tracks the
     trend tightly and adapts to volatility via ATR, with far less lag.
     MT5 does not track "peak price since this position opened" itself,
     so that watermark is now persisted per-ticket in risk_state.json and
     pruned once a ticket is no longer open.

v7 change (kept): prompts are split into a static SYSTEM_PROMPT (persona,
trading rules, output-format contract - identical every candle) and a
per-candle user prompt (market state/bars only), sent via proper
system/user roles to both the local Ollama model and every cloud provider.

Cloud escalation (kept): when the local Ollama model returns a confidence
below CFG.escalation_confidence_threshold, the daemon escalates the same
market context to a cloud LLM (Groq/Cerebras/OpenRouter, tried in order)
for a second opinion. Both the local and cloud answers are written to the
trade log as an "escalation" event; the FINAL trade decision uses the
cloud model's action/confidence whenever escalation produced a usable
answer, and falls back to the local model's answer if every provider
fails.

Magic number is configurable via MAGIC_NUMBER (default 260922, changed from
v8's 260921 alongside this rename/rewrite - see the v9 note below on why
that matters for anyone upgrading with open positions). The bot only
sees/manages positions carrying ITS magic number - if you change it while
positions from an older run are open, those positions are orphaned (no
trailing stop, not counted toward the per-direction cap).

v9 changes (file renamed claude_v8.py -> claude_bot.py; version now tracked
via __version__ below and in MAGIC_NUMBER/order comments instead of the
filename - see git tags/CHANGELOG for the actual version history):
  - HTF bias timeframe fixed for H4 (was H1, i.e. LOWER than the trade TF);
    now M1..M30 -> H1, H1 -> H4, H4 -> D1.
  - LLM output is validated/normalised (action enum, confidence coerced to
    a float in [0,1], "85" -> 0.85) and JSON is extracted robustly, incl.
    <think>...</think> blocks emitted by reasoning models.
  - Chandelier watermark is now tracked EVERY loop for every open position
    (previously only after activation, so an earlier peak was forgotten);
    activation is judged on peak profit, and stays active once reached.
  - TP extension only re-sent when it improves by a meaningful step
    (was: a broker modify request on almost every 5s tick).
  - Trailing SL/TP rounded to the symbol's real digits, respect the broker's
    minimum stop distance, and failed modifications are now reported.
  - New entries are skipped if the daily circuit breaker tripped while the
    LLM request was still in flight.
  - Order filling mode chosen from the symbol's supported modes.
  - Closed-trade P&L now includes commission + swap; first-ever start seeds
    the deal cursor instead of replaying old deals into the risk tiers.
  - "signal" events are now written to trade_log.jsonl (the audit log
    previously only recorded trade attempts, not the decisions themselves).
  - Optional ESCALATION_SHARE_LOCAL_ANSWER (default off = independent
    second opinion). Per-provider extra request params via <NAME>_EXTRA_PARAMS.
  - Defaults no longer contain personal hostnames (localhost:11434).
  - DRY_RUN now defaults to TRUE - live trading requires DRY_RUN=false.

UPGRADING FROM v8 WITH OPEN POSITIONS: v9's default MAGIC_NUMBER (260922)
differs from v8's (260921), so this build will NOT see or manage positions
opened by v8 - no trailing stop, not counted toward the per-direction cap.
Either close out v8 positions first, or run this version with
MAGIC_NUMBER=260921 until they're closed, then switch back to the new
default.

run script
--------------------
See claude_bot.sh (same directory) - it loads .env (see .env.example),
sets every env var this file reads, and launches this file under wine/nohup.

---------------------

Automated MT5 trading daemon using a local Ollama LLM for directional signals.

Design principle: the LLM decides DIRECTION + CONVICTION only.
All risk sizing, stop placement, and circuit-breaking is deterministic code.
Never let the model set exact price levels or lot sizes.

NOT FINANCIAL ADVICE. This is software; markets are not obligated to
reward any strategy, and a small local LLM has no proven statistical
edge on raw price data. Backtest and paper-trade (DRY_RUN=true) before
risking real capital, and treat every parameter below as something to
validate, not trust.
"""

import concurrent.futures
import json
import os
import re
import signal
import sys
import time
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

# Force UTF-8 stdout/stderr regardless of the host codepage (matters under
# wine on Windows, where stdout otherwise defaults to cp1252/"charmap" and
# silently mangles/crashes on any non-ASCII character an LLM response
# might contain, e.g. U+202F narrow no-break space). Belt-and-suspenders
# alongside PYTHONUTF8/PYTHONIOENCODING set in claude_bot.sh.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

try:
    import MetaTrader5 as mt5
except ImportError:
    try:
        from mt5linux import MetaTrader5 as mt5
    except ImportError:
        print("[!] MetaTrader5 package not found in this prefix.")
        sys.exit(1)

__version__ = "9.0"

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# CONFIGURATION
# ============================================================
@dataclass
class Config:
    # ---- MT5 / Ollama connection ----
    # Hostname (or IP) of the machine running Ollama. Your MT5/wine box
    # calls out to this over HTTP; it does not need to be the same machine.
    truenas_ip: str = os.getenv("TRUENAS_IP", "localhost")
    # TCP port Ollama is listening on at truenas_ip.
    ollama_port: int = int(os.getenv("OLLAMA_PORT", "11434"))

    # ---- Instrument ----
    # MT5 symbol name exactly as your broker lists it (e.g. "EURUSD",
    # "EURUSD.a", "ETHUSD" - check mt5.symbols_get() if unsure).
    symbol: str = os.getenv("TRADE_SYMBOL", "EURUSD")
    # Tag written onto every order this bot sends (request["magic"]) so the
    # bot can find/filter/manage only ITS OWN positions among everything
    # else in the account, and so trade history can be attributed to this
    # exact code version. Bump this if you make a change you want to be
    # able to distinguish in MT5's history/position list later.
    magic_number: int = int(os.getenv("MAGIC_NUMBER", "260922"))
    # How often (seconds) the main loop wakes up to: check MT5 connection,
    # run the risk circuit breaker, sync closed trades, update trailing
    # stops, and check for a new candle. This is NOT how often the LLM is
    # called - that only happens on a new candle open (see TIMEFRAME_TABLE).
    check_interval_sec: int = 5
    # Cap on simultaneously open positions per direction (BUY and SELL are
    # counted independently) for THIS symbol+magic. E.g. 4 means up to 4
    # concurrent BUYs AND up to 4 concurrent SELLs could be open at once.
    max_positions_per_direction: int = int(os.getenv("MAX_POSITIONS_PER_DIRECTION", "1"))

    # ---- Candle / indicator lookback ----
    # How many candles are fetched from MT5 to compute EMA9/EMA21/RSI14/ATR14.
    # Must stay >= atr_period+1 (15) and >= 22, or EMA21/RSI/ATR silently
    # return None and every signal falls back to HOLD.
    indicator_lookback: int = int(os.getenv("LOOKBACK_CANDLES", "50"))
    # How many of the most-recent (fully closed) candles are actually shown
    # to the LLM in the prompt, as compact Time|O|H|L|C strings. Smaller
    # keeps the prompt short (cheaper/faster inference); larger gives the
    # model more visible price action to reason about.
    prompt_bars: int = int(os.getenv("PROMPT_BARS", "8"))
    # How many higher-timeframe candles are pulled to compute the HTF bias
    # (EMA20 vs EMA50 direction) shown to the LLM as context.
    htf_lookback: int = 60

    # ---- Bar-timestamp display (prompt only - does not affect trading) ----
    # The "Recent bars" list shown to the LLM (and printed to the console)
    # is labeled and formatted in UTC, plus this many hours, so the label
    # is always explicit about what clock it's using. Leave at 0 to show
    # plain UTC. Set to your broker's fixed UTC offset (commonly +2 or +3
    # for EET/EEST brokers, but check your broker - it varies and some
    # shift with DST) if you want the prompt/console times to match what
    # your MT5 terminal shows. This is a DISPLAY-ONLY setting: it never
    # touches bar['time'] itself, order timestamps, or any trading logic -
    # only how the string in the prompt is rendered.
    broker_utc_offset_hours: float = float(os.getenv("BROKER_UTC_OFFSET_HOURS", "0"))

    # ---- Progressive risk sizing ----
    # Risk-per-trade (% of account equity) starts here (tier 0) ...
    starting_risk_pct: float = float(os.getenv("STARTING_RISK_PCT", "0.25"))
    # ... and is never allowed to exceed this, however many tiers are earned.
    max_risk_pct: float = float(os.getenv("MAX_RISK_PCT", "1.5"))
    # Each tier promotion increases risk-per-trade by this many percentage points.
    risk_step_pct: float = float(os.getenv("RISK_STEP_PCT", "0.15"))
    # Consecutive net-positive closed trades required to earn a tier promotion.
    trades_per_promotion: int = 5
    # Consecutive losing closed trades that instantly reset risk back to tier 0.
    losses_to_demote: int = 2
    # Absolute lot-size ceiling, regardless of what the risk-% formula would
    # otherwise allow. A hard backstop against a fat-fingered or
    # mis-scaled risk calculation ever sizing an enormous position.
    max_lot_hard_cap: float = 1.0

    # ---- Circuit breaker ----
    # If today's realized+unrealized drawdown (vs. equity at the start of
    # the UTC trading day) reaches this percent, new trades are halted
    # until the next UTC day. Existing positions/trailing stops are still
    # managed - this only blocks opening NEW trades.
    daily_loss_limit_pct: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))

    # ---- Spread guard ----
    # Primary spread filter: block a new trade if current spread exceeds
    # this fraction of ATR(14). Spread-as-fraction-of-ATR is unit-agnostic,
    # so the same ratio makes sense whether the instrument is EURUSD (spread
    # measured in fractions of a pip) or something like ETHUSD (spread
    # measured in whole dollars) - a fixed pip number wouldn't generalize.
    max_spread_atr_ratio: float = 0.35
    # Fallback spread filter (in pips), used ONLY if ATR isn't available
    # yet (e.g. insufficient candle history) so there's still some guard.
    max_spread_pips: float = 2.5

    # ---- Cloud escalation ----
    # Master on/off switch. False = the local Ollama answer is always
    # final, cloud providers are never called regardless of confidence.
    escalation_enabled: bool = os.getenv("ESCALATION_ENABLED", "true").lower() == "true"
    # If the local model's confidence comes back BELOW this, escalate to
    # the cloud provider chain for a second opinion. Independent from
    # min_confidence below, which is the final entry filter applied to
    # whichever answer (local or cloud) ends up being used.
    escalation_confidence_threshold: float = float(os.getenv("ESCALATION_CONFIDENCE_THRESHOLD", "0.70"))
    # Default per-provider HTTP timeout (seconds) for cloud escalation
    # calls, used whenever a provider doesn't set its own *_TIMEOUT_SEC.
    cloud_timeout_sec: int = int(os.getenv("CLOUD_TIMEOUT_SEC", "20"))
    # false (default) = the cloud model sees only the market state and forms
    # an INDEPENDENT opinion (avoids anchoring on a weak local answer).
    # true = the local model's action/confidence/reasoning is appended to the
    # cloud prompt (see build_escalation_user_prompt).
    escalation_share_local_answer: bool = os.getenv(
        "ESCALATION_SHARE_LOCAL_ANSWER", "false").lower() == "true"

    # ---- Trade entry filter ----
    # Minimum confidence (whichever model's answer is actually used, local
    # or escalated-cloud) required to actually place a trade. Below this,
    # the signal is logged but no order is sent - even a BUY/SELL action
    # gets filtered out and treated as a no-trade.
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "0.75"))
    # ATR lookback period (number of candles) used everywhere ATR is computed.
    atr_period: int = 14

    # ---- Initial stop-loss / take-profit (set ONCE, at order entry) ----
    # Initial SL distance = ATR(14) * this multiplier, placed once when the
    # order is sent in execute_protected_trade(). Never recalculated after
    # that - only the trailing-stop logic (below) ever moves the SL again,
    # and only after trail_activation_atr_multiplier of profit is reached.
    atr_sl_multiplier: float = float(os.getenv("ATR_SL_MULTIPLIER", "2.0"))
    # Initial TP distance = ATR(14) * this multiplier, same one-time timing as above.
    atr_tp_multiplier: float = float(os.getenv("ATR_TP_MULTIPLIER", "4.0"))
    # Fallback initial SL/TP (in pips), used only if ATR couldn't be computed
    # for this candle (e.g. insufficient history).
    fallback_sl_pips: int = 20
    fallback_tp_pips: int = 40

    # ---- Extended take-profit (applied alongside the Chandelier trail) ----
    # Once the Chandelier trail activates, the TP is also pushed out to
    # ATR(14) * this multiplier from the CURRENT price (capped by
    # max_tp_extension_atr_multiplier below), letting winners run further
    # than the original one-time TP. Independent of the SL trail logic.
    extended_tp_atr_multiplier: float = float(os.getenv("EXTENDED_TP_ATR_MULT", "6.0"))
    # Hard ceiling on how far the extended TP can be pushed from the
    # position's OWN entry price, regardless of how far extended_tp_atr_multiplier
    # would otherwise place it. Prevents an unbounded "just keep extending forever" TP.
    max_tp_extension_atr_multiplier: float = float(os.getenv("MAX_TP_EXTENSION_ATR_MULT", "10.0"))

    # ---- Chandelier Exit trailing stop ----
    # How much profit (in ATR multiples, measured from the position's own
    # entry price) is required before the trailing stop activates at all.
    # Before this threshold is reached, the position's SL stays exactly
    # where it was set at entry (atr_sl_multiplier above) - trailing logic
    # does not touch it.
    trail_activation_atr_multiplier: float = float(os.getenv("TRAIL_ACTIVATION_ATR_MULT", "1.5"))
    # Once active, the new stop sits this many ATR(14) behind the HIGHEST
    # price reached since entry (BUY) / in front of the LOWEST price reached
    # since entry (SELL) - NOT behind entry price, and NOT behind the
    # current tick. This is the Chandelier Exit distance. Smaller = tighter/
    # more responsive but more prone to getting shaken out by ordinary
    # pullbacks (whipsaw); larger = more patient but gives back more open
    # profit if the trend actually reverses. ~2.5-3.5 is a common starting
    # range for swing-style systems on M15+; go much below ~1.5-2.0 only if
    # you've specifically backtested that tighter setting for this symbol/
    # timeframe, since it starts approaching scalping-style sensitivity.
    chandelier_atr_multiplier: float = float(os.getenv("CHANDELIER_ATR_MULT", "3.0"))
    # Fallback activation distance (pips), used only if ATR is unavailable.
    trail_activation_pips: int = 25
    # Minimum improvement of the extended TP (as a fraction of ATR) before a
    # new modify request is sent to the broker. Without this the TP "improves"
    # on nearly every upward tick and the bot would hammer the broker with
    # SLTP modifications every few seconds.
    tp_update_min_atr_fraction: float = 0.25
    # Fallback Chandelier distance (pips), used only if ATR is unavailable.
    chandelier_fallback_pips: int = 30

    # ---- Safety ----
    # Log/print every order instead of sending it to the broker. Use this
    # for dry-testing new parameters before risking real (or demo) capital.
    # DEFAULTS TO TRUE (safe by default): you must explicitly set
    # DRY_RUN=false to let the bot place real orders.
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    # If > 0, EVERY trade uses this exact lot size, completely bypassing
    # the risk-% position-sizing formula (calculate_risk_lot_size is never
    # called). Useful while validating signal/stop logic on a new
    # instrument without worrying about sizing math. 0 (default) = use
    # real risk-based sizing.
    fixed_lot_override: float = float(os.getenv("FIXED_LOT_SIZE", "0") or "0")

    # ---- Ollama inference tuning (matters most on constrained/no-GPU hardware) ----
    # Number of CPU threads Ollama uses for local inference.
    ollama_num_thread: int = int(os.getenv("OLLAMA_NUM_THREAD", "3"))
    # Context window size (tokens) given to the local model.
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "2048"))
    # Max tokens the local model is allowed to generate per response. The
    # expected JSON answer is short, so this can stay small.
    ollama_num_predict: int = int(os.getenv("OLLAMA_NUM_PREDICT", "128"))

    # ---- Timeframe selection ----
    # Which candle timeframe to trade: M1 | M5 | M15 | M30 | H1 | H4. Drives
    # both the per-candle LLM response time budget and which higher
    # timeframe is used for the bias filter (see TIMEFRAME_TABLE/HTF logic
    # below).
    trade_timeframe: str = os.getenv("TRADE_TIMEFRAME", "M15").upper()
    # 0 (default) = use the per-timeframe budget baked into
    # TIMEFRAME_TABLE. Set > 0 to override that budget explicitly,
    # regardless of which timeframe is selected.
    llm_max_latency_override_sec: int = int(os.getenv("LLM_MAX_LATENCY_SEC", "0") or "0")

    # ---- File paths ----
    # Where per-tier/streak/circuit-breaker/position-extreme state is
    # persisted between restarts.
    state_file: str = str(BASE_DIR / "risk_state.json")
    # Append-only JSON-lines audit log of every signal, escalation, trade
    # attempt, rejection, and closed-trade outcome.
    trade_log: str = str(BASE_DIR / "trade_log.jsonl")


CFG = Config()
OLLAMA_BASE_URL = f"http://{CFG.truenas_ip}:{CFG.ollama_port}"

# ============================================================
# CLOUD ESCALATION PROVIDERS
# ============================================================
# Each entry is {"name", "api_key", "base_url", "model", "timeout"}. base_url
# must be an OpenAI-chat-completions-compatible endpoint (POST {base_url} with
# {"model", "messages", ...} -> choices[0].message.content) - that's what
# call_cloud_provider() below speaks, so most free-tier providers (Groq,
# Cerebras, OpenRouter, Together, Fireworks, ...) drop in unmodified.
#
# Providers are tried IN ORDER; the first one that returns a valid JSON
# signal wins. Groq, Cerebras, and OpenRouter are wired in below - each only
# activates if its API key env var is set, so leaving a key blank simply
# skips that provider. To add a fourth provider later, append another dict
# here in the same shape, e.g.:
#   if os.getenv("TOGETHER_API_KEY"):
#       CLOUD_PROVIDERS.append({
#           "name": "together",
#           "api_key": os.getenv("TOGETHER_API_KEY"),
#           "base_url": os.getenv("TOGETHER_BASE_URL", "https://api.together.xyz/v1/chat/completions"),
#           "model": os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
#           "timeout": int(os.getenv("TOGETHER_TIMEOUT_SEC", str(CFG.cloud_timeout_sec))),
#       })
CLOUD_PROVIDERS: list[dict] = []


def _extra_params(prefix: str) -> dict:
    """Optional provider-specific request fields, given as a JSON object in
    <PREFIX>_EXTRA_PARAMS, merged into the chat-completions payload as-is.
    Example: GROQ_EXTRA_PARAMS='{"reasoning_effort": "low"}' (check your
    provider's docs for accepted fields/values)."""
    raw = os.getenv(f"{prefix}_EXTRA_PARAMS", "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return val
        print(f"[!] {prefix}_EXTRA_PARAMS must be a JSON object - ignoring.")
    except json.JSONDecodeError as e:
        print(f"[!] {prefix}_EXTRA_PARAMS is not valid JSON ({e}) - ignoring.")
    return {}

if os.getenv("GROQ_API_KEY"):
    CLOUD_PROVIDERS.append({
        "name": "groq",
        "api_key": os.getenv("GROQ_API_KEY"),
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1/chat/completions"),
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "timeout": int(os.getenv("GROQ_TIMEOUT_SEC", str(CFG.cloud_timeout_sec))),
        "extra_params": _extra_params("GROQ"),
    })

if os.getenv("CEREBRAS_API_KEY"):
    CLOUD_PROVIDERS.append({
        "name": "cerebras",
        "api_key": os.getenv("CEREBRAS_API_KEY"),
        "base_url": os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1/chat/completions"),
        "model": os.getenv("CEREBRAS_MODEL", "llama3.1-8b"),
        "timeout": int(os.getenv("CEREBRAS_TIMEOUT_SEC", str(CFG.cloud_timeout_sec))),
        "extra_params": _extra_params("CEREBRAS"),
    })

if os.getenv("OPENROUTER_API_KEY"):
    CLOUD_PROVIDERS.append({
        "name": "openrouter",
        "api_key": os.getenv("OPENROUTER_API_KEY"),
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
        "model": os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        "timeout": int(os.getenv("OPENROUTER_TIMEOUT_SEC", str(CFG.cloud_timeout_sec))),
        "extra_params": _extra_params("OPENROUTER"),
    })

# --- Add future free-tier providers above this line, same dict shape ---

if CFG.escalation_enabled and not CLOUD_PROVIDERS:
    print("[!] ESCALATION_ENABLED=true but no cloud provider API keys were found "
          "(e.g. GROQ_API_KEY). Low-confidence local signals will just fall through "
          "to the local model's own answer.")

# Candle length + LLM response budget per timeframe. The budget is a ceiling,
# not a target: while a signal request is in flight, trailing-stop management
# still runs every CHECK_INTERVAL_SEC on the main loop (see run_daemon) - the
# budget only bounds how long a slow/stuck request can delay a trade DECISION
# before it's abandoned as a timeout -> HOLD.
TIMEFRAME_TABLE = {
    "M1":  {"mt5_tf": mt5.TIMEFRAME_M1,  "seconds": 60,   "max_llm_latency": 10},
    "M5":  {"mt5_tf": mt5.TIMEFRAME_M5,  "seconds": 300,  "max_llm_latency": 20},
    "M15": {"mt5_tf": mt5.TIMEFRAME_M15, "seconds": 900,  "max_llm_latency": 40},
    "M30": {"mt5_tf": mt5.TIMEFRAME_M30, "seconds": 1800, "max_llm_latency": 60},
    "H1":  {"mt5_tf": mt5.TIMEFRAME_H1,  "seconds": 3600, "max_llm_latency": 90},
    "H4":  {"mt5_tf": mt5.TIMEFRAME_H4,  "seconds": 14400, "max_llm_latency": 90},
}

if CFG.trade_timeframe not in TIMEFRAME_TABLE:
    print(f"[!] Unknown TRADE_TIMEFRAME='{CFG.trade_timeframe}', "
          f"falling back to M15. Valid: {list(TIMEFRAME_TABLE)}")
    CFG.trade_timeframe = "M15"

_TF_INFO = TIMEFRAME_TABLE[CFG.trade_timeframe]
TIMEFRAME = _TF_INFO["mt5_tf"]
MAX_LLM_LATENCY_SEC = CFG.llm_max_latency_override_sec or _TF_INFO["max_llm_latency"]
# Bias timeframe must be strictly higher than the trade timeframe:
# M1..M30 -> H1, H1 -> H4, H4 -> D1.
_HTF_MAP = {
    "M1": ("H1", mt5.TIMEFRAME_H1), "M5": ("H1", mt5.TIMEFRAME_H1),
    "M15": ("H1", mt5.TIMEFRAME_H1), "M30": ("H1", mt5.TIMEFRAME_H1),
    "H1": ("H4", mt5.TIMEFRAME_H4), "H4": ("D1", mt5.TIMEFRAME_D1),
}
HTF_TIMEFRAME_NAME, HTF_TIMEFRAME = _HTF_MAP[CFG.trade_timeframe]

running = True


def graceful_exit(sig, frame):
    global running
    print("\n[*] Shutdown signal received. Stopping daemon...")
    running = False


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


def log_event(record: dict):
    """Append a JSON line for post-hoc auditing / future backtesting calibration."""
    record["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        with open(CFG.trade_log, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        print(f"[!] Could not write trade log: {e}")


# ============================================================
# OLLAMA MODEL RESOLUTION
# ============================================================
def get_active_model(base_url: str) -> str:
    if env_model := os.getenv("OLLAMA_MODEL"):
        return env_model
    try:
        res = requests.get(f"{base_url}/api/ps", timeout=3)
        if res.status_code == 200:
            models = res.json().get("models", [])
            if models:
                return models[0]["name"]
    except requests.RequestException:
        pass
    try:
        res = requests.get(f"{base_url}/api/tags", timeout=3)
        if res.status_code == 200:
            installed = res.json().get("models", [])
            if installed:
                return installed[0]["name"]
    except requests.RequestException:
        pass
    return "qwen2.5:3b"


# ============================================================
# TECHNICAL INDICATORS (pure python, no external deps)
# ============================================================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    val = sum(values[:period]) / period
    for price in values[period:]:
        val = price * k + val * (1 - k)
    return val


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def atr(rates, period=14):
    """Simplified (SMA-based, not Wilder-smoothed) ATR in price units."""
    if rates is None or len(rates) < period + 1:
        return None
    trs = []
    for i in range(1, len(rates)):
        high = float(rates[i]["high"])
        low = float(rates[i]["low"])
        prev_close = float(rates[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def get_trading_session() -> str:
    hour = time.gmtime().tm_hour
    if 0 <= hour < 7:
        return "Asian"
    if 7 <= hour < 12:
        return "London"
    if 12 <= hour < 16:
        return "London/NY Overlap"
    if 16 <= hour < 21:
        return "New York"
    return "Late NY / Pre-Asian"


def get_htf_bias(symbol: str, count: int = CFG.htf_lookback) -> str:
    rates = mt5.copy_rates_from_pos(symbol, HTF_TIMEFRAME, 1, count)
    if rates is None or len(rates) < count:
        return "unknown"
    closes = [float(r["close"]) for r in rates]
    ema_fast, ema_slow = ema(closes, 20), ema(closes, 50)
    if ema_fast is None or ema_slow is None:
        return "unknown"
    last_close = closes[-1]
    if ema_fast > ema_slow and last_close > ema_fast:
        return "bullish"
    if ema_fast < ema_slow and last_close < ema_fast:
        return "bearish"
    return "neutral"


def get_pip_size(symbol_info) -> float:
    """
    Price size of one 'pip' for this instrument. Standard 5-digit forex quotes
    (and 3-digit JPY pairs) use pip = 10 * point. Instruments quoted with fewer
    decimal digits - many crypto/CFD symbols, e.g. ETHUSD often at 2 digits -
    don't follow that convention: for those, one point IS the tradable increment.
    Getting this wrong silently makes every spread/ATR/stop number off by ~10x.
    """
    digits = symbol_info.digits
    point = symbol_info.point
    if digits in (3, 5):
        return point * 10
    return point


def get_spread_pips(symbol: str):
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        return None
    pip_size = get_pip_size(info)
    return round((tick.ask - tick.bid) / pip_size, 1)


def format_bar_time(epoch_seconds) -> str:
    """
    Renders an MT5 bar['time'] (epoch seconds) as an unambiguous, explicitly-
    labeled UTC timestamp, optionally shifted by CFG.broker_utc_offset_hours.

    This intentionally does NOT use time.localtime()/time.strftime() on the
    epoch value: that converts using whatever timezone the host OS process
    happens to be configured with (arbitrary under wine, and unrelated to
    either the broker's server time or the operator's real local time),
    which is what produced confusing/wrong-looking bar times previously.
    Using dt.datetime.fromtimestamp(..., tz=utc) instead is deterministic
    regardless of host TZ configuration.

    Includes the month-day, not just hour:minute, so a session that spans
    midnight doesn't produce an ambiguous/out-of-order-looking bar list.
    """
    utc_time = dt.datetime.fromtimestamp(float(epoch_seconds), tz=dt.timezone.utc)
    if CFG.broker_utc_offset_hours:
        utc_time = utc_time + dt.timedelta(hours=CFG.broker_utc_offset_hours)
    return utc_time.strftime("%m-%d %H:%M")


def bar_timezone_label() -> str:
    if CFG.broker_utc_offset_hours:
        sign = "+" if CFG.broker_utc_offset_hours >= 0 else ""
        return f"UTC{sign}{CFG.broker_utc_offset_hours:g}"
    return "UTC"


# ============================================================
# MARKET CONTEXT
# ============================================================
def fetch_dynamic_context(symbol: str, timeframe=TIMEFRAME):
    """
    Fetches enough candles to compute indicators, plus a compact recent-bar
    view for the LLM prompt.

    NOTE on candle selection: copy_rates_from_pos(symbol, timeframe, 1, N) -
    the "1" is the START POSITION, not a count, and position 0 is always the
    currently-forming (incomplete) candle. Starting at 1 deliberately skips
    it, so every bar returned here - including the LAST one in the list - is
    a fully CLOSED candle. This means the most recent bar shown to the LLM
    can lag "now" by up to one full timeframe interval (e.g. up to ~15
    minutes on M15), which is expected behavior (never feed the model a
    partial candle), not a bug. See format_bar_time()/bar_timezone_label()
    above for how the displayed timestamp itself is made unambiguous.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, CFG.indicator_lookback)
    symbol_info = mt5.symbol_info(symbol)
    if rates is None or len(rates) < CFG.indicator_lookback or symbol_info is None:
        print(f"[!] Failed to fetch {CFG.indicator_lookback} bars for {symbol}")
        return None

    pip_size = get_pip_size(symbol_info)
    closes = [float(r["close"]) for r in rates]

    recent = rates[-CFG.prompt_bars:]
    d = symbol_info.digits
    compact_bars = [
        f"{format_bar_time(bar['time'])}|"
        f"{bar['open']:.{d}f}|{bar['high']:.{d}f}|{bar['low']:.{d}f}|{bar['close']:.{d}f}"
        for bar in recent
    ]

    atr_val = atr(rates, CFG.atr_period)
    atr_pips = round(atr_val / pip_size, 1) if atr_val else None
    rsi_val = rsi(closes, 14)
    ema9, ema21 = ema(closes, 9), ema(closes, 21)
    ema_trend = "unknown"
    if ema9 is not None and ema21 is not None:
        ema_trend = "up" if ema9 > ema21 else "down" if ema9 < ema21 else "flat"

    return {
        "symbol": symbol,
        "timeframe": CFG.trade_timeframe,
        "rates": rates,          # kept for SL/TP math, not sent verbatim to LLM
        "atr_price": atr_val,
        "atr_pips": atr_pips,
        "rsi14": rsi_val,
        "ema9_vs_ema21": ema_trend,
        "recent_bars": compact_bars,
    }


OUTPUT_FORMAT_INSTRUCTION = (
    'Output JSON ONLY, no markdown fences, no commentary:\n'
    '{"reasoning": "short string", "action": "BUY|SELL|HOLD", "confidence": 0.0-1.0}'
)

# Static persona + rules + output-format contract. Identical for every
# candle and every provider (local or cloud), so it belongs in the system
# role rather than being re-stated inside the per-candle user prompt.
SYSTEM_PROMPT = f"""You are a disciplined intraday forex analyst. Provide your reasoning in exactly one short, punchy sentence. Do not add intro or outro filler text.

Rules:
- Prefer trading in the direction of the higher-timeframe bias, but a counter-trend BUY or SELL is allowed when the trade timeframe's own price action/momentum clearly contradicts the bias - don't discard a valid counter-trend setup just because it opposes the higher-timeframe bias.
- Widen skepticism (lower confidence) if spread is elevated relative to ATR.
- You decide direction and conviction only. Do not propose stop-loss or take-profit levels; risk is handled separately.

{OUTPUT_FORMAT_INSTRUCTION}"""


def build_user_prompt(ctx: dict) -> str:
    """Per-candle market state only. No persona/rules/output-format text here -
    that's all in SYSTEM_PROMPT, sent once via the system role."""
    pos = ctx.get("open_position") or "none"
    tz_label = bar_timezone_label()
    return f"""Analyze {ctx['symbol']} on {ctx['timeframe']}.

Market state:
- Session: {ctx.get('session')}
- Current spread: {ctx.get('spread_pips')} pips
- ATR(14): {ctx.get('atr_pips')} pips
- RSI(14): {ctx.get('rsi14')}
- EMA9 vs EMA21 ({ctx['timeframe']}): {ctx.get('ema9_vs_ema21')}
- {HTF_TIMEFRAME_NAME} trend bias: {ctx.get('htf_bias')}
- Open positions for this strategy: {pos}
- Today's realized P&L so far: {ctx.get('daily_pnl_pct')}%

Recent {ctx['timeframe']} bars (Date-Time [{tz_label}]|Open|High|Low|Close), oldest first.
The last bar below is the most recent FULLY CLOSED candle (the still-forming current candle is intentionally excluded):
{json.dumps(ctx.get('recent_bars', []))}"""


_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def parse_llm_json(raw: str) -> dict:
    """Extracts the JSON object from an LLM answer. Tolerates markdown fences,
    <think>...</think> blocks (reasoning models), and stray text around the
    object. Raises ValueError (json.JSONDecodeError is a subclass) if no
    JSON object can be recovered."""
    text = _THINK_CLOSED_RE.sub("", raw or "")
    text = _THINK_OPEN_RE.sub("", text)          # truncated/unclosed think block
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError(f"LLM returned JSON that is not an object: {obj!r}")
    return obj


def normalize_signal(result: dict) -> dict:
    """Guarantees action in {BUY,SELL,HOLD} and confidence a float in [0,1],
    whatever the model actually returned (e.g. "buy", "0.8", 85, "85%")."""
    action = str(result.get("action", "HOLD")).strip().upper()
    if action not in ("BUY", "SELL", "HOLD"):
        result["reasoning"] = f"Invalid action {action!r}: {result.get('reasoning', '')}"
        action = "HOLD"
    try:
        conf = float(str(result.get("confidence", 0.0)).strip().rstrip("%"))
    except (TypeError, ValueError):
        conf = 0.0
    if conf != conf:                              # NaN
        conf = 0.0
    if 1.0 < conf <= 100.0:                       # model answered in percent
        conf /= 100.0
    result["action"] = action
    result["confidence"] = round(max(0.0, min(1.0, conf)), 3)
    result.setdefault("reasoning", "")
    return result


def pick_filling_mode(symbol_info) -> int:
    """Order filling policy the symbol actually supports (bit 1 = FOK,
    bit 2 = IOC). Prefers IOC (the original behaviour), then FOK, then RETURN."""
    fm = getattr(symbol_info, "filling_mode", 0) or 0
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def generate_trade_signal(ctx: dict) -> dict:
    model_name = get_active_model(OLLAMA_BASE_URL)
    user_prompt = build_user_prompt(ctx)

    print(f"[*] Using model: {model_name}")

    print("\n" + "=" * 15 + " [THE QUESTION] " + "=" * 15)
    print("--- system ---")
    print(SYSTEM_PROMPT)
    print("--- user ---")
    print(user_prompt)
    print("\n" + "=" * 48 + "\n")

    payload = {
        "model": model_name,
        "system": SYSTEM_PROMPT,
        "prompt": user_prompt,
        "format": {
            "type": "object",
                "properties": {
                "reasoning": {"type": "string"},
                "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                "confidence": {"type": "number"}
                },
        "required": ["reasoning", "action", "confidence"]
        },
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": CFG.ollama_num_predict,
            "num_ctx": CFG.ollama_num_ctx,
            "num_thread": CFG.ollama_num_thread,
        },
    }

    t0 = time.perf_counter()
    try:
        res = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload,
                             timeout=(5, MAX_LLM_LATENCY_SEC))
        res.raise_for_status()
        latency = time.perf_counter() - t0
        raw_response = res.json().get("response", "").strip()

        print("\n" + "=" * 15 + " [RAW LLM ANSWER] " + "=" * 15)
        print(raw_response)
        print(f"[*] Response Time: {latency:.2f} seconds")
        print("=" * 48 + "\n")

        result = parse_llm_json(raw_response)
        if "action" not in result:
            print(f"[!] Malformed LLM response, missing 'action' key: {result}")
            result = {"action": "HOLD", "confidence": 0.0, "reasoning": f"Malformed: {result}"}
        result = normalize_signal(result)
        result["latency_sec"] = round(latency, 2)
        result["model"] = model_name
        return result

    except requests.exceptions.Timeout:
        latency = time.perf_counter() - t0
        err_msg = f"Ollama request timed out (>{MAX_LLM_LATENCY_SEC}s budget for {CFG.trade_timeframe})."
        print(f"[!] {err_msg}")
        return {"action": "HOLD", "confidence": 0.0, "reasoning": err_msg,
                "latency_sec": round(latency, 2), "model": model_name}

    except (requests.RequestException, ValueError) as e:      # ValueError includes JSONDecodeError
        latency = time.perf_counter() - t0
        print(f"[!] LLM Request Error: {e}")
        return {"action": "HOLD", "confidence": 0.0, "reasoning": str(e),
                "latency_sec": round(latency, 2), "model": model_name}

    except Exception as e:
        latency = time.perf_counter() - t0
        print(f"[!] LLM Generation Error after {latency:.2f}s: {e}")
        return {"action": "HOLD", "confidence": 0.0, "reasoning": str(e),
                "latency_sec": round(latency, 2), "model": model_name}


# ============================================================
# CLOUD ESCALATION
# ============================================================
def build_escalation_user_prompt(ctx: dict, local_result: dict) -> str:
    """Same per-candle market state as the local user prompt, plus the local
    model's own (low-confidence) answer, framed as a request for an
    independent second opinion rather than a rubber stamp. SYSTEM_PROMPT
    (persona/rules/output-format) is sent separately via the system role -
    it is NOT repeated here."""
    base = build_user_prompt(ctx)
    return base + f"""

A smaller local model already reviewed this exact setup and returned:
- action: {local_result.get('action')}
- confidence: {local_result.get('confidence')}
- reasoning: {local_result.get('reasoning')}

Its confidence ({local_result.get('confidence')}) was below the escalation
threshold ({CFG.escalation_confidence_threshold}), so you are being consulted
as a stronger second opinion. Independently evaluate the market state above -
you may agree or disagree with the local model's action."""


def call_cloud_provider(provider: dict, system_prompt: str, user_prompt: str) -> dict:
    """POSTs an OpenAI-chat-completions-style request. Raises on any failure
    (network, HTTP, malformed JSON) so the caller can fall through to the
    next configured provider."""
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    payload.update(provider.get("extra_params") or {})

    def _post(body: dict):
        return requests.post(provider["base_url"], headers=headers, json=body,
                             timeout=provider.get("timeout", CFG.cloud_timeout_sec))

    t0 = time.perf_counter()
    res = _post(payload)
    # Some (mostly free-tier) models reject JSON mode outright. Retry once
    # without it - parse_llm_json() copes with plain-text JSON anyway.
    if (res.status_code == 400 and "response_format" in payload
            and "response_format" in res.text.lower()):
        print(f"[!] Provider '{provider['name']}' rejected response_format - "
              f"retrying once without JSON mode.")
        payload.pop("response_format")
        res = _post(payload)
    if not res.ok:
        try:
            err_body = res.json()
            err_detail = err_body.get("error", err_body)
        except (ValueError, json.JSONDecodeError):
            err_detail = res.text[:500]
        raise requests.HTTPError(
            f"{res.status_code} {res.reason} for url {res.url} - body: {err_detail}",
            response=res,
        )

    latency = time.perf_counter() - t0

    raw = (res.json()["choices"][0]["message"].get("content") or "").strip()
    result = parse_llm_json(raw)
    if "action" not in result:
        raise ValueError(f"Malformed cloud response, missing 'action' key: {result}")
    result = normalize_signal(result)

    result["latency_sec"] = round(latency, 2)
    result["model"] = provider["model"]
    result["provider"] = provider["name"]
    return result


def escalate_to_cloud(ctx: dict, local_result: dict):
    """Tries each configured cloud provider in order. Logs the escalation
    (local answer + cloud answer) on success, and logs failures per provider
    so the trade log shows exactly what was tried. Returns the first
    successful cloud signal dict, or None if every provider failed / none
    are configured."""
    if CFG.escalation_share_local_answer:
        user_prompt = build_escalation_user_prompt(ctx, local_result)
    else:
        user_prompt = build_user_prompt(ctx)

    for provider in CLOUD_PROVIDERS:
        try:
            print(f"[*] Local confidence {local_result.get('confidence')} < "
                  f"{CFG.escalation_confidence_threshold} - escalating to cloud "
                  f"provider '{provider['name']}' ({provider['model']})...")
            cloud_result = call_cloud_provider(provider, SYSTEM_PROMPT, user_prompt)
            print(f"[+] Cloud provider '{provider['name']}' answered: "
                  f"{cloud_result.get('action')} (Conf: {cloud_result.get('confidence')}) "
                  f"in {cloud_result.get('latency_sec')}s")

            log_event({
                "event": "escalation",
                "provider": provider["name"],
                "cloud_model": provider["model"],
                "reason": f"local confidence {local_result.get('confidence')} < "
                          f"threshold {CFG.escalation_confidence_threshold}",
                "local_signal": local_result,
                "cloud_signal": cloud_result,
            })
            return cloud_result

        except Exception as e:   # any provider failure must fall through to the next one
            print(f"[!] Cloud provider '{provider['name']}' failed: {type(e).__name__}: {e}")
            log_event({
                "event": "escalation_failed",
                "provider": provider["name"],
                "cloud_model": provider.get("model"),
                "error_type": type(e).__name__,
                "error": str(e),
                "local_signal": local_result,
            })
            continue

    print("[!] All cloud providers failed or none configured - keeping local signal.")
    log_event({
        "event": "escalation_exhausted",
        "reason": "no cloud provider produced a usable answer",
        "local_signal": local_result,
    })
    return None


def get_trade_decision(ctx: dict) -> dict:
    """Entry point submitted to the background executor. Runs the local
    model first; if its confidence is below the escalation threshold, asks
    the cloud provider chain for a second opinion and uses THAT answer as
    the final decision (falling back to the local answer if escalation is
    disabled, unconfigured, or every provider failed)."""
    local_result = generate_trade_signal(ctx)
    local_result["source"] = "local"

    if not CFG.escalation_enabled:
        return local_result

    confidence = local_result.get("confidence", 0.0) or 0.0
    if confidence >= CFG.escalation_confidence_threshold:
        return local_result

    if not CLOUD_PROVIDERS:
        return local_result

    cloud_result = escalate_to_cloud(ctx, local_result)
    if cloud_result is None:
        return local_result

    cloud_result["source"] = "cloud"
    cloud_result["escalated_from_action"] = local_result.get("action")
    cloud_result["escalated_from_confidence"] = local_result.get("confidence")
    return cloud_result


# ============================================================
# PROGRESSIVE RISK MANAGEMENT
# ============================================================
class RiskManager:
    """
    Persists sizing state to disk so 'start conservative, grow with proof'
    survives restarts. Tier 0 = starting_risk_pct. Each tier promotion adds
    risk_step_pct, capped at max_risk_pct. Any loss streak of
    losses_to_demote resets straight back to tier 0 - growth is earned
    slowly and lost quickly, on purpose.

    Also persists position_extremes: the running high-water mark (BUY) or
    low-water mark (SELL) reached since each open position's entry, keyed
    by MT5 ticket. MT5 itself does not track this, and it's what the
    Chandelier Exit trailing stop (see apply_dynamic_trailing_stop) trails
    behind.
    """

    def __init__(self, path: str):
        self.path = path
        self.state = self._load()

    def _default_state(self):
        return {
            "tier": 0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "trading_day": None,
            "day_start_equity": None,
            "halted_today": False,
            "last_processed_deal_ticket": 0,
            "position_extremes": {},   # {str(ticket): highest_or_lowest_price_seen}
        }

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return {**self._default_state(), **json.load(f)}
            except (OSError, json.JSONDecodeError):
                pass
        return self._default_state()

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.state, f, indent=2)
        except OSError as e:
            print(f"[!] Could not save risk state: {e}")

    def risk_pct(self) -> float:
        pct = CFG.starting_risk_pct + self.state["tier"] * CFG.risk_step_pct
        return min(pct, CFG.max_risk_pct)

    def record_trade_result(self, profit: float):
        if profit > 0:
            self.state["consecutive_wins"] += 1
            self.state["consecutive_losses"] = 0
            if self.state["consecutive_wins"] >= CFG.trades_per_promotion:
                self.state["tier"] += 1
                self.state["consecutive_wins"] = 0
                print(f"[+] Risk tier promoted to {self.state['tier']} "
                      f"(risk now {self.risk_pct():.2f}%)")
        elif profit < 0:
            self.state["consecutive_losses"] += 1
            self.state["consecutive_wins"] = 0
            if self.state["consecutive_losses"] >= CFG.losses_to_demote:
                if self.state["tier"] != 0:
                    print("[!] Loss streak hit - resetting risk tier to 0")
                self.state["tier"] = 0
                self.state["consecutive_losses"] = 0
        self.save()

    def check_daily_reset(self, current_equity: float):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        if self.state["trading_day"] != today:
            self.state["trading_day"] = today
            self.state["day_start_equity"] = current_equity
            self.state["halted_today"] = False
            self.save()

    def check_circuit_breaker(self, current_equity: float) -> bool:
        """Returns True if new trades should be blocked for today."""
        start_equity = self.state.get("day_start_equity")
        if not start_equity:
            return False
        drawdown_pct = (start_equity - current_equity) / start_equity * 100
        if drawdown_pct >= CFG.daily_loss_limit_pct:
            if not self.state["halted_today"]:
                print(f"[!!] Daily loss limit hit ({drawdown_pct:.2f}%). "
                      f"Halting new trades until next UTC day.")
                self.state["halted_today"] = True
                self.save()
            return True
        return False

    def update_extreme(self, ticket: int, price: float, is_buy: bool) -> float:
        """Updates (if the new price is a new high/low) and returns the
        running watermark price for this ticket. The first call for a given
        ticket seeds the watermark with `price`."""
        key = str(ticket)
        extremes = self.state["position_extremes"]
        current = extremes.get(key)
        if current is None:
            extremes[key] = price
            self.save()
            return price
        if is_buy and price > current:
            extremes[key] = price
            self.save()
            return price
        if not is_buy and price < current:
            extremes[key] = price
            self.save()
            return price
        return current

    def prune_closed_extremes(self, open_tickets: set):
        """Drops watermark entries for tickets that are no longer open, so
        risk_state.json doesn't grow unbounded and a stale watermark can
        never leak into a brand-new position that happens to reuse... well,
        MT5 tickets aren't reused, but this keeps the file tidy regardless."""
        extremes = self.state["position_extremes"]
        stale = [k for k in extremes if int(k) not in open_tickets]
        for k in stale:
            del extremes[k]
        if stale:
            self.save()


def sync_closed_trades(risk_mgr: RiskManager, symbol: str, magic: int):
    """Pulls recently closed deals for this strategy and feeds outcomes to the
    RiskManager. Net result = profit + commission + swap.

    On the very first run (no cursor stored yet) the cursor is only SEEDED to
    the newest matching deal, so old history on a reused account is not
    replayed into the win/loss streaks and risk tiers."""
    from_date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    to_date = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    deals = mt5.history_deals_get(from_date, to_date)
    if not deals:
        return
    mine = [d for d in deals if d.magic == magic and d.symbol == symbol]
    last_ticket = risk_mgr.state.get("last_processed_deal_ticket", 0)

    if last_ticket == 0:
        seed = max((d.ticket for d in mine), default=0)
        if seed:
            print(f"[*] First run: seeding closed-deal cursor at #{seed} "
                  f"(older deals are not counted toward risk tiers).")
            risk_mgr.state["last_processed_deal_ticket"] = seed
            risk_mgr.save()
        return

    new_max = last_ticket
    for deal in mine:
        if getattr(deal, "entry", None) != mt5.DEAL_ENTRY_OUT:
            continue
        if deal.ticket <= last_ticket:
            continue
        net = (deal.profit + getattr(deal, "commission", 0.0) + getattr(deal, "swap", 0.0))
        risk_mgr.record_trade_result(net)
        log_event({"event": "trade_closed", "ticket": deal.ticket,
                   "profit": deal.profit, "commission": getattr(deal, "commission", 0.0),
                   "swap": getattr(deal, "swap", 0.0), "net": net})
        new_max = max(new_max, deal.ticket)
    if new_max != last_ticket:
        risk_mgr.state["last_processed_deal_ticket"] = new_max
        risk_mgr.save()


# ============================================================
# POSITION SIZING & TRADE STRUCTURE
# ============================================================
def count_open_positions_in_direction(symbol: str, action: str, magic: int) -> int:
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return 0
    target_type = mt5.POSITION_TYPE_BUY if action == "BUY" else mt5.POSITION_TYPE_SELL
    return sum(1 for pos in positions if pos.magic == magic and pos.type == target_type)

def calculate_risk_lot_size(symbol: str, order_type: int, entry_price: float,
                             sl_price: float, risk_pct: float) -> float:
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    if not account_info or not symbol_info:
        return 0.0

    equity = account_info.equity
    free_margin = account_info.margin_free
    vol_min, vol_max, vol_step = symbol_info.volume_min, symbol_info.volume_max, symbol_info.volume_step

    if free_margin <= 0:
        print("[!] Insufficient free margin.")
        return 0.0

    risk_amount = equity * (risk_pct / 100.0)
    profit_for_1_lot = mt5.order_calc_profit(order_type, symbol, 1.0, entry_price, sl_price)
    if profit_for_1_lot is None or profit_for_1_lot >= 0:
        print("[!] Could not calculate SL loss for 1 lot.")
        return 0.0

    loss_per_lot = abs(profit_for_1_lot)
    raw_lot = risk_amount / loss_per_lot
    lot_size = round(raw_lot / vol_step) * vol_step
    lot_size = max(vol_min, min(vol_max, lot_size, CFG.max_lot_hard_cap))

    max_usable_margin = free_margin * 0.80
    required_margin = mt5.order_calc_margin(order_type, symbol, lot_size, entry_price)
    while (required_margin is not None and required_margin > max_usable_margin
           and lot_size > vol_min):
        lot_size = round((lot_size - vol_step) / vol_step) * vol_step
        required_margin = mt5.order_calc_margin(order_type, symbol, lot_size, entry_price)

    if required_margin is not None and required_margin > free_margin:
        print(f"[!] Cannot afford min lot ({vol_min}). "
              f"Required: ${required_margin:.2f} | Free: ${free_margin:.2f}")
        return 0.0

    return round(lot_size, 2)


def apply_dynamic_trailing_stop(symbol: str, magic: int, risk_mgr: RiskManager):
    """
    Chandelier Exit trailing stop, run every CHECK_INTERVAL_SEC for every
    open position belonging to this strategy (magic number).

    Two ATR-scaled distances govern this:
      - trail_activation_atr_multiplier: how much profit (in ATR multiples,
        from the position's own entry price) its PEAK must have reached
        before the stop starts trailing. Before that, the position keeps the
        fixed SL it was given at entry (atr_sl_multiplier). Once reached,
        trailing stays active even if price later pulls back below the
        activation level.
      - chandelier_atr_multiplier: once trailing is active, the new SL sits
        this many ATR behind the HIGHEST price reached since entry (BUY) /
        above the LOWEST price reached since entry (SELL) - not behind the
        current tick, and not behind the entry price.

    The running high/low watermark per position is NOT something MT5 tracks
    for us, so it is persisted in risk_mgr.state["position_extremes"] keyed
    by ticket. It is updated on EVERY loop iteration for every open position
    (not only after activation), so a peak reached before activation is never
    forgotten. Note it is sampled every CHECK_INTERVAL_SEC from the tick, so
    a spike that lives shorter than that interval is not captured.

    Alongside the SL trail, once active the TP is extended out to
    extended_tp_atr_multiplier * ATR from the CURRENT price, capped so it can
    never sit further than max_tp_extension_atr_multiplier * ATR from the
    position's own entry. A TP change is only sent when it improves by at
    least tp_update_min_atr_fraction * ATR.

    SL and TP only ever move in the favorable direction - this function never
    loosens a stop or pulls in a target once set. New levels are rounded to
    the symbol's digits and skipped if they would violate the broker's
    minimum stop distance from the current price.
    """
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return
    pip_size = get_pip_size(symbol_info)
    digits = symbol_info.digits
    min_gap = symbol_info.trade_stops_level * symbol_info.point

    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 1, CFG.indicator_lookback)
    atr_price = atr(rates, CFG.atr_period)

    if atr_price:
        activation_dist = atr_price * CFG.trail_activation_atr_multiplier
        chandelier_dist = atr_price * CFG.chandelier_atr_multiplier
        extended_tp_dist = atr_price * CFG.extended_tp_atr_multiplier
        max_tp_dist = atr_price * CFG.max_tp_extension_atr_multiplier
        tp_min_step = atr_price * CFG.tp_update_min_atr_fraction
    else:
        activation_dist = CFG.trail_activation_pips * pip_size
        chandelier_dist = CFG.chandelier_fallback_pips * pip_size
        extended_tp_dist = None
        max_tp_dist = None
        tp_min_step = 0.0

    # Prune watermark entries for any ticket that's no longer open (closed,
    # stopped out, etc.) before processing this iteration's live positions.
    open_tickets = {pos.ticket for pos in positions if pos.magic == magic}
    risk_mgr.prune_closed_extremes(open_tickets)

    for pos in positions:
        if pos.magic != magic:
            continue
        if pos.type == mt5.POSITION_TYPE_BUY:
            is_buy = True
        elif pos.type == mt5.POSITION_TYPE_SELL:
            is_buy = False
        else:
            continue

        mark = tick.bid if is_buy else tick.ask     # price this position would close at
        extreme = risk_mgr.update_extreme(pos.ticket, mark, is_buy=is_buy)
        peak_profit = (extreme - pos.price_open) if is_buy else (pos.price_open - extreme)
        if peak_profit < activation_dist:
            continue  # not activated yet - leave the original entry SL/TP alone

        if is_buy:
            new_sl = extreme - chandelier_dist
            new_tp = (mark + extended_tp_dist) if extended_tp_dist else pos.tp
            if max_tp_dist:
                new_tp = min(new_tp, pos.price_open + max_tp_dist)
            sl_improved = ((pos.sl == 0.0 or new_sl > pos.sl + pip_size)
                           and new_sl < mark - min_gap)
            tp_improved = bool(extended_tp_dist) and (pos.tp == 0.0 or new_tp > pos.tp + tp_min_step)
        else:
            new_sl = extreme + chandelier_dist
            new_tp = (mark - extended_tp_dist) if extended_tp_dist else pos.tp
            if max_tp_dist:
                new_tp = max(new_tp, pos.price_open - max_tp_dist)
            sl_improved = ((pos.sl == 0.0 or new_sl < pos.sl - pip_size)
                           and new_sl > mark + min_gap)
            tp_improved = bool(extended_tp_dist) and (pos.tp == 0.0 or new_tp < pos.tp - tp_min_step)

        if sl_improved or tp_improved:
            final_sl = new_sl if sl_improved else pos.sl
            final_tp = new_tp if tp_improved else pos.tp
            _update_position_sl(pos.ticket, final_sl, final_tp, digits)


def _update_position_sl(ticket: int, new_sl: float, new_tp: float, digits: int):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": round(new_sl, digits),
        "tp": round(new_tp, digits),
    }
    res = mt5.order_send(request)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[+] Trailing Stop Updated: Ticket #{ticket} -> "
              f"SL: {new_sl:.{digits}f} | TP: {new_tp:.{digits}f}")
    elif res is None:
        print(f"[!] Trailing update for #{ticket} not sent (terminal-level failure): "
              f"{mt5.last_error()}")
    elif res.retcode != getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025):
        print(f"[!] Trailing update for #{ticket} rejected "
              f"(retcode {res.retcode}): {res.comment}")


def execute_protected_trade(signal_data: dict, ctx: dict, risk_mgr: RiskManager) -> bool:
    action = signal_data.get("action", "HOLD")
    confidence = signal_data.get("confidence", 0.0)
    symbol = CFG.symbol

    if action == "HOLD" or confidence < CFG.min_confidence:
        print(f"[*] Signal filtered: Action={action}, Confidence={confidence} "
              f"(Min: {CFG.min_confidence})")
        return False

    open_count = count_open_positions_in_direction(symbol, action, CFG.magic_number)
    if open_count >= CFG.max_positions_per_direction:
        print(f"[*] Execution blocked: {open_count} open {action} position(s) already exist "
              f"on {symbol} (max {CFG.max_positions_per_direction}).")
        return False

    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        print("[!] Symbol info or tick unavailable.")
        return False

    point = symbol_info.point
    digits = symbol_info.digits
    pip_size = get_pip_size(symbol_info)
    spread_price = tick.ask - tick.bid
    atr_price = ctx.get("atr_price")
    spread_pips = round(spread_price / pip_size, 1)  # always computed, used for logging

    # Spread guard: prefer spread-as-fraction-of-ATR (unit-agnostic, works
    # the same whether the instrument is EURUSD or ETHUSD). Only fall back
    # to the pip threshold when ATR isn't available yet.
    if atr_price:
        spread_ratio = spread_price / atr_price
        if spread_ratio > CFG.max_spread_atr_ratio:
            print(f"[*] Execution blocked: spread is {spread_ratio:.2f}x ATR, "
                  f"exceeds max {CFG.max_spread_atr_ratio}x ATR "
                  f"(spread={spread_price:.5f}, ATR={atr_price:.5f}).")
            return False
    else:
        if spread_pips > CFG.max_spread_pips:
            print(f"[*] Execution blocked: spread {spread_pips:.1f} pips exceeds "
                  f"max {CFG.max_spread_pips} pips (no ATR available for ratio check).")
            return False

    # Volatility-aware stop distance, spread-buffered, floored by broker minimum.
    if atr_price:
        sl_dist = atr_price * CFG.atr_sl_multiplier
        tp_dist = atr_price * CFG.atr_tp_multiplier
    else:
        sl_dist = CFG.fallback_sl_pips * pip_size
        tp_dist = CFG.fallback_tp_pips * pip_size

    min_stop_distance = (symbol_info.trade_stops_level + 20) * point + spread_price
    actual_sl_dist = max(sl_dist, min_stop_distance)
    actual_tp_dist = max(tp_dist, min_stop_distance)

    if action == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        sl = price - actual_sl_dist
        tp = price + actual_tp_dist
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        sl = price + actual_sl_dist
        tp = price - actual_tp_dist

    price, sl, tp = round(price, digits), round(sl, digits), round(tp, digits)

    if CFG.fixed_lot_override > 0:
        lot_size = max(symbol_info.volume_min,
                        min(symbol_info.volume_max, CFG.fixed_lot_override))
        print(f"[*] Using fixed test lot size override: {lot_size} "
              f"(risk-based sizing bypassed)")
    else:
        lot_size = calculate_risk_lot_size(symbol, order_type, price, sl, risk_mgr.risk_pct())

    if lot_size <= 0.0:
        print("[!] Trade aborted: lot size calculated to 0.0 "
              "(insufficient margin/capital, or SL math failed).")
        return False

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": CFG.magic_number,
        "comment": f"ClaudeBot {__version__} Chandelier",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": pick_filling_mode(symbol_info),
    }

    log_event({
        "event": "trade_attempt", "action": action, "confidence": confidence,
        "lot": lot_size, "price": price, "sl": sl, "tp": tp,
        "spread_pips": spread_pips, "risk_pct": risk_mgr.risk_pct(),
        "reasoning": signal_data.get("reasoning"), "dry_run": CFG.dry_run,
        "model": signal_data.get("model"),
    })

    if CFG.dry_run:
        print(f"[DRY_RUN] Would send order: {request}")
        return True

    result = mt5.order_send(request)
    print(f"[*] Send order to MT5: {request}")
    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
        if result is None:
            # order_send() returning None (rather than a result object with a
            # numeric retcode) means the TERMINAL rejected the request before
            # it ever reached the broker - most commonly AutoTrading/Algo
            # Trading being disabled in the terminal UI, a disconnected/not
            # logged-in terminal, or an IPC timeout. mt5.last_error() is the
            # only place that reason is exposed.
            last_err = mt5.last_error()
            comment = f"Order Send Failed (terminal-level, no result object). last_error: {last_err}"
            retcode = "N/A"
            print(f"[!] Order Rejected (Retcode: {retcode}): {comment}")
            print("[!] Check: is AutoTrading/Algo Trading enabled in the MT5 terminal? "
                  "Is the terminal connected and logged in?")
        else:
            comment = result.comment
            retcode = result.retcode
            print(f"[!] Order Rejected (Retcode: {retcode}): {comment}")
        log_event({
            "event": "order_rejected", "action": action, "retcode": retcode,
            "comment": comment, "request": request,
        })
        return False

    print(f"[+] Order Executed! Ticket #{result.order} | {action} {lot_size} Lots {symbol} | "
          f"Entry: {price} | SL: {sl} | TP: {tp} | Risk tier: {risk_mgr.state['tier']} "
          f"({risk_mgr.risk_pct():.2f}%)")
    return True


# ============================================================
# DAEMON MAIN LOOP
# ============================================================
def get_current_candle_opentime(symbol: str, timeframe):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 1)
    return rates[0]["time"] if rates is not None and len(rates) > 0 else None


def run_daemon():
    global running

    if not mt5.initialize():
        print(f"[!] MT5 Init Failed: {mt5.last_error()}")
        sys.exit(1)

    if CFG.indicator_lookback < 22:
        print(f"[!] WARNING: LOOKBACK_CANDLES={CFG.indicator_lookback} is below 22 - "
              f"EMA21 will return None and htf_trend/rsi may also be starved. "
              f"Signals will likely fall back to HOLD until this is raised.")
    if CFG.prompt_bars > CFG.indicator_lookback:
        CFG.prompt_bars = CFG.indicator_lookback

    risk_mgr = RiskManager(CFG.state_file)
    mode = "DRY RUN (no live orders)" if CFG.dry_run else "LIVE"
    print(f"=== Daemon Claude v{__version__} Started for {CFG.symbol} | Mode: {mode} | "
          f"Timeframe: {CFG.trade_timeframe} | LLM budget: {MAX_LLM_LATENCY_SEC}s | "
          f"Checking every {CFG.check_interval_sec}s ===")

    last_candle_time = get_current_candle_opentime(CFG.symbol, TIMEFRAME)

    # The Ollama call runs in a background thread so trailing-stop management
    # (below) keeps running on its normal cadence even while a signal request
    # is still in flight - a slow LLM response should never leave open
    # positions unmanaged.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pending_future = None
    pending_ctx = None
    pending_started = None

    while running:
        try:
            if not mt5.terminal_info():
                print("[!] Lost MT5 connection, attempting reconnect...")
                mt5.initialize()
                time.sleep(CFG.check_interval_sec)
                continue

            account_info = mt5.account_info()
            if account_info:
                risk_mgr.check_daily_reset(account_info.equity)
                halted = risk_mgr.check_circuit_breaker(account_info.equity)
            else:
                halted = True

            sync_closed_trades(risk_mgr, CFG.symbol, CFG.magic_number)

            # Trailing stop management runs every loop iteration regardless of
            # halt state or a pending LLM request - protecting open positions
            # is not the same as opening new risk.
            apply_dynamic_trailing_stop(CFG.symbol, CFG.magic_number, risk_mgr)

            # Collect a previously-submitted signal request if it has finished.
            if pending_future is not None and pending_future.done():
                elapsed = time.time() - pending_started
                try:
                    signal_data = pending_future.result()
                except Exception as e:
                    signal_data = {"action": "HOLD", "confidence": 0.0,
                                    "reasoning": f"Worker thread error: {e}"}
                source = signal_data.get("source", "local")
                escalation_note = ""
                if source == "cloud":
                    escalation_note = (f" [escalated from local {signal_data.get('escalated_from_action')} "
                                        f"@ {signal_data.get('escalated_from_confidence')}]")
                print(f"\n[+] Decision (after {elapsed:.1f}s) from {source} model "
                      f"'{signal_data.get('model', 'unknown')}': {signal_data.get('action')} "
                      f"(Conf: {signal_data.get('confidence')}){escalation_note}")
                print(f"    Reason: {signal_data.get('reasoning')}")
                log_event({
                    "event": "signal",
                    "source": source,
                    "model": signal_data.get("model"),
                    "action": signal_data.get("action"),
                    "confidence": signal_data.get("confidence"),
                    "reasoning": signal_data.get("reasoning"),
                    "latency_sec": signal_data.get("latency_sec"),
                    "decision_elapsed_sec": round(elapsed, 1),
                    "escalated_from_action": signal_data.get("escalated_from_action"),
                    "escalated_from_confidence": signal_data.get("escalated_from_confidence"),
                    "market": {k: pending_ctx.get(k) for k in (
                        "symbol", "timeframe", "spread_pips", "atr_pips", "rsi14",
                        "ema9_vs_ema21", "htf_bias", "session")},
                })
                if halted:
                    print("[*] Daily circuit breaker is active - not opening a new trade "
                          "for this signal.")
                else:
                    execute_protected_trade(signal_data, pending_ctx, risk_mgr)
                pending_future, pending_ctx, pending_started = None, None, None

            current_candle_time = get_current_candle_opentime(CFG.symbol, TIMEFRAME)
            if current_candle_time and current_candle_time != last_candle_time:
                last_candle_time = current_candle_time

                if halted:
                    print("[*] New candle, but daily circuit breaker is active. Skipping signal.")
                elif pending_future is not None and not pending_future.done():
                    still_waiting = time.time() - pending_started
                    print(f"[!] New {CFG.trade_timeframe} candle, but the previous signal "
                          f"request is still running after {still_waiting:.1f}s - "
                          f"skipping this candle rather than stacking requests.")
                else:
                    now = dt.datetime.now()
                    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[!] {current_time} New {CFG.trade_timeframe} candle. Running context analysis...")

                    ctx = fetch_dynamic_context(CFG.symbol, TIMEFRAME)
                    if ctx:
                        ctx["spread_pips"] = get_spread_pips(CFG.symbol)
                        ctx["htf_bias"] = get_htf_bias(CFG.symbol)
                        ctx["session"] = get_trading_session()
                        buy_open = count_open_positions_in_direction(CFG.symbol, "BUY", CFG.magic_number)
                        sell_open = count_open_positions_in_direction(CFG.symbol, "SELL", CFG.magic_number)
                        ctx["open_position"] = (
                            f"{buy_open} BUY / {sell_open} SELL"
                            #f"(max {CFG.max_positions_per_direction} per direction, "
                            #f"so BUY room={max(0, CFG.max_positions_per_direction - buy_open)}, "
                            #f"SELL room={max(0, CFG.max_positions_per_direction - sell_open)})"
                        )
                        if account_info and risk_mgr.state.get("day_start_equity"):
                            start_eq = risk_mgr.state["day_start_equity"]
                            ctx["daily_pnl_pct"] = round(
                                (account_info.equity - start_eq) / start_eq * 100, 2
                            )
                        else:
                            ctx["daily_pnl_pct"] = 0.0

                        pending_future = executor.submit(get_trade_decision, ctx)
                        pending_ctx = ctx
                        pending_started = time.time()
                        print(f"\n[*] Signal request submitted in background "
                              f"(budget {MAX_LLM_LATENCY_SEC}s, trailing stops keep running).")

        except Exception as e:
            print(f"[!] Daemon Error: {e}")

        time.sleep(CFG.check_interval_sec)

    executor.shutdown(wait=False)
    mt5.shutdown()
    print("[+] MT5 shutdown cleanly. Daemon stopped.")


if __name__ == "__main__":
    run_daemon()
