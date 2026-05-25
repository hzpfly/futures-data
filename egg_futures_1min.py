"""
鸡蛋主力合约 1分钟K线实时监控
自动识别当前主力合约（最近月合约），因为免费版天勤不支持 KQ.m@DCE.JD 主力连续格式
合约: 自动从 DCE 未过期鸡蛋合约中选取最近月
周期: 60 秒 K线
更新: 每当最新一根 K线的 datetime 发生变化时打印（新 bar 形成）
      同时监听 close 字段变化以实时刷新未收盘 bar 的价格

用法:
    pip install tqsdk
    python egg_futures_1min.py
"""

from tqsdk import TqApi, TqAuth
from datetime import datetime, time
import time as _time
from config_loader import get_tqsdk_auth


KLINE_DUR   = 60           # 1分钟 = 60秒
DATA_LEN    = 200           # 保留最近 200 根
WAIT_SEC    = 3             # 盘后 wait_update 超时秒数
STATUS_SLOT = 30            # 盘后状态刷新间隔（秒），避免频繁打印

# DCE 鸡蛋期货交易时段（日盘，无夜盘）
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]


def is_trading_time():
    """判断当前是否在 DCE 鸡蛋期货交易时段内"""
    now = datetime.now().time()
    return any(start <= now <= end for start, end in TRADING_SESSIONS)


def next_trading_time():
    """返回下一个交易时段的描述"""
    now = datetime.now().time()
    for start, end in TRADING_SESSIONS:
        if now < start:
            return f"{start:%H:%M}"
    # 今天全部收盘，明天 9:00
    return "次日 09:00"


def discover_main_contract(api):
    """
    自动发现鸡蛋当前主力合约。
    TqSdk 免费版不支持 KQ.m@DCE.JD 主力连续格式，
    因此从 DCE 未过期鸡蛋合约列表中取最近月合约作为代理。
    """
    quotes = api.query_quotes(ins_class="FUTURE", exchange_id="DCE", expired=False)
    jd_contracts = sorted([q for q in quotes if "jd" in q.lower()])
    if jd_contracts:
        return jd_contracts[0]  # 最近月
    # fallback
    return "DCE.jd2605"


def fmt_time(ns_datetime):
    """将 TqSdk 的纳秒时间戳转为可读字符串"""
    if ns_datetime and ns_datetime > 0:
        return datetime.fromtimestamp(ns_datetime / 1e9).strftime("%m-%d %H:%M")
    return "---"


def print_recent_bars(klines, symbol, n=10):
    """打印最近 n 根 K 线"""
    print("\n" + "=" * 72)
    print(f"  鸡蛋主力 ({symbol})  1分钟K线   {datetime.now().strftime('%H:%M:%S')} 更新")
    print("=" * 72)
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

    print("=" * 72)

    last = klines.iloc[-1]
    if last["close"] > 0 and last["open"] > 0:
        chg     = last["close"] - last["open"]
        chg_pct = chg / last["open"] * 100
        flag    = "▲" if chg >= 0 else "▼"
        print(f"  最新bar: {last['close']:.2f}  {flag} {abs(chg):.2f} ({abs(chg_pct):.2f}%)")
    print()


def main():
    username, password = get_tqsdk_auth()
    print(f"正在连接天勤量化 (账号: {username})...")

    # ── 1. 创建 API 实例 ──
    api = TqApi(auth=TqAuth(username, password))

    # ── 2. 自动发现主力合约 ──
    symbol = discover_main_contract(api)
    print(f"鸡蛋主力合约: {symbol} (自动识别)")

    # ── 3. 列出所有可用鸡蛋合约供参考 ──
    all_jd = sorted([q for q in api.query_quotes(
        ins_class="FUTURE", exchange_id="DCE", expired=False
    ) if "jd" in q.lower()])
    print(f"可用鸡蛋合约: {', '.join(all_jd)}")
    print(f"K线周期: {KLINE_DUR}s  |  按 Ctrl+C 退出")

    # ── 4. 订阅 K 线 ──
    klines = api.get_kline_serial(symbol, KLINE_DUR, data_length=DATA_LEN)

    # ── 5. 等待初始数据就绪 ──
    print("正在获取初始数据...")
    deadline = _time.time() + 15
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())
        if len(klines) > 0 and klines.iloc[-1]["close"] > 0:
            break
    else:
        # 超时：可能盘后无数据 or 合约无交易
        if len(klines) == 0 or klines.iloc[-1]["close"] <= 0:
            print("\n⚠️  该合约暂无1分钟K线数据，请确认合约是否正确。")
            api.close()
            return

    # ── 6. 打印初始数据 ──
    now = datetime.now()
    if is_trading_time():
        print(f"\n🟢 交易中  (当前时段)  →  等待行情推送...\n")
    else:
        next_t = next_trading_time()
        print(f"\n⏸️  盘后    (下一时段: {next_t})  →  显示最后交易数据\n")

    print_recent_bars(klines, symbol, n=15)
    bar_count = 0
    last_status_print = 0.0  # 上次打印盘后状态的时间戳
    last_processed_dt = klines.iloc[-1]["datetime"]  # 哨兵：避免历史数据触发新bar

    # ── 7. 核心更新循环 ──
    try:
        while True:
            # 交易时段：正常阻塞等待；盘后：超时 3 秒返回
            if is_trading_time():
                api.wait_update()
            else:
                api.wait_update(deadline=_time.time() + WAIT_SEC)

            # 7a. 新 bar 形成（仅当 datetime 严格大于上次已处理的）
            cur_dt = klines.iloc[-1]["datetime"]
            if cur_dt > last_processed_dt:
                last_processed_dt = cur_dt
                bar_count += 1
                print(f"[新K线 #{bar_count}] ", end="")
                print_recent_bars(klines, symbol, n=10)

            # 7b. 当前 bar 价格实时更新（仅交易时段 + 当前最新bar）
            elif is_trading_time() and cur_dt == last_processed_dt \
                    and api.is_changing(klines.iloc[-1], "close"):
                last = klines.iloc[-1]
                t    = fmt_time(last["datetime"])
                print(f"\r  实时 {t}  O:{last['open']:.2f}  H:{last['high']:.2f}"
                      f"  L:{last['low']:.2f}  C:{last['close']:.2f}"
                      f"  V:{int(last['volume'])}    ", end="", flush=True)

            # 7c. 盘后：定期打印状态（避免刷屏）
            elif not is_trading_time():
                if _time.time() - last_status_print > STATUS_SLOT:
                    last_status_print = _time.time()
                    next_t = next_trading_time()
                    last_bar = klines.iloc[-1]
                    bt = fmt_time(last_bar["datetime"])
                    print(f"\r  ⏸️  盘后 | 最后K线: {bt}  "
                          f"C:{last_bar['close']:.2f}  V:{int(last_bar['volume'])}  "
                          f"| 等待 {next_t} 开盘...     ", end="", flush=True)

    except KeyboardInterrupt:
        print("\n\n已退出。")
    finally:
        api.close()


if __name__ == "__main__":
    main()
