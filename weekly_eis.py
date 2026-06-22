"""
周线 EIS 分析脚本 — Elder Impulse System
同时分析 CF609（棉花）和 JD主力（鸡蛋）的日线与周线

EIS 信号判断规则:
  GREEN  : EMA(13) 斜率上升 AND MACD柱 斜率上升  → 只做多
  RED    : EMA(13) 斜率下降 AND MACD柱 斜率下降  → 只做空
  BLUE   : 其余情况（方向冲突）               → 中性，等待

用法:
    python weekly_eis.py
"""

from tqsdk import TqApi, TqAuth
import pandas as pd
from datetime import datetime
import time as _time

from config_loader import get_tqsdk_auth
from egg_futures_1min import calc_macd, calc_ema, discover_main_contract


# ── 周线 = 604800 秒；日线 = 86400 秒 ──
WEEKLY_DUR  = 7 * 24 * 3600   # 604800
DAILY_DUR   = 86400
DATA_LEN    = 100              # 周线 100 根 ≈ 2 年


def fmt_date(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%Y-%m-%d")
    return "---"


def determine_eis_color(klines, label=""):
    """
    计算 EIS 颜色信号
    返回 dict:
      color        : "GREEN" / "RED" / "BLUE"
      ema_slope    : "UP" / "DOWN" / "FLAT"
      hist_slope   : "UP" / "DOWN" / "FLAT"
      ema_cur      : 当前 EMA(13)
      ema_prev     : 上一根 EMA(13)
      dif          : 当前 DIF
      dea          : 当前 DEA
      hist_cur     : 当前 MACD 柱
      hist_prev    : 上一根 MACD 柱
      last_close   : 最新收盘价
      last_time    : 最新 K 线时间字符串
    """
    valid = klines[klines["close"] > 0]
    if len(valid) < 30:
        return {"color": "BLUE", "note": "数据不足 30 根"}

    _, _, dif, dea, hist = calc_macd(valid)
    ema13 = calc_ema(valid, span=13)

    ema_cur   = ema13.iloc[-1]
    ema_prev  = ema13.iloc[-2]
    hist_cur  = hist.iloc[-1]
    hist_prev = hist.iloc[-2]

    ema_slope  = "UP"   if ema_cur  > ema_prev  else ("DOWN" if ema_cur  < ema_prev  else "FLAT")
    hist_slope = "UP"   if hist_cur > hist_prev else ("DOWN" if hist_cur < hist_prev else "FLAT")

    if ema_slope == "UP" and hist_slope == "UP":
        color = "GREEN"
    elif ema_slope == "DOWN" and hist_slope == "DOWN":
        color = "RED"
    else:
        color = "BLUE"

    return {
        "color"      : color,
        "ema_slope"  : ema_slope,
        "hist_slope" : hist_slope,
        "ema_cur"    : ema_cur,
        "ema_prev"   : ema_prev,
        "dif"        : dif.iloc[-1],
        "dea"        : dea.iloc[-1],
        "hist_cur"   : hist_cur,
        "hist_prev"  : hist_prev,
        "last_close" : valid.iloc[-1]["close"],
        "last_time"  : fmt_date(valid.iloc[-1]["datetime"]),
        "note"       : "",
    }


COLOR_EMOJI = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}


