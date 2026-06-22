"""
Test exit rules and stop-loss tracking with historical data.

Walks through historical klines chronologically, simulates the full trade lifecycle:
  entry (Screen 3 triggered) → trailing stop updates → exit (one of 4 rules)

Exit rules (priority order):
  1. stop_hit           : 1min low/high touches current_stop
  2. s1_reversal        : Screen 1 trend no longer favors position
  3. opposite_divergence: Screen 2 shows divergence against position
  4. trailing           : update current_stop using 2-bar low/high (ratchet only)

Prints trade log, exit reason distribution, summary stats, and invariant checks.

Usage:
    python test_exit_rules.py
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
    update_position,
    Position,
    discover_main_contract,
    KLINE_DURS,
)


DATA_LEN   = 2000
WARMUP_25  = 50


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"


def fetch_historical_data(api, symbol):
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


def backtest(klines_map):
    """
    Walk historical bars chronologically, simulating the full entry→exit lifecycle.
    Returns (trades, events, stats).
    """
    klines_25 = klines_map["25min"]
    klines_5  = klines_map["5min"]
    klines_1  = klines_map["1min"]
    n_25 = len(klines_25)
    n_5  = len(klines_5)

    position = None
    events = []          # full event log
    trades = []          # closed trade records
    invariant_violations = {
        "entry_when_s1_neutral": 0,
        "trailing_unfavorable": 0,
    }

    # ── Pre-compute Screen 1 for every 25min bar ──
    print(f"\nPre-computing Screen 1 for 25min bars {WARMUP_25}..{n_25-1}...")
    s1_cache = {}
    for i in range(WARMUP_25, n_25):
        s1_cache[i] = determine_screen1_trend(klines_25.iloc[:i+1])

    print(f"Scanning 5min/1min bars chronologically...")
    for i in range(WARMUP_25, n_25):
        s1_trend = s1_cache[i]["trend"]
        t_start_25 = klines_25.iloc[i]["datetime"]
        t_end_25   = klines_25.iloc[i+1]["datetime"] if i+1 < n_25 else t_start_25 + 1500 * 1e9

        # 5min bars within this 25min window
        mask_5 = (klines_5["datetime"] >= t_start_25) & (klines_5["datetime"] < t_end_25)
        idx_5_list = klines_5.index[mask_5].tolist()

        for j_pos in idx_5_list:
            slice_5 = klines_5.iloc[:j_pos+1]
            s2 = determine_screen2_signal(s1_trend, slice_5)
            s2_signal = s2["signal"]

            t_start_5 = klines_5.iloc[j_pos]["datetime"]
            t_end_5   = klines_5.iloc[j_pos+1]["datetime"] if j_pos+1 < n_5 else t_start_5 + 300 * 1e9

            # 1min bars within this 5min window
            mask_1 = (klines_1["datetime"] >= t_start_5) & (klines_1["datetime"] < t_end_5)
            idx_1_list = klines_1.index[mask_1].tolist()

            for k_pos in idx_1_list:
                slice_1 = klines_1.iloc[:k_pos+1]
                prev_stop = position.current_stop if (position and position.status == "open") else None
                prev_direction = position.direction if (position and position.status == "open") else None
                was_open = position is not None and position.status == "open"

                new_pos = update_position(position, s1_trend, s2_signal, slice_1, events)

                # ── Invariant: never open a position when S1 is neutral ──
                if was_open is False and new_pos is not None and new_pos.status == "open":
                    if s1_trend == "neutral":
                        invariant_violations["entry_when_s1_neutral"] += 1

                # ── Invariant: trailing stop must only move favorably ──
                if (was_open and new_pos and new_pos.status == "open"
                        and prev_stop is not None and prev_direction is not None
                        and new_pos.current_stop != prev_stop):
                    if prev_direction == "long" and new_pos.current_stop < prev_stop:
                        invariant_violations["trailing_unfavorable"] += 1
                    elif prev_direction == "short" and new_pos.current_stop > prev_stop:
                        invariant_violations["trailing_unfavorable"] += 1

                # ── Detect trade closure (position was open, now closed) ──
                if was_open and new_pos is not None and new_pos.status == "closed":
                    trades.append({
                        "direction": new_pos.direction,
                        "entry_time": new_pos.entry_time,
                        "entry_price": new_pos.entry_price,
                        "exit_time": new_pos.exit_time,
                        "exit_price": new_pos.exit_price,
                        "exit_reason": new_pos.exit_reason,
                        "bars_held": new_pos.bars_held,
                        "pnl": new_pos.realized_pnl(),
                        "peak_profit": new_pos.peak_profit,
                    })

                position = new_pos

    # ── Close any still-open position at end of data ──
    if position is not None and position.status == "open":
        last_1min = klines_1.iloc[-1]
        position.status = "closed"
        position.exit_price = last_1min["close"]
        position.exit_time = last_1min["datetime"]
        position.exit_reason = "end_of_data"
        trades.append({
            "direction": position.direction,
            "entry_time": position.entry_time,
            "entry_price": position.entry_price,
            "exit_time": position.exit_time,
            "exit_price": position.exit_price,
            "exit_reason": position.exit_reason,
            "bars_held": position.bars_held,
            "pnl": position.realized_pnl(),
            "peak_profit": position.peak_profit,
        })

    return trades, events, invariant_violations


def print_results(trades, events, invariant_violations):
    print(f"\n{'='*72}")
    print(f"  Exit Rules Backtest -- Results")
    print(f"{'='*72}")

    # ── Exit reason distribution ──
    reason_counts = {}
    for t in trades:
        reason = t["exit_reason"].split("(")[0]  # strip s1_reversal(trend) suffix
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    print(f"\n-- Trades: {len(trades)} total --")
    print(f"  Exit reason distribution:")
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"    {reason:25s}: {count:4d}")

    # ── Summary stats ──
    closed_for_stats = [t for t in trades if t["exit_reason"] != "end_of_data"]
    pnls = [t["pnl"] for t in closed_for_stats]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    print(f"\n-- Summary stats (excluding end_of_data closures) --")
    print(f"  Total closed trades: {len(closed_for_stats)}")
    if pnls:
        print(f"  Win rate:    {len(wins)}/{len(pnls)} = {100*len(wins)/len(pnls):.1f}%")
        print(f"  Avg PnL:     {sum(pnls)/len(pnls):+.1f} points")
        print(f"  Total PnL:   {sum(pnls):+.1f} points")
        print(f"  Max win:     {max(pnls):+.1f}")
        print(f"  Max loss:    {min(pnls):+.1f}")
        if wins and losses:
            print(f"  Avg win:     {sum(wins)/len(wins):+.1f}  |  Avg loss: {sum(losses)/len(losses):+.1f}")
            if sum(losses) != 0:
                print(f"  Risk/reward: {abs(sum(wins)/len(wins) / (sum(losses)/len(losses))):.2f}")
        print(f"  Avg bars held: {sum(t['bars_held'] for t in closed_for_stats)/len(closed_for_stats):.1f}")

    # ── Trade log (first 10 + last 5) ──
    print(f"\n-- Trade log (first 10) --")
    print(f"  {'#':>3}  {'方向':<6}  {'入场时间':<16}  {'入场':>7}  {'平仓时间':<16}  {'平仓':>7}  "
          f"{'原因':<22}  {'bars':>4}  {'PnL':>7}")
    print("  " + "-" * 110)
    for i, t in enumerate(trades[:10]):
        dir_cn = "多头" if t["direction"] == "long" else "空头"
        print(f"  {i+1:>3}  {dir_cn:<6}  {fmt_time(t['entry_time']):<16}  {t['entry_price']:>7.0f}  "
              f"{fmt_time(t['exit_time']):<16}  {t['exit_price']:>7.0f}  "
              f"{t['exit_reason']:<22}  {t['bars_held']:>4}  {t['pnl']:>+7.0f}")

    if len(trades) > 10:
        print(f"\n  ... ({len(trades) - 15} more) ...")
        print(f"\n-- Trade log (last 5) --")
        for i, t in enumerate(trades[-5:], start=len(trades)-4):
            dir_cn = "多头" if t["direction"] == "long" else "空头"
            print(f"  {i:>3}  {dir_cn:<6}  {fmt_time(t['entry_time']):<16}  {t['entry_price']:>7.0f}  "
                  f"{fmt_time(t['exit_time']):<16}  {t['exit_price']:>7.0f}  "
                  f"{t['exit_reason']:<22}  {t['bars_held']:>4}  {t['pnl']:>+7.0f}")

    # ── Detailed example trade ──
    if trades:
        print(f"\n{'='*72}")
        print(f"  Detailed Trade Example (first trade)")
        print(f"{'='*72}")
        t = trades[0]
        dir_cn = "多头" if t["direction"] == "long" else "空头"
        print(f"  方向:       {dir_cn}")
        print(f"  入场:       {fmt_time(t['entry_time'])}  @ {t['entry_price']:.0f}")
        print(f"  平仓:       {fmt_time(t['exit_time'])}  @ {t['exit_price']:.0f}")
        print(f"  退出原因:   {t['exit_reason']}")
        print(f"  持仓 bar 数: {t['bars_held']}")
        print(f"  已实现 PnL: {t['pnl']:+.0f} 点")
        print(f"  最大有利偏移: {t['peak_profit']:.0f} 点")

    # ── Invariant checks ──
    print(f"\n-- Invariant checks --")
    print(f"  Entry opened when Screen 1 was neutral:  {invariant_violations['entry_when_s1_neutral']} violations")
    print(f"  Trailing stop moved unfavorably:         {invariant_violations['trailing_unfavorable']} violations")

    # ── Verdict ──
    print(f"\n{'='*72}")
    if not trades:
        verdict = "NO TRADES -- no entries triggered in historical window"
    elif len(closed_for_stats) == 0:
        verdict = "PARTIAL -- only end_of_data closures (no exits fired)"
    else:
        verdict = f"PASSED -- {len(closed_for_stats)} full trade cycles"

    total_violations = sum(invariant_violations.values())
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

        trades, events, invariant_violations = backtest(klines_map)
        print_results(trades, events, invariant_violations)

    finally:
        api.close()


if __name__ == "__main__":
    main()
