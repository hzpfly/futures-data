"""
鸡蛋主力合约 1/5/25分钟K线实时监控
自动识别当前主力合约（持仓量最大），免费版天勤不支持 KQ.m@DCE.JD
周期: 1分钟 / 5分钟 / 25分钟 三周期并行
更新: 任意周期新 bar 形成时打印全部三个周期

用法:
    pip install tqsdk
    python egg_futures_1min.py
"""

from tqsdk import TqApi, TqAuth
import pandas as pd
from datetime import datetime, time
import time as _time
from config_loader import get_tqsdk_auth


# ── 三周期配置 ──────────────────────────────────────────
KLINE_DURS = {"1min": 60, "5min": 300, "25min": 1500}
DATA_LEN   = 200            # 每个周期保留最近 200 根
SHOW_N     = 8              # 打印最近 N 根
WAIT_SEC   = 3              # 盘后 wait_update 超时秒数
STATUS_SLOT = 30            # 盘后状态刷新间隔（秒）

# DCE 鸡蛋期货交易时段（日盘，无夜盘）
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]


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


def fmt_time(ns_datetime):
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%m-%d %H:%M")
    return "---"


def calc_macd(klines, fast=12, slow=26, signal=9):
    """计算 MACD：返回 (DIF, DEA, MACD柱) 三个 Series"""
    closes = klines["close"]
    ema12  = closes.ewm(span=fast, adjust=False).mean()
    ema26  = closes.ewm(span=slow, adjust=False).mean()
    dif    = ema12 - ema26
    dea    = dif.ewm(span=signal, adjust=False).mean()
    bar    = 2 * (dif - dea)
    return dif, dea, bar


def print_macd_summary(klines):
    """在 25 分钟 K 线下方打印 MACD 指标"""
    dif, dea, bar = calc_macd(klines)
    # 最近 4 根
    recent_idx = klines.index[-4:]
    if len(recent_idx) < 2:
        return

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
            s = f"\033[31m▲ 多\033[0m"    # 红柱 = 多头
        else:
            s = f"\033[32m▼ 空\033[0m"    # 绿柱 = 空头
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
    header = f"\n{'='*72}\n  鸡蛋主力 ({symbol})  1/5/25分钟K线  {datetime.now().strftime('%H:%M:%S')}\n{'='*72}"
    print(header)
    print_period_bars(klines_map["25min"], symbol, "25分钟 K线")
    print_macd_summary(klines_map["25min"])
    print_period_bars(klines_map["5min"],  symbol, "5分钟 K线")
    print_period_bars(klines_map["1min"],  symbol, "1分钟 K线")
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
