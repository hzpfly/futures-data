"""
鸡蛋主力合约 1/5/25分钟K线 图形监控
自动识别当前主力合约（持仓量最大），免费版天勤不支持 KQ.m@DCE.JD
周期: 1分钟 / 5分钟 / 25分钟 三周期并行
显示: 从上到下 25分钟 / 5分钟 / 1分钟 三行蜡烛图

用法:
    pip install tqsdk matplotlib
    python egg_futures_chart.py
"""

from tqsdk import TqApi, TqAuth
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import pandas as pd
import numpy as np
from datetime import datetime, time
import warnings
warnings.filterwarnings("ignore")

from config_loader import get_tqsdk_auth
from egg_futures_1min import Position, update_position
import time as _time


# ── 三周期配置 ──────────────────────────────────────────
KLINE_DURS = {"1min": 60, "5min": 300, "25min": 1500}
DATA_LEN   = 200
SHOW_N     = 60         # 图上显示最近 60 根
VIEW_SEC   = 60         # 盘后重绘间隔（秒）

# DCE 鸡蛋期货交易时段（日盘，无夜盘）
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]

api      = None
SYMBOL   = None
klines_map = {}           # {"1min": df, "5min": df, "25min": df}
_last_processed = {}      # 每个周期的哨兵 datetime
_last_draw_time = 0.0     # 上次重绘时间
_POSITION = None          # Position 实例（paper trading）
_POSITION_EVENTS = []     # 滚动事件日志


def is_trading_time():
    now = datetime.now().time()
    return any(start <= now <= end for start, end in TRADING_SESSIONS)


def next_trading_time():
    now = datetime.now().time()
    for start, end in TRADING_SESSIONS:
        if now < start:
            return f"{start:%H:%M}"
    return "次日 09:00"


def discover_main_contract(api):
    """按持仓量确定鸡蛋主力合约"""
    quotes = api.query_quotes(ins_class="FUTURE", exchange_id="DCE", expired=False)
    jd_contracts = sorted([q for q in quotes if "jd" in q.lower()])

    if not jd_contracts:
        return "DCE.jd2605"
    if len(jd_contracts) == 1:
        return jd_contracts[0]

    jd_quotes = {c: api.get_quote(c) for c in jd_contracts}
    deadline = _time.time() + 5
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())

    best_code = jd_contracts[0]
    best_oi = 0
    for code in jd_contracts:
        q = jd_quotes[code]
        oi = getattr(q, "open_interest", 0) or 0
        if oi > best_oi:
            best_oi = oi
            best_code = code

    if best_oi == 0:
        return jd_contracts[0]

    print(f"  持仓最大: {best_code} ({int(best_oi)} 手)")
    others = sorted(
        [(c, getattr(jd_quotes[c], "open_interest", 0) or 0) for c in jd_contracts],
        key=lambda x: x[1], reverse=True
    )
    if len(others) > 1:
        print(f"  其他: " + ", ".join(f"{c}({int(oi)})" for c, oi in others[1:5]))

    return best_code


def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%H:%M")
    return ""


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


def determine_screen1_trend(klines, hist_lookback=5, ema_lookback=10):
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

    # ── MACD histogram slope (5-bar lookback) ──
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
    ema_threshold = avg_price * 0.0005
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


