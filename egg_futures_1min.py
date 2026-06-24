"""
Triple Screen 核心指标库 + 鸡蛋期货单合约监控
=============================================
提供三重滤网全部指标计算函数（与时间周期无关）:
  - calc_macd / calc_ema / calc_force_index
  - determine_screen1_trend   (趋势方向过滤)
  - determine_screen2_signal  (FI + 价格双重回调确认)
  - determine_screen3_entry   (精确入场 + 止损)
  - Position / update_position (持仓跟踪 + 退出规则)

也包含鸡蛋主力 1/5/25min 单合约终端监控入口 (main)。
多合约多周期监控请使用 triple_screen_monitor.py。

用法:
    pip install tqsdk
    python egg_futures_1min.py        # 鸡蛋单合约终端监控
    python triple_screen_monitor.py   # 多合约多周期监控 (推荐)
"""

from tqsdk import TqApi, TqAuth
import pandas as pd
import re
from datetime import datetime, time
import time as _time
from config_loader import get_tqsdk_auth


# ── 三周期配置 ──────────────────────────────────────────
KLINE_DURS = {"1min": 60, "5min": 300, "25min": 1500}
DATA_LEN   = 200            # 每个周期保留最近 200 根
SHOW_N     = 8              # 打印最近 N 根
WAIT_SEC   = 3              # 盘后 wait_update 超时秒数
STATUS_SLOT = 30            # 盘后状态刷新间隔（秒）
STOP_LOOKBACK = 5           # 初始止损 + 追踪止损回看 bar 数（1min）

# DCE 鸡蛋期货交易时段（日盘，无夜盘）
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]

# ── 持仓跟踪状态（跨 print_all_periods 调用持久化） ──
_POSITION = None           # Position 实例或 None
_POSITION_EVENTS = []      # 滚动事件日志（保留最近 20 条）


def is_trading_time():
    now = datetime.now().time()
    return any(start <= now <= end for start, end in TRADING_SESSIONS)


def next_trading_time():
    now = datetime.now().time()
    for start, end in TRADING_SESSIONS:
        if now < start:
            return f"{start:%H:%M}"
    return "次日 09:00"


def discover_main_contract_generic(api, exchange_id, product_code):
    """通用主力合约发现 —— 按持仓量最大

    Args:
        api:           TqApi 实例
        exchange_id:   交易所代码 (DCE / CZCE)
        product_code:  品种代码 (jd / lh / c / CF / CJ …)

    Returns:
        str: 类似 "DCE.jd2605" / "CZCE.CF609", 找不到时返回 None

    匹配规则: 正则 ^exchange.product_code + 数字$ ，避免例如
    DCE.c 误匹配 DCE.cs（淀粉）等情况。
    """
    quotes = api.query_quotes(ins_class="FUTURE", exchange_id=exchange_id, expired=False)

    # 精确匹配: EXCHANGE.PRODUCT_CODE + 至少一位数字
    pattern = re.compile(
        rf'^{re.escape(exchange_id)}\.{re.escape(product_code)}\d+$',
        re.IGNORECASE,
    )
    contracts = sorted([q for q in quotes if pattern.match(q)])

    if not contracts:
        print(f"  ⚠️  {exchange_id}.{product_code} 未找到任何合约")
        return None
    if len(contracts) == 1:
        return contracts[0]

    # ── 按持仓量选最大 ──
    contract_quotes = {c: api.get_quote(c) for c in contracts}
    deadline = _time.time() + 5
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())

    best_code = contracts[0]
    best_oi = 0
    for code in contracts:
        q = contract_quotes[code]
        oi = getattr(q, "open_interest", 0) or 0
        if oi > best_oi:
            best_oi = oi
            best_code = code

    if best_oi == 0:
        return contracts[0]

    print(f"  主力合约: {best_code} (持仓 {int(best_oi)} 手)")
    others = sorted(
        [(c, getattr(contract_quotes[c], "open_interest", 0) or 0) for c in contracts],
        key=lambda x: x[1], reverse=True,
    )
    if len(others) > 1:
        print(f"  其他: " + ", ".join(f"{c}({int(oi)})" for c, oi in others[1:5]))

    return best_code


def discover_main_contract(api):
    """按持仓量确定鸡蛋主力合约 (保持向后兼容)"""
    return discover_main_contract_generic(api, "DCE", "jd")


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%m-%d %H:%M")
    return "---"


def calc_macd(klines, fast=12, slow=26, signal=9):
    """计算 MACD：返回 (EMA12, EMA26, DIF, DEA, MACD柱) 五个 Series"""
    closes = klines["close"]
    ema12  = closes.ewm(span=fast, adjust=False).mean()
    ema26  = closes.ewm(span=slow, adjust=False).mean()
    dif    = ema12 - ema26
    dea    = dif.ewm(span=signal, adjust=False).mean()
    bar    = 2 * (dif - dea)
    return ema12, ema26, dif, dea, bar


def calc_ema(klines, span=13):
    """计算 EMA：返回 EMA Series"""
    closes = klines["close"]
    ema = closes.ewm(span=span, adjust=False).mean()
    return ema


def calc_force_index(klines, ema_span=2):
    """
    计算 Force Index（力度指数）并用 EMA 平滑

    Force Index = (close - close.shift(1)) * volume
    FI_EMA = EMA(ema_span) of Force Index
    """
    closes = klines["close"]
    volumes = klines["volume"]
    price_change = closes - closes.shift(1)
    raw_fi = price_change * volumes
    fi_ema = raw_fi.ewm(span=ema_span, adjust=False).mean()
    return fi_ema


def determine_screen1_trend(klines, hist_lookback=2, ema_lookback=10,
                              ema_threshold_ratio=0.0005):
    """
    Elder's Triple Screen — Screen 1: 长期趋势方向过滤

    使用 MACD 柱状图斜率作为主信号，EMA(13) 斜率作为确认。
    当两者一致时返回明确的趋势方向；冲突时返回 neutral（不允许交易）。

    Returns:
        dict: trend / hist_slope / ema_slope / hist_recent / ema_recent
    """
    _, _, dif, dea, bar = calc_macd(klines, fast=12, slow=26, signal=9)
    ema13 = calc_ema(klines, span=13)

    required_len = max(hist_lookback, ema_lookback) + 1
    if len(bar.dropna()) < required_len or len(ema13.dropna()) < required_len:
        return {
            "trend": "neutral", "hist_slope": "flat", "ema_slope": "flat",
            "hist_recent": [], "ema_recent": (0.0, 0.0),
        }

    # ── MACD histogram slope (2-bar lookback, Elder: each bar vs previous) ──
    recent_hist = bar.iloc[-hist_lookback:].dropna()
    if len(recent_hist) < hist_lookback:
        hist_slope = "flat"
    else:
        bar_current = recent_hist.iloc[-1]
        bar_past = recent_hist.iloc[0]
        diff = bar_current - bar_past
        avg_magnitude = recent_hist.abs().mean()
        threshold = avg_magnitude * 0.05
        if diff > threshold:
            hist_slope = "rising"
        elif diff < -threshold:
            hist_slope = "falling"
        else:
            hist_slope = "flat"

    # ── EMA(13) slope (10-bar lookback) ──
    ema_current = ema13.iloc[-1]
    ema_past = ema13.iloc[-ema_lookback]
    ema_diff = ema_current - ema_past
    avg_price = klines["close"].iloc[-ema_lookback:].mean()
    ema_threshold = avg_price * ema_threshold_ratio
    if ema_diff > ema_threshold:
        ema_slope = "rising"
    elif ema_diff < -ema_threshold:
        ema_slope = "falling"
    else:
        ema_slope = "flat"

    # ── Trend determination ──
    if hist_slope == "rising" and ema_slope in ("rising", "flat"):
        trend = "bullish"
    elif hist_slope == "falling" and ema_slope in ("falling", "flat"):
        trend = "bearish"
    else:
        trend = "neutral"

    hist_recent = [round(bar.iloc[-i], 4) for i in range(hist_lookback, 0, -1)]
    return {
        "trend": trend, "hist_slope": hist_slope, "ema_slope": ema_slope,
        "hist_recent": hist_recent,
        "ema_recent": (round(ema_current, 2), round(ema_past, 2)),
    }


