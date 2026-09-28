# Changelog

Versions are tracked here (and via git tags/releases), not in filenames. The
running version is available as `__version__` in `claude_bot.py`, is printed
at daemon startup, and is stamped on every order's `comment` field.

## 9.2

Replaces the v9.1 local gateway experiment with a single unified gateway
(**llmspy**) for every model call, local and cloud alike, and generalizes
escalation from a single local→cloud hop into an ordered N-tier chain.

- **Single gateway, single code path.** `claude_bot.py` no longer speaks
  Ollama's native `/api/generate` for the local model and a separate
  OpenAI-chat-completions dialect for cloud providers. Every tier now goes
  through one call (`call_llmspy_model`) to `LLMSPY_BASE_URL`
  (`.../v1/chat/completions`); llmspy itself decides whether a given model
  name resolves to a local (Ollama) or cloud backend.
- **`CLOUD_PROVIDERS` removed**, along with all Groq/Cerebras/OpenRouter/
  LocalCloud-specific config (`GROQ_*`, `CEREBRAS_*`, `OPENROUTER_*`,
  `LOCALCLOUD_*`) and their API keys. This bot no longer holds any cloud
  credentials at all — llmspy is the only thing that authenticates outward.
- **Escalation generalized to an ordered chain.** `LLMSPY_MODELS` is now a
  comma-separated, ordered list of model names (tier 0 first, e.g.
  `phi4-mini,gpt-oss-20b`). If tier 0's confidence is below
  `ESCALATION_CONFIDENCE_THRESHOLD`, later tiers are tried in order and the
  first one to return a valid answer wins (its own confidence isn't
  re-checked — `MIN_CONFIDENCE` still filters whichever answer is finally
  used, at trade-execution time). A tier that errors or times out falls
  through to the next; if every later tier fails, tier 0's answer is kept
  as the final fallback regardless of its confidence. Adding a third or
  fourth tier is now just adding another name to `LLMSPY_MODELS` — no code
  change required.
- **Per-call timeout unified.** `CLOUD_TIMEOUT_SEC` (used only for cloud
  providers) is replaced by `LLMSPY_TIMEOUT_SEC`, applied to every tier's
  call alike. Each individual call is capped at
  `min(LLMSPY_TIMEOUT_SEC, time remaining in this candle's overall
  MAX_LLM_LATENCY_SEC budget)`, so a slow chain still can't blow past the
  per-timeframe decision budget that already existed.
- **Ollama-native tuning removed.** `OLLAMA_NUM_THREAD`, `OLLAMA_NUM_CTX`,
  and `OLLAMA_MODEL` auto-detection (via `/api/ps` / `/api/tags`) are gone
  — confirmed via direct testing that llmspy's chat-completions endpoint
  does not forward a nested Ollama `options` object, so `num_ctx`/
  `num_thread` have no effect from this bot and must be configured on the
  llmspy/Ollama side directly. `OLLAMA_NUM_PREDICT` is replaced by
  `LLMSPY_MAX_TOKENS`, sent as the standard `max_tokens` field (confirmed
  to work through llmspy, unlike the nested `options` object).
- **New: `KEEP_LOCAL_ALIVE`** (minutes) — sent as Ollama's `keep_alive`
  field, confirmed to pass through llmspy and extend how long Ollama keeps
  the local model resident in RAM between candles. Only ever sent on the
  tier-0 call; later (presumably cloud) tiers never receive it.
- `ESCALATION_SHARE_LOCAL_ANSWER` behavior is unchanged in spirit but now
  applies uniformly to every tier past tier 0, not just a single cloud hop.
- Trade log events renamed/extended for the chain: `escalation` and
  `escalation_failed` events now carry a `tier` index and `model` name
  instead of a `provider` name; the `signal` event gains an
  `escalation_tier` field.

## 9.1

Added local openai compatible gateway for LLM
New parameters for using are in .env file

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
