"""
CF609 Triple Screen 级联回测 — Set B (短线) P&L 统计

Periods: 小时线 → 15min → 3min (Elder 短线交易组合)

用法:
    python scripts/backtest_cf609_setB_pnl.py

输出:
    每笔交易明细 (入场/出场/盈亏/原因)
    汇总统计 (胜率, 盈亏比, 利润因子, 最大回撤等)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tqsdk import TqApi, TqAuth
from datetime import datetime
import time as _time

from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    calc_macd, calc_ema,
    determine_screen1_trend,
    determine_screen2_signal,
    determine_screen3_entry,
    Position,
)


SYMBOL     = "CZCE.CF609"
TICK_SIZE  = 5.0      # 棉花跳动 5 元/吨
MULTIPLIER = 5        # 合约乘数 5 吨/手 → 每点=5元
DATA_LEN   = 8964     # tqsdk 免费账户最大 K 线数
STOP_BARS   = 5       # 追踪止损回看 bar 数 (3min 级别)
# Set B 使用默认 Screen 1 参数 (小时线波动频率正常，无需特殊适配)

# K 线周期秒数
KLINE_DURS = {
    "3min":  180,
    "15min": 900,
    "1hour": 3600,
}


# ── 统计容器 ──
class Trade:
    """单个已平仓交易的完整记录"""
    __slots__ = (
        "direction", "entry_time", "entry_price", "entry_signal",
        "exit_time", "exit_price", "exit_reason",
        "pnl_points", "pnl_rmb", "bars_held",
        "initial_stop", "stop_risk_points",
        "mfe_points", "mae_points",
    )
    def __init__(self, pos, exit_price, exit_time, exit_reason, bars_held):
        self.direction   = pos.direction
        self.entry_time  = pos.entry_time
        self.entry_price = pos.entry_price
        self.entry_signal = pos.entry_signal
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
        self.pnl_rmb = self.pnl_points * MULTIPLIER


class BacktestStats:
    """回测统计"""
    def __init__(self):
        self.trades = []
        self._equity_curve = []

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

    @property
    def win_rate(self):
        return len(self.wins) / max(1, self.total_trades)

    @property
    def avg_win(self):
        w = self.wins
        return sum(t.pnl_points for t in w) / max(1, len(w))

    @property
    def avg_loss(self):
        l = self.losses
        return sum(t.pnl_points for t in l) / max(1, len(l))

    @property
    def profit_factor(self):
        gross_profit = sum(t.pnl_points for t in self.wins)
        gross_loss   = abs(sum(t.pnl_points for t in self.losses))
        return gross_profit / max(1, gross_loss)

    @property
    def total_pnl_points(self):
        return sum(t.pnl_points for t in self.trades)

    @property
    def total_pnl_rmb(self):
        return self.total_pnl_points * MULTIPLIER

    @property
    def max_drawdown(self):
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0][1]
        worst_dd = 0.0
        for _, cum in self._equity_curve:
            peak = max(peak, cum)
            dd = peak - cum
            worst_dd = max(worst_dd, dd)
        return worst_dd

    @property
    def avg_bars_held(self):
        return sum(t.bars_held for t in self.trades) / max(1, self.total_trades)

    def by_exit_reason(self):
        from collections import Counter
        return dict(Counter(t.exit_reason for t in self.trades))

    def by_signal(self):
        from collections import Counter
        return dict(Counter(t.entry_signal for t in self.trades))


# ── 工具函数 ──
def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M")
    return "---"

def fmt_day(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")
    return "---"


# ── 数据获取 ──
def fetch_historical_data(api, symbol):
    print(f"\n获取历史数据: {symbol} (data_length={DATA_LEN})...")
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
        vals = [(i, fmt_day(k.iloc[i]["datetime"])) for i in range(n) if k.iloc[i]["close"] > 0]
        if vals:
            print(f"  {label:6s}: {n:5d} bars  ({len(vals)} valid)  "
                  f"{vals[0][1]} -> {vals[-1][1]}")
        else:
            print(f"  {label:6s}: {n:5d} bars  (0 valid)")
    return klines_map


def run_backtest(klines_map):
    """
    时序遍历三重滤网 Set B 级联，管理持仓和 P&L。

    流程:
        for 每根小时 bar:
            S1 = 趋势方向
            if 中性: 跳过

            for 当小时内每根 15min bar:
                S2 = 回调信号
                if 无信号: 跳过

                for 15min 内每根 3min bar:
                    若持仓 → 出场检查
                    若空仓 → S3 挂单/触发

    出场规则 (优先级):
        1. stop_hit           — 价格突破当前追踪止损
        2. s1_reversal        — 小时线趋势不再支持持仓
        3. opposite_divergence — S2 出现反向背离
        4. trailing_update    — 顺向移动追踪止损
    """
    k1h = klines_map["1hour"]
    k15 = klines_map["15min"]
    k3m = klines_map["3min"]
    n_h = len(k1h)
    n_15 = len(k15)

    stats = BacktestStats()
    position = None

    # Scan progress trackers
    s1_bullish = s1_bearish = s1_neutral = 0
    s2_buy = s2_sell = s2_no = 0
    s3_events = {"triggered_long": 0, "triggered_short": 0,
                 "pending_long": 0, "pending_short": 0, "none": 0}

    # ── Screen 1: 探测小时线有效数据范围 ──
    print("  计算小时线 MACD/EMA 确定有效回测范围...")
    _, _, dif_h, dea_h, hist_h = calc_macd(k1h, fast=12, slow=26, signal=9)
    ema13_h = calc_ema(k1h, span=13)
    # MACD warmup 33 + EMA lookback 10 + margin 5
    min_warmup = 33 + 10 + 5
    hist_dropna = hist_h.dropna()
    ema_dropna  = ema13_h.dropna()
    if len(hist_dropna) == 0 or len(ema_dropna) == 0:
        print("  ⚠️ 小时线数据不足，无法回测")
        return
    first_hist_idx = hist_dropna.index[0]
    first_ema_idx  = ema_dropna.index[0]
    first_valid_h = max(first_hist_idx, first_ema_idx, min_warmup)
    if first_valid_h < 0 or first_valid_h >= n_h:
        print("  ⚠️ 小时线有效范围为空，无法回测")
        return
    n_valid_hours = n_h - first_valid_h
    print(f"  小时线有效范围: bar {first_valid_h}..{n_h-1}  ({n_valid_hours} 小时)")

    # ── 对齐 3min 数据范围 ──
    # 小时线和 15min 可能有多年数据，3min 通常只有几天到几周
    k3m_earliest_ns = None
    for i in range(len(k3m)):
        dt_ns = k3m.iloc[i]["datetime"]
        if dt_ns > 0 and k3m.iloc[i]["close"] > 0:
            k3m_earliest_ns = dt_ns
            break
    if k3m_earliest_ns is None:
        print("  ⚠️ 3min 数据不足，无法回测")
        return

    # 找到第一个覆盖 3min 数据的小时
    first_h_with_k3m = first_valid_h
    while first_h_with_k3m < n_h:
        next_idx = first_h_with_k3m + 1
        t_hour_end = (k1h.iloc[next_idx]["datetime"] if next_idx < n_h
                      else k1h.iloc[first_h_with_k3m]["datetime"] + 3600 * 1e9)
        if t_hour_end > k3m_earliest_ns:
            break
        first_h_with_k3m += 1

    if first_h_with_k3m >= n_h:
        print("  ⚠️ 小时线数据与 3min 数据无重叠，无法回测")
        return

    loop_start_h = max(first_valid_h, first_h_with_k3m)
    from datetime import datetime as dt_mod
    h_date = dt_mod.fromtimestamp(k1h.iloc[loop_start_h]["datetime"] / 1e9).strftime("%Y-%m-%d %H:%M")
    k3m_date = dt_mod.fromtimestamp(k3m_earliest_ns / 1e9).strftime("%Y-%m-%d %H:%M")
    print(f"  3min 最早: {k3m_date}, 首个重叠小时: bar {loop_start_h} ({h_date})")
    print()

    # ── Screen 1 缓存 (使用默认参数，小时线频率正常) ──
    s1_cache = {}
    for i in range(first_valid_h, n_h):
        s1_cache[i] = determine_screen1_trend(k1h.iloc[:i+1])

    print(f"开始级联遍历 {loop_start_h}..{n_h-1} 小时...")
    total_3m_bars = 0

    for i in range(loop_start_h, n_h):
        s1 = s1_cache[i]
        if s1["trend"] == "bullish":   s1_bullish += 1
        elif s1["trend"] == "bearish": s1_bearish += 1
        else:                           s1_neutral += 1

        if s1["trend"] == "neutral":
            continue

        t_h_start = k1h.iloc[i]["datetime"]
        t_h_end   = k1h.iloc[i+1]["datetime"] if i+1 < n_h else t_h_start + 3600 * 1e9

        # 15min bars in this hour
        mask_15 = (k15["datetime"] >= t_h_start) & (k15["datetime"] < t_h_end)
        idx_15_list = k15.index[mask_15].tolist()

        for j_pos in idx_15_list:
            s2 = determine_screen2_signal(s1["trend"], k15.iloc[:j_pos+1])

            if s1["trend"] == "bullish":
                if s2["signal"] in ("buy_signal", "divergence_buy"):
                    s2_buy += 1
                else:
                    s2_no += 1
                    continue
            else:
                if s2["signal"] in ("sell_signal", "divergence_sell"):
                    s2_sell += 1
                else:
                    s2_no += 1
                    continue

            t_15_start = k15.iloc[j_pos]["datetime"]
            t_15_end   = k15.iloc[j_pos+1]["datetime"] if j_pos+1 < n_15 else t_15_start + 900 * 1e9

            # 3min bars in this 15min
            mask_3m = (k3m["datetime"] >= t_15_start) & (k3m["datetime"] < t_15_end)
            idx_3m_list = k3m.index[mask_3m].tolist()

            for k_pos in idx_3m_list:
                total_3m_bars += 1
                slice_3m = k3m.iloc[:k_pos+1]
                curr_bar = k3m.iloc[k_pos]
                curr_high = curr_bar["high"]
                curr_low  = curr_bar["low"]
                curr_close = curr_bar["close"]
                curr_time  = curr_bar["datetime"]

                # ── 持仓管理：出场检查 ──
                if position is not None and position.status == "open":
                    position.bars_held += 1

                    # MFE tracking
                    if position.direction == "long":
                        position.peak_profit = max(position.peak_profit,
                                                   curr_high - position.entry_price)
                        position.max_adverse = min(getattr(position, "max_adverse", 0),
                                                   curr_low - position.entry_price)
                    else:
                        position.peak_profit = max(position.peak_profit,
                                                   position.entry_price - curr_low)
                        position.max_adverse = min(getattr(position, "max_adverse", 0),
                                                   position.entry_price - curr_high)

                    # 1. Stop hit
                    if position.direction == "long" and curr_low <= position.current_stop:
                        _close_trade(stats, position, position.current_stop,
                                     curr_time, "stop_hit")
                        position = None
                        continue
                    if position.direction == "short" and curr_high >= position.current_stop:
                        _close_trade(stats, position, position.current_stop,
                                     curr_time, "stop_hit")
                        position = None
                        continue

                    # 2. S1 reversal (also exit on neutral)
                    # S1 可能随着新小时 bar 变化，用最新的
                    s1_now = s1_cache.get(i, s1)
                    s1_bad = (
                        (position.direction == "long" and s1_now["trend"] != "bullish")
                        or (position.direction == "short" and s1_now["trend"] != "bearish")
                    )
                    if s1_bad:
                        _close_trade(stats, position, curr_close, curr_time,
                                     f"s1_reversal({s1_now['trend']})")
                        position = None
                        continue

                    # 3. Opposite divergence
                    s2_now = determine_screen2_signal(s1_now["trend"], k15.iloc[:j_pos+1])
                    opp_div = (
                        (position.direction == "long" and s2_now["signal"] == "divergence_sell")
                        or (position.direction == "short" and s2_now["signal"] == "divergence_buy")
                    )
                    if opp_div:
                        _close_trade(stats, position, curr_close, curr_time,
                                     "opposite_divergence")
                        position = None
                        continue

                    # 4. Trailing stop update
                    n_stop = min(STOP_BARS, k_pos)
                    if n_stop > 0:
                        if position.direction == "long":
                            completed_lows = k3m["low"].iloc[k_pos - n_stop:k_pos]
                            new_stop = completed_lows.min()
                            if new_stop > position.current_stop:
                                position.current_stop = new_stop
                        else:
                            completed_highs = k3m["high"].iloc[k_pos - n_stop:k_pos]
                            new_stop = completed_highs.max()
                            if new_stop < position.current_stop:
                                position.current_stop = new_stop

                    continue  # holding, next bar

                # ── 空仓：入场检查 ──
                s3 = determine_screen3_entry(s1["trend"], s2["signal"],
                                             slice_3m, tick_size=TICK_SIZE)

                sig = s3["signal"]
                if sig in s3_events:
                    s3_events[sig] += 1

                if sig == "triggered_long":
                    position = Position("long", s3["entry_price"], curr_time,
                                        s3["stop_loss"], s2["signal"])
                    position.max_adverse = 0.0
                elif sig == "triggered_short":
                    position = Position("short", s3["entry_price"], curr_time,
                                        s3["stop_loss"], s2["signal"])
                    position.max_adverse = 0.0

    # ── 强制平仓 ──
    if position is not None and position.status == "open":
        last_bar = k3m.iloc[-1]
        _close_trade(stats, position, last_bar["close"],
                     last_bar["datetime"], "end_of_data")

    _print_results(stats, s1_bullish, s1_bearish, s1_neutral,
                   s2_buy, s2_sell, s2_no, s3_events, total_3m_bars)
    return stats


def _close_trade(stats, position, exit_price, exit_time, reason):
    position.status = "closed"
    trade = Trade(position, exit_price, exit_time, reason, position.bars_held)
    stats.add_trade(trade)
    return trade


def _print_results(stats, s1_b, s1_r, s1_n, s2_buy, s2_sell, s2_no, s3_ev, total_bars):
    print(f"\n{'='*72}")
    print(f"  CF609 Triple Screen 级联回测 — SET B (短线) P&L 报告")
    print(f"{'='*72}")

    # ── 级别统计 ──
    print(f"\n── Screen 1 (小时线) 趋势分布 ──")
    total_h = s1_b + s1_r + s1_n
    print(f"  看涨 {s1_b:5d}  ({100*s1_b/max(1,total_h):5.1f}%)")
    print(f"  看跌 {s1_r:5d}  ({100*s1_r/max(1,total_h):5.1f}%)")
    print(f"  中性 {s1_n:5d}  ({100*s1_n/max(1,total_h):5.1f}%)")

    print(f"\n── Screen 2 (15min) 信号 ──")
    print(f"  买入信号: {s2_buy:5d}")
    print(f"  卖出信号: {s2_sell:5d}")
    print(f"  无信号:   {s2_no:5d}")

    print(f"\n── Screen 3 (3min) 触发 ──")
    print(f"  triggered_long:   {s3_ev['triggered_long']:5d}")
    print(f"  triggered_short:  {s3_ev['triggered_short']:5d}")
    print(f"  pending_long:     {s3_ev['pending_long']:5d}")
    print(f"  pending_short:    {s3_ev['pending_short']:5d}")
    print(f"  总 3min bar 数:   {total_bars}")

    if stats.total_trades == 0:
        print(f"\n⚠️  未产生任何成交")
        return

    # ── P&L 汇总 ──
    print(f"\n{'='*72}")
    print(f"  P&L 统计  ({stats.total_trades} 笔交易)")
    print(f"{'='*72}")

    print(f"\n── 核心指标 ──")
    print(f"  总交易数:       {stats.total_trades:5d}")
    print(f"  胜率:           {stats.win_rate*100:5.1f}%  ({len(stats.wins)}/{stats.total_trades})")
    print(f"  平均盈利:       {stats.avg_win:+8.1f} 点  (¥{stats.avg_win*MULTIPLIER:+8.1f})")
    print(f"  平均亏损:       {stats.avg_loss:+8.1f} 点  (¥{stats.avg_loss*MULTIPLIER:+8.1f})")
    print(f"  盈亏比:         {abs(stats.avg_win/max(0.01,stats.avg_loss)):5.2f}")
    print(f"  利润因子:       {stats.profit_factor:5.2f}")
    print(f"  总盈亏:         {stats.total_pnl_points:+8.1f} 点  (¥{stats.total_pnl_rmb:+8.1f})")
    print(f"  最大回撤:       {stats.max_drawdown:8.1f} 点")
    print(f"  平均持仓 bar:   {stats.avg_bars_held:5.1f}  (3min)")

    # ── 按出场原因 ──
    print(f"\n── 按出场原因 ──")
    by_reason = stats.by_exit_reason()
    reason_cn = {
        "stop_hit": "止损触发",
        "s1_reversal(bearish)": "S1转空",
        "s1_reversal(bullish)": "S1转多",
        "s1_reversal(neutral)": "S1变中性",
        "opposite_divergence": "反向背离",
        "end_of_data": "数据结束",
    }
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
        label = reason_cn.get(reason, reason)
        subtrades = [t for t in stats.trades if t.exit_reason == reason]
        sub_pnl = sum(t.pnl_points for t in subtrades)
        sub_wr = sum(1 for t in subtrades if t.pnl_points > 0) / max(1, len(subtrades))
        print(f"  {label:<18s} {count:3d}笔  PnL={sub_pnl:+7.1f}  胜率={sub_wr*100:4.0f}%")

    # ── 按入场信号 ──
    print(f"\n── 按入场信号 ──")
    by_sig = stats.by_signal()
    sig_cn = {
        "buy_signal": "普通买入",
        "divergence_buy": "底背离买入",
        "sell_signal": "普通卖出",
        "divergence_sell": "顶背离卖出",
    }
    for sig, count in sorted(by_sig.items(), key=lambda x: -x[1]):
        label = sig_cn.get(sig, sig)
        subtrades = [t for t in stats.trades if t.entry_signal == sig]
        sub_pnl = sum(t.pnl_points for t in subtrades)
        sub_wr = sum(1 for t in subtrades if t.pnl_points > 0) / max(1, len(subtrades))
        print(f"  {label:<18s} {count:3d}笔  PnL={sub_pnl:+7.1f}  胜率={sub_wr*100:4.0f}%")

    # ── 最近 20 笔交易明细 ──
    print(f"\n── 交易明细 (最近 {min(20, stats.total_trades)} 笔) ──")
    header = f"  {'方向':4s} {'入场时间':16s} {'入场价':>7s} {'出场时间':16s} {'出场价':>7s} {'PnL(点)':>8s} {'PnL(元)':>8s} {'原因':18s}"
    print(header)
    print(f"  {'-'*106}")
    for t in stats.trades[-20:]:
        d = "多" if t.direction == "long" else "空"
        r = reason_cn.get(t.exit_reason, t.exit_reason)[:18]
        print(f"  {d:4s} {fmt_time(t.entry_time):16s} {t.entry_price:7.0f} "
              f"{fmt_time(t.exit_time):16s} {t.exit_price:7.0f} "
              f"{t.pnl_points:+8.1f} {t.pnl_rmb:+8.1f} {r:18s}")

    # ── 权益曲线摘要 ──
    print(f"\n── 权益曲线摘要 ──")
    cum = 0
    dd_peak = 0
    worst_with_date = (0.0, "", "")
    for trade in stats.trades:
        cum += trade.pnl_points
        dd_peak = max(dd_peak, cum)
        dd = dd_peak - cum
        if dd > worst_with_date[0]:
            worst_with_date = (dd, fmt_time(trade.exit_time), dd_peak)

    print(f"  最终累计 PnL:     {cum:+8.1f} 点")
    print(f"  权益峰值:         {dd_peak:+8.1f} 点")
    print(f"  最大回撤:         {worst_with_date[0]:8.1f} 点  "
          f"(峰值 {worst_with_date[2]:.0f} @ {worst_with_date[1]})")

    # ── MFE/MAE ──
    mfe_vals = [t.mfe_points for t in stats.trades]
    mae_vals = [t.mae_points for t in stats.trades]
    print(f"\n── MFE/MAE (最大有利/不利偏移) ──")
    print(f"  平均 MFE: {sum(mfe_vals)/max(1,len(mfe_vals)):+6.1f} 点")
    print(f"  最大 MFE: {max(mfe_vals):+6.1f} 点")
    print(f"  平均 MAE: {sum(mae_vals)/max(1,len(mae_vals)):+6.1f} 点")
    print(f"  最大 MAE: {min(mae_vals):+6.1f} 点  (最糟浮亏)")

    print(f"\n{'='*72}")


def main():
    username, password = get_tqsdk_auth()
    print(f"连接 TqSdk (账户: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        print(f"目标合约: {SYMBOL}  (tick={TICK_SIZE}, 乘数={MULTIPLIER})")
        print(f"组合: Set B (小时线 → 15min → 3min)")
        quote = api.get_quote(SYMBOL)
        deadline = _time.time() + 10
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if getattr(quote, "last_price", 0) > 0:
                break

        klines_map = fetch_historical_data(api, SYMBOL)

        for label, k in klines_map.items():
            if len(k) < 100:
                print(f"\n错误: {label} 仅 {len(k)} bars — 需要至少 100 条")
                return

        run_backtest(klines_map)

    finally:
        api.close()


if __name__ == "__main__":
    main()