def _find_local_extrema(series, window=3, mode="max"):
    """查找序列中的局部极值点索引"""
    extrema = []
    for i in range(window, len(series) - window):
        segment = series.iloc[i - window:i + window + 1]
        if mode == "max" and series.iloc[i] == segment.max():
            extrema.append(i)
        elif mode == "min" and series.iloc[i] == segment.min():
            extrema.append(i)
    return extrema


def determine_screen2_signal(screen1_trend, klines_s2, lookback=20, swing_window=3,
                              price_ema_span=5):
    """
    Elder's Triple Screen — Screen 2: 中期振荡器回调信号

    当 Screen 1 有明确方向时，三步验证逆势回调：
    1. Force Index EMA(2) 方向 —— 空头/多头力量确认
    2. 价格 vs 短期EMA —— 价格是否确实回抽（存在性确认）
    3. FI 背离 —— 卖压/买压是否衰竭（到位确认）

    只有 FI 和价格同时满足条件，才发出有效 Screen 2 信号。

    Returns:
        dict: signal / fi_value / fi_recent / fi_above_zero /
              zero_cross / divergence / price_confirmed / pullback_desc
    """
    no_signal = {
        "signal": "no_signal", "fi_value": 0.0, "fi_recent": [],
        "fi_above_zero": True, "zero_cross": "none",
        "divergence": "none", "price_confirmed": False,
        "pullback_desc": "Screen 1 趋势不明 → 无回调信号",
    }

    if screen1_trend == "neutral":
        return no_signal

    fi = calc_force_index(klines_s2, ema_span=2)
    recent_fi = fi.iloc[-lookback:].dropna()
    recent_close = klines_s2["close"].iloc[-lookback:]

    if len(recent_fi) < 5:
        no_signal["pullback_desc"] = "Force Index 数据不足"
        return no_signal

    # ── Current FI state ──
    fi_value = recent_fi.iloc[-1]
    fi_above_zero = fi_value > 0

    # ── Price pullback check: close vs short EMA ──
    close_series = klines_s2["close"]
    close_ema = close_series.ewm(span=price_ema_span, adjust=False).mean()
    latest_close = close_series.iloc[-1]
    latest_ema = close_ema.iloc[-1]
    price_below_ema = latest_close < latest_ema   # 多头回调确认
    price_above_ema = latest_close > latest_ema   # 空头反弹确认

    # ── Zero-crossing detection (last 2 bars) ──
    prev_fi = recent_fi.iloc[-2]
    if prev_fi >= 0 and fi_value < 0:
        zero_cross = "crossed_below"
    elif prev_fi <= 0 and fi_value > 0:
        zero_cross = "crossed_above"
    else:
        zero_cross = "none"

    # ── Divergence detection ──
    divergence = "none"
    fi_for_div = fi.iloc[-lookback:].dropna()
    close_for_div = klines_s2["close"].iloc[-lookback:]

    # Align lengths
    min_len = min(len(fi_for_div), len(close_for_div))
    fi_for_div = fi_for_div.iloc[-min_len:]
    close_for_div = close_for_div.iloc[-min_len:]

    if min_len >= 2 * swing_window + 1:
        # Bullish divergence: price lower low + FI higher low
        troughs_fi = _find_local_extrema(fi_for_div, window=swing_window, mode="min")
        troughs_price = _find_local_extrema(close_for_div, window=swing_window, mode="min")
        if len(troughs_fi) >= 2 and len(troughs_price) >= 2:
            if (close_for_div.iloc[troughs_price[-1]] < close_for_div.iloc[troughs_price[-2]]
                    and fi_for_div.iloc[troughs_fi[-1]] > fi_for_div.iloc[troughs_fi[-2]]):
                divergence = "bullish"

        # Bearish divergence: price higher high + FI lower high
        peaks_fi = _find_local_extrema(fi_for_div, window=swing_window, mode="max")
        peaks_price = _find_local_extrema(close_for_div, window=swing_window, mode="max")
        if len(peaks_fi) >= 2 and len(peaks_price) >= 2:
            if (close_for_div.iloc[peaks_price[-1]] > close_for_div.iloc[peaks_price[-2]]
                    and fi_for_div.iloc[peaks_fi[-1]] < fi_for_div.iloc[peaks_fi[-2]]):
                divergence = "bearish"

    # ── Signal determination (FI + Price 双重确认) ──
    price_confirmed = False
    if screen1_trend == "bullish":
        if fi_value < 0:
            if price_below_ema:
                # FI确认空头力量 + 价格确认回抽 → 有效回调信号
                price_confirmed = True
                if divergence == "bullish":
                    signal = "divergence_buy"
                    pullback_desc = (f"多头回调: FI为负(卖压在) + 收盘{latest_close:.0f}低于"
                                     f"EMA{price_ema_span}({latest_ema:.0f}) → 价格已回抽 "
                                     f"┃ FI底背离 → 卖压衰竭，回调到位")
                else:
                    signal = "buy_signal"
                    pullback_desc = (f"多头回调: FI为负(卖压在) + 收盘{latest_close:.0f}低于"
                                     f"EMA{price_ema_span}({latest_ema:.0f}) → 价格已回抽，回调买入机会")
            else:
                # FI为负但价格未跌破短期EMA → 回调力度不足
                signal = "no_signal"
                pullback_desc = (f"多头趋势FI为负，但收盘{latest_close:.0f}≥EMA{price_ema_span}"
                                 f"({latest_ema:.0f}) → 价格未真正回抽，回调信号不成立")
        else:
            signal = "no_signal"
            pullback_desc = "多头趋势中FI为正 → 趋势延续，无回调机会"
    elif screen1_trend == "bearish":
        if fi_value > 0:
            if price_above_ema:
                # FI确认多头力量 + 价格确认反弹 → 有效反弹信号
                price_confirmed = True
                if divergence == "bearish":
                    signal = "divergence_sell"
                    pullback_desc = (f"空头反弹: FI为正(买压在) + 收盘{latest_close:.0f}高于"
                                     f"EMA{price_ema_span}({latest_ema:.0f}) → 价格已反弹 "
                                     f"┃ FI顶背离 → 买压衰竭，反弹到位")
                else:
                    signal = "sell_signal"
                    pullback_desc = (f"空头反弹: FI为正(买压在) + 收盘{latest_close:.0f}高于"
                                     f"EMA{price_ema_span}({latest_ema:.0f}) → 价格已反弹，反弹卖出机会")
            else:
                # FI为正但价格未突破短期EMA → 反弹力度不足
                signal = "no_signal"
                pullback_desc = (f"空头趋势FI为正，但收盘{latest_close:.0f}≤EMA{price_ema_span}"
                                 f"({latest_ema:.0f}) → 价格未真正反弹，反弹信号不成立")
        else:
            signal = "no_signal"
            pullback_desc = "空头趋势中FI为负 → 趋势延续，无反弹机会"
    else:
        signal = "no_signal"
        pullback_desc = "Screen 1 趋势不明 → 无回调信号"

    fi_recent = [round(fi.iloc[-i], 2) for i in range(min(5, len(fi.dropna())), 0, -1)]
    return {
        "signal": signal, "fi_value": round(fi_value, 2),
        "fi_recent": fi_recent, "fi_above_zero": fi_above_zero,
        "zero_cross": zero_cross, "divergence": divergence,
        "price_confirmed": price_confirmed,
        "pullback_desc": pullback_desc,
    }


