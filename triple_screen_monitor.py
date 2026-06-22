"""
Triple Screen 多周期信号实时监控
================================
同时监控两套 Elder's Triple Screen 三重滤网组合:

  Set A (长线):  周线 → 日线 → 小时
                 (大趋势  /  中期回调  /  精确入场)

  Set B (短线):  小时 → 15min → 3min
                 (大趋势  /  中期回调  /  精确入场)

每套三重滤网覆盖两个合约:
  - CZCE.CF609   (棉花 2609, 含夜盘 21:00-23:00)
  - JD 主力        (持仓量最大合约, 仅日盘)

信号触发时机 (Screen 3 状态变化):
  no_signal  → pending_long / pending_short    : 入场机会出现
  pending_*  → triggered_long / triggered_short: 实际入场触发
  pending_*  → cancelled / no_signal           : 信号取消
  triggered* → cancelled / no_signal           : 信号消失/退出

Screen 1 趋势反转也独立通知 (决定平仓方向).

K 线订阅共享:
  Set A 的 Screen 3 (小时) == Set B 的 Screen 1 (小时)
  共 10 个独立订阅 = 5 周期 × 2 合约

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
    discover_main_contract,
)


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
# (symbol_template, name, tick_size, has_night_session)
# symbol_template 用 {JD_MAIN} 占位会替换成实际主力合约
CONTRACTS = [
    {"symbol": "CZCE.CF609", "name": "CF609 棉花", "tick": 5, "night": True},
    # JD 主力在运行时通过 discover_main_contract 填充
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
]

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
SIGNAL_DESC = {
    "no_signal":        "无信号",
    "pending_long":     "🟢 待做多 (买入止损单已挂)",
    "triggered_long":   "🟢🟢 做多触发!",
    "pending_short":    "🔴 待做空 (卖出止损单已挂)",
    "triggered_short":  "🔴🔴 做空触发!",
    "cancelled":        "❌ 信号取消",
}

TREND_DESC = {
    "bullish": "🟢 多头 (只做多)",
    "bearish": "🔴 空头 (只做空)",
    "neutral": "🔵 中性 (观望)",
}


# ── 全局状态 ──────────────────────────────────────────────
_event_log = []              # 信号事件历史
_last_alert_time = {}        # 冷却期记录


# ── 工具函数 ──────────────────────────────────────────────
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
    print(f"  Screen 1 ({PERIOD_LABEL.get(TRIPLE_SETS[0]['screen1_period'] if set_name.startswith('A') else TRIPLE_SETS[1]['screen1_period'], '')})")
    print(f"    趋势: {TREND_DESC.get(new_state['s1_trend'], new_state['s1_trend'])}")
    print(f"    MACD柱斜率: {new_state['s1_hist_slope']}  EMA(13)斜率: {new_state['s1_ema_slope']}")
    if new_state.get("s1_hist_recent"):
        print(f"    MACD柱近5根: {' → '.join(f'{v:.2f}' for v in new_state['s1_hist_recent'])}")

    print(f"  Screen 2 ({PERIOD_LABEL.get(TRIPLE_SETS[0]['screen2_period'] if set_name.startswith('A') else TRIPLE_SETS[1]['screen2_period'], '')})")
    print(f"    信号: {new_state['s2_signal']}  | FI: {new_state['s2_fi_value']:.2f}")
    print(f"    {new_state['s2_pullback']}")

    print(f"  Screen 3 ({PERIOD_LABEL.get(TRIPLE_SETS[0]['screen3_period'] if set_name.startswith('A') else TRIPLE_SETS[1]['screen3_period'], '')})")
    print(f"    信号: {new_state['s3_signal']}")
    if new_state["s3_signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
        print(f"    入场价: {new_state['s3_entry']:.0f}  止损: {new_state['s3_stop']:.0f}")
    print(f"    {new_state['s3_desc']}")
    print(f"  {'='*72}\n", flush=True)

    # ── Windows Toast 通知 ──
    if change_type == "signal_change":
        title = f"三重滤网 {set_name}: {contract_name}"
        body = f"{SIGNAL_DESC.get(new_state['combined'], new_state['combined'])}\n时间: {now_str}"
        if new_state["s3_signal"] in ("pending_long", "triggered_long", "pending_short", "triggered_short"):
            body += f"\n入场: {new_state['s3_entry']:.0f}  止损: {new_state['s3_stop']:.0f}"
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
    """打印所有 (set, contract) 的当前状态"""
    print(f"\n  [{''.join(['─']*72)}]")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] 状态快照")
    print(f"  {'─'*72}")
    print(f"  {'Set':<10} {'合约':<18} {'屏1趋势':<14} {'屏2信号':<16} {'屏3综合信号':<24}")
    print(f"  {'─'*72}")

    for (set_name, contract_name), s in state.items():
        s1_t = s["current"].get("s1_trend", "neutral")
        s2_s = s["current"].get("s2_signal", "no_signal")
        comb = s["current"].get("combined", "no_signal")

        trend_str = TREND_DESC.get(s1_t, s1_t)
        sig_str   = SIGNAL_DESC.get(comb, comb)
        print(f"  {set_name:<10} {contract_name:<18} {trend_str:<14} {s2_s:<16} {sig_str:<24}")
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
    print(f"\n  交易时段:")
    print(f"    白盘: 09:00-10:15 / 10:30-11:30 / 13:30-15:00")
    print(f"    夜盘: 21:00-23:00 (仅 CF609)")
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
        # ── 确定 JD 主力 ──
        print("\n--- 确定 JD 主力合约 ---")
        jd_symbol = discover_main_contract(api)
        print(f"  JD 主力: {jd_symbol}")

        # ── 构建合约列表 ──
        contracts = list(CONTRACTS) + [
            {"symbol": jd_symbol, "name": f"{jd_symbol.split('.')[-1].upper()} 鸡蛋主力",
             "tick": 1, "night": False},
        ]
        print(f"\n监控合约:")
        for c in contracts:
            print(f"  {c['name']:<22} {c['symbol']:<14} tick={c['tick']} 夜盘={'有' if c['night'] else '无'}")

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
        for c in contracts:
            for ts in TRIPLE_SETS:
                k_s1 = klines_store[(c["symbol"], ts["screen1_period"])]
                k_s2 = klines_store[(c["symbol"], ts["screen2_period"])]
                k_s3 = klines_store[(c["symbol"], ts["screen3_period"])]

                # 用已收盘 K 线
                k_s1_closed = get_closed_klines(k_s1, c["night"])
                k_s2_closed = get_closed_klines(k_s2, c["night"])
                k_s3_closed = get_closed_klines(k_s3, c["night"])

                if len(k_s1_closed) < 30 or len(k_s2_closed) < 30 or len(k_s3_closed) < 5:
                    print(f"  ⚠️  [{ts['set_name']}|{c['name']}] 数据不足, 跳过")
                    continue

                init_eval = evaluate_triple_screen(k_s1_closed, k_s2_closed, k_s3_closed, c["tick"])
                state[(ts["set_name"], c["name"])] = {
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

        if not state:
            print("\n  ⚠️  没有任何监控对象初始化成功, 退出")
            return

        print_startup_banner(state)
        print_status(state)

        # ── 主循环 ──
        last_status_time = _time.time()

        while True:
            api.wait_update(deadline=_time.time() + SCAN_SEC)

            # 检测每个 (set, contract) 状态
            for key, s in state.items():
                set_name     = key[0]
                contract_n   = key[1]
                ts           = s["set_config"]
                sym          = s["symbol"]
                tick         = s["tick"]
                night        = s["night"]

                k_s1 = klines_store[(sym, ts["screen1_period"])]
                k_s2 = klines_store[(sym, ts["screen2_period"])]
                k_s3 = klines_store[(sym, ts["screen3_period"])]

                # 各 screen 最新已收盘 K 线时间
                dt1 = get_last_closed_dt(k_s1, night)
                dt2 = get_last_closed_dt(k_s2, night)
                dt3 = get_last_closed_dt(k_s3, night)

                # 是否任一周期有新 K 线收盘
                new_bar = (
                    dt1 > s["last_dt_per_screen"]["screen1"]
                    or dt2 > s["last_dt_per_screen"]["screen2"]
                    or dt3 > s["last_dt_per_screen"]["screen3"]
                )

                if not new_bar:
                    continue

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
                if old_combined != new_combined:
                    fire_signal(set_name, contract_n, "signal_change",
                                s["current"], new_eval)

                # ── 趋势反转通知 (bullish ↔ bearish, 不算 neutral) ──
                elif (old_trend in ("bullish", "bearish")
                      and new_trend in ("bullish", "bearish")
                      and old_trend != new_trend):
                    fire_signal(set_name, contract_n, "trend_reversal",
                                s["current"], new_eval)

                # ── 静默状态更新 (即使无信号变化, 也刷新 s2_fi 等数值) ──
                else:
                    # 新 K 线收盘时简短打印
                    now_str = datetime.now().strftime("%H:%M:%S")
                    sig_str = SIGNAL_DESC.get(new_combined, new_combined)
                    which_period = ""
                    if dt1 > s["last_dt_per_screen"].get("screen1", 0) - 1:
                        which_period = ts["screen1_period"]
                    print(f"  [{now_str}] [{set_name}|{contract_n}] "
                          f"新K线收盘 {PERIOD_LABEL[which_period]}  "
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