def determine_screen2_signal(screen1_trend, klines_5min, lookback=20, swing_window=3):
    """
    Elder's Triple Screen — Screen 2: 中期振荡器回调信号

    当 Screen 1 有明确方向时，使用 Force Index EMA(2) 检测逆势回调：
    - Screen 1 多头 + FI 为负 → 回调买入信号
    - Screen 1 空头 + FI 为正 → 反弹卖出信号
    - Screen 1 中性 → 无信号
    - 额外检测 FI 与价格的背离

    Returns:
        dict: signal / fi_value / fi_recent / fi_above_zero /
              zero_cross / divergence / pullback_desc
    """
    no_signal = {
        "signal": "no_signal", "fi_value": 0.0, "fi_recent": [],
        "fi_above_zero": True, "zero_cross": "none",
        "divergence": "none", "pullback_desc": "Screen 1 趋势不明 → 无回调信号",
    }

    if screen1_trend == "neutral":
        return no_signal

    fi = calc_force_index(klines_5min, ema_span=2)
    recent_fi = fi.iloc[-lookback:].dropna()
    recent_close = klines_5min["close"].iloc[-lookback:]

    if len(recent_fi) < 5:
        no_signal["pullback_desc"] = "Force Index 数据不足"
        return no_signal

    # ── Current FI state ──
    fi_value = recent_fi.iloc[-1]
    fi_above_zero = fi_value > 0

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
    close_for_div = klines_5min["close"].iloc[-lookback:]

    min_len = min(len(fi_for_div), len(close_for_div))
    fi_for_div = fi_for_div.iloc[-min_len:]
    close_for_div = close_for_div.iloc[-min_len:]

    if min_len >= 2 * swing_window + 1:
        troughs_fi = _find_local_extrema(fi_for_div, window=swing_window, mode="min")
        troughs_price = _find_local_extrema(close_for_div, window=swing_window, mode="min")
        if len(troughs_fi) >= 2 and len(troughs_price) >= 2:
            if (close_for_div.iloc[troughs_price[-1]] < close_for_div.iloc[troughs_price[-2]]
                    and fi_for_div.iloc[troughs_fi[-1]] > fi_for_div.iloc[troughs_fi[-2]]):
                divergence = "bullish"

        peaks_fi = _find_local_extrema(fi_for_div, window=swing_window, mode="max")
        peaks_price = _find_local_extrema(close_for_div, window=swing_window, mode="max")
        if len(peaks_fi) >= 2 and len(peaks_price) >= 2:
            if (close_for_div.iloc[peaks_price[-1]] > close_for_div.iloc[peaks_price[-2]]
                    and fi_for_div.iloc[peaks_fi[-1]] < fi_for_div.iloc[peaks_fi[-2]]):
                divergence = "bearish"

    # ── Signal determination ──
    if screen1_trend == "bullish":
        if fi_value < 0:
            if divergence == "bullish":
                signal = "divergence_buy"
            else:
                signal = "buy_signal"
            pullback_desc = "多头回调: FI为负 → 回调买入机会"
        else:
            signal = "no_signal"
            pullback_desc = "多头趋势中FI为正 → 趋势延续，无回调机会"
    elif screen1_trend == "bearish":
        if fi_value > 0:
            if divergence == "bearish":
                signal = "divergence_sell"
            else:
                signal = "sell_signal"
            pullback_desc = "空头反弹: FI为正 → 反弹卖出机会"
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

    # ── LONG signal check ──
    if screen1_trend == "bullish" and screen2_signal in ("buy_signal", "divergence_buy"):
        entry_price = prev_high + tick_size
        stop_loss = min(curr_bar["low"], prev_low)
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
        stop_loss = max(curr_bar["high"], prev_high)
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


def setup_api():
    global api, klines_map, SYMBOL, _last_processed
    print("正在连接天勤量化...")
    username, password = get_tqsdk_auth()
    api = TqApi(auth=TqAuth(username, password))
    SYMBOL = discover_main_contract(api)
    print(f"鸡蛋主力合约: {SYMBOL}")

    all_jd = sorted([q for q in api.query_quotes(
        ins_class="FUTURE", exchange_id="DCE", expired=False
    ) if "jd" in q.lower()])
    print(f"可用合约: {', '.join(all_jd)}")

    # 订阅三个周期
    for label, dur in KLINE_DURS.items():
        klines_map[label] = api.get_kline_serial(SYMBOL, dur, data_length=DATA_LEN)

    # 等待初始数据
    deadline = _time.time() + 15
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())
        all_ready = all(
            len(k) > 0 and k.iloc[-1]["close"] > 0
            for k in klines_map.values()
        )
        if all_ready:
            break

    # 初始化哨兵
    for label, k in klines_map.items():
        _last_processed[label] = k.iloc[-1]["datetime"]

    print("连接成功，等待行情推送...")