def determine_screen3_entry(screen1_trend, screen2_signal, klines_1min, tick_size=1):
    """
    Elder's Triple Screen — Screen 3: 精确入场信号

    当 Screen 1+2 一致时，在1分钟图上使用追踪止损确定入场位：
    - 做多: 买入止损 = 前一根1分钟K线最高价 + tick_size
    - 做空: 卖出止损 = 前一根1分钟K线最低价 - tick_size
    - 止损: 做多取2根K线最低价, 做空取2根K线最高价

    Returns:
        dict: signal / entry_price / stop_loss / prev_high / prev_low / desc
    """
    no_entry = {
        "signal": "none", "entry_price": 0.0, "stop_loss": 0.0,
        "prev_high": 0.0, "prev_low": 0.0,
        "desc": "Screens 1+2 未一致 → 无入场信号",
    }

    if len(klines_1min) < 2 or klines_1min.iloc[-1]["close"] <= 0:
        return no_entry

    prev_bar = klines_1min.iloc[-2]
    curr_bar = klines_1min.iloc[-1]
    prev_high = prev_bar["high"]
    prev_low = prev_bar["low"]
    curr_close = curr_bar["close"]

    # ── 初始止损：回看 STOP_LOOKBACK 根 bar 的低点/高点 ──
    n = min(STOP_LOOKBACK, len(klines_1min))
    recent_lows  = klines_1min["low"].iloc[-n:]
    recent_highs = klines_1min["high"].iloc[-n:]

    # ── LONG signal check ──
    if screen1_trend == "bullish" and screen2_signal in ("buy_signal", "divergence_buy"):
        entry_price = prev_high + tick_size
        stop_loss = recent_lows.min()
        if curr_close >= entry_price:
            return {
                "signal": "triggered_long", "entry_price": entry_price,
                "stop_loss": stop_loss, "prev_high": prev_high, "prev_low": prev_low,
                "desc": f"做多触发! 价格{curr_close:.0f}>=买入止损{entry_price:.0f} | 止损:{stop_loss:.0f}",
            }
        else:
            return {
                "signal": "pending_long", "entry_price": entry_price,
                "stop_loss": stop_loss, "prev_high": prev_high, "prev_low": prev_low,
                "desc": f"待做多: 买入止损 {entry_price:.0f} (前高{prev_high:.0f}+{tick_size}) | 止损:{stop_loss:.0f}",
            }

    # ── SHORT signal check ──
    if screen1_trend == "bearish" and screen2_signal in ("sell_signal", "divergence_sell"):
        entry_price = prev_low - tick_size
        stop_loss = recent_highs.max()
        if curr_close <= entry_price:
            return {
                "signal": "triggered_short", "entry_price": entry_price,
                "stop_loss": stop_loss, "prev_high": prev_high, "prev_low": prev_low,
                "desc": f"做空触发! 价格{curr_close:.0f}<=卖出止损{entry_price:.0f} | 止损:{stop_loss:.0f}",
            }
        else:
            return {
                "signal": "pending_short", "entry_price": entry_price,
                "stop_loss": stop_loss, "prev_high": prev_high, "prev_low": prev_low,
                "desc": f"待做空: 卖出止损 {entry_price:.0f} (前低{prev_low:.0f}-{tick_size}) | 止损:{stop_loss:.0f}",
            }

    # ── Screen 1 reversed but Screen 2 had signal → cancelled ──
    if screen2_signal in ("buy_signal", "divergence_buy") and screen1_trend != "bullish":
        return {
            "signal": "cancelled", "entry_price": 0.0, "stop_loss": 0.0,
            "prev_high": prev_high, "prev_low": prev_low,
            "desc": "Screen 1趋势反转 → 买入信号取消",
        }
    if screen2_signal in ("sell_signal", "divergence_sell") and screen1_trend != "bearish":
        return {
            "signal": "cancelled", "entry_price": 0.0, "stop_loss": 0.0,
            "prev_high": prev_high, "prev_low": prev_low,
            "desc": "Screen 1趋势反转 → 卖出信号取消",
        }

    return no_entry


# ── 持仓跟踪：Exit Rules + Stop-Loss Trailing ──────────────────────
class Position:
    """
    纸面持仓（paper trading）。不发送真实订单，仅用于监控/可视化。

    字段：
        direction      : "long" / "short"
        entry_price    : 入场价 (Screen 3 触发价)
        entry_time     : 入场时间 (ns)
        initial_stop   : 入场时设置的初始止损
        current_stop   : 当前追踪止损（只能顺向移动）
        entry_signal   : 入场时 Screen 2 信号 (buy_signal/divergence_buy/sell_signal/divergence_sell)
        peak_profit    : 最大有利偏移 (MFE)
        bars_held      : 持仓 bar 数
        status         : "open" / "closed"
        exit_price     : 平仓价
        exit_time      : 平仓时间 (ns)
        exit_reason    : "stop_hit" / "s1_reversal(<trend>)" / "opposite_divergence" / "end_of_data"
    """

    def __init__(self, direction, entry_price, entry_time, initial_stop, entry_signal):
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.initial_stop = initial_stop
        self.current_stop = initial_stop
        self.entry_signal = entry_signal
        self.peak_profit = 0.0
        self.bars_held = 0
        self.status = "open"
        self.exit_price = 0.0
        self.exit_time = 0
        self.exit_reason = ""

    def unrealized_pnl(self, current_price):
        if self.status != "open":
            return 0.0
        if self.direction == "long":
            return current_price - self.entry_price
        return self.entry_price - current_price

    def realized_pnl(self):
        if self.status != "closed":
            return 0.0
        if self.direction == "long":
            return self.exit_price - self.entry_price
        return self.entry_price - self.exit_price


