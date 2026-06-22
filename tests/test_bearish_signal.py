"""
Test bearish signal path with historical data.

Walks through historical klines bar-by-bar and scans for bearish cascade events:
  Screen 1 (25min) bearish  -> Screen 2 (5min) sell_signal  -> Screen 3 (1min) triggered_short

Verifies the cascade logic end-to-end with real market data, and checks invariants:
  - When Screen 1 is neutral or bullish, Screen 2 must not yield a sell_signal
    that propagates to a triggered_short.
  - Cascade only produces short entries when Screens 1+2 agree on bearish side.

Usage:
    python test_bearish_signal.py
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


def scan_bearish_cascade(klines_map):
    """
    Walk through historical data bar-by-bar:
      1. For each 25min bar i (after warmup), compute Screen 1.
      2. If Screen 1 is bearish, scan all 5min bars within that 25min window
         and compute Screen 2 (with screen1_trend='bearish').
      3. If Screen 2 yields sell_signal / divergence_sell, scan all 1min bars
         within that 5min window and compute Screen 3.
      4. Log all triggered_short / pending_short events.
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
        "s2_sell_signal": 0, "s2_divergence_sell": 0, "s2_no_signal_when_bearish": 0,
        "s3_triggered_short": 0, "s3_pending_short": 0,
        "invariant_violations_bullish_s2_sell": 0,
        "invariant_violations_neutral_s2_sell": 0,
    }

    bearish_periods = []
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

    # ── Step 2: For each bearish Screen 1, scan 5min bars in that 25min window ──
    print(f"Scanning 5min bars during bearish periods...")
    for i in range(WARMUP_25, n_25):
        s1 = s1_cache[i]
        if s1["trend"] != "bearish":
            continue

        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9

        bearish_periods.append({
            "i": i, "t": fmt_time(t_start_25),
            "hist_slope": s1["hist_slope"], "ema_slope": s1["ema_slope"],
        })

        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()

        for j_pos in idx_5_list:
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal("bearish", slice_5)

            if s2["signal"] == "sell_signal":
                stats["s2_sell_signal"] += 1
            elif s2["signal"] == "divergence_sell":
                stats["s2_divergence_sell"] += 1
            else:
                if s2["signal"] == "no_signal":
                    stats["s2_no_signal_when_bearish"] += 1
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
                s3 = determine_screen3_entry("bearish", s2["signal"], slice_1)

                if s3["signal"] == "triggered_short":
                    stats["s3_triggered_short"] += 1
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
                elif s3["signal"] == "pending_short":
                    stats["s3_pending_short"] += 1

    # ── Invariant check A: bullish Screen 1 must not propagate sell_signal ──
    # Per Screen 2 contract: when screen1_trend='bullish', Screen 2 only returns
    # buy_signal/divergence_buy/no_signal -- never sell_signal. Verify directly.
    print(f"Running invariant checks (bullish/neutral Screen 1 must not yield sell_signal)...")
    for i in range(WARMUP_25, n_25):
        s1 = s1_cache[i]
        if s1["trend"] != "bullish":
            continue
        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9
        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()
        for j_pos in idx_5_list[:1]:  # sample first 5min bar
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal("bullish", slice_5)
            if s2["signal"] in ("sell_signal", "divergence_sell"):
                stats["invariant_violations_bullish_s2_sell"] += 1

    # ── Invariant check B: neutral Screen 1 must yield no_signal ──
    for i in range(WARMUP_25, n_25):
        s1 = s1_cache[i]
        if s1["trend"] != "neutral":
            continue
        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9
        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()
        for j_pos in idx_5_list[:1]:
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal("neutral", slice_5)
            if s2["signal"] in ("sell_signal", "divergence_sell"):
                stats["invariant_violations_neutral_s2_sell"] += 1

    return stats, bearish_periods, screen2_events, screen3_events