def draw_one(ax, df, title, show_ema13=False):
    """在单个子图上绘制蜡烛图 + MA + EMA13(可选)"""
    ax.cla()

    # ── Bug fix: compute MAs on full dataset, then slice ──
    full_data = df[df["close"] > 0].copy()
    if full_data.empty:
        ax.set_title(f"{title}  (无数据)", fontsize=10)
        return

    data = full_data.tail(SHOW_N).copy()

    x     = np.arange(len(data))
    times = [fmt_time(v) for v in data["datetime"].values]

    # 蜡烛图
    for i, (_, r) in enumerate(data.iterrows()):
        color = "#e74c3c" if r["close"] >= r["open"] else "#26a65b"
        lo    = min(r["open"], r["close"])
        hi    = max(r["open"], r["close"])
        ax.bar(i, hi - lo, bottom=lo, color=color, width=0.6, linewidth=0)
        ax.plot([i, i], [r["low"], r["high"]], color=color, linewidth=0.8)

    # MAs — computed on full data, then sliced
    full_closes = full_data["close"]
    for n, c in [(5, "#f39c12"), (10, "#3498db"), (20, "#9b59b6")]:
        if len(full_closes) >= n:
            ma_full = full_closes.rolling(n).mean()
            ma_display = ma_full.tail(SHOW_N).values
            if len(ma_display) == len(data):
                ax.plot(x, ma_display, color=c, linewidth=0.8, label=f"MA{n}")

    # ── EMA(13) for 25min subplot only ──
    if show_ema13:
        ema13_full = full_closes.ewm(span=13, adjust=False).mean()
        ema13_display = ema13_full.tail(SHOW_N).values
        if len(ema13_display) == len(data):
            ax.plot(x, ema13_display, color="#00ffff", linewidth=1.2,
                    label="EMA13", linestyle="-")

    # 标题 + 最新价
    last    = data.iloc[-1]
    chg     = last["close"] - data.iloc[0]["open"]
    chg_pct = chg / data.iloc[0]["open"] * 100 if data.iloc[0]["open"] != 0 else 0
    flag    = "▲" if chg >= 0 else "▼"

    ax.set_title(
        f"{title}    最新: {last['close']:.2f}  {flag} {abs(chg):.2f} ({abs(chg_pct):.2f}%)",
        fontsize=10, loc="left"
    )

    # X 轴刻度
    step = max(1, len(data) // 8)
    tick_pos   = x[::step]
    tick_label = [times[i] for i in range(0, len(data), step)]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_label, fontsize=7, rotation=30)
    ax.set_xlim(-1, len(data))
    ax.grid(axis="y", alpha=0.25, linewidth=0.5)
    ax.set_ylabel("价格", fontsize=8)
    ax.legend(fontsize=7, loc="upper left")