def update_position(position, screen1_trend, screen2_signal, klines_1min, events):
    """
    根据最新 1min bar 更新纸面持仓。返回 (possibly new) position。

    Exit 优先级（持仓时按顺序检查）:
        1. stop_hit       : 1min 突破 current_stop → 平仓
        2. s1_reversal    : Screen 1 趋势不再支持持仓 → 平仓
        3. opposite_divergence : Screen 2 出现反向背离 → 平仓
        4. trailing       : 仍持仓则更新追踪止损（只能顺向移动）

    Entry（空仓时）:
        Screen 3 触发 triggered_long/triggered_short → 开新仓
    """
    if len(klines_1min) < STOP_LOOKBACK + 1:
        return position

    curr_bar = klines_1min.iloc[-1]
    curr_high = curr_bar["high"]
    curr_low = curr_bar["low"]
    curr_close = curr_bar["close"]
    curr_time = curr_bar["datetime"]

    # ── EXIT CHECKS ──
    if position is not None and position.status == "open":
        position.bars_held += 1

        # Update peak profit (MFE)
        if position.direction == "long":
            position.peak_profit = max(position.peak_profit, curr_high - position.entry_price)
        else:
            position.peak_profit = max(position.peak_profit, position.entry_price - curr_low)

        # 1. Stop-loss hit (intraday extreme)
        if position.direction == "long" and curr_low <= position.current_stop:
            position.status = "closed"
            position.exit_price = position.current_stop
            position.exit_time = curr_time
            position.exit_reason = "stop_hit"
            events.append({
                "type": "exit", "reason": "stop_hit",
                "direction": position.direction,
                "entry_price": position.entry_price, "exit_price": position.exit_price,
                "pnl": position.realized_pnl(), "bars_held": position.bars_held,
                "time": curr_time,
            })
            return position

        if position.direction == "short" and curr_high >= position.current_stop:
            position.status = "closed"
            position.exit_price = position.current_stop
            position.exit_time = curr_time
            position.exit_reason = "stop_hit"
            events.append({
                "type": "exit", "reason": "stop_hit",
                "direction": position.direction,
                "entry_price": position.entry_price, "exit_price": position.exit_price,
                "pnl": position.realized_pnl(), "bars_held": position.bars_held,
                "time": curr_time,
            })
            return position

        # 2. Screen 1 trend reversal (also exits on neutral — tide no longer favorable)
        s1_unfavorable = (
            (position.direction == "long" and screen1_trend != "bullish")
            or (position.direction == "short" and screen1_trend != "bearish")
        )
        if s1_unfavorable:
            position.status = "closed"
            position.exit_price = curr_close
            position.exit_time = curr_time
            position.exit_reason = f"s1_reversal({screen1_trend})"
            events.append({
                "type": "exit", "reason": position.exit_reason,
                "direction": position.direction,
                "entry_price": position.entry_price, "exit_price": position.exit_price,
                "pnl": position.realized_pnl(), "bars_held": position.bars_held,
                "time": curr_time,
            })
            return position

        # 3. Opposite divergence exit
        opposite_div = (
            (position.direction == "long" and screen2_signal == "divergence_sell")
            or (position.direction == "short" and screen2_signal == "divergence_buy")
        )
        if opposite_div:
            position.status = "closed"
            position.exit_price = curr_close
            position.exit_time = curr_time
            position.exit_reason = "opposite_divergence"
            events.append({
                "type": "exit", "reason": "opposite_divergence",
                "direction": position.direction,
                "entry_price": position.entry_price, "exit_price": position.exit_price,
                "pnl": position.realized_pnl(), "bars_held": position.bars_held,
                "time": curr_time,
            })
            return position

        # 4. Trailing stop update (N-bar low/high on completed bars, excludes forming bar)
        if position.direction == "long":
            n = min(STOP_LOOKBACK, len(klines_1min) - 1)
            completed_lows = klines_1min["low"].iloc[-n-1:-1]
            new_stop = completed_lows.min()
            if new_stop > position.current_stop:
                old_stop = position.current_stop
                position.current_stop = new_stop
                events.append({
                    "type": "trailing", "direction": "long",
                    "old_stop": old_stop, "new_stop": new_stop,
                    "time": curr_time,
                })
        else:  # short
            n = min(STOP_LOOKBACK, len(klines_1min) - 1)
            completed_highs = klines_1min["high"].iloc[-n-1:-1]
            new_stop = completed_highs.max()
            if new_stop < position.current_stop:
                old_stop = position.current_stop
                position.current_stop = new_stop
                events.append({
                    "type": "trailing", "direction": "short",
                    "old_stop": old_stop, "new_stop": new_stop,
                    "time": curr_time,
                })

        return position

    # ── ENTRY CHECK (flat) ──
    screen3 = determine_screen3_entry(screen1_trend, screen2_signal, klines_1min)
    if screen3["signal"] == "triggered_long":
        new_pos = Position(
            direction="long",
            entry_price=screen3["entry_price"],
            entry_time=curr_time,
            initial_stop=screen3["stop_loss"],
            entry_signal=screen2_signal,
        )
        events.append({
            "type": "entry", "direction": "long",
            "entry_price": new_pos.entry_price, "stop_loss": new_pos.initial_stop,
            "signal": screen2_signal, "time": curr_time,
        })
        return new_pos

    if screen3["signal"] == "triggered_short":
        new_pos = Position(
            direction="short",
            entry_price=screen3["entry_price"],
            entry_time=curr_time,
            initial_stop=screen3["stop_loss"],
            entry_signal=screen2_signal,
        )
        events.append({
            "type": "entry", "direction": "short",
            "entry_price": new_pos.entry_price, "stop_loss": new_pos.initial_stop,
            "signal": screen2_signal, "time": curr_time,
        })
        return new_pos

    return position


