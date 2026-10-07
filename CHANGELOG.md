# Changelog

Versions are tracked here (and via git tags/releases), not in filenames. The
running version is available as `__version__` in `claude_bot.py`, is printed
at daemon startup, and is stamped on every order's `comment` field.

## 9.3

- `ESCALATION_MODE` (new): `replace` (default, same as 9.2) swaps a tier-0 answer below `ESCALATION_CONFIDENCE_THRESHOLD` for the next tier's answer; both answers are logged so models can be compared. `confirm` lets tier 0 decide and calls the next tier only for candidate trades (BUY/SELL with confidence >= `MIN_CONFIDENCE`), where it can veto (disagreement -> HOLD) or confirm (confidence = min of both).
- `ESCALATION_CONFIDENCE_THRESHOLD` default is now 0.80 in the Python code as well (previously 0.70 in Python but 0.80 in `claude_bot.sh`).
- Models are configured only through `LLMSPY_MODELS`; `LLMSPY_BASE_URL` points at the gateway. `LLMSPY_MAX_TOKENS` default raised to 1500 (reasoning models spend tokens thinking).
- Prompt no longer contains open positions / daily P&L (`PROMPT_INCLUDE_POSITIONS=true` restores it); the system prompt states that BUY/SELL open a new position and HOLD means no new trade.
- New entry filters: `BLOCK_OPPOSITE_SIDE` (default true), `MIN_ENTRY_GAP_MIN` (default 30); `MAX_SPREAD_ATR_RATIO` is now configurable.
- Trailing SL step is ATR-scaled (`TRAIL_SL_MIN_STEP_ATR_FRACTION`, default 0.1) instead of a fixed 1 pip.
- Audit log: new `trade_opened` (position ticket), `position_peak` (watermark at close) and `confirmation` events; `trade_closed` gains `position_id`, `reason`, `price`; `signal.market` gains `last_close`, `ema9`, `ema21`.
- Added `eval_signals.py`: scores every logged signal (each model plus a plain EMA9/EMA21 rule) against real M15 forward returns.
- Documented that MT5 bar times are broker server time (`BROKER_UTC_OFFSET_HOURS`).

## 9.2

- All model calls go through the llmspy gateway (one OpenAI-chat-completions endpoint); an ordered `LLMSPY_MODELS` chain replaces the Ollama / cloud-provider split. Provider API keys are no longer held by this bot.
- `KEEP_LOCAL_ALIVE` and `LLMSPY_MAX_TOKENS` replace the Ollama-specific options.

## 9.1

- Added a local OpenAI-compatible gateway for LLM calls; new parameters are configured in `.env`.

## 9.0

- **Renamed** `claude_v8.py` → `claude_bot.py`, `claude_bot_v8.sh` → `claude_bot.sh`. No version suffix on filenames going forward — see the note above on how to check the running version.
- **`MAGIC_NUMBER` default changed 260921 → 260922.** The bot only manages positions carrying its own magic number, so upgrading from a prior build with open positions requires either closing them first or temporarily setting `MAGIC_NUMBER=260921` until they close.
- Fixed the higher-timeframe bias mapping: it previously mapped `H1` trade timeframe to an `H1` bias (i.e. *equal to*, not higher than, the trade timeframe). Now: `M1`–`M30` → `H1`, `H1` → `H4`, `H4` → `D1`.
- Fixed the Chandelier Exit watermark only being updated *after* trailing activated, which could forget an earlier price peak reached before activation. It's now updated every loop iteration for every open position; activation is judged on peak (not current) profit and, once reached, stays active through a later pullback.
- LLM output is now validated and normalised: unrecognised `action` values fall back to `HOLD`, `confidence` is coerced into `[0, 1]` (accepts `"85"`, `"85%"`, `85`, etc.), and JSON is extracted robustly — including from `<think>...</think>` blocks emitted by reasoning models, and from responses with stray text around the JSON object.
- Cloud provider calls: if a provider rejects `response_format: json_object`, the request is retried once without it. Added optional `<PROVIDER>_EXTRA_PARAMS` (JSON) for provider-specific fields such as `reasoning_effort`. Any exception from a provider now correctly falls through to the next one (was narrower before, missing certain failure modes).
- Added `ESCALATION_SHARE_LOCAL_ANSWER` (default `false`): the cloud model forms an independent opinion by default instead of seeing the local model's (possibly weak) answer first.
- Extended-TP updates during trailing are now only sent when they improve by at least `tp_update_min_atr_fraction` (default `0.25`) × ATR, instead of on almost every 5-second tick.
- Trailing stop/TP updates are rounded to the symbol's actual digits, checked against the broker's minimum stop distance, and a failed/rejected modification is now logged instead of silently ignored.
- New trade entries are now skipped if the daily circuit breaker tripped while an LLM request was in flight (previously only checked at candle-open time).
- Order filling mode is now chosen from what the symbol actually supports (FOK/IOC/RETURN) instead of hardcoded IOC.
- `sync_closed_trades`: net P&L now includes commission and swap, not just raw profit. On a fresh account/first run, the closed-deal cursor is seeded to the most recent deal instead of replaying all existing history into the risk tiers.
- Every trade decision (not just trade attempts) is now written to `trade_log.jsonl` as a `"signal"` event, including the market context, so `HOLD`s and filtered-out signals are auditable too.
- Removed the unused `has_open_position_in_direction` function.
- `DRY_RUN` now **defaults to `true`** — live orders require explicitly setting `DRY_RUN=false`.
- `claude_bot.sh` rewritten to load a `.env` file (proper quoting/CRLF handling; real environment variables always take precedence), warn on insecure `.env` file permissions, and track a PID file to prevent accidentally launching a second instance.
- Removed personal/environment-specific defaults (e.g. a private hostname) in favor of generic defaults (`localhost:11434`).
- Added `.env.example`, `.gitignore`, `requirements.txt`, `README.md`, this changelog.

## 8.0 (as `claude_v8.py`)

- Chandelier Exit trailing stop, replacing the previous "lock in a fraction of profit-from-entry" trail. The stop now trails behind the highest (BUY) / lowest (SELL) price reached since entry, at a fixed ATR multiple, rather than lagging behind the position's entry price.
- Fixed bar timestamps shown to the LLM/console: previously formatted with `time.localtime()`, which used whatever timezone the Wine/Python host process happened to be running under. Bars are now formatted as explicit UTC, with an optional `BROKER_UTC_OFFSET_HOURS` display shift.
- Removed `TRAIL_STEP_ATR_MULT` (exported but never read — leftover from an earlier trailing design) and `TRAIL_LOCKIN_FRACTION` (superseded by the Chandelier Exit).

## 7.0 (as `claude_v7.py`)

- Split prompts into a static `SYSTEM_PROMPT` (persona, trading rules, output-format contract) and a per-candle user prompt (market state only), sent via proper system/user roles to both the local Ollama model and every cloud provider.
- Added cloud escalation: when the local model's confidence is below a threshold, the same market context is sent to a cloud LLM (Groq / Cerebras / OpenRouter, tried in order) for a second opinion.