def draw_macd(ax, df):
    """在子图上绘制 MACD 指标 + Elder Screen 1 趋势方向"""
    ax.cla()

    # ── Bug fix: compute indicators on FULL dataset ──
    full_data = df[df["close"] > 0].copy()
    if full_data.empty or len(full_data) < 35:
        ax.set_title("MACD (12,26,9)  数据不足", fontsize=10)
        return

    _, _, dif, dea, bar = calc_macd(full_data, fast=12, slow=26, signal=9)
    screen1 = determine_screen1_trend(full_data)

    # Slice for display only (indicators already computed on full data)
    data = full_data.tail(SHOW_N)
    dif_display = dif.tail(SHOW_N)
    dea_display = dea.tail(SHOW_N)
    bar_display = bar.tail(SHOW_N)

    x = np.arange(len(data))

    # ── Trend direction background color ──
    trend = screen1["trend"]
    if trend == "bullish":
        ax.set_facecolor("#fff0f0")
        trend_text = "Screen1: 多头趋势 (仅做多)"
        trend_color = "#e74c3c"
        restriction_text = "仅做多 → 禁止做空"
    elif trend == "bearish":
        ax.set_facecolor("#f0fff0")
        trend_text = "Screen1: 空头趋势 (仅做空)"
        trend_color = "#26a65b"
        restriction_text = "仅做空 → 禁止做多"
    else:
        ax.set_facecolor("#f0f0f0")
        trend_text = "Screen1: 趋势不明 (不交易)"
        trend_color = "#888888"
        restriction_text = "不交易 → 等待方向明确"

    # ── MACD histogram bars ──
    for i in range(len(data)):
        b = bar_display.iloc[i]
        if pd.isna(b):
            continue
        color = "#e74c3c" if b >= 0 else "#26a65b"
        ax.bar(i, b, color=color, width=0.6, linewidth=0, alpha=0.8)

    # ── DIF and DEA lines ──
    ax.plot(x, dif_display.values, color="#3498db", linewidth=1.0, label="DIF")
    ax.plot(x, dea_display.values, color="#e67e22", linewidth=1.0, label="DEA")

    # ── Zero axis ──
    ax.axhline(y=0, color="#888888", linewidth=0.5, linestyle="--")

    # ── Histogram slope annotation ──
    hist_slope_cn = {"rising": "上升↑", "falling": "下降↓", "flat": "平缓"}[screen1["hist_slope"]]
    ema_slope_cn = {"rising": "上升↑", "falling": "下降↓", "flat": "平缓"}[screen1["ema_slope"]]
    hist_vals_str = "→".join(f"{v:.2f}" for v in screen1["hist_recent"])

    # ── Title with Screen 1 trend ──
    last_dif = dif_display.iloc[-1]
    last_dea = dea_display.iloc[-1]
    last_bar = bar_display.iloc[-1]
    bar_flag = "多头" if last_bar >= 0 else "空头"

    ax.set_title(
        f"{trend_text}    DIF: {last_dif:.2f}  DEA: {last_dea:.2f}  "
        f"柱: {last_bar:.2f} ({bar_flag})",
        fontsize=10, loc="left", color=trend_color, fontweight="bold"
    )

    # ── Slope info annotation (lower-left) ──
    slope_text = f"MACD柱: {hist_slope_cn}  [{hist_vals_str}]\nEMA13: {ema_slope_cn}\n{restriction_text}"
    ax.text(0.02, 0.02, slope_text, transform=ax.transAxes,
            fontsize=8, verticalalignment="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8, edgecolor=trend_color))

    # X axis ticks
    step = max(1, len(data) // 8)
    tick_pos = x[::step]
    times = [fmt_time(v) for v in data["datetime"].values]
    tick_label = [times[i] for i in range(0, len(data), step)]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_label, fontsize=7, rotation=30)
    ax.set_xlim(-1, len(data))
    ax.grid(axis="y", alpha=0.25, linewidth=0.5)
    ax.legend(fontsize=7, loc="upper left")


def draw_force_index(ax, df, screen1_trend):
    """在子图上绘制 Force Index + Elder Screen 2 回调信号"""
    ax.cla()

    # ── Compute indicators on FULL dataset ──
    full_data = df[df["close"] > 0].copy()
    if full_data.empty or len(full_data) < 5:
        ax.set_title("Force Index EMA(2)  数据不足", fontsize=10)
        return

    fi = calc_force_index(full_data, ema_span=2)
    screen2 = determine_screen2_signal(screen1_trend, full_data)

    # Slice for display only
    data = full_data.tail(SHOW_N)
    fi_display = fi.tail(SHOW_N)

    x = np.arange(len(data))

    # ── Background tint based on signal ──
    signal = screen2["signal"]
    if signal in ("buy_signal", "divergence_buy"):
        ax.set_facecolor("#fff0f0")
        signal_text = "Screen2: 回调买入 ▲"
        signal_color = "#e74c3c"
    elif signal in ("sell_signal", "divergence_sell"):
        ax.set_facecolor("#f0fff0")
        signal_text = "Screen2: 反弹卖出 ▼"
        signal_color = "#26a65b"
    else:
        ax.set_facecolor("#f0f0f0")
        signal_text = "Screen2: 无信号"
        signal_color = "#888888"

    # ── FI color-coded bars ──
    for i in range(len(data)):
        v = fi_display.iloc[i]
        if pd.isna(v):
            continue
        color = "#e74c3c" if v >= 0 else "#26a65b"
        ax.bar(i, v, color=color, width=0.6, linewidth=0, alpha=0.7)

    # ── FI line overlay ──
    ax.plot(x, fi_display.values, color="#2c3e50", linewidth=1.0, label="FI-EMA(2)")

    # ── Zero line ──
    ax.axhline(y=0, color="#888888", linewidth=0.5, linestyle="--")

    # ── Divergence annotation ──
    div_text = ""
    if signal == "divergence_buy":
        div_text = " ★ 底背离"
    elif signal == "divergence_sell":
        div_text = " ★ 顶背离"

    # ── Title with Screen 2 signal ──
    fi_val = screen2["fi_value"]
    ax.set_title(
        f"{signal_text}{div_text}    FI: {fi_val:.0f}  "
        f"({'零轴上方' if screen2['fi_above_zero'] else '零轴下方'})",
        fontsize=10, loc="left", color=signal_color, fontweight="bold"
    )

    # ── Signal info annotation (lower-left) ──
    cross_cn = {"crossed_below": "下穿零轴↓", "crossed_above": "上穿零轴↑", "none": "—"}
    info_text = f"{screen2['pullback_desc']}\n零轴穿越: {cross_cn[screen2['zero_cross']]}"
    ax.text(0.02, 0.02, info_text, transform=ax.transAxes,
            fontsize=8, verticalalignment="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      alpha=0.8, edgecolor=signal_color))

    # X axis ticks
    step = max(1, len(data) // 8)
    tick_pos = x[::step]
    times = [fmt_time(v) for v in data["datetime"].values]
    tick_label = [times[i] for i in range(0, len(data), step)]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_label, fontsize=7, rotation=30)
    ax.set_xlim(-1, len(data))
    ax.grid(axis="y", alpha=0.25, linewidth=0.5)
    ax.legend(fontsize=7, loc="upper left")


def draw_screen3_overlay(ax, screen1_trend, screen2_signal, klines_1min):
    """在1分钟K线子图上叠加 Screen 3 入场信号线"""
    screen3 = determine_screen3_entry(screen1_trend, screen2_signal, klines_1min)
    sig = screen3["signal"]

    if sig in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
        is_long = "long" in sig
        entry_color = "#e74c3c" if is_long else "#26a65b"

        # Draw entry price line (dashed)
        ax.axhline(y=screen3["entry_price"], color=entry_color,
                    linewidth=1.5, linestyle="--", alpha=0.9,
                    label=f"入场: {screen3['entry_price']:.0f}")

        # Draw stop loss line (dotted)
        ax.axhline(y=screen3["stop_loss"], color=entry_color,
                    linewidth=1.0, linestyle=":", alpha=0.7,
                    label=f"止损: {screen3['stop_loss']:.0f}")

        # Annotation text box
        if sig == "triggered_long":
            ann_text = f"做多触发! 入场:{screen3['entry_price']:.0f}"
        elif sig == "triggered_short":
            ann_text = f"做空触发! 入场:{screen3['entry_price']:.0f}"
        elif sig == "pending_long":
            ann_text = f"待做多 买入止损:{screen3['entry_price']:.0f}\n止损:{screen3['stop_loss']:.0f}"
        else:
            ann_text = f"待做空 卖出止损:{screen3['entry_price']:.0f}\n止损:{screen3['stop_loss']:.0f}"

        ax.text(0.98, 0.98, ann_text, transform=ax.transAxes,
                fontsize=9, verticalalignment="top", horizontalalignment="right",
                fontweight="bold", color=entry_color,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          alpha=0.85, edgecolor=entry_color))

        # Background tint
        bg_color = "#fff5f5" if is_long else "#f5fff5"
        ax.set_facecolor(bg_color)

    elif sig == "cancelled":
        ax.text(0.98, 0.98, "信号取消\n(S1趋势反转)", transform=ax.transAxes,
                fontsize=9, verticalalignment="top", horizontalalignment="right",
                color="#888888",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          alpha=0.8, edgecolor="#888888"))


