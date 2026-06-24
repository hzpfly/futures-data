"""
Triple Screen 多周期信号实时监控 (含 EIS 交叉验证)
===================================================
同时监控两套 Elder's Triple Screen 三重滤网组合:

  Set A (长线):  周线 → 日线 → 小时
                 (大趋势  /  中期回调  /  精确入场)

  Set B (短线):  小时 → 15min → 3min
                 (大趋势  /  中期回调  /  精确入场)

每套三重滤网覆盖 5 个期货主力合约:
  - 棉花 CF (CZCE, 夜盘)     - 鸡蛋 JD (DCE, 仅日盘)
  - 生猪 LH (DCE, 仅日盘)     - 红枣 CJ (CZCE, 仅日盘)
  - 玉米 C  (DCE, 夜盘)

所有合约通过持仓量自动发现当前主力。

⚡ 交叉验证: 信号触发时自动运行 EIS 动力系统多周期检查，
   给出可信度评分 (加权总分) 和明确的风险提示。
   Triple Screen 和 EIS 方向一致 → 高可信; 方向冲突 → 强烈警告。

信号触发时机 (Screen 3 状态变化):
  no_signal  → pending_long / pending_short    : 入场机会出现
  pending_*  → triggered_long / triggered_short: 实际入场触发
  pending_*  → cancelled / no_signal           : 信号取消
  triggered* → cancelled / no_signal           : 信号消失/退出

Screen 1 趋势反转也独立通知 (决定平仓方向).

K 线订阅共享:
  Set A 的 Screen 3 (小时) == Set B 的 Screen 1 (小时)
  共 25 个独立订阅 = 5 周期 × 5 合约

用法:
    python triple_screen_monitor.py
    按 Ctrl+C 退出
"""

from tqsdk import TqApi, TqAuth
from datetime import datetime, time
import time as _time
import subprocess
import winsound

from config_loader import get_tqsdk_auth
from egg_futures_1min import (
    determine_screen1_trend,
    determine_screen2_signal,
    determine_screen3_entry,
    calc_macd,
    calc_ema,
    discover_main_contract_generic,
)
from weekly_eis import determine_eis_color


# ── 配置 ──────────────────────────────────────────────────
# K 线周期 (秒)
DUR_WEEKLY  = 604800
DUR_DAILY   = 86400
DUR_HOURLY  = 3600
DUR_15MIN   = 900
DUR_3MIN    = 180

DATA_LEN       = 300          # 每个 K 线订阅请求的数据长度
SCAN_SEC       = 5             # 主循环 wait_update 超时
STATUS_SEC     = 120           # 非交易时段状态行刷新间隔
ALERT_COOLDOWN = 180           # 同一(合约,set,signal) 冷却期(秒)

# ── 监控合约 ──────────────────────────────────────────────
# 每个品种自动通过持仓量发现主力合约
# (exchange, product_code, display_name, tick_size, has_night_session)
CONTRACTS = [
    {"exchange": "CZCE", "product": "CF",  "name": "棉花", "tick": 5, "night": True},
    {"exchange": "DCE",  "product": "jd",  "name": "鸡蛋", "tick": 1, "night": False},
    {"exchange": "DCE",  "product": "lh",  "name": "生猪", "tick": 5, "night": False},
    {"exchange": "CZCE", "product": "CJ",  "name": "红枣", "tick": 5, "night": False},
    {"exchange": "DCE",  "product": "c",   "name": "玉米", "tick": 1, "night": True},
]

# ── 两套三重滤网周期配置 ──────────────────────────────────
# 每套 = (set_name, 屏1周期, 屏2周期, 屏3周期)
TRIPLE_SETS = [
    {
        "set_name": "A_长线",
        "desc": "周线/日线/小时",
        "screen1_period": "weekly",
        "screen2_period": "daily",
        "screen3_period": "hourly",
    },
    {
        "set_name": "B_短线",
        "desc": "小时/15min/3min",
        "screen1_period": "hourly",
        "screen2_period": "15min",
        "screen3_period": "3min",
    },
    {
        "set_name": "C_中线",
        "desc": "日线/小时/15分钟",
        "screen1_period": "daily",
        "screen2_period": "hourly",
        "screen3_period": "15min",
    },
]

# set_name → set_config 快速索引
SET_BY_NAME = {s["set_name"]: s for s in TRIPLE_SETS}

# ── 某些合约排除特定周期组合 (波动不足不适配短线) ──
# key: set_name, value: set of contract names (CONTRACTS 中的 "name" 字段)
SET_EXCLUDE_CONTRACTS = {
    "B_短线": {"玉米"},
}

# 周期 → 秒数
PERIOD_DUR = {
    "weekly": DUR_WEEKLY,
    "daily":  DUR_DAILY,
    "hourly": DUR_HOURLY,
    "15min":  DUR_15MIN,
    "3min":   DUR_3MIN,
}

PERIOD_LABEL = {
    "weekly": "周线",
    "daily":  "日线",
    "hourly": "小时",
    "15min":  "15分钟",
    "3min":   "3分钟",
}