def print_macd_detail(klines, ema12, ema26, dif, dea, bar, fast=12, slow=26, signal=9):
    """展示 MACD 每一步的完整计算过程，方便用户手动验证"""
    alpha_fast  = 2 / (fast + 1)
    alpha_slow  = 2 / (slow + 1)
    alpha_sig   = 2 / (signal + 1)

    # 取最近 2 根有有效 close 的 bar
    valid = klines[klines["close"] > 0]
    if len(valid) < 2:
        return
    recent = valid.iloc[-2:]
    idx0, idx1 = recent.index[0], recent.index[1]
    close0, close1 = float(recent.iloc[0]["close"]), float(recent.iloc[1]["close"])
    t0, t1 = fmt_time(klines.loc[idx0, "datetime"]), fmt_time(klines.loc[idx1, "datetime"])

    e12_0, e12_1 = float(ema12.iloc[idx0]), float(ema12.iloc[idx1])
    e26_0, e26_1 = float(ema26.iloc[idx0]), float(ema26.iloc[idx1])
    d0, d1     = float(dif.iloc[idx0]), float(dif.iloc[idx1])
    e0, e1     = float(dea.iloc[idx0]), float(dea.iloc[idx1])
    b0, b1     = float(bar.iloc[idx0]), float(bar.iloc[idx1])

    if any(pd.isna(v) for v in [e12_0, e12_1, e26_0, e26_1, d0, d1, e0, e1]):
        return

    print(f"\n  ── MACD 详细计算 | α_fast=2/{fast+1}={alpha_fast:.4f}  "
          f"α_slow=2/{slow+1}={alpha_slow:.4f}  α_signal=2/{signal+1}={alpha_sig:.4f} ──")

    for bar_idx, (t, close, e12, e26, d, ea, b) in enumerate([
        (t0, close0, e12_0, e26_0, d0, e0, b0),
        (t1, close1, e12_1, e26_1, d1, e1, b1),
    ]):
        marker = "← 最新" if bar_idx == 1 else ""
        print(f"\n  [{t}] {marker}")
        print(f"    ┌─ 公式: EMA({fast}) = α_close × close + (1−α_close) × 上期EMA({fast})")
        print(f"    │ = {alpha_fast:.4f} × {close:.2f} + {1-alpha_fast:.4f} × {e12:.4f}  [递推]")
        print(f"    └─ EMA({fast}) = {e12:.2f}")

        print(f"    ┌─ 公式: EMA({slow}) = α_slow × close + (1−α_slow) × 上期EMA({slow})")
        print(f"    │ = {alpha_slow:.4f} × {close:.2f} + {1-alpha_slow:.4f} × {e26:.4f}  [递推]")
        print(f"    └─ EMA({slow}) = {e26:.2f}")

        print(f"    DIF = EMA({fast}) − EMA({slow}) = {e12:.2f} − {e26:.2f} = {d:.2f}")

        print(f"    ┌─ 公式: DEA = α_signal × DIF + (1−α_signal) × 上期DEA")
        print(f"    └─ = {alpha_sig:.4f} × {d:.2f} + {1-alpha_sig:.4f} × {ea:.4f}  [递推] = {ea:.2f}")

        bar_sign = "多头(红柱)" if b >= 0 else "空头(绿柱)"
        print(f"    MACD柱 = 2 × (DIF − DEA) = 2 × ({d:.2f} − {ea:.2f}) = 2 × {d-ea:.2f} = {b:.2f}  [{bar_sign}]")

    print(f"\n  ──────────────────────────────")
    print(f"  速查: α_fast={alpha_fast:.4f}  α_slow={alpha_slow:.4f}  α_signal={alpha_sig:.4f}")
    print(f"  递推初始值: EMA({fast})≈{e12_0:.2f}  EMA({slow})≈{e26_0:.2f}  DEA≈{e0:.2f}")


def print_macd_summary(klines):
    """在 25 分钟 K 线下方打印 MACD 指标 + Elder Screen 1 趋势方向"""
    ema12, ema26, dif, dea, bar = calc_macd(klines)
    screen1 = determine_screen1_trend(klines)

    recent_idx = klines.index[-4:]
    if len(recent_idx) < 2:
        return

    # ── Screen 1 趋势方向（最醒目） ──
    trend = screen1["trend"]
    if trend == "bullish":
        trend_label = "\033[31m▲ 多头趋势 (仅做多)\033[0m"
        restriction = "\033[31m仅做多 → 禁止做空\033[0m"
        box_color = "\033[31m"
    elif trend == "bearish":
        trend_label = "\033[32m▼ 空头趋势 (仅做空)\033[0m"
        restriction = "\033[32m仅做空 → 禁止做多\033[0m"
        box_color = "\033[32m"
    else:
        trend_label = "\033[90m◆ 趋势不明 (不交易)\033[0m"
        restriction = "\033[90m不交易 → 等待方向明确\033[0m"
        box_color = "\033[90m"

    hist_slope_cn = {"rising": "上升", "falling": "下降", "flat": "平缓"}[screen1["hist_slope"]]
    ema_slope_cn = {"rising": "上升", "falling": "下降", "flat": "平缓"}[screen1["ema_slope"]]
    hist_vals_str = " → ".join(f"{v:.2f}" for v in screen1["hist_recent"])

    print()
    print(f"  {box_color}╔══════════════════════════════════════════╗\033[0m")
    print(f"  {box_color}║  Screen 1 (25min) 趋势方向过滤          ║\033[0m")
    print(f"  {box_color}║  {trend_label:<34}║\033[0m")
    print(f"  {box_color}║  {restriction:<34}║\033[0m")
    print(f"  {box_color}╚══════════════════════════════════════════╝\033[0m")
    print(f"  MACD柱斜率: {hist_slope_cn}  |  近5根: {hist_vals_str}")
    # ── Screen 1 斜率计算详情 ──
    hist_list = screen1["hist_recent"]
    if len(hist_list) >= 2:
        bar_first = hist_list[0]
        bar_last  = hist_list[-1]
        diff = bar_last - bar_first
        avg_mag = sum(abs(v) for v in hist_list) / len(hist_list)
        threshold = avg_mag * 0.05
        print(f"    公式: diff = bar_last({bar_last:.4f}) − bar_first({bar_first:.4f}) = {diff:.4f}")
        print(f"    阈值 = avg(|bars|) × 5% = {avg_mag:.4f} × 0.05 = {threshold:.4f}")
        print(f"    判定: {'diff > 阈值 → rising' if diff > threshold else 'diff < −阈值 → falling' if diff < -threshold else '|diff| ≤ 阈值 → flat'} → {hist_slope_cn}")

    ema_cur, ema_past = screen1["ema_recent"]
    print(f"  EMA(13): 当前={ema_cur:.2f}  |  斜率: {ema_slope_cn}  |  10根前={ema_past:.2f}")
    # ── EMA(13) 斜率计算详情 ──
    ema_diff = ema_cur - ema_past
    avg_price_10 = float(klines["close"].iloc[-10:].mean()) if len(klines) >= 10 else 0
    ema_threshold = avg_price_10 * 0.0005
    print(f"    公式: diff = EMA_cur({ema_cur:.2f}) − EMA_10ago({ema_past:.2f}) = {ema_diff:.4f}")
    print(f"    阈值 = avg_price({avg_price_10:.2f}) × 0.05% = {ema_threshold:.4f}")
    print(f"    判定: {'diff > 阈值 → rising' if ema_diff > ema_threshold else 'diff < −阈值 → falling' if ema_diff < -ema_threshold else '|diff| ≤ 阈值 → flat'} → {ema_slope_cn}")
    print(f"    综合: {'hist ' + hist_slope_cn + ' ∧ ema ' + ema_slope_cn + ' → ' + trend}")

    # ── MACD 详细数据 ──
    print(f"\n  ── 25分钟 MACD (12,26,9) ──")
    print(f"  {'时间':<16} {'DIF':>8} {'DEA':>8} {'MACD柱':>8} {'信号':>6}")
    print("  " + "-" * 58)

    for idx in recent_idx:
        if idx >= len(dif):
            break
        t = fmt_time(klines.loc[idx, "datetime"])
        d = dif.iloc[idx]
        e = dea.iloc[idx]
        b = bar.iloc[idx]
        if pd.isna(d) or pd.isna(e):
            continue
        if b >= 0:
            s = f"\033[31m▲ 多\033[0m"
        else:
            s = f"\033[32m▼ 空\033[0m"
        print(f"  {t:<16} {d:>8.2f} {e:>8.2f} {b:>8.2f} {s}")

    # 最新信号判断
    last_d = dif.iloc[-1]
    last_e = dea.iloc[-1]
    last_b = bar.iloc[-1]
    if pd.isna(last_d) or pd.isna(last_e):
        return
    if last_b > 0:
        summary = "DIF在DEA上方，多头主导"
    else:
        summary = "DIF在DEA下方，空头主导"
    if last_d > last_e and last_d > 0:
        summary += " | DIF>0且>DEA → 强势多头 ⚠️"
    elif last_d < last_e and last_d < 0:
        summary += " | DIF<0且<DEA → 强势空头 ⚠️"

    # 金叉/死叉检测
    if len(recent_idx) >= 2:
        d_prev = dif.iloc[-2]
        e_prev = dea.iloc[-2]
        d_cur  = dif.iloc[-1]
        e_cur  = dea.iloc[-1]
        if d_prev <= e_prev and d_cur > e_cur:
            summary += " | 🆕 金叉!"
        elif d_prev >= e_prev and d_cur < e_cur:
            summary += " | 🆕 死叉!"

    print(f"  → {summary}")
    print()

    # ── 详细计算步骤（可复制到 Excel 验证） ──
    print_macd_detail(klines, ema12, ema26, dif, dea, bar)