def draw_position_overlay(ax, klines_1min):
    """在 1 分钟 K 线子图上叠加持仓跟踪信息"""
    pos = _POSITION
    if pos is None:
        return

    if pos.status == "open":
        is_long = pos.direction == "long"
        color = "#e74c3c" if is_long else "#26a65b"

        # 入场价（实线）
        ax.axhline(y=pos.entry_price, color=color,
                   linewidth=1.5, linestyle="-", alpha=0.9,
                   label=f"入场: {pos.entry_price:.0f}")

        # 当前追踪止损（虚线）
        ax.axhline(y=pos.current_stop, color=color,
                   linewidth=1.2, linestyle="--", alpha=0.85,
                   label=f"追踪止损: {pos.current_stop:.0f}")

        # 初始止损（点线，淡）
        ax.axhline(y=pos.initial_stop, color=color,
                   linewidth=0.8, linestyle=":", alpha=0.4,
                   label=f"初始止损: {pos.initial_stop:.0f}")

        # 最新 1min 收盘价 → 计算 PnL
        last_close = klines_1min.iloc[-1]["close"]
        pnl = pos.unrealized_pnl(last_close)
        pnl_color = "#c0392b" if pnl >= 0 else "#27ae60"

        info_text = (
            f"{'▲ 多头' if is_long else '▼ 空头'}  paper\n"
            f"入场: {pos.entry_price:.0f}  当前: {last_close:.0f}\n"
            f"追踪止损: {pos.current_stop:.0f}\n"
            f"持仓: {pos.bars_held} bars  MFE: {pos.peak_profit:.0f}\n"
            f"浮盈: {pnl:+.0f} 点"
        )
        ax.text(0.02, 0.98, info_text, transform=ax.transAxes,
                fontsize=9, verticalalignment="top", horizontalalignment="left",
                fontweight="bold", color=pnl_color,
                bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                          alpha=0.9, edgecolor=color))

    elif pos.status == "closed":
        pnl = pos.realized_pnl()
        pnl_color = "#c0392b" if pnl >= 0 else "#27ae60"
        info_text = (
            f"已平仓  {'▲ 多头' if pos.direction == 'long' else '▼ 空头'}\n"
            f"入场: {pos.entry_price:.0f} → 平仓: {pos.exit_price:.0f}\n"
            f"原因: {pos.exit_reason}\n"
            f"已实现 PnL: {pnl:+.0f} 点"
        )
        ax.text(0.02, 0.98, info_text, transform=ax.transAxes,
                fontsize=9, verticalalignment="top", horizontalalignment="left",
                fontweight="bold", color=pnl_color,
                bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                          alpha=0.85, edgecolor=pnl_color))


