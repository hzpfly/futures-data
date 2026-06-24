"""
探针: 棉花 CF609 B_短线 三重滤网详细数据
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from datetime import datetime
from tqsdk import TqApi, TqAuth
from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    calc_force_index, calc_macd,
    determine_screen1_trend, determine_screen2_signal, determine_screen3_entry,
)

username, password = get_tqsdk_auth()
print(f"连接 TqSdk...")
api = TqApi(auth=TqAuth(username, password))

symbol = "CZCE.CF609"
from triple_screen_monitor import TRIPLE_SETS, PERIOD_DUR
set_b = TRIPLE_SETS[1]  # B_短线: 小时/15min/3min
periods = [set_b["screen1_period"], set_b["screen2_period"], set_b["screen3_period"]]
period_labels = ["小时", "15分钟", "3分钟"]

print(f"\n{'='*70}")
print(f"  棉花 CF609  B_短线 三重滤网数据  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}")

# 获取 K 线数据
klines = {}
for period, label in zip(periods, period_labels):
    dur = PERIOD_DUR[period]
    k = api.get_kline_serial(symbol, dur, 200)
    klines[label] = pd.DataFrame({
        "open":  k["open"],
        "high":  k["high"],
        "low":   k["low"],
        "close": k["close"],
        "volume": k["volume"],
    })

api.close()

# ── Screen 1: 小时线趋势 ──
k1 = klines["小时"]
s1 = determine_screen1_trend(k1)
_, _, dif1, dea1, bar1 = calc_macd(k1)
ema13_1 = k1["close"].ewm(span=13, adjust=False).mean()

print(f"\n{'─'*70}")
print(f"  Screen 1: 小时线趋势判断")
print(f"{'─'*70}")
print(f"  最近收盘: {k1['close'].iloc[-1]:.0f}")
print(f"  EMA13:    {ema13_1.iloc[-1]:.0f}")
print(f"  最新 5 根 K 线收盘:")
for i in range(-5, 0):
    print(f"    [{i+5+1}] close={k1['close'].iloc[i]:.0f}  EMA13={ema13_1.iloc[i]:.0f}  volume={k1['volume'].iloc[i]:.0f}")
print(f"  MACD: DIF={dif1.iloc[-1]:.2f}, DEA={dea1.iloc[-1]:.2f}, BAR={bar1.iloc[-1]:.2f}")
print(f"  近 5 根 MACD BAR: {[f'{bar1.iloc[i]:.2f}' for i in range(-5, 0)]}")
print(f"  EMA13 斜率: {s1['ema_slope']}")
print(f"  MACD HIST 斜率: {s1['hist_slope']}")
print(f"  → 趋势判断: {s1['trend']}")
print(f"  → 解读: {'多头 (只做多)' if s1['trend']=='bullish' else '空头 (只做空)' if s1['trend']=='bearish' else '中性 (观望)'}")

# ── Screen 2: 15分钟 FI + 价格确认 ──
k2 = klines["15分钟"]
s2 = determine_screen2_signal(s1["trend"], k2)
fi2 = calc_force_index(k2, ema_span=2)
ema5_2 = k2["close"].ewm(span=5, adjust=False).mean()

print(f"\n{'─'*70}")
print(f"  Screen 2: 15分钟 Force Index + 价格确认")
print(f"{'─'*70}")
print(f"  最新收盘: {k2['close'].iloc[-1]:.0f}")
print(f"  EMA5:     {ema5_2.iloc[-1]:.0f}")
print(f"  close vs EMA5: {'close < EMA5 (价格回抽确认)' if k2['close'].iloc[-1] < ema5_2.iloc[-1] else 'close >= EMA5 (价格未回抽)'}")
print(f"  近 5 根 FI(EMA2): {[f'{fi2.iloc[i]:.0f}' for i in range(-5, 0)]}")
print(f"  最新 FI: {s2['fi_value']}")
print(f"  FI 在零轴: {'上方' if s2['fi_above_zero'] else '下方'}")
print(f"  零轴穿越: {s2['zero_cross']}")
print(f"  FI 背离:  {s2['divergence']}")
print(f"  价格确认: {'✅ 通过' if s2['price_confirmed'] else '❌ 未通过'}")
print(f"  → 信号:   {s2['signal']}")
print(f"  → 说明:   {s2['pullback_desc']}")

# Screen 2 近 10 根 bar 详细数据
print(f"\n  近 10 根 15min K 线 + FI 详情:")
print(f"  {'idx':<5} {'close':>7} {'volume':>8} {'FI_raw':>8} {'FI_ema2':>8} {'EMA5':>7} {'close<EMA5'}")
raw_fi2 = (k2['close'].diff() * k2['volume']) / 1000000
for i in range(-10, 0):
    raw = raw_fi2.iloc[i]
    fi_v = fi2.iloc[i]
    ema_v = ema5_2.iloc[i]
    lo = k2['close'].iloc[i]
    check = "✅" if lo < ema_v else "  "
    print(f"  {i+10+1:<5} {lo:>7.0f} {k2['volume'].iloc[i]:>8.0f} {raw:>8.1f} {fi_v:>8.1f} {ema_v:>7.0f} {check}")

# ── Screen 3: 3分钟入场 ──
k3 = klines["3分钟"]
s3 = determine_screen3_entry(s1["trend"], s2["signal"], k3, tick_size=5)

print(f"\n{'─'*70}")
print(f"  Screen 3: 3分钟入场判断")
print(f"{'─'*70}")
print(f"  入场类型:  {'做多 (买入止损单)' if 'long' in s3['signal'] else '做空 (卖出止损单)' if 'short' in s3['signal'] else '无信号'}")
if s3.get("entry_price"):
    print(f"  入场价格:  {s3['entry_price']:.0f}")
if s3.get("stop_loss"):
    print(f"  止损价格:  {s3['stop_loss']:.0f}")
    if s3.get("entry_price") and s3.get("stop_loss"):
        risk = abs(s3["entry_price"] - s3["stop_loss"])
        print(f"  风险金额:  {risk:.0f} 点")
print(f"  前一根 bar 高: {s3.get('prev_high', 0):.0f}  低: {s3.get('prev_low', 0):.0f}")
print(f"  → 状态:    {s3['signal']}")
print(f"  → 说明:    {s3.get('desc', '')}")

# 近 10 根 3min K 线
print(f"\n  近 10 根 3min K 线:")
print(f"  {'idx':<5} {'open':>7} {'high':>7} {'low':>7} {'close':>7} {'volume':>8}")
for i in range(-10, 0):
    r = k3.iloc[i]
    print(f"  {i+10+1:<5} {r['open']:>7.0f} {r['high']:>7.0f} {r['low']:>7.0f} {r['close']:>7.0f} {r['volume']:>8.0f}")

# ── 汇总 ──
print(f"\n{'='*70}")
print(f"  三级滤网汇总")
print(f"{'='*70}")
print(f"  Screen 1 (小时):  趋势 = {s1['trend']} → {'多头 (只做多)' if s1['trend']=='bullish' else '空头 (只做空)' if s1['trend']=='bearish' else '中性'}")
print(f"  Screen 2 (15min): 信号 = {s2['signal']} (FI={s2['fi_value']}, 价格确认={'✅' if s2['price_confirmed'] else '❌'})")
print(f"  Screen 3 (3min):  状态 = {s3['signal']}")
if s3.get("entry_price") and s3.get("stop_loss"):
    print(f"                   入场 {s3['entry_price']:.0f} / 止损 {s3['stop_loss']:.0f}")
print(f"{'='*70}")
