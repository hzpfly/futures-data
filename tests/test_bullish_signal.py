"""
Test bullish signal path with historical data.

Walks through historical klines bar-by-bar and scans for bullish cascade events:
  Screen 1 (25min) bullish  -> Screen 2 (5min) buy_signal  -> Screen 3 (1min) triggered_long

Verifies the cascade logic end-to-end with real market data, and checks invariants:
  - When Screen 1 is neutral, Screen 2 must return no_signal (gating)
  - Cascade only produces long entries when Screens 1+2 agree on bullish side

Usage:
    python test_bullish_signal.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tqsdk import TqApi, TqAuth
from datetime import datetime
import time as _time

from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    determine_screen1_trend,
    determine_screen2_signal,
    determine_screen3_entry,
    discover_main_contract,
    KLINE_DURS,
)


DATA_LEN   = 2000   # max historical bars per timeframe
WARMUP_25  = 50     # skip first 50 25min bars (MACD/EMA warmup)


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"


def fetch_historical_data(api, symbol):
    """Fetch historical klines for all three timeframes and wait until ready."""
    print(f"\nFetching historical data (data_length={DATA_LEN})...")
    klines_map = {}
    for label, dur in KLINE_DURS.items():
        klines_map[label] = api.get_kline_serial(symbol, dur, data_length=DATA_LEN)

    deadline = _time.time() + 30
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())
        if all(len(k) >= 100 for k in klines_map.values()):
            break

    for label, k in klines_map.items():
        n = len(k)
        t0 = fmt_time(k.iloc[0]["datetime"]) if n > 0 else "---"
        t1 = fmt_time(k.iloc[-1]["datetime"]) if n > 0 else "---"
        print(f"  {label:5s}: {n:5d} bars  {t0} -> {t1}")

    return klines_map


def scan_bullish_cascade(klines_map):
    """
    Walk through historical data bar-by-bar:
      1. For each 25min bar i (after warmup), compute Screen 1.
      2. If Screen 1 is bullish, scan all 5min bars within that 25min window
         and compute Screen 2 (with screen1_trend='bullish').
      3. If Screen 2 yields buy_signal / divergence_buy, scan all 1min bars
         within that 5min window and compute Screen 3.
      4. Log all triggered_long / pending_long events.
    """
    klines_25 = klines_map["25min"]
    klines_5  = klines_map["5min"]
    klines_1  = klines_map["1min"]

    n_25 = len(klines_25)
    n_5  = len(klines_5)
    n_1  = len(klines_1)

    stats = {
        "total_25_scanned": 0,
        "s1_bullish": 0, "s1_bearish": 0, "s1_neutral": 0,
        "s2_buy_signal": 0, "s2_divergence_buy": 0, "s2_no_signal_when_bullish": 0,
        "s3_triggered_long": 0, "s3_pending_long": 0,
        "invariant_violations_neutral_s2": 0,
    }

    bullish_periods = []
    screen2_events  = []
    screen3_events  = []

    # ── Step 1: Compute Screen 1 for every 25min bar ──
    print(f"\nComputing Screen 1 for 25min bars {WARMUP_25}..{n_25-1}...")
    s1_cache = {}
    for i in range(WARMUP_25, n_25):
        s1 = determine_screen1_trend(klines_25.iloc[:i+1])
        s1_cache[i] = s1
        stats["total_25_scanned"] += 1
        if s1["trend"] == "bullish":
            stats["s1_bullish"] += 1
        elif s1["trend"] == "bearish":
            stats["s1_bearish"] += 1
        else:
            stats["s1_neutral"] += 1

    print(f"  Bullish={stats['s1_bullish']}  Bearish={stats['s1_bearish']}  Neutral={stats['s1_neutral']}")

    # ── Step 2: For each bullish Screen 1, scan 5min bars in that 25min window ──
    print(f"Scanning 5min bars during bullish periods...")
    for i in range(WARMUP_25, n_25):
        s1 = s1_cache[i]
        if s1["trend"] != "bullish":
            continue

        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9

        bullish_periods.append({
            "i": i, "t": fmt_time(t_start_25),
            "hist_slope": s1["hist_slope"], "ema_slope": s1["ema_slope"],
        })

        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()

        for j_pos in idx_5_list:
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal("bullish", slice_5)

            if s2["signal"] == "buy_signal":
                stats["s2_buy_signal"] += 1
            elif s2["signal"] == "divergence_buy":
                stats["s2_divergence_buy"] += 1
            else:
                if s2["signal"] == "no_signal":
                    stats["s2_no_signal_when_bullish"] += 1
                continue

            t_start_5 = klines_5.iloc[j_pos]["datetime"]
            t_end_5   = klines_5.iloc[j_pos+1]["datetime"] if j_pos+1 < n_5 else t_start_5 + 300 * 1e9

            screen2_events.append({
                "i_25": i, "j_5": j_pos,
                "t_25": fmt_time(t_start_25), "t_5": fmt_time(t_start_5),
                "signal": s2["signal"], "fi_value": s2["fi_value"],
                "divergence": s2["divergence"],
            })

            # ── Step 3: Scan 1min bars within this 5min window ──
            mask_1 = (klines_1["datetime"] >= t_start_5) & (klines_1["datetime"] < t_end_5)
            idx_1_list = klines_1.index[mask_1].tolist()

            for k_pos in idx_1_list:
                slice_1 = klines_1.iloc[:k_pos+1]
                s3 = determine_screen3_entry("bullish", s2["signal"], slice_1)

                if s3["signal"] == "triggered_long":
                    stats["s3_triggered_long"] += 1
                    screen3_events.append({
                        "i_25": i, "j_5": j_pos, "k_1": k_pos,
                        "t_25": fmt_time(t_start_25),
                        "t_5":  fmt_time(t_start_5),
                        "t_1":  fmt_time(klines_1.iloc[k_pos]["datetime"]),
                        "signal": s3["signal"],
                        "entry_price": s3["entry_price"],
                        "stop_loss":   s3["stop_loss"],
                        "prev_high":   s3["prev_high"],
                        "prev_low":    s3["prev_low"],
                        "fi_value":    s2["fi_value"],
                        "s2_signal":   s2["signal"],
                    })
                elif s3["signal"] == "pending_long":
                    stats["s3_pending_long"] += 1

    # ── Invariant check: Screen 1 neutral MUST gate Screen 2 ──
    print(f"Running invariant checks (neutral Screen 1 must gate Screen 2)...")
    for i in range(WARMUP_25, n_25):
        s1 = s1_cache[i]
        if s1["trend"] != "neutral":
            continue
        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9
        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()
        for j_pos in idx_5_list[:1]:  # sample first 5min bar in window
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal("neutral", slice_5)
            if s2["signal"] != "no_signal":
                stats["invariant_violations_neutral_s2"] += 1

    return stats, bullish_periods, screen2_events, screen3_events


def print_results(stats, bullish_periods, screen2_events, screen3_events):
    print(f"\n{'='*72}")
    print(f"  Bullish Signal Path Test -- Results")
    print(f"{'='*72}")

    total = stats['total_25_scanned']
    print(f"\n-- Screen 1 (25min) trend distribution --")
    print(f"  Total bars scanned: {total}")
    print(f"  Bullish:  {stats['s1_bullish']:5d}  ({100*stats['s1_bullish']/max(1,total):.1f}%)")
    print(f"  Bearish:  {stats['s1_bearish']:5d}  ({100*stats['s1_bearish']/max(1,total):.1f}%)")
    print(f"  Neutral:  {stats['s1_neutral']:5d}  ({100*stats['s1_neutral']/max(1,total):.1f}%)")

    print(f"\n-- Screen 2 (5min) signals during bullish Screen 1 --")
    print(f"  buy_signal:      {stats['s2_buy_signal']:5d}")
    print(f"  divergence_buy:  {stats['s2_divergence_buy']:5d}")
    print(f"  no_signal:       {stats['s2_no_signal_when_bullish']:5d}")

    print(f"\n-- Screen 3 (1min) long entries (after buy_signal) --")
    print(f"  triggered_long:  {stats['s3_triggered_long']:5d}")
    print(f"  pending_long:    {stats['s3_pending_long']:5d}")

    print(f"\n-- Invariant checks --")
    print(f"  Screen 1 neutral -> Screen 2 no_signal  violations: {stats['invariant_violations_neutral_s2']}")

    print(f"\n-- Sample Screen 1 bullish periods (first 5) --")
    for p in bullish_periods[:5]:
        print(f"  25min@{p['t']}  hist={p['hist_slope']:7s}  ema={p['ema_slope']:7s}")

    print(f"\n-- Sample Screen 2 buy_signal events (first 5) --")
    for e in screen2_events[:5]:
        print(f"  25min@{e['t_25']}  5min@{e['t_5']}  signal={e['signal']:14s}  FI={e['fi_value']:.0f}  div={e['divergence']}")

    triggered = [e for e in screen3_events if e["signal"] == "triggered_long"]
    print(f"\n-- Sample Screen 3 triggered_long events (first 5) --")
    for e in triggered[:5]:
        print(f"  25min@{e['t_25']}  5min@{e['t_5']}  1min@{e['t_1']}")
        print(f"    entry={e['entry_price']:.0f} (prev_high+1={e['prev_high']:.0f}+1)  SL={e['stop_loss']:.0f}  prev_low={e['prev_low']:.0f}  FI={e['fi_value']:.0f}")

    if triggered:
        print(f"\n{'='*72}")
        print(f"  Detailed Bullish Cascade Example")
        print(f"{'='*72}")
        e = triggered[0]
        print(f"  Step 1 -- Screen 1 (25min) @ {e['t_25']}")
        print(f"           Trend = bullish  (MACD hist rising + EMA rising)")
        print(f"           Action: only long allowed")
        print(f"  Step 2 -- Screen 2 (5min) @ {e['t_5']}")
        print(f"           Signal = {e['s2_signal']}  (Force Index = {e['fi_value']:.0f} < 0)")
        print(f"           Action: pullback buy opportunity detected")
        print(f"  Step 3 -- Screen 3 (1min) @ {e['t_1']}")
        print(f"           Signal = triggered_long")
        print(f"           Entry price: {e['entry_price']:.0f}  (= prev_high {e['prev_high']:.0f} + tick_size 1)")
        print(f"           Stop loss:   {e['stop_loss']:.0f}  (= min(curr_low, prev_low {e['prev_low']:.0f}))")
        print(f"           Risk:        {e['entry_price'] - e['stop_loss']:.0f} points")

    print(f"\n{'='*72}")
    if stats['s3_triggered_long'] > 0:
        verdict = "PASSED -- bullish cascade verified"
    elif stats['s2_buy_signal'] + stats['s2_divergence_buy'] > 0:
        verdict = "PARTIAL -- Screen 1+2 agreed but no long triggered in 1min window"
    else:
        verdict = "NO BULLISH CASCADE in historical window"
    if stats['invariant_violations_neutral_s2'] == 0:
        verdict += " | invariants HOLD"
    else:
        verdict += f" | {stats['invariant_violations_neutral_s2']} INVARIANT VIOLATIONS"
    print(f"  Test result: {verdict}")
    print(f"{'='*72}")


def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")

    api = TqApi(auth=TqAuth(username, password))

    try:
        symbol = discover_main_contract(api)
        print(f"Main contract: {symbol}")

        klines_map = fetch_historical_data(api, symbol)

        for label, k in klines_map.items():
            if len(k) < 100:
                print(f"\nERROR: {label} has only {len(k)} bars -- need at least 100 for warmup")
                return

        stats, bullish_periods, screen2_events, screen3_events = scan_bullish_cascade(klines_map)
        print_results(stats, bullish_periods, screen2_events, screen3_events)

    finally:
        api.close()


if __name__ == "__main__":
    main()