def draw_all(axes):
    """绘制全部五个子图：25min K线 / MACD / 5min K线 / FI / 1min K线"""
    global _POSITION
    draw_one(axes[0], klines_map["25min"], "25分钟 K线", show_ema13=True)
    draw_macd(axes[1], klines_map["25min"])
    # Screen 1 trend for Screen 2 & 3 gating
    screen1 = determine_screen1_trend(klines_map["25min"])
    draw_one(axes[2], klines_map["5min"],  "5分钟 K线", show_ema13=False)
    draw_force_index(axes[3], klines_map["5min"], screen1["trend"])
    # Screen 3 overlay on 1min subplot (must be AFTER draw_one since it calls ax.cla())
    screen2 = determine_screen2_signal(screen1["trend"], klines_map["5min"])
    draw_one(axes[4], klines_map["1min"],  "1分钟 K线", show_ema13=False)
    draw_screen3_overlay(axes[4], screen1["trend"], screen2["signal"], klines_map["1min"])
    # 持仓跟踪：更新 + 叠加显示
    _POSITION = update_position(_POSITION, screen1["trend"], screen2["signal"],
                                klines_map["1min"], _POSITION_EVENTS)
    if len(_POSITION_EVENTS) > 20:
        del _POSITION_EVENTS[:len(_POSITION_EVENTS) - 20]
    draw_position_overlay(axes[4], klines_map["1min"])