# ── 交易时段 ──────────────────────────────────────────────
DAY_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]
NIGHT_SESSIONS = [
    (time(21, 0), time(23, 0)),
]

# ── 信号说明 ──────────────────────────────────────────────
# pending_long:  屏1看涨+屏2回调→屏3挂买入止损单 (buy stop),
#                即"当价格涨到 X 时买入"的预挂单
# pending_short: 屏1看跌+屏2反弹→屏3挂卖出止损单 (sell stop),
#                即"当价格跌到 X 时卖出"的预挂单
# 止损单触发后才执行, 在此之前只是预挂, 不会被成交
SIGNAL_DESC = {
    "no_signal":        "无信号",
    "pending_long":     "🟢 待做多",
    "triggered_long":   "🟢🟢 做多触发!",
    "pending_short":    "🔴 待做空",
    "triggered_short":  "🔴🔴 做空触发!",
    "cancelled":        "❌ 信号取消",
}

TREND_DESC = {
    "bullish": "🟢 多头 (只做多)",
    "bearish": "🔴 空头 (只做空)",
    "neutral": "🔵 中性 (观望)",
}


# ── EIS 交叉验证配置 ───────────────────────────────────────
# 每套滤网对应哪些 EIS 周期做多周期验证
EIS_PERIODS_FOR_SET = {
    "A_长线": ["weekly", "daily", "hourly"],
    "B_短线": ["hourly", "15min", "3min"],
    "C_中线": ["daily", "hourly", "15min"],
}

EIS_COLOR_EMOJI = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}

# ── 全局状态 ──────────────────────────────────────────────
_event_log = []              # 信号事件历史
_last_alert_time = {}        # 冷却期记录


# ── 工具函数 ──────────────────────────────────────────────
def is_night_session():
    """当前是否在夜盘时段 (21:00-23:00)"""
    now = datetime.now().time()
    return any(start <= now <= end for start, end in NIGHT_SESSIONS)


def is_trading_time(has_night=False):
    """是否在交易时段. CF609 含夜盘"""
    now = datetime.now().time()
    sessions = DAY_SESSIONS + (NIGHT_SESSIONS if has_night else [])
    return any(start <= now <= end for start, end in sessions)


def next_trading_time(has_night=False):
    """下次开盘时间字符串"""
    now = datetime.now().time()
    sessions = DAY_SESSIONS + (NIGHT_SESSIONS if has_night else [])
    for start, end in sessions:
        if now < start:
            return f"{start:%H:%M}"
    return "次日 09:00"


def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%m-%d %H:%M")
    return "---"


