"""Score every logged signal against real prices (compare models offline).

Run with the Windows Python (needs MetaTrader5) in the folder with trade_log.jsonl:
    wine python eval_signals.py
Env: TRADE_SYMBOL (default EURUSD), TRADE_TIMEFRAME (M15 only for now, the
log pairing assumes one signal per candle), EVAL_SERVER_OFFSET_H = broker
server time minus UTC (default 3). If many signals report "no matching bar",
adjust EVAL_SERVER_OFFSET_H. Subtract about 1 pip (spread) from every mean.

Compares: tier-0 model, escalation model, and a plain EMA9>EMA21 rule."""
import json, os, statistics as st, datetime as dt
import MetaTrader5 as mt5

SYMBOL = os.getenv("TRADE_SYMBOL", "EURUSD")
OFFSET_H = float(os.getenv("EVAL_SERVER_OFFSET_H", "3"))
PIP = 1e-2 if SYMBOL.upper().endswith("JPY") else 1e-4

if not mt5.initialize():
    raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
bars = {int(r["time"]): r for r in mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 1500)}
ev = [json.loads(l) for l in open("trade_log.jsonl") if l.strip()]
esc = [e for e in ev if e["event"] == "escalation"]
sig = [e for e in ev if e["event"] == "signal"]


def key(ts):
    return int(dt.datetime.fromisoformat(ts).timestamp() // 900 * 900 + OFFSET_H * 3600)


def fwd(k, h, side):
    b0, b1 = bars.get(k), bars.get(k + (h - 1) * 900)
    return None if b0 is None or b1 is None else side * (b1["close"] - b0["open"]) / PIP


rows = []
# every candle with an escalation event has both tier-0 and escalation answers
for e in esc:
    # the matching signal event is the next "signal" after this escalation
    nxt = next((s for s in sig if s["ts"] >= e["ts"]), None)
    if not nxt:
        continue
    k, t0, g, m = key(nxt["ts"]), e["tier0_signal"], e["escalation_signal"], nxt["market"]
    if t0["confidence"] > 0:
        rows.append(("tier0:" + t0.get("model", "?"), t0["action"], t0["confidence"], k))
    rows.append(("tier1:" + g.get("model", "?"), g["action"], g["confidence"], k))
    rows.append(("ema_rule", "BUY" if m["ema9_vs_ema21"] == "up" else "SELL", 1.0, k))

print(f"signals without a matching bar: {sum(1 for s in sig if key(s['ts']) not in bars)}/{len(sig)}")


def report(name, pick=lambda c: True, label=""):
    for h in (1, 2, 4):
        v = [fwd(k, h, 1 if a == "BUY" else -1) for src, a, c, k in rows
             if src == name and a != "HOLD" and pick(c)]
        v = [x for x in v if x is not None]
        if v:
            print(f"{name + label:34s} h={h} n={len(v):3d} hit={sum(x > 0 for x in v) / len(v):.0%} "
                  f"mean={st.mean(v):+.2f} pips")


for n in sorted({r[0] for r in rows}):
    report(n)
    if n.startswith("tier1"):
        report(n, lambda c: c >= 0.75, " conf>=0.75")
        report(n, lambda c: c < 0.75, " conf<0.75")
d = [fwd(key(s["ts"]), 4, 1) for s in sig]
d = [x for x in d if x is not None]
if d:
    print("unconditional 4-bar drift (long):", round(st.mean(d), 2), "pips")
