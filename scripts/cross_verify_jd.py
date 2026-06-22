"""
JD2608 信号交叉验证 — 三重滤网 + 动力系统(EIS)
用法:
    python scripts/cross_verify_jd.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tqsdk import TqApi, TqAuth
from datetime import datetime
import time as _time
import pandas as pd

from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    calc_macd, calc_ema, calc_force_index,
    determine_screen1_trend, determine_screen2_signal, determine_screen3_entry,
)
from weekly_eis import determine_eis_color

USERNAME, PASSWORD = get_tqsdk_auth()

SYM = "DCE.jd2608"
TICK = 1
PERIODS = {
    "weekly": 604800,
    "daily":  86400,
    "hourly": 3600,
    "25min":  1500,
    "15min":  900,
    "3min":   180,
}

def get_closed(klines):
    """去掉最后一条未收盘 K 线"""
    return klines.iloc[:-1]

def format_trend(r):
    t = r.get("trend", "neutral")
    hs = r.get("hist_slope", "flat")
    es = r.get("ema_slope", "flat")
    emoji = {"bullish": "🟢多头", "bearish": "🔴空头", "neutral": "🔵中性"}[t]
    return f"{emoji} (MACD柱{hs}/EMA{es})"

def format_eis(r):
    c = r["color"]
    emoji = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}[c]
    return f"{emoji} {c} (EMA{r['ema_slope']}/MACD{r['hist_slope']})"

def main():
    print("=" * 78)
    print("  JD2608 三重滤网 × 动力系统 交叉验证")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)

    api = TqApi(auth=TqAuth(USERNAME, PASSWORD))

    # ── 拉取所有周期 K 线 ──
    klines = {}
    for name, dur in PERIODS.items():
        klines[name] = api.get_kline_serial(SYM, dur, data_length=200)

    deadline = _time.time() + 10
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())

    # 取已收盘 K 线
    closed = {}
    for name in PERIODS:
        c = klines[name]
        closed[name] = c[c["close"] > 0].iloc[:-1] if len(c) > 1 else c[c["close"] > 0]

    # ── 基础行情 ──
    last = closed["daily"].iloc[-1]
    print(f"\n  JD2608 最新日线: 开 {last['open']:.0f}  高 {last['high']:.0f}"
          f"  低 {last['low']:.0f}  收 {last['close']:.0f}  量 {int(last['volume'])}")

    daily5 = closed["daily"].tail(5)
    for _, r in daily5.iterrows():
        chg = r["close"] - r["open"]
        arrow = "阳" if chg >= 0 else "阴"
        print(f"    {pd.Timestamp(r['datetime']).strftime('%m/%d')}  "
              f"{arrow}线  收 {r['close']:.0f}  最高 {r['high']:.0f}  最低 {r['low']:.0f}")

    # ════════════════════════════════════════════════════
    # 一、动力系统 (EIS) 多周期检查
    # ════════════════════════════════════════════════════
    print(f"\n{'─'*78}")
    print("  一、EIS 动力系统 (Elder Impulse System)")
    print(f"{'─'*78}")

    eis_results = {}
    for period_name in ["weekly", "daily", "25min", "hourly"]:
        r = determine_eis_color(closed[period_name])
        eis_results[period_name] = r
        print(f"\n  [{period_name.upper()}] {format_eis(r)}")
        print(f"    EMA(13): {r['ema_cur']:.1f} (前值 {r['ema_prev']:.1f}, "
              f"Δ{r['ema_cur']-r['ema_prev']:+.1f})")
        print(f"    DIF: {r['dif']:+.2f}  DEA: {r['dea']:+.2f}  "
              f"MACD柱: {r['hist_cur']:+.2f} (前值 {r['hist_prev']:+.2f})")
        print(f"    收盘: {r['last_close']:.0f}  时间: {r['last_time']}")

    # EIS 综合判断
    print(f"\n  ▶ EIS 多周期综合:")
    eis_score = 0
    for pn, r in eis_results.items():
        if r["color"] == "GREEN":
            pts, txt = 1, "🟢+1 (看多)"
        elif r["color"] == "RED":
            pts, txt = -1, "🔴-1 (看空)"
        else:
            pts, txt = 0, "🔵 0 (中性)"
        eis_score += pts
        print(f"    {pn:<8} {txt}")
    eis_verdict = "偏多 ✅" if eis_score >= 2 else "偏空 ❌" if eis_score <= -2 else "中性/分歧 ⚠️"
    print(f"    EIS 总分: {eis_score:+d}  →  {eis_verdict}")

    # ════════════════════════════════════════════════════
    # 二、三重滤网 (Triple Screen)
    # ════════════════════════════════════════════════════
    print(f"\n{'─'*78}")
    print("  二、三重滤网 (Triple Screen)")
    print(f"{'─'*78}")

    # Set A: 周线→日线→小时
    print(f"\n  ▸ Set A 长线: 周线(S1) → 日线(S2) → 小时(S3)")

    s1a = determine_screen1_trend(closed["weekly"])
    s2a = determine_screen2_signal(s1a["trend"], closed["daily"])
    s3a = determine_screen3_entry(s1a["trend"], s2a["signal"], closed["hourly"], TICK)

    print(f"    Screen 1 (周线): {format_trend(s1a)}")
    print(f"    Screen 2 (日线): {s2a['signal']}  "
          f"(FI EMA2: {s2a.get('fi_value', 0):+.0f}, {s2a.get('pullback_desc','')})")
    trend_a = "🟢 多头格局" if s1a["trend"] == "bullish" else "🔴 空头格局" if s1a["trend"] == "bearish" else "🔵 中性"
    print(f"    宏观趋势: {trend_a}")
    print(f"    Screen 3 (小时): {s3a['signal']}")
    if s3a["signal"] != "no_signal" and s3a["signal"] != "none":
        print(f"    入场价: {s3a['entry_price']:.0f}  止损: {s3a['stop_loss']:.0f}  风险: {abs(s3a['entry_price']-s3a['stop_loss'])}点")

    # Set B: 小时→15min→3min
    print(f"\n  ▸ Set B 短线: 小时(S1) → 15min(S2) → 3min(S3)")

    s1b = determine_screen1_trend(closed["hourly"])
    s2b = determine_screen2_signal(s1b["trend"], closed["15min"])
    s3b = determine_screen3_entry(s1b["trend"], s2b["signal"], closed["3min"], TICK)

    print(f"    Screen 1 (小时): {format_trend(s1b)}")
    print(f"    Screen 2 (15min): {s2b['signal']}  "
          f"(FI EMA2: {s2b.get('fi_value', 0):+.0f}, {s2b.get('pullback_desc','')})")
    trend_b = "🟢 多头格局" if s1b["trend"] == "bullish" else "🔴 空头格局" if s1b["trend"] == "bearish" else "🔵 中性"
    print(f"    趋势方向: {trend_b}")
    print(f"    Screen 3 (3min): {s3b['signal']}")
    if s3b["signal"] != "no_signal" and s3b["signal"] != "none":
        print(f"    入场价: {s3b['entry_price']:.0f}  止损: {s3b['stop_loss']:.0f}  风险: {abs(s3b['entry_price']-s3b['stop_loss'])}点")

    # ════════════════════════════════════════════════════
    # 三、Set A 长线: 独立裁决
    # ════════════════════════════════════════════════════
    print(f"\n{'═'*78}")
    print("  三、Set A (长线) 独立裁决: 周线→日线→小时")
    print(f"{'═'*78}")

    # EIS for Set A periods
    eis_a = {}
    for pn in ["weekly", "daily", "hourly"]:
        eis_a[pn] = determine_eis_color(closed[pn])

    print(f"\n  EIS 动力系统 (周/日/小时):")
    eis_a_score = 0
    for pn in ["weekly", "daily", "hourly"]:
        r = eis_a[pn]
        c = r["color"]
        if c == "GREEN":
            pts, txt = 1, "🟢+1"
        elif c == "RED":
            pts, txt = -1, "🔴-1"
        else:
            pts, txt = 0, "🔵 0"
        eis_a_score += pts
        emoji = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}[c]
        print(f"    {pn:<8} {emoji} {c:<5} MACD柱: {r['hist_cur']:+.2f}  {txt}")

    # TS Set A score
    ts_a_raw = 0
    if s1a["trend"] == "bullish":
        ts_a_raw += 1
    elif s1a["trend"] == "bearish":
        ts_a_raw -= 1
    if s2a["signal"] == "buy_signal":
        ts_a_raw += 1
    elif s2a["signal"] == "sell_signal":
        ts_a_raw -= 1
    if s3a["signal"] in ("pending_long", "triggered_long"):
        ts_a_raw += 2
    elif s3a["signal"] in ("pending_short", "triggered_short"):
        ts_a_raw -= 2

    total_a = eis_a_score * 0.5 + ts_a_raw * 0.5
    print(f"\n  Triple Screen (本 set): S1={s1a['trend']} S2={s2a['signal']} S3={s3a['signal']} → {ts_a_raw:+d}分")
    print(f"  加权总分: {total_a:+.1f} (EIS ×0.5 + TS ×0.5)")

    # Verdict for Set A
    if total_a >= 2.0:
        va = "✅ 强烈可信 — EIS+TS 一致确认, 主力仓位"
    elif total_a >= 1.0:
        va = "✅ 可信 — 信号方向明确, 正常仓位"
    elif total_a >= 0.3:
        va = "⚠️ 谨慎 — 信号偏弱, 建议半仓"
    elif total_a >= -0.3:
        va = "⚠️ 观望 — EIS与TS矛盾, 不交易"
    elif total_a >= -1.0:
        va = "⚠️ 谨慎偏空"
    elif total_a >= -2.0:
        va = "❌ 做空可信"
    else:
        va = "❌ 强烈做空可信"
    print(f"  ▶ Set A 裁决: {va}")

    # Warnings for Set A
    if eis_a["weekly"]["color"] == "BLUE":
        print(f"    ⚠️ 周线 EIS 蓝色 → 大趋势不确定")
    if eis_a["daily"]["color"] == "BLUE":
        print(f"    ⚠️ 日线 EIS 蓝色 → 中期方向模糊")

    ts_dir_a = "long" if ts_a_raw > 0 else ("short" if ts_a_raw < 0 else "neutral")
    eis_dir_a = "long" if eis_a_score > 0 else ("short" if eis_a_score < 0 else "neutral")
    if ts_dir_a == "long" and eis_dir_a == "short":
        print(f"    ⚠️ 致命冲突: TS看多但EIS看空 → 不入场!")
    elif ts_dir_a == "short" and eis_dir_a == "long":
        print(f"    ⚠️ 致命冲突: TS看空但EIS看多 → 不入场!")

    if s3a["signal"] != "no_signal" and s3a["signal"] != "none":
        print(f"    入场价: {s3a['entry_price']:.0f}  止损: {s3a['stop_loss']:.0f}")

    # ════════════════════════════════════════════════════
    # 四、Set B 短线: 独立裁决
    # ════════════════════════════════════════════════════
    print(f"\n{'═'*78}")
    print("  四、Set B (短线) 独立裁决: 小时→15min→3min")
    print(f"{'═'*78}")

    # EIS for Set B periods
    eis_b = {}
    for pn in ["hourly", "15min", "3min"]:
        eis_b[pn] = determine_eis_color(closed[pn])

    print(f"\n  EIS 动力系统 (小时/15分/3分):")
    eis_b_score = 0
    for pn in ["hourly", "15min", "3min"]:
        r = eis_b[pn]
        c = r["color"]
        if c == "GREEN":
            pts, txt = 1, "🟢+1"
        elif c == "RED":
            pts, txt = -1, "🔴-1"
        else:
            pts, txt = 0, "🔵 0"
        eis_b_score += pts
        emoji = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}[c]
        print(f"    {pn:<8} {emoji} {c:<5} MACD柱: {r['hist_cur']:+.2f}  {txt}")

    # TS Set B score
    ts_b_raw = 0
    if s1b["trend"] == "bullish":
        ts_b_raw += 1
    elif s1b["trend"] == "bearish":
        ts_b_raw -= 1
    if s2b["signal"] == "buy_signal":
        ts_b_raw += 1
    elif s2b["signal"] == "sell_signal":
        ts_b_raw -= 1
    if s3b["signal"] in ("pending_long", "triggered_long"):
        ts_b_raw += 2
    elif s3b["signal"] in ("pending_short", "triggered_short"):
        ts_b_raw -= 2

    total_b = eis_b_score * 0.5 + ts_b_raw * 0.5
    print(f"\n  Triple Screen (本 set): S1={s1b['trend']} S2={s2b['signal']} S3={s3b['signal']} → {ts_b_raw:+d}分")
    print(f"  加权总分: {total_b:+.1f} (EIS ×0.5 + TS ×0.5)")

    # Verdict for Set B
    if total_b >= 2.0:
        vb = "✅ 强烈可信 — EIS+TS 一致确认, 主力仓位"
    elif total_b >= 1.0:
        vb = "✅ 可信 — 信号方向明确, 正常仓位"
    elif total_b >= 0.3:
        vb = "⚠️ 谨慎 — 信号偏弱, 建议半仓"
    elif total_b >= -0.3:
        vb = "⚠️ 观望 — EIS与TS矛盾, 不交易"
    elif total_b >= -1.0:
        vb = "⚠️ 谨慎偏空"
    elif total_b >= -2.0:
        vb = "❌ 做空可信"
    else:
        vb = "❌ 强烈做空可信"
    print(f"  ▶ Set B 裁决: {vb}")

    # Warnings for Set B
    ts_dir_b = "long" if ts_b_raw > 0 else ("short" if ts_b_raw < 0 else "neutral")
    eis_dir_b = "long" if eis_b_score > 0 else ("short" if eis_b_score < 0 else "neutral")
    if ts_dir_b == "long" and eis_dir_b == "short":
        print(f"    ⚠️ 致命冲突: TS看多但EIS看空 → 不入场!")
    elif ts_dir_b == "short" and eis_dir_b == "long":
        print(f"    ⚠️ 致命冲突: TS看空但EIS看多 → 不入场!")

    if s3b["signal"] != "no_signal" and s3b["signal"] != "none":
        print(f"    入场价: {s3b['entry_price']:.0f}  止损: {s3b['stop_loss']:.0f}")

    # ════════════════════════════════════════════════════
    # 五、两套系统对比
    # ════════════════════════════════════════════════════
    print(f"\n{'═'*78}")
    print("  五、A/B 对比 (不合并, 仅供参照)")
    print(f"{'═'*78}")
    print(f"  Set A (长线): 总分 {total_a:+.1f} → {va}")
    print(f"  Set B (短线): 总分 {total_b:+.1f} → {vb}")

    if ts_dir_a == ts_dir_b != "neutral":
        print(f"  ✅ A/B 方向一致 ({ts_dir_a}) — 大周期和小周期共振")
    elif ts_dir_a != "neutral" and ts_dir_b != "neutral" and ts_dir_a != ts_dir_b:
        print(f"  ⚠️ A/B 方向背离 — 长线{ts_dir_a} vs 短线{ts_dir_b}, 等待方向统一")

    print(f"\n{'═'*78}")
    print(f"  验证完成 | {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'═'*78}")

    api.close()

if __name__ == "__main__":
    main()