def print_screen2_signal(screen1_trend, klines_s2):
    """在 Screen 2 周期 K 线下方打印 Force Index + Elder Screen 2 回调信号"""
    screen2 = determine_screen2_signal(screen1_trend, klines_s2)
    signal = screen2["signal"]

    # ── Signal display styling ──
    if signal in ("buy_signal", "divergence_buy"):
        signal_label = "\033[31m▲ 回调买入 (FI<0 多头回调)\033[0m"
        box_color = "\033[31m"
    elif signal in ("sell_signal", "divergence_sell"):
        signal_label = "\033[32m▼ 反弹卖出 (FI>0 空头反弹)\033[0m"
        box_color = "\033[32m"
    else:
        signal_label = "\033[90m◆ 无回调信号\033[0m"
        box_color = "\033[90m"

    # Divergence badge
    div_text = ""
    if signal == "divergence_buy":
        div_text = "\033[1;31m ★ 底背离 ★\033[0m"
    elif signal == "divergence_sell":
        div_text = "\033[1;32m ★ 顶背离 ★\033[0m"

    # FI position
    fi_pos = "零轴上方 (多方主导)" if screen2["fi_above_zero"] else "零轴下方 (空方主导)"
    fi_vals_str = " → ".join(f"{v:.0f}" for v in screen2["fi_recent"])

    # Zero cross
    cross_cn = {"crossed_below": "刚下穿零轴 ↓", "crossed_above": "刚上穿零轴 ↑", "none": "—"}
    cross_text = cross_cn[screen2["zero_cross"]]

    print()
    print(f"  {box_color}╔══════════════════════════════════════════╗\033[0m")
    print(f"  {box_color}║  Screen 2 (5min) 振荡器回调信号          ║\033[0m")
    print(f"  {box_color}║  {signal_label:<30}{div_text}  ║\033[0m")
    print(f"  {box_color}╚══════════════════════════════════════════╝\033[0m")
    print(f"  Force Index EMA(2): {screen2['fi_value']:.0f}  |  {fi_pos}")
    print(f"  近5根FI: {fi_vals_str}  |  {cross_text}")
    # ── 价格回抽确认 ──
    price_tag = "✅ 价格确认回抽" if screen2.get("price_confirmed") else "❌ 价格未确认"
    print(f"  价格确认: {price_tag}")
    print(f"  → {screen2['pullback_desc']}")
    print()

    # ── Force Index 详细计算 ──
    fi = calc_force_index(klines_s2, ema_span=2)
    recent_bars = klines_s2[klines_s2["close"] > 0]
    if len(recent_bars) >= 3:
        print(f"  ── Force Index 详细计算 | α_ema=2/3=0.6667 ──")
        last3 = recent_bars.iloc[-3:]
        for j, (i, row) in enumerate(last3.iterrows()):
            t = fmt_time(row["datetime"])
            if j == 0:
                prev_close = "—"
                raw_fi = "—"
                fi_val = "—"
                prev_fi = "—"
            else:
                prev_idx = last3.index[j-1]
                prev_close = float(last3.iloc[j-1]["close"])
                raw_fi_val = (float(row["close"]) - prev_close) * float(row["volume"])
                raw_fi = f"{raw_fi_val:.0f}"
                fi_val = f"{fi.iloc[i]:.0f}" if not pd.isna(fi.iloc[i]) else "—"
                prev_fi_val = fi.iloc[prev_idx] if not pd.isna(fi.iloc[prev_idx]) else 0
                prev_fi = f"{prev_fi_val:.0f}"
            marker = " ← 当前" if j == 2 else ""
            print(f"    [{t}]{marker}  close={row['close']:.0f}  V={int(row['volume'])}  "
                  f"prev_close={prev_close}  raw_FI={raw_fi}  FI_EMA(2)={fi_val}  prev_FI={prev_fi}")
        print(f"    公式: raw_FI = (close−prev_close) × volume")
        print(f"    FI_EMA(2) = 0.6667×raw_FI + 0.3333×prev_FI  [递推]")
    print()


