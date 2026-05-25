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


def draw_one(ax, df, title):
    """在单个子图上绘制蜡烛图 + MA"""
    ax.cla()

    data = df[df["close"] > 0].tail(SHOW_N).copy()
    if data.empty:
        ax.set_title(f"{title}  (无数据)", fontsize=10)
        return

    x     = np.arange(len(data))
    times = [fmt_time(v) for v in data["datetime"].values]

    # 蜡烛图
    for i, (_, r) in enumerate(data.iterrows()):
        color = "#e74c3c" if r["close"] >= r["open"] else "#26a65b"
        lo    = min(r["open"], r["close"])
        hi    = max(r["open"], r["close"])
        ax.bar(i, hi - lo, bottom=lo, color=color, width=0.6, linewidth=0)
        ax.plot([i, i], [r["low"], r["high"]], color=color, linewidth=0.8)

    # MAs
    closes = data["close"].values
    for n, c in [(5, "#f39c12"), (10, "#3498db"), (20, "#9b59b6")]:
        if len(closes) >= n:
            ma = pd.Series(closes).rolling(n).mean().values
            ax.plot(x, ma, color=c, linewidth=0.8, label=f"MA{n}")

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


def draw_all(axes):
    """绘制全部三个周期的图表"""
    draw_one(axes[0], klines_map["25min"], "25分钟 K线")
    draw_one(axes[1], klines_map["5min"],  "5分钟 K线")
    draw_one(axes[2], klines_map["1min"],  "1分钟 K线")


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

    if any_new_bar or is_price_change:
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

    # 三行子图：25分钟 / 5分钟 / 1分钟
    fig, axes = plt.subplots(3, 1, figsize=(14, 12))
    fig.subplots_adjust(hspace=0.35)

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