def fmt_dt(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")
    return "---"


def get_closed_klines(klines, has_night):
    """返回已收盘的 K 线 (剔除交易时段内最后一根未收盘 K 线)"""
    valid = klines[klines["close"] > 0]
    if len(valid) == 0:
        return valid
    if is_trading_time(has_night) and len(valid) > 1:
        # 交易时段内, 最后一根还在形成
        return valid.iloc[:-1]
    return valid


def get_last_closed_dt(klines, has_night):
    """返回最新已收盘 K 线的 datetime (ns)"""
    closed = get_closed_klines(klines, has_night)
    if len(closed) == 0:
        return 0
    return closed.iloc[-1]["datetime"]


# ── 通知 ──────────────────────────────────────────────────
def notify_windows(title, body):
    title_safe = title.replace('"', '`"`"').replace("'", "''")
    body_safe  = body.replace('"', '`"`"').replace("'", "''")

    ps_script = (
        f'[Windows.UI.Notifications.ToastNotificationManager, '
        f'Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n'
        f'$template = [Windows.UI.Notifications.ToastNotificationManager]::'
        f'GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n'
        f'$textNodes = $template.GetElementsByTagName("text")\n'
        f'$textNodes.Item(0).AppendChild($template.CreateTextNode("{title_safe}")) | Out-Null\n'
        f'$textNodes.Item(1).AppendChild($template.CreateTextNode("{body_safe}")) | Out-Null\n'
        f'$toast = [Windows.UI.Notifications.ToastNotification]::new($template)\n'
        f'[Windows.UI.Notifications.ToastNotificationManager]::'
        f'CreateToastNotifier("Triple Screen Monitor").Show($toast)\n'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=5, check=False, capture_output=True
        )
    except Exception as e:
        print(f"  [Windows 通知发送失败: {e}]")


def play_alert_sound(level="normal"):
    """level: normal=3声 / critical=5声急促"""
    try:
        if level == "critical":
            for _ in range(2):
                winsound.Beep(1320, 150)
                winsound.Beep(880,  150)
        else:
            winsound.Beep(880,  200)
            winsound.Beep(1100, 200)
            winsound.Beep(1320, 300)
    except Exception:
        pass


# ── EIS 交叉验证 ──────────────────────────────────────────
def compute_eis_cross_verify(klines_closed, set_name, ts_eval, tick_size):
    """
    用 EIS 动力系统对【本套】Triple Screen 信号做交叉验证。
    每套滤网独立评估，不与另一套合并。

    Set A (长线): EIS 验证 周线/日线/小时
    Set B (短线): EIS 验证 小时/15min/3min

    输入:
      klines_closed: dict, {period_key: 已收盘K线DataFrame}
      set_name: "A_长线" / "B_短线"
      ts_eval: evaluate_triple_screen 的输出 (仅本 set)
      tick_size: 最小变动价位

    返回:
      { score, eis_score, ts_score, eis_details, verdict, warnings }
    """
    periods = EIS_PERIODS_FOR_SET.get(set_name, ["daily", "hourly"])
    eis_results = {}

    # ── 第一步: 各周期 EIS 颜色 ──
    for pn in periods:
        k = klines_closed.get(pn)
        if k is None or len(k) < 30:
            eis_results[pn] = {"color": "BLUE", "note": "数据不足"}
            continue
        eis_results[pn] = determine_eis_color(k)

    # ── 第二步: EIS 多周期打分 (每周期 ±1) ──
    eis_score = 0
    eis_details = []
    for pn in periods:
        r = eis_results[pn]
        c = r["color"]
        if c == "GREEN":
            pts, txt = 1, "🟢+1"
        elif c == "RED":
            pts, txt = -1, "🔴-1"
        else:
            pts, txt = 0, "🔵 0"
        eis_score += pts
        eis_details.append((pn, PERIOD_LABEL.get(pn, pn), c, pts, txt,
                           r.get("ema_slope","?"), r.get("hist_slope","?"),
                           r.get("hist_cur", 0), r.get("hist_prev", 0)))

    # ── 第三步: 本套 Triple Screen 自身打分 ──
    # S1趋势±1 / S2回调信号±1 / S3入场信号±2 → 范围 [-4, +4]
    ts_raw = 0
    s1t = ts_eval.get("s1_trend", "neutral")
    s2s = ts_eval.get("s2_signal", "no_signal")
    s3s = ts_eval.get("s3_signal", "no_signal")

    if s1t == "bullish":
        ts_raw += 1
    elif s1t == "bearish":
        ts_raw -= 1
    if s2s == "buy_signal":
        ts_raw += 1
    elif s2s == "sell_signal":
        ts_raw -= 1
    if s3s in ("pending_long", "triggered_long"):
        ts_raw += 2
    elif s3s in ("pending_short", "triggered_short"):
        ts_raw -= 2

    # ── 第四步: 加权汇总 (本 set 独立评分) ──
    # EIS 50% + TS 50% (长线偏宏观, 短线偏敏捷)
    ts_weight = 0.5  # 统一 50% 权重，两者平等
    total = eis_score * 0.5 + ts_raw * ts_weight

    # ── 第五步: 可信度裁决 (仅针对本 set) ──
    if total >= 2.0:
        verdict = "✅ 强烈可信 — EIS+TS 一致确认, 主力仓位"
    elif total >= 1.0:
        verdict = "✅ 可信 — 信号方向明确, 正常仓位"
    elif total >= 0.3:
        verdict = "⚠️ 谨慎 — 信号偏弱, 建议半仓"
    elif total >= -0.3:
        verdict = "⚠️ 观望 — EIS与TS矛盾, 不交易"
    elif total >= -1.0:
        verdict = "⚠️ 谨慎偏空"
    elif total >= -2.0:
        verdict = "❌ 做空可信"
    else:
        verdict = "❌ 强烈做空可信"

    # ── 第六步: 风险提示 ──
    warnings = []
    green_count = sum(1 for r in eis_results.values() if r["color"] == "GREEN")
    red_count   = sum(1 for r in eis_results.values() if r["color"] == "RED")
    blue_count  = sum(1 for r in eis_results.values() if r["color"] == "BLUE")

    # 多周期冲突
    if green_count > 0 and red_count > 0:
        warnings.append(f"本 set EIS 多周期冲突: {green_count}个看多 vs {red_count}个看空 → 趋势不统一")
    if blue_count >= 2:
        warnings.append(f"本 set {blue_count}个周期 EIS 蓝色(方向不明) → 趋势模糊, 不宜重仓")

    # EIS 与 Triple Screen 方向冲突 (本 set 内)
    ts_dir = "long" if ts_raw > 0 else ("short" if ts_raw < 0 else "neutral")
    eis_dir = "long" if eis_score > 0 else ("short" if eis_score < 0 else "neutral")
    if ts_dir == "long" and eis_dir == "short":
        warnings.append("致命冲突: 本 set Triple Screen 看多但 EIS 一致看空 → 信号极不可靠, 不入场")
    elif ts_dir == "short" and eis_dir == "long":
        warnings.append("致命冲突: 本 set Triple Screen 看空但 EIS 一致看多 → 信号极不可靠, 不入场")

    return {
        "score": total,
        "eis_score": eis_score,
        "ts_score": ts_raw,
        "eis_colors": eis_results,
        "eis_details": eis_details,
        "verdict": verdict,
        "warnings": warnings,
    }


# ── 三重滤网综合判定 ──────────────────────────────────────
def evaluate_triple_screen(k_s1, k_s2, k_s3, tick_size):
    """
    对一组 (Screen1, Screen2, Screen3) K 线运行三重滤网判定.

    Returns:
        {
            "s1_trend":      "bullish" / "bearish" / "neutral"
            "s1_hist_slope": ...
            "s1_ema_slope":  ...
            "s1_hist_recent":[...]
            "s2_signal":     "buy_signal" / "sell_signal" / "no_signal" / ...
            "s2_fi_value":   float
            "s2_pullback":   str
            "s3_signal":     "pending_long" / "triggered_short" / ...
            "s3_entry":      float
            "s3_stop":       float
            "s3_desc":       str
            "combined":      "no_signal" / "pending_long" / "triggered_short" / ...
                             (取 s3.signal, cancelled/no_signal 都归为 no_signal)
        }
    """
    s1 = determine_screen1_trend(k_s1)
    s2 = determine_screen2_signal(s1["trend"], k_s2)
    s3 = determine_screen3_entry(s1["trend"], s2["signal"], k_s3, tick_size=tick_size)

    # combined signal: 主要看 Screen 3
    s3_sig = s3["signal"]
    if s3_sig in ("cancelled", "none", ""):
        combined = "no_signal"
    else:
        combined = s3_sig

    return {
        "s1_trend":      s1["trend"],
        "s1_hist_slope": s1["hist_slope"],
        "s1_ema_slope":  s1["ema_slope"],
        "s1_hist_recent": s1["hist_recent"],
        "s1_ema_recent": s1["ema_recent"],
        "s2_signal":     s2["signal"],
        "s2_fi_value":   s2["fi_value"],
        "s2_price_confirmed": s2.get("price_confirmed", False),
        "s2_pullback":   s2["pullback_desc"],
        "s3_signal":     s3["signal"],
        "s3_entry":      s3["entry_price"],
        "s3_stop":       s3["stop_loss"],
        "s3_desc":       s3["desc"],
        "combined":      combined,
    }


# ── 信号事件触发 ──────────────────────────────────────────
def fire_signal(set_name, contract_name, change_type, old_state, new_state, eis_extra=None):
    """
    触发信号: 写日志 + 弹通知 + 响铃.
    change_type: "signal_change" / "trend_reversal"
    """
    now_str = datetime.now().strftime("%H:%M:%S")

    # 冷却期: 同一 (set, contract, change_type, old, new)
    key = (set_name, contract_name, change_type,
           old_state.get("combined", ""),
           new_state.get("combined", ""))
    now_ts = _time.time()
    if key in _last_alert_time and now_ts - _last_alert_time[key] < ALERT_COOLDOWN:
        return False
    _last_alert_time[key] = now_ts

    is_critical = change_type == "signal_change" and "triggered" in new_state.get("combined", "")
    sound_level = "critical" if is_critical else "normal"

    # ── 控制台详细日志 ──
    print(f"\n{'='*72}")
    if change_type == "signal_change":
        old_s = old_state.get("combined", "no_signal")
        new_s = new_state.get("combined", "no_signal")
        print(f"  ⚠️  三重滤网信号变化 | {now_str}  [{set_name} | {contract_name}]")
        print(f"  {'='*72}")
        print(f"  变化: {SIGNAL_DESC.get(old_s, old_s)}  →  {SIGNAL_DESC.get(new_s, new_s)}")
    else:  # trend_reversal
        old_t = old_state.get("s1_trend", "neutral")
        new_t = new_state.get("s1_trend", "neutral")
        print(f"  ⚠️  Screen 1 趋势反转 | {now_str}  [{set_name} | {contract_name}]")
        print(f"  {'='*72}")
        print(f"  变化: {TREND_DESC.get(old_t, old_t)}  →  {TREND_DESC.get(new_t, new_t)}")

    print(f"  {'─'*68}")
    print(f"  Screen 1 ({PERIOD_LABEL.get(SET_BY_NAME.get(set_name, {}).get('screen1_period', ''), '')})")
    s1_t = new_state['s1_trend']
    s1_hs = new_state['s1_hist_slope']
    s1_es = new_state['s1_ema_slope']
    print(f"    趋势: {TREND_DESC.get(s1_t, s1_t)}")
    # ── MACD 柱 bar-to-bar ──
    hist_recent = new_state.get("s1_hist_recent", [])
    if len(hist_recent) >= 2:
        h_prev, h_cur = hist_recent[0], hist_recent[-1]
        h_delta = h_cur - h_prev
        h_thresh = (abs(h_prev) + abs(h_cur)) / 2 * 0.05
        print(f"    MACD柱: {h_prev:+.2f} → {h_cur:+.2f}  Δ={h_delta:+.2f}  ({s1_hs}, 阈值±{h_thresh:.2f})")
    elif hist_recent:
        print(f"    MACD柱: {hist_recent[-1]:+.2f}  ({s1_hs})")
    else:
        print(f"    MACD柱斜率: {s1_hs}")
    # ── EMA(13) 10-bar 斜率 ──
    ema_recent = new_state.get("s1_ema_recent")
    if ema_recent and len(ema_recent) == 2:
        e_cur, e_past = ema_recent[0], ema_recent[1]  # (current, past)
        e_delta = e_cur - e_past
        print(f"    EMA(13): {e_past:.2f} → {e_cur:.2f}  Δ={e_delta:+.2f}  ({s1_es})")
    else:
        print(f"    EMA(13)斜率: {s1_es}")

    print(f"  Screen 2 ({PERIOD_LABEL.get(SET_BY_NAME.get(set_name, {}).get('screen2_period', ''), '')})")
    print(f"    信号: {new_state['s2_signal']}  | FI: {new_state['s2_fi_value']:.2f}  | 价格确认: {'✅' if new_state.get('s2_price_confirmed') else '❌'}")
    print(f"    {new_state['s2_pullback']}")

    print(f"  Screen 3 ({PERIOD_LABEL.get(SET_BY_NAME.get(set_name, {}).get('screen3_period', ''), '')})")
    print(f"    信号: {new_state['s3_signal']}")
    if new_state["s3_signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
        entry_p = new_state['s3_entry']
        stop_p  = new_state['s3_stop']
        print(f"    入场价: {entry_p:.0f}  止损价: {stop_p:.0f}  风险: {abs(entry_p - stop_p):.0f}")
        if "long" in new_state["s3_signal"]:
            print(f"    ▸ 买入止损单 (Buy Stop): 价格突破 {entry_p:.0f} 做多, 跌破 {stop_p:.0f} 止损")
        elif "short" in new_state["s3_signal"]:
            print(f"    ▸ 卖出止损单 (Sell Stop): 价格跌破 {entry_p:.0f} 做空, 突破 {stop_p:.0f} 止损")
        if "pending" in new_state["s3_signal"]:
            print(f"    ▸ 当前为预挂状态, 尚未成交, 等待价格触发")
    print(f"    {new_state['s3_desc']}")
    print(f"  {'='*72}\n", flush=True)

    # ── EIS 交叉验证 ──
    if eis_extra:
        print(f"  ╔{'═'*68}╗")
        print(f"  ║  EIS 动力系统交叉验证")
        print(f"  ╠{'═'*68}╣")

        # 各周期 EIS 颜色
        for pn, plabel, color, pts, ptxt, ema_s, hist_s, hist_cur, hist_prev in eis_extra["eis_details"]:
            ce = EIS_COLOR_EMOJI.get(color, "⬜")
            direction = "↑" if hist_s == "UP" else ("↓" if hist_s == "DOWN" else "→")
            print(f"  ║  [{plabel:<6}] {ce} {color:<5} "
                  f"(EMA{ema_s}/MACD柱{hist_s})  "
                  f"MACD柱: {hist_prev:+.2f} {direction} {hist_cur:+.2f}  {ptxt}")

        print(f"  ╠{'─'*68}╣")

        # 打分明细
        print(f"  ║  EIS 动力系统: {eis_extra['eis_score']:+d}分 × 0.5 = {eis_extra['eis_score']*0.5:+.1f}")
        print(f"  ║  Triple Screen: {eis_extra['ts_score']:+d}分 × 0.5 = {eis_extra['ts_score']*0.5:+.1f}")
        print(f"  ║  加权总分: {eis_extra['score']:+.1f}")
        print(f"  ╠{'─'*68}╣")
        print(f"  ║  ▶ 可信度: {eis_extra['verdict']}")

        # 风险提示
        if eis_extra["warnings"]:
            print(f"  ╠{'─'*68}╣")
            print(f"  ║  ⚠️ 风险提示:")
            for w in eis_extra["warnings"]:
                print(f"  ║    • {w}")

        print(f"  ╚{'═'*68}╝\n", flush=True)

    # ── Windows Toast 通知 ──
    if change_type == "signal_change":
        title = f"三重滤网 {set_name}: {contract_name}"
        body = f"{SIGNAL_DESC.get(new_state['combined'], new_state['combined'])}\n时间: {now_str}"
        if new_state["s3_signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
            ep = new_state['s3_entry']
            sp = new_state['s3_stop']
            risk = abs(ep - sp)
            body += f"\n入场价: {ep:.0f}  止损价: {sp:.0f}  风险: {risk:.0f}"
            if "pending" in new_state["s3_signal"]:
                body += "\n(预挂单, 待价格触发)"
        # EIS 验证摘要
        if eis_extra:
            eis_line = " | ".join(
                f"{PERIOD_LABEL.get(p, p)}:{EIS_COLOR_EMOJI.get(c, '')}"
                for p, _, c, _, _, _, _, _, _ in eis_extra["eis_details"]
            )
            body += f"\n\nEIS验证: {eis_line}"
            body += f"\n可信度评分: {eis_extra['score']:+.1f}"
            short_verdict = eis_extra['verdict'].split('—')[0].strip() if '—' in eis_extra['verdict'] else eis_extra['verdict'][:12]
            body += f"\n{short_verdict}"
    else:
        title = f"趋势反转 {set_name}: {contract_name}"
        body = (f"{TREND_DESC.get(old_state['s1_trend'])} → "
                f"{TREND_DESC.get(new_state['s1_trend'])}\n时间: {now_str}")
    notify_windows(title, body)
    play_alert_sound(sound_level)

    _event_log.append({
        "time": now_str,
        "set": set_name,
        "contract": contract_name,
        "change_type": change_type,
        "old": old_state.get("combined") if change_type == "signal_change" else old_state.get("s1_trend"),
        "new": new_state.get("combined") if change_type == "signal_change" else new_state.get("s1_trend"),
    })
    return True


# ── 状态打印 ──────────────────────────────────────────────
def print_status(state):
    """打印所有 (set, contract) 的当前状态, pending/triggered 时显示入场价和止损"""
    print(f"\n  [{''.join(['─']*72)}]")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] 状态快照")
    print(f"  {'─'*72}")
    print(f"  {'Set':<10} {'合约':<14} {'屏1趋势':<14} {'屏2':<14} {'屏3信号':<20} {'入场/止损':<16}")
    print(f"  {'─'*72}")

    for (set_name, contract_name), s in state.items():
        s1_t = s["current"].get("s1_trend", "neutral")
        s2_s = s["current"].get("s2_signal", "no_signal")
        comb = s["current"].get("combined", "no_signal")
        entry = s["current"].get("s3_entry", 0) or 0
        stop  = s["current"].get("s3_stop",  0) or 0

        trend_str = TREND_DESC.get(s1_t, s1_t)
        sig_str   = SIGNAL_DESC.get(comb, comb)

        # 显示入场/止损价格
        if comb in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
            price_str = f"{entry:.0f} / {stop:.0f}"
        else:
            price_str = "—"

        print(f"  {set_name:<10} {contract_name:<14} {trend_str:<14} {s2_s:<14} {sig_str:<20} {price_str:<16}")
    print(f"  {'─'*72}", flush=True)


def print_startup_banner(state):
    """启动横幅"""
    print(f"\n{'='*72}")
    print(f"  Triple Screen 多周期信号实时监控  | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}")
    print(f"  监控周期组合:")
    for s in TRIPLE_SETS:
        print(f"    [{s['set_name']}] {s['desc']}")
        print(f"      Screen 1 (趋势): {PERIOD_LABEL[s['screen1_period']]:<6}  →  "
              f"Screen 2 (回调): {PERIOD_LABEL[s['screen2_period']]:<6}  →  "
              f"Screen 3 (入场): {PERIOD_LABEL[s['screen3_period']]}")
    print(f"\n  监控合约:")
    seen = set()
    for (set_name, contract_name), s in state.items():
        key = (set_name, contract_name)
        if key in seen:
            continue
        seen.add(key)
        print(f"    [{set_name}] {contract_name} ({s['symbol']})")
    print(f"\n  信号触发时机:")
    print(f"    1. Screen 3 状态变化 (pending/triggered/cancelled/no_signal)")
    print(f"    2. Screen 1 趋势反转 (bullish↔bearish)")
    print(f"  交叉验证:")
    print(f"    信号触发时自动运行 EIS 动力系统多周期检查")
    print(f"    加权评分体系: EIS(×0.5) + Triple Screen(×0.5) — 每套独立评估")
    print(f"\n  交易时段:")
    print(f"    白盘: 09:00-10:15 / 10:30-11:30 / 13:30-15:00")
    print(f"    夜盘: 21:00-23:00 (CF/C 有夜盘; CJ/JD/LH 仅白盘)")
    print(f"  通知方式: Windows 桌面 Toast + 控制台响铃")
    print(f"  冷却期  : 同一信号 {ALERT_COOLDOWN} 秒内不重复")
    print(f"  按 Ctrl+C 退出监控")
    print(f"{'='*72}\n", flush=True)


# ── 主程序 ────────────────────────────────────────────────
def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        # ── 发现所有主力合约 ──
        print("\n--- 发现主力合约 ---")
        contracts = []
        for cfg in CONTRACTS:
            print(f"\n  [{cfg['name']}] {cfg['exchange']}.{cfg['product']}:")
            symbol = discover_main_contract_generic(api, cfg["exchange"], cfg["product"])
            if symbol is None:
                print(f"    ⚠️  跳过 (未找到合约)")
                continue
            contracts.append({
                "symbol": symbol,
                "name": f"{cfg['name']} ({symbol.split('.')[-1].upper()})",
                "base_name": cfg["name"],  # 用于排除匹配, e.g. "玉米"
                "tick": cfg["tick"],
                "night": cfg["night"],
            })

        if not contracts:
            print("\n  ⚠️  没有发现任何合约, 退出")
            return

        print(f"\n  监控 {len(contracts)} 个合约:")
        for c in contracts:
            print(f"  {c['name']:<24} {c['symbol']:<16} tick={c['tick']} 夜盘={'有' if c['night'] else '无'}")

        # ── 订阅所有 K 线 (共享, 每合约 × 5 周期) ──
        # klines_store[(symbol, period_key)] = K 线 DataFrame
        all_periods = ["weekly", "daily", "hourly", "15min", "3min"]
        klines_store = {}
        print(f"\n订阅 K 线 ({len(contracts)} 合约 × {len(all_periods)} 周期 = {len(contracts)*len(all_periods)} 个)...")
        for c in contracts:
            for period in all_periods:
                k = api.get_kline_serial(c["symbol"], PERIOD_DUR[period], data_length=DATA_LEN)
                klines_store[(c["symbol"], period)] = k
                print(f"  {c['name']:<22} {PERIOD_LABEL[period]:<6} 订阅完成")

        # ── 等待数据加载 ──
        print("\n等待数据加载...")
        deadline = _time.time() + 25
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            all_ready = all(
                klines_store[(c["symbol"], p)].iloc[-1]["close"] > 0
                for c in contracts for p in all_periods
            )
            if all_ready:
                break
        print("  数据加载完成")

        # ── 初始化每个 (set, contract) 状态 ──
        # state[(set_name, contract_name)] = {
        #     "symbol": ..., "name": ..., "tick": ..., "night": ...,
        #     "set_config": {...},
        #     "current": {...evaluate_triple_screen result...},
        #     "last_dt_per_screen": {"screen1": ns, "screen2": ns, "screen3": ns},
        # }
        state = {}

        # 夜盘时段无夜盘品种延迟初始化, 记录到 _lazy_contracts (等日盘再补)
        _lazy_contracts = []
        _night_now = is_night_session()

        def _init_state_for_contract(c):
            """为某个合约创建所有 set 的初始状态, 返回新增的 key 列表"""
            keys = []
            for ts in TRIPLE_SETS:
                # 检查该 set 是否排除了此合约
                exclude_names = SET_EXCLUDE_CONTRACTS.get(ts["set_name"], set())
                if c.get("base_name", c["name"]) in exclude_names:
                    print(f"  [{ts['set_name']}|{c['name']}] 已排除 (波动不适配), 跳过")
                    continue

                k_s1 = klines_store[(c["symbol"], ts["screen1_period"])]
                k_s2 = klines_store[(c["symbol"], ts["screen2_period"])]
                k_s3 = klines_store[(c["symbol"], ts["screen3_period"])]

                k_s1_closed = get_closed_klines(k_s1, c["night"])
                k_s2_closed = get_closed_klines(k_s2, c["night"])
                k_s3_closed = get_closed_klines(k_s3, c["night"])

                if len(k_s1_closed) < 30 or len(k_s2_closed) < 30 or len(k_s3_closed) < 5:
                    print(f"  ⚠️  [{ts['set_name']}|{c['name']}] 数据不足, 跳过")
                    continue

                init_eval = evaluate_triple_screen(k_s1_closed, k_s2_closed, k_s3_closed, c["tick"])
                key = (ts["set_name"], c["name"])
                state[key] = {
                    "symbol": c["symbol"],
                    "name":   c["name"],
                    "tick":   c["tick"],
                    "night":  c["night"],
                    "set_config": ts,
                    "current": init_eval,
                    "last_dt_per_screen": {
                        "screen1": get_last_closed_dt(k_s1, c["night"]),
                        "screen2": get_last_closed_dt(k_s2, c["night"]),
                        "screen3": get_last_closed_dt(k_s3, c["night"]),
                    },
                }
                keys.append(key)
            return keys

        for c in contracts:
            if _night_now and not c["night"]:
                print(f"  [{c['name']}] 无夜盘, 夜盘时段延迟初始化")
                _lazy_contracts.append(c)
                continue
            _init_state_for_contract(c)

        if not state:
            print("\n  ⚠️  没有任何监控对象初始化成功, 退出")
            return

        print_startup_banner(state)
        print_status(state)

        # ── 主循环 ──
        last_status_time = _time.time()

        while True:
            api.wait_update(deadline=_time.time() + SCAN_SEC)

            # ── 日盘开盘时补初始化无夜盘品种 ──
            if _lazy_contracts and not is_night_session():
                newly = []
                for c in _lazy_contracts:
                    keys = _init_state_for_contract(c)
                    newly.extend(keys)
                if newly:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    names = ", ".join(c2["name"] for c2 in _lazy_contracts)
                    print(f"\n  [{now_str}] 日盘开盘: 补初始化 {names}", flush=True)
                    print_status(state)
                _lazy_contracts = []

            # 检测每个 (set, contract) 状态
            for key, s in state.items():
                set_name     = key[0]
                contract_n   = key[1]
                ts           = s["set_config"]
                sym          = s["symbol"]
                tick         = s["tick"]
                night        = s["night"]

                # 夜盘时段跳过无夜盘品种
                if not night and is_night_session():
                    continue

                k_s1 = klines_store[(sym, ts["screen1_period"])]
                k_s2 = klines_store[(sym, ts["screen2_period"])]
                k_s3 = klines_store[(sym, ts["screen3_period"])]

                # 各 screen 最新已收盘 K 线时间
                dt1 = get_last_closed_dt(k_s1, night)
                dt2 = get_last_closed_dt(k_s2, night)
                dt3 = get_last_closed_dt(k_s3, night)

                # 是否任一周期有新 K 线收盘
                old_dt = s["last_dt_per_screen"]  # 先保存旧值
                new_bar = (
                    dt1 > old_dt["screen1"]
                    or dt2 > old_dt["screen2"]
                    or dt3 > old_dt["screen3"]
                )

                if not new_bar:
                    continue

                # 判断哪个周期有新 K 线 (在更新前用旧值对比)
                changed_periods = []
                if dt1 > old_dt["screen1"]:
                    changed_periods.append(ts["screen1_period"])
                if dt2 > old_dt["screen2"]:
                    changed_periods.append(ts["screen2_period"])
                if dt3 > old_dt["screen3"]:
                    changed_periods.append(ts["screen3_period"])

                # 更新已收盘时间记录
                s["last_dt_per_screen"] = {"screen1": dt1, "screen2": dt2, "screen3": dt3}

                # 重新评估三重滤网
                k_s1_closed = get_closed_klines(k_s1, night)
                k_s2_closed = get_closed_klines(k_s2, night)
                k_s3_closed = get_closed_klines(k_s3, night)
                if len(k_s1_closed) < 30 or len(k_s2_closed) < 30 or len(k_s3_closed) < 5:
                    continue

                new_eval = evaluate_triple_screen(k_s1_closed, k_s2_closed, k_s3_closed, tick)

                old_combined = s["current"].get("combined", "no_signal")
                new_combined = new_eval.get("combined", "no_signal")
                old_trend    = s["current"].get("s1_trend", "neutral")
                new_trend    = new_eval.get("s1_trend", "neutral")

                # ── 信号变化通知 ──
                if old_combined != new_combined or (
                    old_trend in ("bullish", "bearish")
                    and new_trend in ("bullish", "bearish")
                    and old_trend != new_trend
                ):
                    # 计算 EIS 交叉验证 (信号变化或趋势反转时)
                    eis_periods = EIS_PERIODS_FOR_SET.get(set_name, ["daily", "hourly"])
                    klines_closed_for_eis = {}
                    for pn in eis_periods:
                        k = klines_store.get((sym, pn))
                        if k is not None:
                            klines_closed_for_eis[pn] = get_closed_klines(k, night)
                    eis_verify = compute_eis_cross_verify(
                        klines_closed_for_eis, set_name, new_eval, tick)

                    # ── EIS 交叉验证门控: 无信号时不提醒 ──
                    eis_score = eis_verify["score"]
                    if abs(eis_score) < 0.3 and klines_closed_for_eis:
                        # 交叉验证结果为"观望/无信号" → 静默, 不弹通知
                        now_str = datetime.now().strftime("%H:%M:%S")
                        print(f"\n  [{now_str}] [{set_name}|{contract_n}] "
                              f"EIS交叉验证: 信号不可信 (得分{eis_score:+.1f}) → 不提醒",
                              flush=True)
                    else:
                        if old_combined != new_combined:
                            fire_signal(set_name, contract_n, "signal_change",
                                        s["current"], new_eval, eis_extra=eis_verify)
                        else:
                            fire_signal(set_name, contract_n, "trend_reversal",
                                        s["current"], new_eval, eis_extra=eis_verify)

                # ── 静默状态更新 (即使无信号变化, 也刷新 s2_fi 等数值) ──
                else:
                    # 新 K 线收盘时简短打印
                    now_str = datetime.now().strftime("%H:%M:%S")
                    sig_str = SIGNAL_DESC.get(new_combined, new_combined)
                    periods_str = ",".join(PERIOD_LABEL.get(p, p) for p in changed_periods)
                    print(f"  [{now_str}] [{set_name}|{contract_n}] "
                          f"新K线收盘 {periods_str}  "
                          f"信号: {sig_str}", flush=True)

                s["current"] = new_eval

            # 非交易时段定期状态行
            now_ts = _time.time()
            if now_ts - last_status_time > STATUS_SEC:
                last_status_time = now_ts
                # 任意合约不在交易时段时打印
                any_off = any(not is_trading_time(s["night"]) for s in state.values())
                if any_off:
                    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] "
                          f"⏸ 非交易时段, 监控休眠中...", flush=True)

    except KeyboardInterrupt:
        print(f"\n\n{'='*72}")
        print(f"  监控已停止 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*72}")
        if _event_log:
            print(f"\n  本次监控共捕获 {len(_event_log)} 次信号:\n")
            for i, e in enumerate(_event_log, 1):
                print(f"    {i}. {e['time']}  [{e['set']}|{e['contract']}]")
                print(f"       类型: {e['change_type']}")
                print(f"       {e['old']}  →  {e['new']}")
        else:
            print(f"\n  本次监控未捕获任何信号")
        print(f"\n{'='*72}")
    finally:
        try:
            api.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
