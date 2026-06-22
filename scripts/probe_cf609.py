"""
One-off probe: fetch CZCE.cf609 (Zhengzhou cotton, Sept 2026) via TqSdk and
run the Triple Screen analysis on it.

CZCE = Zhengzhou Commodity Exchange (郑州商品交易所)
cf   = cotton (棉花)
609  = September 2026 delivery

Usage:
    python probe_cf609.py
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
    KLINE_DURS,
)


SYMBOL = "CZCE.CF609"  # TqSdk uses UPPERCASE for CZCE contracts
DATA_LEN = 500


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"


def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        # ── Step 1: Check if CZCE.cf609 exists in the instrument list ──
        print(f"\n--- Step 1: Verify {SYMBOL} exists ---")
        all_czce = api.query_quotes(ins_class="FUTURE", exchange_id="CZCE", expired=False)
        cf_contracts = sorted([q for q in all_czce if "cf" in q.lower()])
        print(f"Available CZCE cotton (cf*) contracts: {cf_contracts}")

        if SYMBOL not in cf_contracts:
            print(f"\nWARNING: {SYMBOL} not found in active CZCE cf contracts.")
            print("Trying to subscribe anyway (it may be expired or not yet listed)...")

        # ── Step 2: Get a quote to verify the instrument is tradeable ──
        print(f"\n--- Step 2: Get quote for {SYMBOL} ---")
        quote = api.get_quote(SYMBOL)
        deadline = _time.time() + 10
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if getattr(quote, "last_price", 0) > 0 or getattr(quote, "pre_close", 0) > 0:
                break

        print(f"  instrument_id    : {getattr(quote, 'instrument_id', 'N/A')}")
        print(f"  instrument_name  : {getattr(quote, 'instrument_name', 'N/A')}")
        print(f"  exchange_id      : {getattr(quote, 'exchange_id', 'N/A')}")
        print(f"  product_id       : {getattr(quote, 'product_id', 'N/A')}")
        print(f"  expired          : {getattr(quote, 'expired', 'N/A')}")
        print(f"  trading_time     : {getattr(quote, 'trading_time', {}).get('day', []) if hasattr(getattr(quote, 'trading_time', {}), 'get') else 'N/A'}")
        print(f"  last_price       : {getattr(quote, 'last_price', 0)}")
        print(f"  pre_close        : {getattr(quote, 'pre_close', 0)}")
        print(f"  volume           : {getattr(quote, 'volume', 0)}")
        print(f"  open_interest    : {getattr(quote, 'open_interest', 0)}")
        print(f"  price_tick       : {getattr(quote, 'price_tick', 'N/A')}")
        print(f"  volume_multiple  : {getattr(quote, 'volume_multiple', 'N/A')}")

        # ── Step 3: Fetch klines for all 3 timeframes ──
        print(f"\n--- Step 3: Fetch klines (data_length={DATA_LEN}) ---")
        klines_map = {}
        for label, dur in KLINE_DURS.items():
            klines_map[label] = api.get_kline_serial(SYMBOL, dur, data_length=DATA_LEN)

        deadline = _time.time() + 15
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if all(len(k) > 0 and k.iloc[-1]["close"] > 0 for k in klines_map.values()):
                break

        for label, k in klines_map.items():
            n = len(k)
            if n == 0:
                print(f"  {label:5s}: NO DATA")
                continue
            t0 = fmt_time(k.iloc[0]["datetime"])
            t1 = fmt_time(k.iloc[-1]["datetime"])
            last_close = k.iloc[-1]["close"]
            print(f"  {label:5s}: {n:4d} bars  {t0} -> {t1}  last_close={last_close:.0f}")

        # ── If no data, stop here ──
        if any(len(k) == 0 or k.iloc[-1]["close"] <= 0 for k in klines_map.values()):
            print(f"\n{SYMBOL} has no kline data -- contract may be expired or not yet active.")
            return

        # ── Step 4: Run Triple Screen analysis ──
        print(f"\n{'='*72}")
        print(f"  Triple Screen Analysis: {SYMBOL}")
        print(f"{'='*72}")

        # Screen 1: 25min trend
        k25 = klines_map["25min"]
        s1 = determine_screen1_trend(k25)
        ema12, ema26, dif, dea, bar = calc_macd(k25)
        ema13 = calc_ema(k25, span=13)

        print(f"\n--- Screen 1 (25min) Trend Direction Filter ---")
        print(f"  Trend        : {s1['trend'].upper()}")
        print(f"  MACD hist    : {s1['hist_slope']}  (last 5: {' -> '.join(f'{v:.2f}' for v in s1['hist_recent'])})")
        print(f"  EMA(13)      : {s1['ema_recent'][0]:.2f}  slope={s1['ema_slope']}  (10 bars ago: {s1['ema_recent'][1]:.2f})")
        print(f"  Last 3 MACD  : DIF={dif.iloc[-1]:.2f}  DEA={dea.iloc[-1]:.2f}  hist={bar.iloc[-1]:.2f}")
        print(f"  Last 3 EMA13 : {ema13.iloc[-3]:.2f} -> {ema13.iloc[-2]:.2f} -> {ema13.iloc[-1]:.2f}")
        action = {
            "bullish": "only long allowed",
            "bearish": "only short allowed",
            "neutral": "no trades -- wait for direction",
        }[s1["trend"]]
        print(f"  Action       : {action}")

        # Screen 2: 5min Force Index pullback
        k5 = klines_map["5min"]
        s2 = determine_screen2_signal(s1["trend"], k5)
        fi = calc_force_index(k5, ema_span=2)

        print(f"\n--- Screen 2 (5min) Oscillator Pullback Signal ---")
        print(f"  Signal       : {s2['signal']}")
        print(f"  FI EMA(2)    : {s2['fi_value']:.0f}  ({'above zero' if s2['fi_above_zero'] else 'below zero'})")
        print(f"  Last 5 FI    : {' -> '.join(f'{v:.0f}' for v in s2['fi_recent'])}")
        print(f"  Zero cross   : {s2['zero_cross']}")
        print(f"  Divergence   : {s2['divergence']}")
        print(f"  Description  : {s2['pullback_desc']}")

        # Screen 3: 1min precise entry
        k1 = klines_map["1min"]
        s3 = determine_screen3_entry(s1["trend"], s2["signal"], k1)

        print(f"\n--- Screen 3 (1min) Precise Entry ---")
        print(f"  Signal       : {s3['signal']}")
        if s3["signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
            print(f"  Entry price  : {s3['entry_price']:.0f}")
            print(f"  Stop loss    : {s3['stop_loss']:.0f}")
            print(f"  Prev bar     : H={s3['prev_high']:.0f}  L={s3['prev_low']:.0f}")
        print(f"  Description  : {s3['desc']}")

        # ── Step 5: Recent bar summary ──
        print(f"\n--- Recent 25min bars (last 5) ---")
        print(f"  {'time':<16} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>10}")
        for _, row in k25.iloc[-5:].iterrows():
            t = fmt_time(row["datetime"])
            print(f"  {t:<16} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>10}")

        print(f"\n--- Recent 5min bars (last 5) ---")
        print(f"  {'time':<16} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>10}")
        for _, row in k5.iloc[-5:].iterrows():
            t = fmt_time(row["datetime"])
            print(f"  {t:<16} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>10}")

        print(f"\n--- Recent 1min bars (last 5) ---")
        print(f"  {'time':<16} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>10}")
        for _, row in k1.iloc[-5:].iterrows():
            t = fmt_time(row["datetime"])
            print(f"  {t:<16} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
                  f"{row['close']:>8.0f} {int(row['volume']):>10}")

        print(f"\n{'='*72}")
        print(f"  Analysis complete for {SYMBOL}")
        print(f"{'='*72}")

    finally:
        api.close()


if __name__ == "__main__":
    main()