def print_screen3_entry(screen1_trend, screen2_signal, klines_1min):
    """在 1 分钟 K 线下方打印 Screen 3 精确入场信号"""
    screen3 = determine_screen3_entry(screen1_trend, screen2_signal, klines_1min)
    sig = screen3["signal"]

    # Color and label
    if sig in ("pending_long", "triggered_long"):
        box_color = "\033[31m"
        if sig == "triggered_long":
            signal_label = "\033[1;31m▲ 做多触发!\033[0m"
        else:
            signal_label = "\033[31m▲ 待做多 (买入止损)\033[0m"
    elif sig in ("pending_short", "triggered_short"):
        box_color = "\033[32m"
        if sig == "triggered_short":
            signal_label = "\033[1;32m▼ 做空触发!\033[0m"
        else:
            signal_label = "\033[32m▼ 待做空 (卖出止损)\033[0m"
    elif sig == "cancelled":
        box_color = "\033[90m"
        signal_label = "\033[90m◆ 信号取消\033[0m"
    else:
        box_color = "\033[90m"
        signal_label = "\033[90m◆ 无入场信号\033[0m"

    print()
    print(f"  {box_color}╔══════════════════════════════════════════╗\033[0m")
    print(f"  {box_color}║  Screen 3 (1min) 精确入场信号            ║\033[0m")
    print(f"  {box_color}║  {signal_label:<34}║\033[0m")
    print(f"  {box_color}╚══════════════════════════════════════════╝\033[0m")

    if sig in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
        direction = "买入止损" if "long" in sig else "卖出止损"
        print(f"  入场价: {screen3['entry_price']:.0f} ({direction})  |  止损: {screen3['stop_loss']:.0f}")
        print(f"  前一K线: H={screen3['prev_high']:.0f}  L={screen3['prev_low']:.0f}")
        # ── 入场 & 止损详细计算 ──
        tick = 1
        n = min(STOP_LOOKBACK, len(klines_1min))
        recent_lows  = klines_1min["low"].iloc[-n:]
        recent_highs = klines_1min["high"].iloc[-n:]
        print(f"    公式: {'买入止损 (long)' if 'long' in sig else '卖出止损 (short)'}")
        if "long" in sig:
            print(f"        = 前K线最高价 + tick = {screen3['prev_high']:.0f} + {tick} = {screen3['entry_price']:.0f}")
            print(f"    止损 = min({[f'{v:.0f}' for v in recent_lows]}) = {screen3['stop_loss']:.0f}")
        else:
            print(f"        = 前K线最低价 − tick = {screen3['prev_low']:.0f} − {tick} = {screen3['entry_price']:.0f}")
            print(f"    止损 = max({[f'{v:.0f}' for v in recent_highs]}) = {screen3['stop_loss']:.0f}")
        # Trigger check
        curr_close = klines_1min.iloc[-1]["close"]
        if "long" in sig:
            if curr_close >= screen3["entry_price"]:
                print(f"    触发: curr_close({curr_close:.0f}) >= entry({screen3['entry_price']:.0f}) → √ 触发")
            else:
                print(f"    未触发: curr_close({curr_close:.0f}) < entry({screen3['entry_price']:.0f})")
        else:
            if curr_close <= screen3["entry_price"]:
                print(f"    触发: curr_close({curr_close:.0f}) <= entry({screen3['entry_price']:.0f}) → √ 触发")
            else:
                print(f"    未触发: curr_close({curr_close:.0f}) > entry({screen3['entry_price']:.0f})")

    print(f"  → {screen3['desc']}")
    print()


def print_position_status(position, events):
    """打印持仓跟踪状态（入场后追踪止损 + 退出规则监控）"""
    # ── 决定颜色和标题 ──
    if position is None:
        box_color = "\033[90m"
        title = "持仓跟踪 (空仓等待)"
    elif position.status == "open":
        if position.direction == "long":
            box_color = "\033[31m"
            title = "持仓跟踪 ▲ 多头 (paper)"
        else:
            box_color = "\033[32m"
            title = "持仓跟踪 ▼ 空头 (paper)"
    else:  # closed
        pnl = position.realized_pnl()
        if pnl > 0:
            box_color = "\033[1;33m"  # yellow bold for win
            title = f"持仓跟踪 已平仓 (盈 +{pnl:.0f})"
        elif pnl < 0:
            box_color = "\033[1;34m"  # blue bold for loss
            title = f"持仓跟踪 已平仓 (亏 {pnl:.0f})"
        else:
            box_color = "\033[90m"
            title = "持仓跟踪 已平仓 (持平)"

    print(f"  {box_color}╔══════════════════════════════════════════╗\033[0m")
    print(f"  {box_color}║  {title:<34}║\033[0m")
    print(f"  {box_color}╚══════════════════════════════════════════╝\033[0m")

    # ── 持仓中：显示详细信息 ──
    if position is not None and position.status == "open":
        last_close = events[-1].get("last_close", 0) if events else 0
        # 取最近 1min 收盘从未使用的 klines 中（这里用 events 不够干净，改用 position 自身）
        # 改用 entry_price 与 current_stop 显示；PnL 用 current_stop 旁的最新 bar close 估算
        # 简化：仅显示已确定字段，PnL 在调用方传入
        print(f"  方向: {position.direction}  |  入场价: {position.entry_price:.0f}  "
              f"|  入场信号: {position.entry_signal}")
        print(f"  初始止损: {position.initial_stop:.0f}  |  当前止损(追踪): {position.current_stop:.0f}")
        print(f"  持仓 bar 数: {position.bars_held}  |  最大有利偏移: {position.peak_profit:.0f}")
        risk = (position.entry_price - position.current_stop
                if position.direction == "long"
                else position.current_stop - position.entry_price)
        print(f"  当前风险 (entry 到 stop): {risk:.0f} 点")

    # ── 已平仓：显示最近一笔 ──
    elif position is not None and position.status == "closed":
        print(f"  方向: {position.direction}  |  入场: {position.entry_price:.0f} → 平仓: {position.exit_price:.0f}")
        print(f"  退出原因: {position.exit_reason}  |  持仓 bar 数: {position.bars_held}")
        print(f"  已实现 PnL: {position.realized_pnl():.0f} 点  |  最大有利偏移: {position.peak_profit:.0f}")

    # ── 空仓：提示 ──
    else:
        print(f"  等待 Screen 1+2+3 一致触发入场信号")

    # ── 最近 5 条事件 ──
    if events:
        print(f"\n  最近事件 (最新在上):")
        for ev in reversed(events[-5:]):
            t = fmt_time(ev.get("time", 0))
            if ev["type"] == "entry":
                print(f"    [{t}] {ev['direction'].upper()} 入场 @ {ev['entry_price']:.0f}  "
                      f"止损 {ev['stop_loss']:.0f}  信号={ev['signal']}")
            elif ev["type"] == "exit":
                pnl_str = f"PnL {ev['pnl']:+.0f}"
                print(f"    [{t}] {ev['direction'].upper()} 平仓 @ {ev['exit_price']:.0f}  "
                      f"原因={ev['reason']}  {pnl_str}  ({ev['bars_held']} bars)")
            elif ev["type"] == "trailing":
                print(f"    [{t}] {ev['direction'].upper()} 追踪止损: "
                      f"{ev['old_stop']:.0f} → {ev['new_stop']:.0f}")
    print()


