"""
全品种全策略综合回测
====================
测试所有策略在所有监控品种上的表现:
  - Set A (周→日→时): Elder Triple Screen 长线
  - Set C (日→时→15m): Elder Triple Screen 中线
  - 伪代码 (周→日+动力): 三重滤网 + 动力系统红绿灯

用法:
    # 实时下载 + 回测
    python scripts/backtest_all_contracts.py

    # 保存数据到 CSV（稍后离线回测用）
    python scripts/backtest_all_contracts.py --dump-data

    # 从保存的 CSV 离线回测（无需 TqSdk 连接）
    python scripts/backtest_all_contracts.py --from-csv data

输出:
    每个品种×策略的 P&L 明细, 以及跨品种汇总对比表
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
import time as _time
import numpy as np
import pandas as pd
from collections import Counter

try:
    from tqsdk import TqApi, TqAuth
    HAS_TQSDK = True
except ImportError:
    HAS_TQSDK = False

from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    calc_macd, calc_ema, calc_force_index,
    determine_screen1_trend,
    determine_screen2_signal,
    determine_screen3_entry,
    Position,
)
from egg_futures_strategies import compute_pseudocode_signals


# ══════════════════════════════════════════════════════════
# 合约配置
# ══════════════════════════════════════════════════════════
CONTRACTS = [
    {"code": "CF",  "exchange": "CZCE", "product": "CF", "name": "棉花",
     "tick": 5.0, "multiplier": 5,  "symbol": "CZCE.CF609"},
    {"code": "JD",  "exchange": "DCE",  "product": "jd", "name": "鸡蛋",
     "tick": 1.0, "multiplier": 10, "symbol": "DCE.jd2509"},
    {"code": "LH",  "exchange": "DCE",  "product": "lh", "name": "生猪",
     "tick": 5.0, "multiplier": 16, "symbol": "DCE.lh2509"},
    {"code": "CJ",  "exchange": "CZCE", "product": "CJ", "name": "红枣",
     "tick": 5.0, "multiplier": 5,  "symbol": "CZCE.CJ509"},
    {"code": "C",   "exchange": "DCE",  "product": "c",  "name": "玉米",
     "tick": 1.0, "multiplier": 10, "symbol": "DCE.c2509"},
]

KLINE_DURS = {
    "1week": 604800,
    "1day":  86400,
    "1hour": 3600,
    "15min": 900,
}

DATA_LEN = 8964  # tqsdk 免费账户上限
STOP_ATR_MULT = 2.0   # 止损 = N × ATR(20)
ATR_PERIOD = 20
TRAILING_R = 1.0      # 盈利达到 1R 后移动止损到保本

# ── Set A 周线专用参数 ──
S1_WEEKLY_HIST = 2
S1_WEEKLY_EMA  = 4
S1_WEEKLY_EMA_THRESH = 0.0002


# ══════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════
def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"

def fmt_day(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")
    return "---"

def calc_atr(klines, period=ATR_PERIOD):
    """Simple True Range → EMA(period)"""
    high, low, close = klines["high"], klines["low"], klines["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ══════════════════════════════════════════════════════════
# Trade & Stats (same as original)
# ══════════════════════════════════════════════════════════
class Trade:
    __slots__ = (
        "direction", "entry_time", "entry_price", "entry_signal",
        "exit_time", "exit_price", "exit_reason",
        "pnl_points", "pnl_rmb", "bars_held",
        "initial_stop", "stop_risk_points",
        "mfe_points", "mae_points",
    )
    def __init__(self, pos, exit_price, exit_time, exit_reason, bars_held, multiplier):
        self.direction   = pos.direction
        self.entry_time  = pos.entry_time
        self.entry_price = pos.entry_price
        self.entry_signal = getattr(pos, "entry_signal", "")
        self.exit_time   = exit_time
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        self.bars_held   = bars_held
        self.initial_stop = pos.initial_stop
        self.mfe_points  = pos.peak_profit
        self.mae_points  = getattr(pos, "max_adverse", 0.0)
        if self.direction == "long":
            self.pnl_points = exit_price - pos.entry_price
            self.stop_risk_points = pos.entry_price - pos.initial_stop
        else:
            self.pnl_points = pos.entry_price - exit_price
            self.stop_risk_points = pos.initial_stop - pos.entry_price
        self.pnl_rmb = self.pnl_points * multiplier


class BacktestStats:
    def __init__(self, multiplier=1):
        self.trades = []
        self._equity_curve = []
        self.multiplier = multiplier

    def add_trade(self, trade):
        self.trades.append(trade)
        cum = sum(t.pnl_points for t in self.trades)
        self._equity_curve.append((trade.exit_time, cum))

    @property
    def total_trades(self):
        return len(self.trades)

    @property
    def wins(self):
        return [t for t in self.trades if t.pnl_points > 0]

    @property
    def losses(self):
        return [t for t in self.trades if t.pnl_points <= 0]

    win_rate  = property(lambda s: len(s.wins) / max(1, s.total_trades))
    avg_win   = property(lambda s: sum(t.pnl_points for t in s.wins) / max(1, len(s.wins)))
    avg_loss  = property(lambda s: sum(t.pnl_points for t in s.losses) / max(1, len(s.losses)))
    total_pnl_points = property(lambda s: sum(t.pnl_points for t in s.trades))
    total_pnl_rmb    = property(lambda s: s.total_pnl_points * s.multiplier)

    @property
    def profit_factor(self):
        gp = sum(t.pnl_points for t in self.wins)
        gl = abs(sum(t.pnl_points for t in self.losses))
        return gp / max(1, gl)

    @property
    def max_drawdown(self):
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0][1]
        worst = 0.0
        for _, cum in self._equity_curve:
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
        return worst

    def to_dict(self):
        return {
            "trades": self.total_trades,
            "win_rate": f"{self.win_rate:.1%}",
            "profit_factor": f"{self.profit_factor:.2f}",
            "total_pnl_pts": f"{self.total_pnl_points:+.0f}",
            "total_pnl_rmb": f"{self.total_pnl_rmb:+,.0f}",
            "avg_win": f"{self.avg_win:.1f}",
            "avg_loss": f"{self.avg_loss:.1f}",
            "max_dd": f"{self.max_drawdown:.0f}",
        }


# ══════════════════════════════════════════════════════════
# Simple Position (for pseudocode strategy)
# ══════════════════════════════════════════════════════════
class SimplePosition:
    def __init__(self, direction, entry_price, entry_time, initial_stop, entry_signal=""):
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.initial_stop = initial_stop
        self.current_stop = initial_stop
        self.entry_signal = entry_signal
        self.peak_profit = 0.0
        self.max_adverse = 0.0
        self.bars_held = 0


# ══════════════════════════════════════════════════════════
# 数据加载 (TqSdk / CSV)
# ══════════════════════════════════════════════════════════

def _short_name(symbol):
    """CZCE.CF609 → CF609"""
    return symbol.split(".")[-1]

def load_klines_tqsdk(api, symbol, periods=None):
    """从 TqSdk 加载 K 线, 分批等待数据到达"""
    if periods is None:
        periods = list(KLINE_DURS.keys())
    klines = {}
    for p in periods:
        klines[p] = api.get_kline_serial(symbol, KLINE_DURS[p], data_length=DATA_LEN)

    deadline = _time.time() + 120
    reported = False
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time() + 1)
        all_ok = True
        for p in periods:
            k = klines[p]
            if len(k) < 100 or k.iloc[-1]["close"] <= 0:
                all_ok = False
                break
        if all_ok:
            break
        if not reported and _time.time() - (deadline - 120) > 5:
            for p in periods:
                k = klines[p]
                print(f"    等待 {p}: {len(k)} bars...")
            reported = True

    for p, k in klines.items():
        n = len(k)
        t0 = fmt_day(k.iloc[0]["datetime"]) if n > 0 else "---"
        t1 = fmt_day(k.iloc[-1]["datetime"]) if n > 0 else "---"
        print(f"    {p:6s}: {n:5d} bars  {t0} .. {t1}")
    return klines


def save_klines_csv(klines, symbol, csv_dir):
    """把 klines dict 存为 CSV 文件"""
    os.makedirs(csv_dir, exist_ok=True)
    sn = _short_name(symbol)
    for p, k in klines.items():
        fname = f"{sn}_{p}.csv"
        fpath = os.path.join(csv_dir, fname)
        # 保存全部 bar（含零值），保持与 tqsdk 订阅完全一致
        out = k[["datetime", "open", "high", "low", "close", "volume"]].copy()
        out.to_csv(fpath, index=False)
        valid = k[k["close"] > 0]
        print(f"    已保存: {fname} ({len(out)} bars, 有效 {len(valid)})")


def load_klines_csv(symbol, csv_dir, periods=None):
    """从 CSV 文件加载 K 线, 返回 {period: DataFrame}"""
    if periods is None:
        periods = list(KLINE_DURS.keys())
    sn = _short_name(symbol)
    klines = {}
    for p in periods:
        fpath = os.path.join(csv_dir, f"{sn}_{p}.csv")
        if not os.path.exists(fpath):
            print(f"    ⚠️ 未找到: {fpath}")
            continue
        df = pd.read_csv(fpath)
        # tqsdk 的 datetime 是 int64 纳秒, CSV 存为 int
        if "datetime" in df.columns:
            df["datetime"] = df["datetime"].astype("int64")
        klines[p] = df
        n = len(df)
        valid_n = len(df[df["close"] > 0])
        t0 = fmt_day(df.iloc[0]["datetime"]) if n > 0 else "---"
        t1 = fmt_day(df.iloc[-1]["datetime"]) if n > 0 else "---"
        print(f"    {p:6s}: {n:5d} bars (有效 {valid_n})  {t0} .. {t1}")
    return klines


def update_klines_csv(api, symbol, csv_dir, periods=None):
    """
    增量更新 CSV 文件:
      - 若 CSV 不存在: 全量下载并保存
      - 若 CSV 已存在: 下载最新数据, 合并 (保留旧历史 + 追加新 bar + 更新已有 bar)
    返回 {period: DataFrame} (合并后的最新数据)
    """
    if periods is None:
        periods = list(KLINE_DURS.keys())
    os.makedirs(csv_dir, exist_ok=True)
    sn = _short_name(symbol)

    # 1) 订阅 tqsdk 实时数据
    print(f"  [{sn}] 订阅 K 线...")
    subs = {}
    for p in periods:
        subs[p] = api.get_kline_serial(symbol, KLINE_DURS[p], data_length=DATA_LEN)

    # 2) 等待数据到达
    deadline = _time.time() + 120
    reported = False
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time() + 1)
        all_ok = True
        for p in periods:
            k = subs[p]
            if len(k) < 100 or k.iloc[-1]["close"] <= 0:
                all_ok = False
                break
        if all_ok:
            break
        if not reported and _time.time() - (deadline - 120) > 5:
            for p in periods:
                k = subs[p]
                print(f"    等待 {p}: {len(k)} bars...")
            reported = True

    # 3) 对每个周期做增量合并
    result = {}
    for p in periods:
        fname = f"{sn}_{p}.csv"
        fpath = os.path.join(csv_dir, fname)
        new_df = subs[p].copy()

        if os.path.exists(fpath):
            # ── 增量更新 ──
            old_df = pd.read_csv(fpath)
            if "datetime" in old_df.columns:
                old_df["datetime"] = old_df["datetime"].astype("int64")

            # 以 datetime 为 key 做 full outer join
            # 优先用 new_df 的值 (tqsdk 数据更新)
            merged = _merge_klines(old_df, new_df)
            n_new = len(merged) - len(old_df[~old_df["datetime"].isin(new_df["datetime"])])

            # 只保留合并且非零的 bar 数变化
            old_valid = len(old_df[old_df["close"] > 0])
            new_valid = len(merged[merged["close"] > 0])
            added = new_valid - old_valid

            merged.to_csv(fpath, index=False)
            t0 = fmt_day(merged.iloc[0]["datetime"]) if len(merged) > 0 else "---"
            t1 = fmt_day(merged.iloc[-1]["datetime"]) if len(merged) > 0 else "---"
            if added > 0:
                print(f"    {p:6s}: 更新 +{added} 新 bar  ({old_valid}→{new_valid} 有效)  {t0} .. {t1}")
            else:
                print(f"    {p:6s}: 已是最新 (有效 {new_valid})  {t0} .. {t1}")
            result[p] = merged
        else:
            # ── 首次下载 ──
            out = new_df.copy()
            out.to_csv(fpath, index=False)
            valid = len(new_df[new_df["close"] > 0])
            t0 = fmt_day(new_df.iloc[0]["datetime"]) if len(new_df) > 0 else "---"
            t1 = fmt_day(new_df.iloc[-1]["datetime"]) if len(new_df) > 0 else "---"
            print(f"    {p:6s}: 首次下载 {len(out)} bars (有效 {valid})  {t0} .. {t1}")
            result[p] = new_df

    return result


def _merge_klines(old_df, new_df):
    """
    合并新旧 K 线数据:
      - 以 datetime 为 key
      - new_df 中有而 old_df 中没有 → 追加
      - 两者都有 → 用 new_df 的值 (tqsdk 数据更准, 尤其是最新 bar)
      - old_df 中有而 new_df 中没有 → 保留 (更早期的历史)
    """
    # 确保 datetime 是 int64
    old_df = old_df.copy()
    new_df = new_df.copy()
    old_df["datetime"] = old_df["datetime"].astype("int64")
    new_df["datetime"] = new_df["datetime"].astype("int64")

    # 以 datetime 为索引合并
    old_idx = set(old_df["datetime"].values)
    new_idx = set(new_df["datetime"].values)

    # 只存在于 old 的 bar
    only_old = old_df[~old_df["datetime"].isin(new_idx)].copy()

    # new_df 的所有 bar (覆盖 + 新增)
    # 但只保留 new_df 中 close>0 或 old 中没有的 bar
    # 实际上直接取 new_df 全部即可, 因为 tqsdk 返回的是最新窗口
    merged = pd.concat([only_old, new_df], ignore_index=True)

    # 去重: 同一 datetime 有多行时, 保留 new_df 的那行
    # (new_df 在后, 所以 keep='last')
    merged = merged.drop_duplicates(subset=["datetime"], keep="last")

    # 按 datetime 排序
    merged = merged.sort_values("datetime").reset_index(drop=True)
    return merged


# ══════════════════════════════════════════════════════════
# 策略 1: 伪代码 (周线趋势 + 日线 FI + 动力系统)
# ══════════════════════════════════════════════════════════
def backtest_pseudocode(k_weekly, k_daily, multiplier, tick_size):
    """
    伪代码策略回测: 周线 MACD 柱斜率确定潮汐方向,
    日线 Force Index + 动力系统过滤, 日线 bar 级别入场.
    """
    n_d = len(k_daily)
    n_w = len(k_weekly)

    # 预计算信号
    print("    计算伪代码信号...")
    signals = compute_pseudocode_signals(k_weekly, k_daily)

    # ATR for stop loss
    atr = calc_atr(k_daily, ATR_PERIOD)

    # 日线 → 周线映射
    week_for_day = np.full(n_d, -1, dtype=int)
    for i in range(n_d):
        dt = k_daily.iloc[i]["datetime"]
        if dt <= 0:
            continue
        for w in range(n_w):
            dw = k_weekly.iloc[w]["datetime"]
            if dw <= 0:
                continue
            end = k_weekly.iloc[w+1]["datetime"] if w+1 < n_w else dw + 604800 * 1e9
            if dw <= dt < end:
                week_for_day[i] = w
                break

    stats = BacktestStats(multiplier)
    pos = None
    start_idx = 50  # warmup

    for i in range(start_idx, n_d):
        curr_bar = k_daily.iloc[i]
        curr_high = curr_bar["high"]
        curr_low  = curr_bar["low"]
        curr_close = curr_bar["close"]
        curr_time  = curr_bar["datetime"]

        if pos is not None:
            pos.bars_held += 1
            # MFE/MAE
            if pos.direction == "long":
                pos.peak_profit = max(pos.peak_profit, curr_high - pos.entry_price)
                pos.max_adverse = min(pos.max_adverse, curr_low - pos.entry_price)
                # Stop check
                if curr_low <= pos.current_stop:
                    stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                          "stop_hit", pos.bars_held, multiplier))
                    pos = None
                    continue
            else:
                pos.peak_profit = max(pos.peak_profit, pos.entry_price - curr_low)
                pos.max_adverse = min(pos.max_adverse, pos.entry_price - curr_high)
                if curr_high >= pos.current_stop:
                    stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                          "stop_hit", pos.bars_held, multiplier))
                    pos = None
                    continue

            # Trailing to breakeven
            if pos.peak_profit > 0:
                risk = abs(pos.entry_price - pos.initial_stop)
                if pos.peak_profit >= TRAILING_R * risk and pos.current_stop != pos.entry_price:
                    if pos.direction == "long":
                        pos.current_stop = max(pos.current_stop, pos.entry_price)
                    else:
                        pos.current_stop = min(pos.current_stop, pos.entry_price)

            # S1 reversal check
            w_now = week_for_day[i]
            if w_now > 0:
                _, _, _, _, hist_w = calc_macd(k_weekly, fast=12, slow=26, signal=9)
                hist_slope = _hist_slope(hist_w, w_now - 1)
                if (pos.direction == "long" and hist_slope <= 0) or \
                   (pos.direction == "short" and hist_slope >= 0):
                    exit_px = curr_close
                    stats.add_trade(Trade(pos, exit_px, curr_time,
                                          "s1_reversal", pos.bars_held, multiplier))
                    pos = None
                    continue

        # Entry
        if pos is None and signals[i] != "WAIT":
            entry_price = curr_bar["open"]
            atr_val = atr.iloc[i] if i < len(atr) and not pd.isna(atr.iloc[i]) else entry_price * 0.02
            if atr_val <= 0:
                atr_val = entry_price * 0.02

            if signals[i] == "BUY_STOP":
                stop = entry_price - STOP_ATR_MULT * atr_val
                pos = SimplePosition("long", entry_price, curr_time, stop)
            elif signals[i] == "SELL_STOP":
                stop = entry_price + STOP_ATR_MULT * atr_val
                pos = SimplePosition("short", entry_price, curr_time, stop)

    # Close any open position
    if pos is not None:
        last_bar = k_daily.iloc[-1]
        stats.add_trade(Trade(pos, last_bar["close"], last_bar["datetime"],
                              "end_of_data", pos.bars_held, multiplier))

    return stats


def _hist_slope(hist_series, idx):
    """Helper: MACD histogram bar-to-bar slope at index"""
    if idx < 1:
        return 0.0
    s = hist_series.iloc[:idx+1].dropna()
    if len(s) < 2:
        return 0.0
    return float(s.iloc[-1] - s.iloc[-2])


# ══════════════════════════════════════════════════════════
# 策略 2: Set A (周→日→时) 简化版
# ══════════════════════════════════════════════════════════
def backtest_set_a(k_weekly, k_daily, k_hourly, multiplier, tick_size):
    """
    Set A 回测: 周线趋势 → 日线回调 → 小时线入场.
    复用已有的 determine_screen1/2/3 函数.
    """
    n_w = len(k_weekly)
    n_d = len(k_daily)
    n_h = len(k_hourly)

    # 周线 S1 cache
    _, _, _, _, hist_w = calc_macd(k_weekly, fast=12, slow=26, signal=9)
    ema13_w = calc_ema(k_weekly, span=13)
    hist_drop = hist_w.dropna()
    ema_drop = ema13_w.dropna()
    first_w = max(
        hist_drop.index[0] if len(hist_drop) > 0 else n_w,
        ema_drop.index[0] if len(ema_drop) > 0 else n_w,
        50
    )

    s1_cache = {}
    for i in range(first_w, n_w):
        s1_cache[i] = determine_screen1_trend(
            k_weekly.iloc[:i+1],
            hist_lookback=S1_WEEKLY_HIST,
            ema_lookback=S1_WEEKLY_EMA,
            ema_threshold_ratio=S1_WEEKLY_EMA_THRESH,
        )

    # Align hourly data: find first week overlapping with hourly
    h_first_ns = None
    for i in range(n_h):
        if k_hourly.iloc[i]["datetime"] > 0 and k_hourly.iloc[i]["close"] > 0:
            h_first_ns = k_hourly.iloc[i]["datetime"]
            break
    if h_first_ns is None:
        return None

    loop_start_w = first_w
    while loop_start_w < n_w:
        end_ns = k_weekly.iloc[loop_start_w+1]["datetime"] if loop_start_w+1 < n_w \
                 else k_weekly.iloc[loop_start_w]["datetime"] + 604800 * 1e9
        if end_ns > h_first_ns:
            break
        loop_start_w += 1

    stats = BacktestStats(multiplier)
    pos = None

    for i in range(loop_start_w, n_w):
        s1 = s1_cache[i]
        if s1["trend"] == "neutral":
            continue

        t_w_start = k_weekly.iloc[i]["datetime"]
        t_w_end = k_weekly.iloc[i+1]["datetime"] if i+1 < n_w else t_w_start + 604800 * 1e9

        mask_d = (k_daily["datetime"] >= t_w_start) & (k_daily["datetime"] < t_w_end)
        idx_d_list = k_daily.index[mask_d].tolist()

        for j in idx_d_list:
            s2 = determine_screen2_signal(s1["trend"], k_daily.iloc[:j+1])
            valid_sig = (s1["trend"] == "bullish" and s2["signal"] in ("buy_signal", "divergence_buy")) or \
                        (s1["trend"] == "bearish" and s2["signal"] in ("sell_signal", "divergence_sell"))
            if not valid_sig:
                continue

            t_d_start = k_daily.iloc[j]["datetime"]
            t_d_end = k_daily.iloc[j+1]["datetime"] if j+1 < n_d else t_d_start + 86400 * 1e9

            mask_h = (k_hourly["datetime"] >= t_d_start) & (k_hourly["datetime"] < t_d_end)
            idx_h_list = k_hourly.index[mask_h].tolist()

            for k in idx_h_list:
                curr_bar = k_hourly.iloc[k]
                curr_high, curr_low = curr_bar["high"], curr_bar["low"]
                curr_time = curr_bar["datetime"]

                # Exit management
                if pos is not None:
                    pos.bars_held += 1
                    if pos.direction == "long":
                        pos.peak_profit = max(pos.peak_profit, curr_high - pos.entry_price)
                        pos.max_adverse = min(pos.max_adverse, curr_low - pos.entry_price)
                        if curr_low <= pos.current_stop:
                            stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                                  "stop_hit", pos.bars_held, multiplier))
                            pos = None
                            continue
                    else:
                        pos.peak_profit = max(pos.peak_profit, pos.entry_price - curr_low)
                        pos.max_adverse = min(pos.max_adverse, pos.entry_price - curr_high)
                        if curr_high >= pos.current_stop:
                            stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                                  "stop_hit", pos.bars_held, multiplier))
                            pos = None
                            continue

                    # Trailing
                    if pos.peak_profit > 0:
                        risk = abs(pos.entry_price - pos.initial_stop)
                        if pos.peak_profit >= TRAILING_R * risk:
                            if pos.direction == "long":
                                pos.current_stop = max(pos.current_stop, pos.entry_price)
                            else:
                                pos.current_stop = min(pos.current_stop, pos.entry_price)

                # Entry
                if pos is None:
                    s3 = determine_screen3_entry(s1["trend"], s2["signal"],
                                                 k_hourly.iloc[:k+1], tick_size=tick_size)
                    if s3["signal"] in ("triggered_long", "triggered_short"):
                        entry_price = s3["entry_price"]
                        stop_price = s3["stop_loss"]
                        entry_time = curr_time

                        if s3["signal"] == "triggered_long":
                            pos = Position("long", entry_price, entry_time, stop_price,
                                           s2["signal"])
                        else:
                            pos = Position("short", entry_price, entry_time, stop_price,
                                           s2["signal"])
                        # Need to set Position attributes
                        pos.bars_held = 0
                        pos.peak_profit = 0.0
                        pos.max_adverse = 0.0
                        pos.status = "open"

    # Close open
    if pos is not None:
        last_bar = k_hourly.iloc[-1]
        stats.add_trade(Trade(pos, last_bar["close"], last_bar["datetime"],
                              "end_of_data", pos.bars_held, multiplier))

    return stats


# ══════════════════════════════════════════════════════════
# 策略 3: Set C (日→时→15m)
# ══════════════════════════════════════════════════════════
def backtest_set_c(k_daily, k_hourly, k_15min, multiplier, tick_size):
    """
    Set C 回测: 日线趋势 → 小时回调 → 15min 入场.
    """
    n_d = len(k_daily)
    n_h = len(k_hourly)
    n_15 = len(k_15min)

    # 日线 S1 cache
    s1_cache = {}
    for i in range(50, n_d):
        s1_cache[i] = determine_screen1_trend(
            k_daily.iloc[:i+1], hist_lookback=2, ema_lookback=10)

    # Align 15min
    k15_first_ns = None
    for i in range(n_15):
        if k_15min.iloc[i]["datetime"] > 0 and k_15min.iloc[i]["close"] > 0:
            k15_first_ns = k_15min.iloc[i]["datetime"]
            break
    if k15_first_ns is None:
        return None

    loop_start_d = 50
    while loop_start_d < n_d:
        end_ns = k_daily.iloc[loop_start_d+1]["datetime"] if loop_start_d+1 < n_d \
                 else k_daily.iloc[loop_start_d]["datetime"] + 86400 * 1e9
        if end_ns > k15_first_ns:
            break
        loop_start_d += 1

    stats = BacktestStats(multiplier)
    pos = None

    for i in range(loop_start_d, n_d):
        s1 = s1_cache[i]
        if s1["trend"] == "neutral":
            continue

        t_d_start = k_daily.iloc[i]["datetime"]
        t_d_end = k_daily.iloc[i+1]["datetime"] if i+1 < n_d else t_d_start + 86400 * 1e9

        mask_h = (k_hourly["datetime"] >= t_d_start) & (k_hourly["datetime"] < t_d_end)
        idx_h_list = k_hourly.index[mask_h].tolist()

        for j in idx_h_list:
            s2 = determine_screen2_signal(s1["trend"], k_hourly.iloc[:j+1])
            valid_sig = (s1["trend"] == "bullish" and s2["signal"] in ("buy_signal", "divergence_buy")) or \
                        (s1["trend"] == "bearish" and s2["signal"] in ("sell_signal", "divergence_sell"))
            if not valid_sig:
                continue

            t_h_start = k_hourly.iloc[j]["datetime"]
            t_h_end = k_hourly.iloc[j+1]["datetime"] if j+1 < n_h else t_h_start + 3600 * 1e9

            mask_15 = (k_15min["datetime"] >= t_h_start) & (k_15min["datetime"] < t_h_end)
            idx_15_list = k_15min.index[mask_15].tolist()

            for m in idx_15_list:
                curr_bar = k_15min.iloc[m]
                curr_high, curr_low = curr_bar["high"], curr_bar["low"]
                curr_time = curr_bar["datetime"]

                if pos is not None:
                    pos.bars_held += 1
                    if pos.direction == "long":
                        pos.peak_profit = max(pos.peak_profit, curr_high - pos.entry_price)
                        pos.max_adverse = min(pos.max_adverse, curr_low - pos.entry_price)
                        if curr_low <= pos.current_stop:
                            stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                                  "stop_hit", pos.bars_held, multiplier))
                            pos = None
                            continue
                    else:
                        pos.peak_profit = max(pos.peak_profit, pos.entry_price - curr_low)
                        pos.max_adverse = min(pos.max_adverse, pos.entry_price - curr_high)
                        if curr_high >= pos.current_stop:
                            stats.add_trade(Trade(pos, pos.current_stop, curr_time,
                                                  "stop_hit", pos.bars_held, multiplier))
                            pos = None
                            continue

                    if pos.peak_profit > 0:
                        risk = abs(pos.entry_price - pos.initial_stop)
                        if pos.peak_profit >= TRAILING_R * risk:
                            if pos.direction == "long":
                                pos.current_stop = max(pos.current_stop, pos.entry_price)
                            else:
                                pos.current_stop = min(pos.current_stop, pos.entry_price)

                if pos is None:
                    s3 = determine_screen3_entry(s1["trend"], s2["signal"],
                                                 k_15min.iloc[:m+1], tick_size=tick_size)
                    if s3["signal"] in ("triggered_long", "triggered_short"):
                        entry_price = s3["entry_price"]
                        stop_price = s3["stop_loss"]
                        entry_time = curr_time

                        if s3["signal"] == "triggered_long":
                            pos = Position("long", entry_price, entry_time, stop_price,
                                           s2["signal"])
                        else:
                            pos = Position("short", entry_price, entry_time, stop_price,
                                           s2["signal"])
                        pos.bars_held = 0
                        pos.peak_profit = 0.0
                        pos.max_adverse = 0.0
                        pos.status = "open"

    if pos is not None:
        last_bar = k_15min.iloc[-1]
        stats.add_trade(Trade(pos, last_bar["close"], last_bar["datetime"],
                              "end_of_data", pos.bars_held, multiplier))

    return stats


# ══════════════════════════════════════════════════════════
# 汇总 & 输出
# ══════════════════════════════════════════════════════════
def print_results_table(all_results):
    """打印全品种全策略对比表"""
    strategies_order = ["伪代码(周+日+动力)", "Set A(周→日→时)", "Set C(日→时→15m)"]

    print(f"\n{'='*100}")
    print(f"  全品种全策略回测对比")
    print(f"{'='*100}")

    # 每个策略一个表
    for strat_name in strategies_order:
        print(f"\n  [{strat_name}]")
        print(f"  {'合约':<8} {'交易':>4} {'胜率':>7} {'盈亏比':>7} {'利润因子':>8} "
              f"{'总盈亏(点)':>10} {'总盈亏(¥)':>12} {'均盈':>8} {'均亏':>8} {'最大回撤':>8}")
        print(f"  {'─'*90}")

        for row in all_results:
            r = row.get(strat_name)
            if r is None:
                continue
            d = r.to_dict()
            print(f"  {row['name']:<8} {d['trades']:>4} {d['win_rate']:>7} "
                  f"{'—':>7} {d['profit_factor']:>8} "
                  f"{d['total_pnl_pts']:>10} {d['total_pnl_rmb']:>12} "
                  f"{d['avg_win']:>8} {d['avg_loss']:>8} {d['max_dd']:>8}")
        print()

    # 跨策略汇总表
    print(f"\n  {'─'*100}")
    print(f"  跨策略收益汇总 (¥)")
    print(f"  {'─'*100}")
    header = f"  {'合约':<8}"
    for sn in strategies_order:
        header += f" {sn:>22}"
    print(header)
    print(f"  {'─'*100}")

    for row in all_results:
        line = f"  {row['name']:<8}"
        for sn in strategies_order:
            r = row.get(sn)
            if r:
                line += f" {r.total_pnl_rmb:>+21,.0f}"
            else:
                line += f" {'—':>22}"
        print(line)

    print(f"\n{'='*100}\n")


# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="全品种全策略综合回测")
    parser.add_argument("--update-csv", metavar="DIR",
                        help="增量更新指定目录的 CSV 数据（不回测）")
    parser.add_argument("--dump-data", action="store_true",
                        help="全量重新下载并保存 CSV, 不运行回测")
    parser.add_argument("--from-csv", metavar="DIR",
                        help="从指定目录的 CSV 文件读取数据（离线模式）")
    parser.add_argument("--save-csv", metavar="DIR",
                        help="回测同时将数据保存到指定目录")
    parser.add_argument("--csv-dir", default="data",
                        help="默认 CSV 目录 (用于 --dump-data / --save-csv)")
    args = parser.parse_args()

    csv_dir = args.csv_dir
    use_tqsdk = not args.from_csv

    # ── 模式 0: 增量更新 CSV ──
    if args.update_csv:
        if not HAS_TQSDK:
            print("错误: tqsdk 未安装，无法更新数据")
            sys.exit(1)
        username, password = get_tqsdk_auth()
        print(f"连接 TqSdk ({username})... [增量更新模式]")
        api = TqApi(auth=TqAuth(username, password))
        try:
            for cfg in CONTRACTS:
                name = cfg["name"]
                sym  = cfg["symbol"]
                sn   = _short_name(sym)
                print(f"\n{'─'*60}")
                print(f"  [{name}] {sym}")
                print(f"{'─'*60}")
                update_klines_csv(api, sym, args.update_csv)
        except KeyboardInterrupt:
            print("\n中断")
        finally:
            try:
                api.close()
            except Exception:
                pass
        print(f"\n  更新完成! 数据目录: {os.path.abspath(args.update_csv)}/")
        print(f"  离线回测: PYTHONUNBUFFERED=1 python scripts/backtest_all_contracts.py --from-csv {args.update_csv}")
        return

    # ── 模式 1: 离线 CSV ──
    if args.from_csv:
        print(f"离线模式: 从 {args.from_csv} 读取 CSV 数据\n")
        all_results = []
        for cfg in CONTRACTS:
            name = cfg["name"]
            sym  = cfg["symbol"]
            mult = cfg["multiplier"]
            tick = cfg["tick"]

            print(f"\n{'─'*60}")
            print(f"  [{name}] {sym}  (乘数={mult}, 跳动={tick})")
            print(f"{'─'*60}")

            row = {"name": name, "symbol": sym}
            klines = load_klines_csv(sym, args.from_csv)
            if not klines:
                print("  ⚠️ 无 CSV 数据, 跳过")
                all_results.append(row)
                continue
            _run_backtests(klines, mult, tick, row)
            all_results.append(row)

        print_results_table(all_results)
        return

    # ── 模式 2: TqSdk 在线 ──
    if not HAS_TQSDK:
        print("错误: tqsdk 未安装，请使用 --from-csv <dir> 离线回测")
        sys.exit(1)

    username, password = get_tqsdk_auth()
    print(f"连接 TqSdk ({username})...")
    api = TqApi(auth=TqAuth(username, password))

    all_results = []
    dump_dir = csv_dir if args.dump_data else (args.save_csv or None)

    try:
        for cfg in CONTRACTS:
            name = cfg["name"]
            sym  = cfg["symbol"]
            mult = cfg["multiplier"]
            tick = cfg["tick"]

            print(f"\n{'─'*60}")
            print(f"  [{name}] {sym}  (乘数={mult}, 跳动={tick})")
            print(f"{'─'*60}")

            row = {"name": name, "symbol": sym}

            try:
                # 若指定了 CSV 目录: 使用增量更新 (自动判断首次/更新)
                if dump_dir:
                    klines = update_klines_csv(api, sym, dump_dir)
                else:
                    klines = load_klines_tqsdk(api, sym)
            except Exception as e:
                print(f"  ⚠️ 数据加载失败: {e}")
                all_results.append(row)
                continue

            # 只 dump 数据就跳过回测
            if args.dump_data:
                all_results.append(row)
                continue

            _run_backtests(klines, mult, tick, row)
            all_results.append(row)

    except KeyboardInterrupt:
        print("\n中断")
    finally:
        try:
            api.close()
        except Exception:
            pass

    if not args.dump_data:
        print_results_table(all_results)
    else:
        print(f"\n  数据已保存到 {os.path.abspath(dump_dir)}/")
        print("  下次直接回测: PYTHONUNBUFFERED=1 python scripts/backtest_all_contracts.py --from-csv data")


def _run_backtests(klines, mult, tick, row):
    """对一份 klines 运行三个策略"""
    # ── 伪代码策略 ──
    print("\n  >> 伪代码策略 (周→日+动力)")
    try:
        stats_ps = backtest_pseudocode(
            klines.get("1week"), klines.get("1day"), mult, tick)
        row["伪代码(周+日+动力)"] = stats_ps
        d = stats_ps.to_dict()
        print(f"     交易{d['trades']}笔  胜率{d['win_rate']}  "
              f"利润因子{d['profit_factor']}  P&L={d['total_pnl_rmb']}")
    except Exception as e:
        print(f"     ⚠️ 回测失败: {e}")

    # ── Set A ──
    print("\n  >> Set A (周→日→时)")
    try:
        k1 = klines.get("1week")
        k2 = klines.get("1day")
        k3 = klines.get("1hour")
        if k1 is not None and k2 is not None and k3 is not None:
            stats_a = backtest_set_a(k1, k2, k3, mult, tick)
            if stats_a:
                row["Set A(周→日→时)"] = stats_a
                d = stats_a.to_dict()
                print(f"     交易{d['trades']}笔  胜率{d['win_rate']}  "
                      f"利润因子{d['profit_factor']}  P&L={d['total_pnl_rmb']}")
            else:
                print("     数据不足, 跳过")
    except Exception as e:
        print(f"     ⚠️ 回测失败: {e}")

    # ── Set C ──
    print("\n  >> Set C (日→时→15m)")
    try:
        k1 = klines.get("1day")
        k2 = klines.get("1hour")
        k3 = klines.get("15min")
        if k1 is not None and k2 is not None and k3 is not None:
            stats_c = backtest_set_c(k1, k2, k3, mult, tick)
            if stats_c:
                row["Set C(日→时→15m)"] = stats_c
                d = stats_c.to_dict()
                print(f"     交易{d['trades']}笔  胜率{d['win_rate']}  "
                      f"利润因子{d['profit_factor']}  P&L={d['total_pnl_rmb']}")
            else:
                print("     数据不足, 跳过")
    except Exception as e:
        print(f"     ⚠️ 回测失败: {e}")


if __name__ == "__main__":
    main()
