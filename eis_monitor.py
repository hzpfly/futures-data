"""
EIS 实时信号监控
=================
盘中监控 CF609 与 JD 主力合约的 25 分钟 EIS 信号 (Elder Impulse System)。
信号变化时弹出 Windows 桌面通知 + 控制台响铃 + 详细日志。

EIS 信号规则:
  GREEN  : EMA(13)↑ AND MACD柱↑ → 多头加速，只允许买入
  RED    : EMA(13)↓ AND MACD柱↓ → 空头加速，只允许卖出
  BLUE   : 方向冲突             → 中性，等待

信号触发时机:
  🔵→🟢 蓝变绿 : 多头动量启动 → 可买入做多
  🔵→🔴 蓝变红 : 空头动量启动 → 可卖出做空
  🟢→🔵 绿变蓝 : 多头动量衰竭 → 立即平多仓 (快速退出)
  🔴→🔵 红变蓝 : 空头动量衰竭 → 立即平空仓 (快速退出)
  🟢→🔴 / 🔴→🟢 : 极端反转

设计要点:
  - 基于已收盘 K 线计算 EIS，避免未收盘 K 线噪音
  - 交易时段内：用倒数第二根 (iloc[-2]) 作为已收盘 K 线
  - 盘后时段：用最后一根 (iloc[-1]) 作为已收盘 K 线
  - 每 10 秒检测一次，新 K 线收盘时必检
  - 同一颜色变化只通知一次，避免重复

用法:
    python eis_monitor.py
    按 Ctrl+C 退出
"""

from tqsdk import TqApi, TqAuth
from datetime import datetime, time
import time as _time
import subprocess
import sys
import winsound

from config_loader import get_tqsdk_auth
from egg_futures_1min import calc_macd, calc_ema, discover_main_contract
from weekly_eis import determine_eis_color


# ── 配置 ──────────────────────────────────────────────────
KLINE_DUR  = 1500       # 25 分钟
DATA_LEN   = 200
SCAN_SEC   = 10         # 主循环 wait_update 超时
STATUS_SEC = 60         # 状态行刷新间隔
ALERT_COOLDOWN = 300    # 同一合约同一信号 5 分钟内不重复通知

# 日盘交易时段 (鸡蛋只有日盘；棉花有夜盘但这里仅监控日盘)
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]

COLOR_EMOJI = {"GREEN": "🟢", "RED": "🔴", "BLUE": "🔵"}

# 信号含义
SIGNAL_MEANING = {
    ("BLUE",  "GREEN"): "🔵→🟢 蓝变绿: 多头动量启动 → 可买入做多",
    ("BLUE",  "RED"  ): "🔵→🔴 蓝变红: 空头动量启动 → 可卖出做空",
    ("GREEN", "BLUE" ): "🟢→🔵 绿变蓝: 多头动量衰竭 → 立即平多仓 (快速退出)",
    ("RED",   "BLUE" ): "🔴→🔵 红变蓝: 空头动量衰竭 → 立即平空仓 (快速退出)",
    ("GREEN", "RED"  ): "🟢→🔴 绿变红: 多头极端反转 → 平多开空",
    ("RED",   "GREEN"): "🔴→🟢 红变绿: 空头极端反转 → 平空开多",
}


# ── 全局状态 ───────────────────────────────────────────────
_event_log = []           # [(time, symbol, name, from, to, eis)]
_last_alert_time = {}     # {(symbol, from, to): timestamp} - 冷却期


# ── 工具函数 ───────────────────────────────────────────────
def is_trading_time():
    now = datetime.now().time()
    return any(start <= now <= end for start, end in TRADING_SESSIONS)


def next_trading_time():
    now = datetime.now().time()
    for start, end in TRADING_SESSIONS:
        if now < start:
            return f"{start:%H:%M}"
    return "次日 09:00"


def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%m-%d %H:%M")
    return "---"


def get_closed_bar_eis(klines, name=""):
    """基于已收盘 K 线计算 EIS 颜色。
    交易时段内：最后一根 K 线还在形成，用 iloc[:-1] (倒数第二根开始)
    盘后时段：所有 K 线都已收盘，直接用全部"""
    valid = klines[klines["close"] > 0]
    if len(valid) < 30:
        return {"color": "BLUE", "note": "数据不足 30 根", "last_close": 0,
                "ema_cur": 0, "ema_slope": "FLAT", "hist_cur": 0,
                "hist_slope": "FLAT", "dif": 0, "dea": 0, "last_time": ""}

    # 交易时段内剔除还在形成的最后一根
    if is_trading_time() and len(valid) > 30:
        valid = valid.iloc[:-1]

    return determine_eis_color(valid, name)