def animate(frame, axes):
    global api, klines_map, _last_draw_time, _last_processed
    if api is None or not klines_map:
        return

    api.wait_update(deadline=_time.time())

    # 检测任意周期新 bar
    any_new_bar = False
    for label, k in klines_map.items():
        cur_dt = k.iloc[-1]["datetime"]
        if cur_dt > _last_processed[label]:
            _last_processed[label] = cur_dt
            any_new_bar = True

    # 1 分钟实时价格变化
    k1m = klines_map["1min"]
    is_price_change = (
        not any_new_bar
        and is_trading_time()
        and k1m.iloc[-1]["datetime"] == _last_processed["1min"]
        and api.is_changing(k1m.iloc[-1], "close")
    )

    # 25 分钟 MACD 变化（DIF/DEA 也会随最新 bar 刷新）
    k25 = klines_map["25min"]
    macd_change = (
        not any_new_bar and not is_price_change
        and len(k25) >= 26
        and api.is_changing(k25.iloc[-1], "close")
    )

    # 5 分钟 Force Index 变化
    k5 = klines_map["5min"]
    fi_change = (
        not any_new_bar and not is_price_change and not macd_change
        and len(k5) >= 3
        and api.is_changing(k5.iloc[-1], "close")
    )

    if any_new_bar or is_price_change or macd_change or fi_change:
        draw_all(axes)
        _last_draw_time = _time.time()
        return

    # 盘后定期刷新
    if not is_trading_time() and _time.time() - _last_draw_time > VIEW_SEC:
        draw_all(axes)
        _last_draw_time = _time.time()


def main():
    setup_api()

    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    # 五行子图：25min K线 / 25min MACD / 5min K线 / 5min Force Index / 1min K线
    fig, axes = plt.subplots(5, 1, figsize=(14, 16),
                             gridspec_kw={"height_ratios": [4, 1.5, 3, 1.5, 3]})
    fig.subplots_adjust(hspace=0.4)

    # 全局标题
    if is_trading_time():
        status = "🟢 交易中"
    else:
        status = f"⏸️  盘后 (等 {next_trading_time()})"
    fig.suptitle(
        f"天勤量化 — 鸡蛋期货 ({SYMBOL})  1/5/25分钟K线  {status}    "
        f"{datetime.now().strftime('%H:%M:%S')}",
        fontsize=12
    )

    draw_all(axes)

    ani = animation.FuncAnimation(
        fig, animate,
        fargs=(axes,),
        interval=2000,
        cache_frame_data=False
    )

    try:
        plt.tight_layout()
        plt.show()
    finally:
        if api:
            api.close()
            print("已关闭 TqApi 连接。")


if __name__ == "__main__":
    main()
