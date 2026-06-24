"""
CF609 Triple Screen 级联回测 — 带完整 P&L 统计

Periods: 周线 → 日线 → 15min (Elder 持仓交易组合)

用法:
    python scripts/backtest_cf609_pnl.py

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
WARMUP_W   = 50       # 跳过前 50 周 (MACD/EMA 预热)
MIN_WARMUP  = 50       # MACD/EMA 最小预热 bar (12+26+9=33, 加上 ema_lookback=10)
STOP_BARS   = 5        # 追踪止损回看 bar 数 (15min 级别)
# ── Screen 1 周线参数 (比 15min/日线更宽松，周线变化慢) ──
S1_WEEKLY_HIST = 2     # MACD hist lookback
S1_WEEKLY_EMA  = 4     # EMA lookback (4 周 ≈ 1 月，比默认 10 更适合周线)
S1_WEEKLY_EMA_THRESH = 0.0002  # EMA 斜率阈值 (0.02% vs 默认 0.05%，周线价格大变化慢)

# K 线周期秒数
KLINE_DURS = {
    "15min": 900,
    "1day":  86400,
    "1week": 604800,
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
        self._equity_curve = []  # [(time, cumulative_pnl_points)]

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
        c = Counter(t.exit_reason for t in self.trades)
        return dict(c)

    def by_signal(self):
        from collections import Counter
        c = Counter(t.entry_signal for t in self.trades)
        return dict(c)


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
        t0 = fmt_day(k.iloc[0]["datetime"]) if n > 0 else "---"
        t1 = fmt_day(k.iloc[-1]["datetime"]) if n > 0 else "---"
        print(f"  {label:6s}: {n:5d} bars  {t0} -> {t1}")
    return klines_map


def run_backtest(klines_map):
    """
    时序遍历三重滤网级联，管理持仓和 P&L。

    流程:
        for 每周 bar:
            S1 = 趋势方向
            if 中性: 跳过

            for 当周内每天 bar:
                S2 = 回调信号
                if 无信号: 跳过

                for 当天内每 15min bar:
                    若持仓 → 出场检查
                    若空仓 → S3 挂单/触发

    出场规则 (优先级):
        1. stop_hit           — 价格突破当前追踪止损
        2. s1_reversal        — 周线趋势不再支持持仓
        3. opposite_divergence — S2 出现反向背离
        4. trailing_update    — 顺向移动追踪止损
    """
    kw  = klines_map["1week"]
    kd  = klines_map["1day"]
    k15 = klines_map["15min"]
    n_w = len(kw)
    n_d = len(kd)

    stats = BacktestStats()
    position = None  # 当前持仓

    # Scan progress trackers
    s1_bullish = s1_bearish = s1_neutral = 0
    s2_buy = s2_sell = s2_no = 0
    s3_events = {"triggered_long": 0, "triggered_short": 0,
                 "pending_long": 0, "pending_short": 0, "none": 0}

    # ── Screen 1: 探测周线有效数据范围 ──
    # CF609 周线历史数据可能不到 100 根有效 bar，
    # MACD(12,26,9) 需 33 根预热，EMA(13) 需 13 根，ema_lookback 需额外 S1_WEEKLY_EMA 根
    print("  计算周线 MACD/EMA 确定有效回测范围...")
    _, _, dif_w, dea_w, hist_w = calc_macd(kw, fast=12, slow=26, signal=9)
    ema13_w = calc_ema(kw, span=13)
    # 找到第一根同时有 MACD hist 和 EMA13 且足够做 lookback 的 bar
    first_valid_w = -1
    min_warmup = max(33, S1_WEEKLY_EMA + 5)  # MACD warmup 33 + EMA lookback margin
    hist_dropna = hist_w.dropna()
    ema_dropna  = ema13_w.dropna()
    if len(hist_dropna) > 0 and len(ema_dropna) > 0:
        first_hist_idx = hist_dropna.index[0]
        first_ema_idx  = ema_dropna.index[0]
        first_valid_w = max(first_hist_idx, first_ema_idx, min_warmup)
    if first_valid_w < 0:
        print("  ⚠️ 周线数据不足，无法回测")
        stats.print_report(0, {})
        return
    # 有效范围: first_valid_w .. n_w-1
    n_valid_weeks = n_w - first_valid_w
    print(f"  周线有效范围: bar {first_valid_w}..{n_w-1}  ({n_valid_weeks} 周)")

    # ── 对齐 15min 数据范围 ──
    # 15min 历史数据通常只有 2-3 个月，周线/日线可能有 10 年数据
    # 需要找到第一个与 15min 数据有重叠的周
    k15_earliest_ns = None
    for i in range(len(k15)):
        dt_ns = k15.iloc[i]["datetime"]
        if dt_ns > 0 and k15.iloc[i]["close"] > 0:
            k15_earliest_ns = dt_ns
            break
    if k15_earliest_ns is None:
        print("  ⚠️ 15min 数据不足，无法回测")
        return

    # 找到第一个覆盖 15min 数据的周
    first_w_with_k15 = first_valid_w
    while first_w_with_k15 < n_w:
        # 这一周的截止时间是下一周的开始时间
        next_idx = first_w_with_k15 + 1
        t_week_end = (kw.iloc[next_idx]["datetime"] if next_idx < n_w
                      else kw.iloc[first_w_with_k15]["datetime"] + 604800 * 1e9)
        if t_week_end > k15_earliest_ns:
            break
        first_w_with_k15 += 1

    if first_w_with_k15 >= n_w:
        print("  ⚠️ 周线数据与 15min 数据无重叠，无法回测")
        return

    loop_start_w = max(first_valid_w, first_w_with_k15)
    from datetime import datetime as dt_mod
    w_date = dt_mod.fromtimestamp(kw.iloc[loop_start_w]["datetime"] / 1e9).strftime("%Y-%m-%d")
    k15_date = dt_mod.fromtimestamp(k15_earliest_ns / 1e9).strftime("%Y-%m-%d")
    print(f"  15min 最早: {k15_date}, 首个重叠周: bar {loop_start_w} ({w_date})")
    print()

    # Screen 1 cache (用周线适配参数)
    s1_cache = {}
    for i in range(first_valid_w, n_w):
        s1_cache[i] = determine_screen1_trend(
            kw.iloc[:i+1],
            hist_lookback=S1_WEEKLY_HIST,
            ema_lookback=S1_WEEKLY_EMA,
            ema_threshold_ratio=S1_WEEKLY_EMA_THRESH,
        )

    print(f"开始级联遍历 {loop_start_w}..{n_w-1} 周...")
    total_15m_bars = 0

    for i in range(loop_start_w, n_w):
        s1 = s1_cache[i]
        if s1["trend"] == "bullish":  s1_bullish += 1
        elif s1["trend"] == "bearish": s1_bearish += 1
        else:                          s1_neutral += 1

        if s1["trend"] == "neutral":
            continue

        t_w_start = kw.iloc[i]["datetime"]
        t_w_end   = kw.iloc[i+1]["datetime"] if i+1 < n_w else t_w_start + 604800 * 1e9

        # Daily bars in this week
        mask_d = (kd["datetime"] >= t_w_start) & (kd["datetime"] < t_w_end)
        idx_d_list = kd.index[mask_d].tolist()

        for j_pos in idx_d_list:
            s2 = determine_screen2_signal(s1["trend"], kd.iloc[:j_pos+1])

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

            t_d_start = kd.iloc[j_pos]["datetime"]
            t_d_end   = kd.iloc[j_pos+1]["datetime"] if j_pos+1 < n_d else t_d_start + 86400 * 1e9

            # 15min bars in this day
            mask_15 = (k15["datetime"] >= t_d_start) & (k15["datetime"] < t_d_end)
            idx_15_list = k15.index[mask_15].tolist()

            for k_pos in idx_15_list:
                total_15m_bars += 1
                slice_15 = k15.iloc[:k_pos+1]
                curr_bar = k15.iloc[k_pos]
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
                    s1_now = s1_cache.get(i, s1)  # same week, same S1
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
                    s2_now = determine_screen2_signal(s1_now["trend"], kd.iloc[:j_pos+1])
                    opp_div = (
                        (position.direction == "long" and s2_now["signal"] == "divergence_sell")
                        or (position.direction == "short" and s2_now["signal"] == "divergence_buy")
                    )
                    if opp_div:
                        _close_trade(stats, position, curr_close, curr_time,
                                     "opposite_divergence")
                        position = None
                        continue

                    # 4. Trailing stop update (use STOP_BARS completed 15min bars)
                    n_stop = min(STOP_BARS, k_pos)  # bars before current
                    if n_stop > 0:
                        if position.direction == "long":
                            completed_lows = k15["low"].iloc[k_pos - n_stop:k_pos]
                            new_stop = completed_lows.min()
                            if new_stop > position.current_stop:
                                position.current_stop = new_stop
                        else:
                            completed_highs = k15["high"].iloc[k_pos - n_stop:k_pos]
                            new_stop = completed_highs.max()
                            if new_stop < position.current_stop:
                                position.current_stop = new_stop

                    continue  # holding, next bar

                # ── 空仓：入场检查 ──
                s3 = determine_screen3_entry(s1["trend"], s2["signal"],
                                             slice_15, tick_size=TICK_SIZE)

                # Track events
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

    # ── 强制平仓：遍历结束后仍有持仓 ──
    if position is not None and position.status == "open":
        last_bar = k15.iloc[-1]
        _close_trade(stats, position, last_bar["close"],
                     last_bar["datetime"], "end_of_data")

    _print_results(stats, s1_bullish, s1_bearish, s1_neutral,
                   s2_buy, s2_sell, s2_no, s3_events, total_15m_bars)
    return stats


def _close_trade(stats, position, exit_price, exit_time, reason):
    position.status = "closed"
    trade = Trade(position, exit_price, exit_time, reason, position.bars_held)
    stats.add_trade(trade)
    return trade


def _print_results(stats, s1_b, s1_r, s1_n, s2_buy, s2_sell, s2_no, s3_ev, total_bars):
    print(f"\n{'='*72}")
    print(f"  CF609 Triple Screen 级联回测 — P&L 报告")
    print(f"{'='*72}")

    # ── 级别统计 ──
    print(f"\n── Screen 1 (周线) 趋势分布 ──")
    total_w = s1_b + s1_r + s1_n
    print(f"  看涨 {s1_b:5d}  ({100*s1_b/max(1,total_w):5.1f}%)")
    print(f"  看跌 {s1_r:5d}  ({100*s1_r/max(1,total_w):5.1f}%)")
    print(f"  中性 {s1_n:5d}  ({100*s1_n/max(1,total_w):5.1f}%)")

    print(f"\n── Screen 2 (日线) 信号 ──")
    print(f"  买入信号: {s2_buy:5d}")
    print(f"  卖出信号: {s2_sell:5d}")
    print(f"  无信号:   {s2_no:5d}")

    print(f"\n── Screen 3 (15min) 触发 ──")
    print(f"  triggered_long:   {s3_ev['triggered_long']:5d}")
    print(f"  triggered_short:  {s3_ev['triggered_short']:5d}")
    print(f"  pending_long:     {s3_ev['pending_long']:5d}")
    print(f"  pending_short:    {s3_ev['pending_short']:5d}")
    print(f"  总 15min bar 数:  {total_bars}")

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
    print(f"  平均持仓 bar:   {stats.avg_bars_held:5.1f}  (15min)")

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