def notify_windows(title, body):
    """弹 Windows 10/11 Toast 通知"""
    # 转义 PowerShell 字符串中的特殊字符
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
        f'CreateToastNotifier("EIS Monitor").Show($toast)\n'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=5, check=False, capture_output=True
        )
    except Exception as e:
        print(f"  [Windows 通知发送失败: {e}]")


def play_alert_sound():
    """播放提醒铃声 (3 个短促音)"""
    try:
        winsound.Beep(880, 200)
        winsound.Beep(1100, 200)
        winsound.Beep(1320, 300)
    except Exception:
        pass


def fire_signal(sym, name, old_color, new_color, eis):
    """触发信号：写日志 + 弹通知 + 响铃"""
    now_str = datetime.now().strftime("%H:%M:%S")

    # 冷却期检查：同一 (symbol, from, to) 5 分钟内不重复
    key = (sym, old_color, new_color)
    now_ts = _time.time()
    if key in _last_alert_time and now_ts - _last_alert_time[key] < ALERT_COOLDOWN:
        return False
    _last_alert_time[key] = now_ts

    meaning = SIGNAL_MEANING.get(
        (old_color, new_color),
        f"{old_color}→{new_color} 颜色变化"
    )

    # ── 控制台详细日志 ──
    print(f"\n{'='*64}")
    print(f"  ⚠️  EIS 信号触发 | {now_str}")
    print(f"  {'='*64}")
    print(f"  合约: {name} ({sym})")
    print(f"  变化: {COLOR_EMOJI[old_color]} {old_color}  →  {COLOR_EMOJI[new_color]} {new_color}")
    print(f"  操作: {meaning}")
    print(f"  {'─'*60}")
    print(f"  最新价    : {eis['last_close']:.0f}")
    print(f"  EMA(13)   : {eis['ema_cur']:.2f}  斜率: {eis['ema_slope']}"
          f"  (前值: {eis['ema_prev']:.2f})")
    print(f"  MACD 柱   : {eis['hist_cur']:.2f}  斜率: {eis['hist_slope']}"
          f"  (前值: {eis['hist_prev']:.2f})")
    print(f"  DIF / DEA : {eis['dif']:.2f} / {eis['dea']:.2f}")
    print(f"  K线时间   : {eis['last_time']}")
    print(f"  {'='*64}\n", flush=True)

    # ── Windows Toast 通知 ──
    notify_windows(
        f"EIS 信号: {name}",
        f"{meaning}\n价格: {eis['last_close']:.0f}  时间: {now_str}"
    )

    # ── 响铃 ──
    play_alert_sound()

    # ── 记录事件 ──
    _event_log.append({
        "time": now_str,
        "symbol": sym,
        "name": name,
        "from": old_color,
        "to": new_color,
        "price": eis["last_close"],
        "meaning": meaning,
    })

    return True


def print_status(state):
    """打印当前状态快照"""
    trading = is_trading_time()
    status_str = "🟢 交易时段" if trading else f"⏸ 非交易时段 (下次: {next_trading_time()})"
    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] {status_str}")
    print(f"  {'─'*60}")
    for sym, s in state.items():
        eis = s["last_eis"]
        ce = COLOR_EMOJI.get(eis["color"], "⬜")
        print(f"  {ce} {s['name']:<20} {sym:<20} "
              f"颜色: {eis['color']:<6} 价格: {eis['last_close']:.0f}")
    print(f"  {'─'*60}", flush=True)


