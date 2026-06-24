"""
伪代码策略: Elder 三重滤网 + 动力系统（红绿灯）结合
====================================================

基于用户提供的伪代码:
  Screen 1 (周线): MACD 柱斜率 → 潮汐方向
  Screen 2 (日线): Force Index 回调 + 动力系统滤波
  Screen 3      : 挂日内突破单

核心逻辑:
  多头潮汐 (周线 MACD hist↑) + 日线 FI<0 (超卖回踩) + 动力≠红灯 → 买
  空头潮汐 (周线 MACD hist↓) + 日线 FI>0 (超买反弹) + 动力≠绿灯 → 卖

动力系统 (Impulse System):
  GREEN: 日线 EMA↑ 且 MACD↑ → 严禁做空
  RED  : 日线 EMA↓ 且 MACD↓ → 严禁做多
  BLUE : 多空不限

用法:
    from egg_futures_strategies import compute_pseudocode_signals
    signals = compute_pseudocode_signals(k_weekly, k_daily)
"""

import numpy as np
import pandas as pd

from egg_futures_1min import calc_macd, calc_ema, calc_force_index


# ── 参数配置 ──────────────────────────────────────────────
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
EMA_SPAN    = 13       # 日线 EMA 参数
EMA_LOOKBACK = 10      # EMA 斜率回看 bar 数
FI_EMA_SPAN = 2        # Force Index EMA 平滑


def _macd_hist_slope_at(bar_series, idx):
    """Bar-to-bar MACD 柱斜率 (当前 vs 前一根)"""
    if idx < 40:
        return 0.0
    hist = bar_series.iloc[:idx+1]
    valid = hist.dropna()
    if len(valid) < 2:
        return 0.0
    return float(valid.iloc[-1] - valid.iloc[-2])


def _ema_slope_at(ema_series, idx, lookback=EMA_LOOKBACK):
    """EMA 斜率: 当前 vs lookback 根前"""
    if idx < lookback + 1:
        return 0.0
    ema = ema_series.iloc[:idx+1]
    valid = ema.dropna()
    if len(valid) < lookback + 1:
        return 0.0
    return float(valid.iloc[-1] - valid.iloc[-lookback-1])


def compute_pseudocode_signals(k_weekly, k_daily):
    """
    根据伪代码计算每日信号。

    Args:
        k_weekly: 周线 DataFrame (columns: close, volume, datetime)
        k_daily:  日线 DataFrame

    Returns:
        list of str: 长度 = len(k_daily), 每个元素为:
            "BUY_STOP" / "SELL_STOP" / "WAIT"
    """
    n_d = len(k_daily)

    # ── 预计算周线 MACD ──
    close_w = k_weekly["close"].values
    _, _, _, _, hist_w = calc_macd(k_weekly, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)

    # ── 预计算日线指标 ──
    _, _, _, _, hist_d = calc_macd(k_daily, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    ema_d = calc_ema(k_daily, span=EMA_SPAN)
    fi_d  = calc_force_index(k_daily, ema_span=FI_EMA_SPAN)

    # ── 日线 bar 到周线 bar 映射 ──
    # daily_bar[i] 属于哪根周线 bar
    week_idx_for_day = np.full(n_d, -1, dtype=int)
    for i in range(n_d):
        dt_d = k_daily.iloc[i]["datetime"]
        if dt_d <= 0:
            continue
        for w in range(len(k_weekly)):
            dt_w = k_weekly.iloc[w]["datetime"]
            if dt_w <= 0:
                continue
            # 该周线 bar 涵盖 [dt_w, dt_w + 1 week)
            dt_w_end = k_weekly.iloc[w+1]["datetime"] if w+1 < len(k_weekly) else dt_w + 604800 * 1e9
            if dt_w <= dt_d < dt_w_end:
                week_idx_for_day[i] = w
                break

    signals = ["WAIT"] * n_d

    for i in range(n_d):
        # 日线指标值
        hist_slope_d = _macd_hist_slope_at(hist_d, i)
        ema_slope_d  = _ema_slope_at(ema_d, i, lookback=EMA_LOOKBACK)
        fi_val = fi_d.iloc[i] if i < len(fi_d) and not pd.isna(fi_d.iloc[i]) else 0.0

        # 动力系统
        if ema_slope_d > 0 and hist_slope_d > 0:
            impulse = "GREEN"
        elif ema_slope_d < 0 and hist_slope_d < 0:
            impulse = "RED"
        else:
            impulse = "BLUE"

        # 周线趋势：用上一根已确认的周线 bar
        w_idx = week_idx_for_day[i]
        if w_idx < 0:
            continue
        # 使用 前一周 的趋势 (避免 look-ahead — 当周日线在同周结束前不应使用当周柱)
        # 但在回测中, 我们用 w_idx-1 的趋势（上周已确认的趋势）
        # 实际上伪代码用的是"当前"周线趋势，但在实盘中只有上周的才已确认
        # 这里用 w_idx-1 的趋势（保守），w_idx 是包含当天的周
        trend_w = w_idx - 1
        if trend_w < 1:
            continue

        hist_slope_w = _macd_hist_slope_at(hist_w, trend_w)
        is_bull_tide = hist_slope_w > 0
        is_bear_tide = hist_slope_w < 0

        if is_bull_tide and fi_val < 0 and impulse != "RED":
            signals[i] = "BUY_STOP"
        elif is_bear_tide and fi_val > 0 and impulse != "GREEN":
            signals[i] = "SELL_STOP"

    return signals
