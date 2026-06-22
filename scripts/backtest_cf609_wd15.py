"""
Backtest the Triple Screen cascade on CZCE.CF609 using weekly/daily/15min timeframes.

Elder's position-trading combo:
  Screen 1 (tide)   : Weekly  — trend direction (MACD hist + EMA13)
  Screen 2 (wave)   : Daily   — Force Index pullback
  Screen 3 (ripple) : 15min   — trailing stop entry

Walks historical data chronologically:
  For each weekly bar i (after warmup):
    Compute Screen 1 trend (slice up to week i).
    If bullish/bearish, scan all daily bars within that week:
      Compute Screen 2 signal (with s1 trend).
      If buy/sell signal, scan all 15min bars within that day:
        Compute Screen 3 entry.
        Log triggered_long / triggered_short events.

Reports cascade stats for both directions + invariant checks.

Usage:
    python backtest_cf609_wd15.py
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
)


SYMBOL     = "CZCE.CF609"
TICK_SIZE  = 5.0       # cotton price tick = 5 yuan/ton
DATA_LEN   = 1000      # max history per timeframe
WARMUP_W   = 50        # skip first 50 weekly bars (MACD/EMA warmup)

# Weekly / Daily / 15min durations in seconds
KLINE_DURS = {
    "15min": 900,
    "1day":  86400,
    "1week": 604800,
}


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"


def fmt_day(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d")
    return "---"


def fetch_historical_data(api, symbol):
    print(f"\nFetching historical data for {symbol} (data_length={DATA_LEN})...")
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
        t0 = fmt_day(k.iloc[0]["datetime"]) if n > 0 else "---"
        t1 = fmt_day(k.iloc[-1]["datetime"]) if n > 0 else "---"
        print(f"  {label:6s}: {n:5d} bars  {t0} -> {t1}")
    return klines_map


def scan_cascade(klines_map):
    """
    Walk weekly bars chronologically. For each week with a clear Screen 1 trend,
    scan daily bars within that week for Screen 2 signals. For each Screen 2
    signal, scan 15min bars within that day for Screen 3 entries.
    """
    kw = klines_map["1week"]
    kd = klines_map["1day"]
    k15 = klines_map["15min"]
    n_w = len(kw)
    n_d = len(kd)

    stats = {
        "total_weeks_scanned": 0,
        "s1_bullish": 0, "s1_bearish": 0, "s1_neutral": 0,
        # Bullish side
        "s2_buy_signal": 0, "s2_divergence_buy": 0, "s2_no_signal_when_bullish": 0,
        "s3_triggered_long": 0, "s3_pending_long": 0,
        # Bearish side
        "s2_sell_signal": 0, "s2_divergence_sell": 0, "s2_no_signal_when_bearish": 0,
        "s3_triggered_short": 0, "s3_pending_short": 0,
        # Invariants
        "inv_neutral_yields_signal": 0,
        "inv_bullish_yields_sell": 0,
        "inv_bearish_yields_buy": 0,
    }

    bullish_weeks = []
    bearish_weeks = []
    screen2_events = []   # list of dicts
    screen3_events = []   # list of dicts (triggered only)

    # ── Step 1: Compute Screen 1 for every weekly bar ──
    print(f"\nComputing Screen 1 for weekly bars {WARMUP_W}..{n_w-1}...")
    s1_cache = {}
    for i in range(WARMUP_W, n_w):
        s1 = determine_screen1_trend(kw.iloc[:i+1])
        s1_cache[i] = s1
        stats["total_weeks_scanned"] += 1
        if s1["trend"] == "bullish":
            stats["s1_bullish"] += 1
        elif s1["trend"] == "bearish":
            stats["s1_bearish"] += 1
        else:
            stats["s1_neutral"] += 1

    print(f"  Bullish={stats['s1_bullish']}  Bearish={stats['s1_bearish']}  Neutral={stats['s1_neutral']}")

    # ── Step 2: For each week with clear trend, scan daily bars ──
    print(f"Scanning daily bars within trending weeks...")
    for i in range(WARMUP_W, n_w):
        s1 = s1_cache[i]
        if s1["trend"] == "neutral":
            continue

        t_start_w = kw.iloc[i]["datetime"]
        t_end_w   = kw.iloc[i+1]["datetime"] if i+1 < n_w else t_start_w + 604800 * 1e9

        week_info = {
            "i": i, "t": fmt_day(t_start_w),
            "trend": s1["trend"],
            "hist_slope": s1["hist_slope"], "ema_slope": s1["ema_slope"],
        }
        if s1["trend"] == "bullish":
            bullish_weeks.append(week_info)
        else:
            bearish_weeks.append(week_info)

        # Daily bars within this week
        mask_d = (kd["datetime"] >= t_start_w) & (kd["datetime"] < t_end_w)
        idx_d_list = kd.index[mask_d].tolist()

        for j_pos in idx_d_list:
            slice_d = kd.iloc[:j_pos+1]
            s2 = determine_screen2_signal(s1["trend"], slice_d)

            if s1["trend"] == "bullish":
                if s2["signal"] == "buy_signal":
                    stats["s2_buy_signal"] += 1
                elif s2["signal"] == "divergence_buy":
                    stats["s2_divergence_buy"] += 1
                else:
                    if s2["signal"] == "no_signal":
                        stats["s2_no_signal_when_bullish"] += 1
                    continue
            else:  # bearish
                if s2["signal"] == "sell_signal":
                    stats["s2_sell_signal"] += 1
                elif s2["signal"] == "divergence_sell":
                    stats["s2_divergence_sell"] += 1
                else:
                    if s2["signal"] == "no_signal":
                        stats["s2_no_signal_when_bearish"] += 1
                    continue

            t_start_d = kd.iloc[j_pos]["datetime"]
            t_end_d   = kd.iloc[j_pos+1]["datetime"] if j_pos+1 < n_d else t_start_d + 86400 * 1e9

            screen2_events.append({
                "i_w": i, "j_d": j_pos,
                "t_w": fmt_day(t_start_w), "t_d": fmt_day(t_start_d),
                "s1_trend": s1["trend"], "signal": s2["signal"],
                "fi_value": s2["fi_value"], "divergence": s2["divergence"],
            })

            # ── Step 3: Scan 15min bars within this day ──
            mask_15 = (k15["datetime"] >= t_start_d) & (k15["datetime"] < t_end_d)
            idx_15_list = k15.index[mask_15].tolist()

            for k_pos in idx_15_list:
                slice_15 = k15.iloc[:k_pos+1]
                s3 = determine_screen3_entry(s1["trend"], s2["signal"], slice_15, tick_size=TICK_SIZE)

                if s3["signal"] == "triggered_long":
                    stats["s3_triggered_long"] += 1
                    screen3_events.append({
                        "i_w": i, "j_d": j_pos, "k_15": k_pos,
                        "t_w": fmt_day(t_start_w),
                        "t_d": fmt_day(t_start_d),
                        "t_15": fmt_time(k15.iloc[k_pos]["datetime"]),
                        "s1_trend": s1["trend"], "s2_signal": s2["signal"],
                        "signal": s3["signal"],
                        "entry_price": s3["entry_price"],
                        "stop_loss":   s3["stop_loss"],
                        "prev_high":   s3["prev_high"],
                        "prev_low":    s3["prev_low"],
                        "fi_value":    s2["fi_value"],
                    })
                elif s3["signal"] == "triggered_short":
                    stats["s3_triggered_short"] += 1
                    screen3_events.append({
                        "i_w": i, "j_d": j_pos, "k_15": k_pos,
                        "t_w": fmt_day(t_start_w),
                        "t_d": fmt_day(t_start_d),
                        "t_15": fmt_time(k15.iloc[k_pos]["datetime"]),
                        "s1_trend": s1["trend"], "s2_signal": s2["signal"],
                        "signal": s3["signal"],
                        "entry_price": s3["entry_price"],
                        "stop_loss":   s3["stop_loss"],
                        "prev_high":   s3["prev_high"],
                        "prev_low":    s3["prev_low"],
                        "fi_value":    s2["fi_value"],
                    })
                elif s3["signal"] == "pending_long":
                    stats["s3_pending_long"] += 1
                elif s3["signal"] == "pending_short":
                    stats["s3_pending_short"] += 1

    # ── Invariant checks ──
    print(f"Running invariant checks...")
    for i in range(WARMUP_W, n_w):
        s1 = s1_cache[i]
        t_start_w = kw.iloc[i]["datetime"]
        t_end_w   = kw.iloc[i+1]["datetime"] if i+1 < n_w else t_start_w + 604800 * 1e9
        mask_d = (kd["datetime"] >= t_start_w) & (kd["datetime"] < t_end_w)
        idx_d_list = kd.index[mask_d].tolist()
        for j_pos in idx_d_list[:1]:  # sample first day of week
            slice_d = kd.iloc[:j_pos+1]
            s2 = determine_screen2_signal(s1["trend"], slice_d)
            if s1["trend"] == "neutral" and s2["signal"] != "no_signal":
                stats["inv_neutral_yields_signal"] += 1
            elif s1["trend"] == "bullish" and s2["signal"] in ("sell_signal", "divergence_sell"):
                stats["inv_bullish_yields_sell"] += 1
            elif s1["trend"] == "bearish" and s2["signal"] in ("buy_signal", "divergence_buy"):
                stats["inv_bearish_yields_buy"] += 1

    return stats, bullish_weeks, bearish_weeks, screen2_events, screen3_events


def print_results(stats, bullish_weeks, bearish_weeks, screen2_events, screen3_events):
    print(f"\n{'='*72}")
    print(f"  CF609 Weekly/Daily/15min Cascade Backtest -- Results")
    print(f"{'='*72}")

    total = stats['total_weeks_scanned']
    print(f"\n-- Screen 1 (WEEKLY) trend distribution --")
    print(f"  Total weeks scanned: {total}")
    print(f"  Bullish:  {stats['s1_bullish']:5d}  ({100*stats['s1_bullish']/max(1,total):.1f}%)")
    print(f"  Bearish:  {stats['s1_bearish']:5d}  ({100*stats['s1_bearish']/max(1,total):.1f}%)")
    print(f"  Neutral:  {stats['s1_neutral']:5d}  ({100*stats['s1_neutral']/max(1,total):.1f}%)")

    print(f"\n-- Screen 2 (DAILY) signals --")
    print(f"  BULLISH weeks:")
    print(f"    buy_signal:      {stats['s2_buy_signal']:5d}")
    print(f"    divergence_buy:  {stats['s2_divergence_buy']:5d}")
    print(f"    no_signal:       {stats['s2_no_signal_when_bullish']:5d}")
    print(f"  BEARISH weeks:")
    print(f"    sell_signal:     {stats['s2_sell_signal']:5d}")
    print(f"    divergence_sell: {stats['s2_divergence_sell']:5d}")
    print(f"    no_signal:       {stats['s2_no_signal_when_bearish']:5d}")

    print(f"\n-- Screen 3 (15min) entries --")
    print(f"  BULLISH:  triggered_long:  {stats['s3_triggered_long']:5d}  |  pending_long:  {stats['s3_pending_long']:5d}")
    print(f"  BEARISH:  triggered_short: {stats['s3_triggered_short']:5d}  |  pending_short: {stats['s3_pending_short']:5d}")

    print(f"\n-- Invariant checks --")
    print(f"  Neutral S1 -> signal  violations:        {stats['inv_neutral_yields_signal']}")
    print(f"  Bullish S1 -> sell_signal violations:    {stats['inv_bullish_yields_sell']}")
    print(f"  Bearish S1 -> buy_signal  violations:    {stats['inv_bearish_yields_buy']}")

    # ── Sample trending weeks ──
    print(f"\n-- Sample BULLISH weeks (first 5) --")
    for w in bullish_weeks[:5]:
        print(f"  week of {w['t']}  hist={w['hist_slope']:7s}  ema={w['ema_slope']:7s}")

    print(f"\n-- Sample BEARISH weeks (first 5) --")
    for w in bearish_weeks[:5]:
        print(f"  week of {w['t']}  hist={w['hist_slope']:7s}  ema={w['ema_slope']:7s}")

    # ── Sample Screen 2 events ──
    print(f"\n-- Sample Screen 2 buy_signal events (first 5) --")
    buy_events = [e for e in screen2_events if e["signal"] in ("buy_signal", "divergence_buy")]
    for e in buy_events[:5]:
        print(f"  week {e['t_w']}  day {e['t_d']}  {e['signal']:14s}  FI={e['fi_value']:.0f}  div={e['divergence']}")

    print(f"\n-- Sample Screen 2 sell_signal events (first 5) --")
    sell_events = [e for e in screen2_events if e["signal"] in ("sell_signal", "divergence_sell")]
    for e in sell_events[:5]:
        print(f"  week {e['t_w']}  day {e['t_d']}  {e['signal']:16s}  FI={e['fi_value']:.0f}  div={e['divergence']}")

    # ── Sample Screen 3 triggered events ──
    triggered_long  = [e for e in screen3_events if e["signal"] == "triggered_long"]
    triggered_short = [e for e in screen3_events if e["signal"] == "triggered_short"]

    print(f"\n-- Sample Screen 3 triggered_long events (first 5) --")
    for e in triggered_long[:5]:
        print(f"  week {e['t_w']}  day {e['t_d']}  15min {e['t_15']}")
        print(f"    entry={e['entry_price']:.0f} (prev_high+{TICK_SIZE:.0f}={e['prev_high']:.0f}+{TICK_SIZE:.0f})  "
              f"SL={e['stop_loss']:.0f}  prev_low={e['prev_low']:.0f}  FI={e['fi_value']:.0f}")

    print(f"\n-- Sample Screen 3 triggered_short events (first 5) --")
    for e in triggered_short[:5]:
        print(f"  week {e['t_w']}  day {e['t_d']}  15min {e['t_15']}")
        print(f"    entry={e['entry_price']:.0f} (prev_low-{TICK_SIZE:.0f}={e['prev_low']:.0f}-{TICK_SIZE:.0f})  "
              f"SL={e['stop_loss']:.0f}  prev_high={e['prev_high']:.0f}  FI={e['fi_value']:.0f}")

    # ── Detailed cascade examples ──
    if triggered_long:
        print(f"\n{'='*72}")
        print(f"  Detailed BULLISH Cascade Example (first triggered_long)")
        print(f"{'='*72}")
        e = triggered_long[0]
        print(f"  Step 1 -- Screen 1 (WEEKLY)  week of {e['t_w']}")
        print(f"           Trend = bullish  (MACD hist rising + EMA rising)")
        print(f"           Action: only long allowed")
        print(f"  Step 2 -- Screen 2 (DAILY)  {e['t_d']}")
        print(f"           Signal = {e['s2_signal']}  (Force Index = {e['fi_value']:.0f} < 0)")
        print(f"           Action: pullback buy opportunity")
        print(f"  Step 3 -- Screen 3 (15min)  {e['t_15']}")
        print(f"           Signal = triggered_long")
        print(f"           Entry: {e['entry_price']:.0f}  (= prev_high {e['prev_high']:.0f} + tick {TICK_SIZE:.0f})")
        print(f"           Stop:  {e['stop_loss']:.0f}")
        risk = e['entry_price'] - e['stop_loss']
        print(f"           Risk:  {risk:.0f} points ({risk/TICK_SIZE:.0f} ticks)")

    if triggered_short:
        print(f"\n{'='*72}")
        print(f"  Detailed BEARISH Cascade Example (first triggered_short)")
        print(f"{'='*72}")
        e = triggered_short[0]
        print(f"  Step 1 -- Screen 1 (WEEKLY)  week of {e['t_w']}")
        print(f"           Trend = bearish  (MACD hist falling + EMA falling)")
        print(f"           Action: only short allowed")
        print(f"  Step 2 -- Screen 2 (DAILY)  {e['t_d']}")
        print(f"           Signal = {e['s2_signal']}  (Force Index = {e['fi_value']:.0f} > 0)")
        print(f"           Action: rebound sell opportunity")
        print(f"  Step 3 -- Screen 3 (15min)  {e['t_15']}")
        print(f"           Signal = triggered_short")
        print(f"           Entry: {e['entry_price']:.0f}  (= prev_low {e['prev_low']:.0f} - tick {TICK_SIZE:.0f})")
        print(f"           Stop:  {e['stop_loss']:.0f}")
        risk = e['stop_loss'] - e['entry_price']
        print(f"           Risk:  {risk:.0f} points ({risk/TICK_SIZE:.0f} ticks)")

    # ── Verdict ──
    print(f"\n{'='*72}")
    total_triggered = stats['s3_triggered_long'] + stats['s3_triggered_short']
    total_violations = (stats['inv_neutral_yields_signal']
                        + stats['inv_bullish_yields_sell']
                        + stats['inv_bearish_yields_buy'])
    if total_triggered > 0:
        verdict = f"PASSED -- {total_triggered} cascade entries verified"
        if stats['s3_triggered_long'] > 0 and stats['s3_triggered_short'] > 0:
            verdict += " (both directions)"
    elif stats['s2_buy_signal'] + stats['s2_divergence_buy'] + stats['s2_sell_signal'] + stats['s2_divergence_sell'] > 0:
        verdict = "PARTIAL -- Screen 1+2 agreed but no 15min trigger in window"
    else:
        verdict = "NO CASCADE in historical window"
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
        # Verify the contract exists
        print(f"Target symbol: {SYMBOL}  (tick_size={TICK_SIZE})")
        quote = api.get_quote(SYMBOL)
        deadline = _time.time() + 10
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if getattr(quote, "last_price", 0) > 0 or getattr(quote, "pre_close", 0) > 0:
                break
        print(f"  Contract: {getattr(quote, 'instrument_name', 'N/A')}  "
              f"last={getattr(quote, 'last_price', 0)}  "
              f"OI={getattr(quote, 'open_interest', 0)}")

        klines_map = fetch_historical_data(api, SYMBOL)

        for label, k in klines_map.items():
            if len(k) < 100:
                print(f"\nERROR: {label} has only {len(k)} bars -- need at least 100 for warmup")
                return

        stats, bullish_weeks, bearish_weeks, screen2_events, screen3_events = scan_cascade(klines_map)
        print_results(stats, bullish_weeks, bearish_weeks, screen2_events, screen3_events)

    finally:
        api.close()


if __name__ == "__main__":
    main()