def print_period_bars(klines, symbol, label, n=SHOW_N):
    """打印单个周期最近 n 根 K 线"""
    print(f"\n  ── {label} ──")
    print(f"  {'时间':<16} {'开盘':>8} {'最高':>8} {'最低':>8} {'收盘':>8} {'成交量':>8}")
    print("  " + "-" * 70)

    recent = klines.iloc[-n:]
    for _, row in recent.iterrows():
        t = fmt_time(row["datetime"])
        o = f"{row['open']:.2f}"   if row["open"]  > 0 else "---"
        h = f"{row['high']:.2f}"   if row["high"]  > 0 else "---"
        l = f"{row['low']:.2f}"    if row["low"]   > 0 else "---"
        c = f"{row['close']:.2f}"  if row["close"] > 0 else "---"
        v = f"{int(row['volume'])}" if row["volume"] > 0 else "---"
        print(f"  {t:<16} {o:>8} {h:>8} {l:>8} {c:>8} {v:>8}")

    last = klines.iloc[-1]
    if last["close"] > 0 and last["open"] > 0:
        chg     = last["close"] - last["open"]
        chg_pct = chg / last["open"] * 100
        flag    = "▲" if chg >= 0 else "▼"
        print(f"  → 最新: {last['close']:.2f}  {flag} {abs(chg):.2f} ({abs(chg_pct):.2f}%)")


def print_all_periods(klines_map, symbol):
    """打印全部三个周期"""
    global _POSITION
    header = f"\n{'='*72}\n  鸡蛋主力 ({symbol})  1/5/25分钟K线  {datetime.now().strftime('%H:%M:%S')}\n{'='*72}"
    print(header)
    print_period_bars(klines_map["25min"], symbol, "25分钟 K线")
    # ── EMA(13) 值 ──
    ema13 = calc_ema(klines_map["25min"], span=13)
    last_ema = ema13.iloc[-1]
    if not pd.isna(last_ema):
        print(f"  EMA(13) = {last_ema:.2f}")
    print_macd_summary(klines_map["25min"])
    print_period_bars(klines_map["5min"],  symbol, "5分钟 K线")
    # ── Screen 2 — Force Index pullback signal on 5min ──
    screen1 = determine_screen1_trend(klines_map["25min"])
    print_screen2_signal(screen1["trend"], klines_map["5min"])
    print_period_bars(klines_map["1min"],  symbol, "1分钟 K线")
    # ── Screen 3 — Precise entry signal on 1min ──
    screen2 = determine_screen2_signal(screen1["trend"], klines_map["5min"])
    print_screen3_entry(screen1["trend"], screen2["signal"], klines_map["1min"])
    # ── 持仓跟踪：更新 + 显示 ──
    _POSITION = update_position(_POSITION, screen1["trend"], screen2["signal"],
                                klines_map["1min"], _POSITION_EVENTS)
    # 滚动保留最近 20 条事件
    if len(_POSITION_EVENTS) > 20:
        del _POSITION_EVENTS[:len(_POSITION_EVENTS) - 20]
    print_position_status(_POSITION, _POSITION_EVENTS)
    print()


def main():
    username, password = get_tqsdk_auth()
    print(f"正在连接天勤量化 (账号: {username})...")

    api = TqApi(auth=TqAuth(username, password))

    # ── 发现主力合约 ──
    symbol = discover_main_contract(api)
    print(f"鸡蛋主力合约: {symbol}")

    all_jd = sorted([q for q in api.query_quotes(
        ins_class="FUTURE", exchange_id="DCE", expired=False
    ) if "jd" in q.lower()])
    print(f"可用鸡蛋合约: {', '.join(all_jd)}")
    print(f"K线周期: 1min / 5min / 25min  |  按 Ctrl+C 退出")

    # ── 订阅三个周期 ──
    klines_map = {}
    for label, dur in KLINE_DURS.items():
        klines_map[label] = api.get_kline_serial(symbol, dur, data_length=DATA_LEN)

    # ── 等待初始数据就绪 ──
    print("正在获取初始数据...")
    deadline = _time.time() + 15
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())
        all_ready = all(
            len(k) > 0 and k.iloc[-1]["close"] > 0
            for k in klines_map.values()
        )
        if all_ready:
            break
    else:
        # 检查哪些周期缺数据
        missing = [label for label, k in klines_map.items()
                   if len(k) == 0 or k.iloc[-1]["close"] <= 0]
        if missing:
            print(f"\n⚠️  以下周期暂无K线数据: {', '.join(missing)}")
            api.close()
            return

    # ── 显示初始状态 ──
    now = datetime.now()
    if is_trading_time():
        print(f"\n🟢 交易中  →  等待行情推送...\n")
    else:
        print(f"\n⏸️  盘后    (下一时段: {next_trading_time()})  →  显示最后交易数据\n")

    print_all_periods(klines_map, symbol)

    # ── 哨兵：每个周期独立跟踪已处理的 datetime ──
    last_processed = {}
    for label, k in klines_map.items():
        last_processed[label] = k.iloc[-1]["datetime"]

    bar_count = {"1min": 0, "5min": 0, "25min": 0}
    last_status_print = 0.0

    # ── 核心更新循环 ──
    try:
        while True:
            if is_trading_time():
                api.wait_update()
            else:
                api.wait_update(deadline=_time.time() + WAIT_SEC)

            # 检测任意周期新 bar
            any_new_bar = False
            for label, klines in klines_map.items():
                cur_dt = klines.iloc[-1]["datetime"]
                if cur_dt > last_processed[label]:
                    last_processed[label] = cur_dt
                    bar_count[label] += 1
                    any_new_bar = True

            if any_new_bar:
                bar_info = ", ".join(
                    f"{label}#{bar_count[label]}"
                    for label in KLINE_DURS
                )
                print(f"[{bar_info}] ", end="")
                print_all_periods(klines_map, symbol)

            # 仅 1 分钟做实时 close 刷新
            elif is_trading_time():
                k1m = klines_map["1min"]
                cur_dt_1m = k1m.iloc[-1]["datetime"]
                if cur_dt_1m == last_processed["1min"] \
                        and api.is_changing(k1m.iloc[-1], "close"):
                    last = k1m.iloc[-1]
                    t = fmt_time(last["datetime"])
                    print(f"\r  1min实时 {t}  O:{last['open']:.2f}  H:{last['high']:.2f}"
                          f"  L:{last['low']:.2f}  C:{last['close']:.2f}"
                          f"  V:{int(last['volume'])}    ", end="", flush=True)

            # 盘后定期状态
            elif not is_trading_time():
                if _time.time() - last_status_print > STATUS_SLOT:
                    last_status_print = _time.time()
                    next_t = next_trading_time()
                    parts = []
                    for label, k in klines_map.items():
                        lb = k.iloc[-1]
                        bt = fmt_time(lb["datetime"])
                        parts.append(f"{label}:{bt} C:{lb['close']:.0f}")
                    print(f"\r  ⏸️  盘后 | {' | '.join(parts)}  | 等待 {next_t}...     ",
                          end="", flush=True)

    except KeyboardInterrupt:
        print("\n\n已退出。")
    finally:
        api.close()


if __name__ == "__main__":
    main()
