"""
Triple Screen analysis of CZCE.CF609 on weekly / daily / 15min timeframes.

This is Elder's classic position-trading combo:
  Screen 1 (tide)   : Weekly  — long-term trend direction (MACD hist + EMA13)
  Screen 2 (wave)   : Daily   — oscillator pullback (Force Index EMA2)
  Screen 3 (ripple) : 15min   — precise entry (trailing stop on prev bar high/low)

Different from the egg 25min/5min/1min day-trading combo -- weekly/daily/15min
is for swing/position trades that may last days to weeks.

Usage:
    python probe_cf609_wd15.py
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
    calc_ema,
    calc_macd,
    calc_force_index,
)


SYMBOL = "CZCE.CF609"
TICK_SIZE = 5.0  # cotton price tick = 5 yuan/ton

# Weekly / Daily / 15min durations in seconds
KLINE_DURS = {
    "15min": 900,
    "1day":  86400,
    "1week": 604800,
}
DATA_LEN = 500


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"


def fmt_day(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d")
    return "---"


def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        # ── Fetch klines for all 3 timeframes ──
        print(f"\nFetching klines for {SYMBOL} (weekly / daily / 15min)...")
        klines_map = {}
        for label, dur in KLINE_DURS.items():
            klines_map[label] = api.get_kline_serial(SYMBOL, dur, data_length=DATA_LEN)

        deadline = _time.time() + 20
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if all(len(k) > 0 and k.iloc[-1]["close"] > 0 for k in klines_map.values()):
                break

        for label, k in klines_map.items():
            n = len(k)
            if n == 0:
                print(f"  {label:6s}: NO DATA")
                continue
            t0 = fmt_day(k.iloc[0]["datetime"])
            t1 = fmt_day(k.iloc[-1]["datetime"])
            last_close = k.iloc[-1]["close"]
            print(f"  {label:6s}: {n:4d} bars  {t0} -> {t1}  last_close={last_close:.0f}")

        if any(len(k) == 0 or k.iloc[-1]["close"] <= 0 for k in klines_map.values()):
            print(f"\n{SYMBOL} has no kline data for one or more timeframes.")
            return

        kw = klines_map["1week"]
        kd = klines_map["1day"]
        k15 = klines_map["15min"]

        # ── Screen 1: Weekly trend ──
        s1 = determine_screen1_trend(kw)
        dif_w, dea_w, bar_w = calc_macd(kw)
        ema13_w = calc_ema(kw, span=13)

        print(f"\n{'='*72}")
        print(f"  Triple Screen Analysis: {SYMBOL}  (Weekly / Daily / 15min)")
        print(f"{'='*72}")

        print(f"\n--- Screen 1 (WEEKLY) Tide -- Trend Direction Filter ---")
        print(f"  Trend        : {s1['trend'].upper()}")
        print(f"  MACD hist    : {s1['hist_slope']}  (last 5: {' -> '.join(f'{v:.2f}' for v in s1['hist_recent'])})")
        print(f"  EMA(13)      : {s1['ema_recent'][0]:.2f}  slope={s1['ema_slope']}  (10 weeks ago: {s1['ema_recent'][1]:.2f})")
        print(f"  Last 3 MACD  : DIF={dif_w.iloc[-1]:.2f}  DEA={dea_w.iloc[-1]:.2f}  hist={bar_w.iloc[-1]:.2f}")
        print(f"  Last 3 EMA13 : {ema13_w.iloc[-3]:.2f} -> {ema13_w.iloc[-2]:.2f} -> {ema13_w.iloc[-1]:.2f}")
        action = {
            "bullish": "only long allowed -- ride the weekly tide",
            "bearish": "only short allowed -- ride the weekly tide",
            "neutral": "no trades -- wait for weekly direction",
        }[s1["trend"]]
        print(f"  Action       : {action}")

        # ── Screen 2: Daily Force Index pullback ──
        s2 = determine_screen2_signal(s1["trend"], kd)
        fi_d = calc_force_index(kd, ema_span=2)

        print(f"\n--- Screen 2 (DAILY) Wave -- Oscillator Pullback Signal ---")
        print(f"  Signal       : {s2['signal']}")
        print(f"  FI EMA(2)    : {s2['fi_value']:.0f}  ({'above zero' if s2['fi_above_zero'] else 'below zero'})")
        if s2['fi_recent']:
            print(f"  Last 5 FI    : {' -> '.join(f'{v:.0f}' for v in s2['fi_recent'])}")
        print(f"  Zero cross   : {s2['zero_cross']}")
        print(f"  Divergence   : {s2['divergence']}")
        print(f"  Description  : {s2['pullback_desc']}")

        # ── Screen 3: 15min precise entry ──
        s3 = determine_screen3_entry(s1["trend"], s2["signal"], k15, tick_size=TICK_SIZE)

        print(f"\n--- Screen 3 (15min) Ripple -- Precise Entry ---")
        print(f"  Signal       : {s3['signal']}")
        if s3["signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
            print(f"  Entry price  : {s3['entry_price']:.0f}  (tick_size={TICK_SIZE:.0f})")
            print(f"  Stop loss    : {s3['stop_loss']:.0f}")
            print(f"  Prev bar     : H={s3['prev_high']:.0f}  L={s3['prev_low']:.0f}")
            risk = abs(s3["entry_price"] - s3["stop_loss"])
            print(f"  Risk         : {risk:.0f} points ({risk/TICK_SIZE:.0f} ticks)")
        print(f"  Description  : {s3['desc']}")

        # ── Recent bars summary ──
        print(f"\n--- Recent WEEKLY bars (last 5) ---")
        print(f"  {'week ending':<14} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>12}")
        for _, row in kw.iloc[-5:].iterrows():
            t = fmt_day(row["datetime"])
            print(f"  {t:<14} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>12}")

        print(f"\n--- Recent DAILY bars (last 5) ---")
        print(f"  {'date':<14} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>12}")
        for _, row in kd.iloc[-5:].iterrows():
            t = fmt_day(row["datetime"])
            print(f"  {t:<14} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>12}")

        print(f"\n--- Recent 15min bars (last 5) ---")
        print(f"  {'time':<16} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>10}")
        for _, row in k15.iloc[-5:].iterrows():
            t = fmt_time(row["datetime"])
            print(f"  {t:<16} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>10}")

        # ── Summary verdict ──
        print(f"\n{'='*72}")
        print(f"  Cascade Summary")
        print(f"{'='*72}")
        print(f"  Weekly tide  : {s1['trend']:8s}  (MACD hist {s1['hist_slope']}, EMA {s1['ema_slope']})")
        print(f"  Daily wave   : {s2['signal']:16s}  (FI={s2['fi_value']:.0f})")
        print(f"  15min ripple : {s3['signal']:16s}")
        print()
        if s1["trend"] == "neutral":
            print(f"  --> STAND ASIDE. Weekly trend is neutral -- no trades allowed.")
            print(f"      Wait for weekly MACD histogram and EMA(13) to agree on direction.")
        elif s3["signal"] == "triggered_long":
            print(f"  --> LONG ENTRY TRIGGERED at {s3['entry_price']:.0f} (stop {s3['stop_loss']:.0f})")
        elif s3["signal"] == "triggered_short":
            print(f"  --> SHORT ENTRY TRIGGERED at {s3['entry_price']:.0f} (stop {s3['stop_loss']:.0f})")
        elif s3["signal"] == "pending_long":
            print(f"  --> PENDING LONG. Buy stop at {s3['entry_price']:.0f} if 15min breaks above.")
        elif s3["signal"] == "pending_short":
            print(f"  --> PENDING SHORT. Sell stop at {s3['entry_price']:.0f} if 15min breaks below.")
        else:
            print(f"  --> No entry signal. Screens not aligned.")
        print(f"{'='*72}")

    finally:
        api.close()


if __name__ == "__main__":
    main()