def print_eis_report(symbol, weekly_klines, daily_klines):
    """打印一个品种的 EIS 分析报告（周线 + 日线）"""
    w = determine_eis_color(weekly_klines, "weekly")
    d = determine_eis_color(daily_klines,  "daily")

    print(f"\n{'='*72}")
    print(f"  {symbol}  |  截至 {d['last_time']}")
    print(f"{'='*72}")

    for frame_label, r in [("周线 (W)", w), ("日线 (D)", d)]:
        ce = COLOR_EMOJI.get(r["color"], "⬜")
        print(f"\n  [{frame_label}]  {ce} {r['color']}")
        if r.get("note") == "数据不足 30 根":
            print(f"    ⚠️  数据不足，无法计算")
            continue

        # EMA(13) 斜率
        ema_arrow = "↑" if r["ema_slope"] == "UP" else ("↓" if r["ema_slope"] == "DOWN" else "→")
        print(f"    EMA(13):    {r['ema_cur']:>10.2f}  ({ema_arrow})  前值: {r['ema_prev']:.2f}"
              f"  Δ={r['ema_cur']-r['ema_prev']:+.2f}")

        # MACD 柱斜率
        hist_arrow = "↑" if r["hist_slope"] == "UP" else ("↓" if r["hist_slope"] == "DOWN" else "→")
        print(f"    MACD 柱:    {r['hist_cur']:>10.2f}  ({hist_arrow})  前值: {r['hist_prev']:.2f}"
              f"  Δ={r['hist_cur']-r['hist_prev']:+.2f}")
        print(f"    DIF / DEA : {r['dif']:.2f}  /  {r['dea']:.2f}")

        # 颜色含义
        color_desc = {
            "GREEN": "EMA↑ & MACD柱↑ → 多头主导，只允许买入",
            "RED"  : "EMA↓ & MACD柱↓ → 空头主导，只允许卖出",
            "BLUE" : "EMA 与 MACD柱 方向相反 → 中性，等待明确信号",
        }[r["color"]]
        print(f"    → {color_desc}")

    # 综合判断
    print(f"\n  {'─'*66}")
    print(f"  综合操作建议 ({symbol})")
    print(f"  {'─'*66}")

    wc, dc = w["color"], d["color"]

    # 强趋势：两个周期同色
    if wc == "GREEN" and dc == "GREEN":
        print(f"  ✅ 周线+日线均为绿 → 多头趋势强烈")
        print(f"     操作: 只做多。等待日内 25min 出现绿色K线，入场做多")
    elif wc == "RED" and dc == "RED":
        print(f"  ✅ 周线+日线均为红 → 空头趋势强烈")
        print(f"     操作: 只做空。等待日内 25min 出现红色K线，入场做空")

    # 周线决定方向，日线是入场时机
    elif wc == "GREEN" and dc == "BLUE":
        print(f"  📊 周线绿 + 日线蓝 → 大趋势多头，日线动量暂时减弱")
        print(f"     操作: 偏多。等待日线从蓝→绿切换时买入（动量重新加速）")
        print(f"           日线蓝色期间不做空")
    elif wc == "GREEN" and dc == "RED":
        print(f"  ⚠️  周线绿 + 日线红 → 周线多头趋势 vs 日线空头回调")
        print(f"     操作: 暂时观望。这是多头主升趋势中的技术性回调")
        print(f"           等待日线恢复绿色再做多，切勿做空")
    elif wc == "RED" and dc == "BLUE":
        print(f"  📊 周线红 + 日线蓝 → 大趋势空头，日线动量暂时减弱")
        print(f"     操作: 偏空。等待日线从蓝→红切换时做空（动量重新加速）")
        print(f"           日线蓝色期间不做多")
    elif wc == "RED" and dc == "GREEN":
        print(f"  ⚠️  周线红 + 日线绿 → 周线空头趋势 vs 日线多头反弹")
        print(f"     操作: 暂时观望。这是空头主跌趋势中的反弹，假多头")
        print(f"           等待日线回落变红再做空，切勿做多")
    elif wc == "BLUE" and dc == "GREEN":
        print(f"  📊 周线蓝 + 日线绿 → 周线方向不明，日线偏多")
        print(f"     操作: 谨慎偏多。仓位减半，等待周线确认绿色")
    elif wc == "BLUE" and dc == "RED":
        print(f"  📊 周线蓝 + 日线红 → 周线方向不明，日线偏空")
        print(f"     操作: 谨慎偏空。仓位减半，等待周线确认红色")
    else:  # wc == "BLUE" and dc == "BLUE"
        print(f"  ⏸️  周线蓝 + 日线蓝 → 完全中性，趋势不明")
        print(f"     操作: 空仓等待。价格进入震荡区间，EIS 不提供信号")

    # 打印最近周线数据
    valid_w = weekly_klines[weekly_klines["close"] > 0].tail(6)
    print(f"\n  最近周线 K 线 (近 6 根)")
    print(f"  {'周起始':>12} {'开':>8} {'高':>8} {'低':>8} {'收':>8} {'涨跌':>8}")
    for _, row in valid_w.iterrows():
        t    = fmt_date(row["datetime"])
        chg  = row["close"] - row["open"]
        flag = "▲" if chg >= 0 else "▼"
        print(f"  {t:>12} {row['open']:>8.0f} {row['high']:>8.0f} {row['low']:>8.0f} "
              f"{row['close']:>8.0f} {flag}{abs(chg):>6.0f}")


def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        # ── 确定 JD 主力合约 ──
        print("\n--- 确定 JD 主力合约 ---")
        jd_symbol = discover_main_contract(api)
        print(f"  JD 主力: {jd_symbol}")

        symbols = {
            "CZCE.CF609": "CF609 (棉花)",
            jd_symbol:    f"{jd_symbol.split('.')[-1].upper()} (鸡蛋主力)",
        }

        # ── 订阅所有周线/日线 ──
        klines = {}
        for sym in symbols:
            klines[sym] = {
                "weekly": api.get_kline_serial(sym, WEEKLY_DUR, data_length=DATA_LEN),
                "daily":  api.get_kline_serial(sym, DAILY_DUR,  data_length=DATA_LEN),
            }

        # 等待数据到位
        print("  等待数据加载...")
        deadline = _time.time() + 20
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            all_ready = all(
                len(klines[s]["weekly"]) > 5 and klines[s]["weekly"].iloc[-1]["close"] > 0
                and len(klines[s]["daily"]) > 5 and klines[s]["daily"].iloc[-1]["close"] > 0
                for s in symbols
            )
            if all_ready:
                break

        print(f"\n{'='*72}")
        print(f"  Elder Impulse System — 周线/日线 分析")
        print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*72}")

        for sym, name in symbols.items():
            print_eis_report(
                name,
                klines[sym]["weekly"],
                klines[sym]["daily"],
            )

        print(f"\n{'='*72}")
        print(f"  EIS 三色含义速查:")
        print(f"  🟢 GREEN = EMA↑ & MACD柱↑ → 多头加速，只做多")
        print(f"  🔴 RED   = EMA↓ & MACD柱↓ → 空头加速，只做空")
        print(f"  🔵 BLUE  = 两者方向冲突    → 中性，等待信号")
        print(f"{'='*72}\n")

    finally:
        api.close()


if __name__ == "__main__":
    main()