def print_results(stats, bearish_periods, screen2_events, screen3_events):
    print(f"\n{'='*72}")
    print(f"  Bearish Signal Path Test -- Results")
    print(f"{'='*72}")

    total = stats['total_25_scanned']
    print(f"\n-- Screen 1 (25min) trend distribution --")
    print(f"  Total bars scanned: {total}")
    print(f"  Bullish:  {stats['s1_bullish']:5d}  ({100*stats['s1_bullish']/max(1,total):.1f}%)")
    print(f"  Bearish:  {stats['s1_bearish']:5d}  ({100*stats['s1_bearish']/max(1,total):.1f}%)")
    print(f"  Neutral:  {stats['s1_neutral']:5d}  ({100*stats['s1_neutral']/max(1,total):.1f}%)")

    print(f"\n-- Screen 2 (5min) signals during bearish Screen 1 --")
    print(f"  sell_signal:      {stats['s2_sell_signal']:5d}")
    print(f"  divergence_sell:  {stats['s2_divergence_sell']:5d}")
    print(f"  no_signal:        {stats['s2_no_signal_when_bearish']:5d}")

    print(f"\n-- Screen 3 (1min) short entries (after sell_signal) --")
    print(f"  triggered_short:  {stats['s3_triggered_short']:5d}")
    print(f"  pending_short:    {stats['s3_pending_short']:5d}")

    print(f"\n-- Invariant checks --")
    print(f"  Screen 1 bullish -> Screen 2 sell_signal  violations: {stats['invariant_violations_bullish_s2_sell']}")
    print(f"  Screen 1 neutral -> Screen 2 sell_signal  violations: {stats['invariant_violations_neutral_s2_sell']}")

    print(f"\n-- Sample Screen 1 bearish periods (first 5) --")
    for p in bearish_periods[:5]:
        print(f"  25min@{p['t']}  hist={p['hist_slope']:7s}  ema={p['ema_slope']:7s}")

    print(f"\n-- Sample Screen 2 sell_signal events (first 5) --")
    for e in screen2_events[:5]:
        print(f"  25min@{e['t_25']}  5min@{e['t_5']}  signal={e['signal']:16s}  FI={e['fi_value']:.0f}  div={e['divergence']}")

    triggered = [e for e in screen3_events if e["signal"] == "triggered_short"]
    print(f"\n-- Sample Screen 3 triggered_short events (first 5) --")
    for e in triggered[:5]:
        print(f"  25min@{e['t_25']}  5min@{e['t_5']}  1min@{e['t_1']}")
        print(f"    entry={e['entry_price']:.0f} (prev_low-1={e['prev_low']:.0f}-1)  SL={e['stop_loss']:.0f}  prev_high={e['prev_high']:.0f}  FI={e['fi_value']:.0f}")

    if triggered:
        print(f"\n{'='*72}")
        print(f"  Detailed Bearish Cascade Example")
        print(f"{'='*72}")
        e = triggered[0]
        print(f"  Step 1 -- Screen 1 (25min) @ {e['t_25']}")
        print(f"           Trend = bearish  (MACD hist falling + EMA falling)")
        print(f"           Action: only short allowed")
        print(f"  Step 2 -- Screen 2 (5min) @ {e['t_5']}")
        print(f"           Signal = {e['s2_signal']}  (Force Index = {e['fi_value']:.0f} > 0)")
        print(f"           Action: rebound sell opportunity detected")
        print(f"  Step 3 -- Screen 3 (1min) @ {e['t_1']}")
        print(f"           Signal = triggered_short")
        print(f"           Entry price: {e['entry_price']:.0f}  (= prev_low {e['prev_low']:.0f} - tick_size 1)")
        print(f"           Stop loss:   {e['stop_loss']:.0f}  (= max(curr_high, prev_high {e['prev_high']:.0f}))")
        print(f"           Risk:        {e['stop_loss'] - e['entry_price']:.0f} points")

    print(f"\n{'='*72}")
    if stats['s3_triggered_short'] > 0:
        verdict = "PASSED -- bearish cascade verified"
    elif stats['s2_sell_signal'] + stats['s2_divergence_sell'] > 0:
        verdict = "PARTIAL -- Screen 1+2 agreed but no short triggered in 1min window"
    else:
        verdict = "NO BEARISH CASCADE in historical window"
    total_violations = (stats['invariant_violations_bullish_s2_sell']
                        + stats['invariant_violations_neutral_s2_sell'])
    if total_violations == 0:
        verdict += " | invariants HOLD"
    else:
        verdict += f" | {total_violations} INVARIANT VIOLATIONS"
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

        stats, bearish_periods, screen2_events, screen3_events = scan_bearish_cascade(klines_map)
        print_results(stats, bearish_periods, screen2_events, screen3_events)

    finally:
        api.close()


if __name__ == "__main__":
    main()