def main():
    username, password = get_tqsdk_auth()
    print(f"Connecting to TqSdk (account: {username})...")
    api = TqApi(auth=TqAuth(username, password))

    try:
        # ── 确定 JD 主力 ──
        print("\n--- 确定 JD 主力合约 ---")
        jd_symbol = discover_main_contract(api)
        print(f"  JD 主力: {jd_symbol}")

        # ── 监控列表 ──
        watchlist = [
            ("CZCE.CF609", "CF609 棉花"),
            (jd_symbol,    f"{jd_symbol.split('.')[-1].upper()} 鸡蛋主力"),
        ]

        # ── 订阅 25min K 线 ──
        state = {}
        for sym, name in watchlist:
            k25 = api.get_kline_serial(sym, KLINE_DUR, data_length=DATA_LEN)
            state[sym] = {
                "name": name,
                "klines": k25,
                "color": None,        # 已确认的上一颜色
                "last_dt": 0,         # 上次处理的已收盘 K 线时间
                "last_eis": {"color": "BLUE", "last_close": 0,
                             "ema_cur": 0, "ema_slope": "FLAT",
                             "hist_cur": 0, "hist_slope": "FLAT",
                             "dif": 0, "dea": 0,
                             "ema_prev": 0, "hist_prev": 0,
                             "last_time": ""},
            }

        # ── 等待数据加载 ──
        print("  等待数据加载...")
        deadline = _time.time() + 15
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time())
            if all(s["klines"].iloc[-1]["close"] > 0 for s in state.values()):
                break

        # ── 初始化 EIS 颜色 ──
        for sym, name in watchlist:
            eis = get_closed_bar_eis(state[sym]["klines"], name)
            state[sym]["color"]     = eis["color"]
            state[sym]["last_eis"]  = eis
            # 记录已收盘 K 线的 datetime
            valid = state[sym]["klines"][state[sym]["klines"]["close"] > 0]
            if is_trading_time() and len(valid) > 1:
                state[sym]["last_dt"] = valid.iloc[-2]["datetime"]
            else:
                state[sym]["last_dt"] = valid.iloc[-1]["datetime"]

        # ── 启动横幅 ──
        print(f"\n{'='*64}")
        print(f"  EIS 实时信号监控启动 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*64}")
        print(f"  监控品种:")
        for sym, s in state.items():
            ce = COLOR_EMOJI[s["last_eis"]["color"]]
            print(f"    {ce} {s['name']:<20} {sym}")
        print(f"\n  信号触发时机:")
        for (f, t), m in SIGNAL_MEANING.items():
            print(f"    {COLOR_EMOJI[f]}→{COLOR_EMOJI[t]}  {m}")
        print(f"\n  交易时段: 09:00-10:15, 10:30-11:30, 13:30-15:00")
        print(f"  通知方式: Windows 桌面 Toast + 控制台响铃")
        print(f"  冷却期  : 同一信号 {ALERT_COOLDOWN} 秒内不重复")
        print(f"  按 Ctrl+C 退出监控")
        print(f"{'='*64}\n", flush=True)

        print_status(state)

        # ── 主循环 ──
        last_status_time = _time.time()

        while True:
            api.wait_update(deadline=_time.time() + SCAN_SEC)

            # 检测每个合约
            for sym, name in watchlist:
                s   = state[sym]
                k   = s["klines"]
                valid = k[k["close"] > 0]
                if len(valid) == 0:
                    continue

                # 已收盘 K 线的 datetime
                if is_trading_time() and len(valid) > 1:
                    closed_dt = valid.iloc[-2]["datetime"]
                else:
                    closed_dt = valid.iloc[-1]["datetime"]

                # 检测是否新 K 线收盘
                new_bar_closed = closed_dt > s["last_dt"]
                if new_bar_closed:
                    s["last_dt"] = closed_dt

                # 重新计算 EIS
                eis = get_closed_bar_eis(k, name)

                # 颜色变化检测
                old_color = s["color"]
                new_color = eis["color"]

                if old_color is not None and new_color != old_color:
                    # 触发信号！
                    fired = fire_signal(sym, name, old_color, new_color, eis)
                    if fired:
                        s["color"]    = new_color
                        s["last_eis"] = eis
                elif new_bar_closed:
                    # 新 K 线但颜色未变，静默更新
                    s["last_eis"] = eis
                    # 打印简短状态（仅新 bar 收盘时）
                    ce = COLOR_EMOJI[new_color]
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                          f"{name}: 新25min K线收盘 {fmt_time(closed_dt)} "
                          f"{ce} {new_color}  价格: {eis['last_close']:.0f}",
                          flush=True)

            # 定期打印状态行
            if _time.time() - last_status_time > STATUS_SEC:
                last_status_time = _time.time()
                # 简短状态行
                trading = is_trading_time()
                if not trading:
                    statuses = " | ".join(
                        f"{s['name']}: {s['color']}" for s in state.values()
                    )
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                          f"⏸ 非交易时段 | {statuses}", flush=True)

    except KeyboardInterrupt:
        print(f"\n\n{'='*64}")
        print(f"  监控已停止 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*64}")
        if _event_log:
            print(f"\n  本次监控共捕获 {len(_event_log)} 次信号:\n")
            for i, e in enumerate(_event_log, 1):
                print(f"    {i}. {e['time']}  {e['name']}")
                print(f"       {COLOR_EMOJI[e['from']]}{e['from']} → "
                      f"{COLOR_EMOJI[e['to']]}{e['to']}  价格: {e['price']:.0f}")
                print(f"       {e['meaning']}")
        else:
            print(f"\n  本次监控未捕获任何信号")
        print(f"\n{'='*64}")
    finally:
        try:
            api.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
