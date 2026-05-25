"""
鸡蛋主力合约 1分钟K线 图形监控
自动识别当前主力合约（最近月合约），免费版天勤不支持 KQ.m@DCE.JD
周期: 60 秒 K线

更新规则（严格按 TqSdk 文档）:
  - get_kline_serial 只调用一次，循环中复用同一 DataFrame 引用
  - 用 api.is_changing(klines.iloc[-1], "datetime") 检测新 bar 形成
  - 用 api.is_changing(klines.iloc[-1], "close") 检测当前 bar 实时刷新
  - 不使用 sleep()，不在循环里重新调用 get_*

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
from datetime import datetime
import time
import warnings
warnings.filterwarnings("ignore")

from config_loader import get_tqsdk_auth


# ── 配置 ─────────────────────────────────────────────────
DUR_SEC  = 60
DATA_LEN = 200
SHOW_N   = 60       # 图上显示最近 60 根 K 线

api    = None
klines = None
SYMBOL = None


def discover_main_contract(api):
    quotes = api.query_quotes(ins_class="FUTURE", exchange_id="DCE", expired=False)
    jd_contracts = sorted([q for q in quotes if "jd" in q.lower()])
    if jd_contracts:
        return jd_contracts[0]
    return "DCE.jd2605"


def fmt_time(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%H:%M")
    return ""


def setup_api():
    global api, klines, SYMBOL
    print("正在连接天勤量化...")
    username, password = get_tqsdk_auth()
    api = TqApi(auth=TqAuth(username, password))
    SYMBOL = discover_main_contract(api)
    print(f"鸡蛋主力合约: {SYMBOL}")

    all_jd = sorted([q for q in api.query_quotes(
        ins_class="FUTURE", exchange_id="DCE", expired=False
    ) if "jd" in q.lower()])
    print(f"可用合约: {', '.join(all_jd)}")

    klines = api.get_kline_serial(SYMBOL, DUR_SEC, data_length=DATA_LEN)
    print("连接成功，等待行情推送...")


def draw(ax_k, ax_v, df):
    """绘制蜡烛图 + MA + 成交量"""
    ax_k.cla()
    ax_v.cla()

    data = df[df["close"] > 0].tail(SHOW_N).copy()
    if data.empty:
        return

    x     = np.arange(len(data))
    times = [fmt_time(v) for v in data["datetime"].values]

    for i, (_, r) in enumerate(data.iterrows()):
        color = "#e74c3c" if r["close"] >= r["open"] else "#26a65b"
        lo    = min(r["open"], r["close"])
        hi    = max(r["open"], r["close"])
        ax_k.bar(i, hi - lo, bottom=lo,   color=color, width=0.6, linewidth=0)
        ax_k.plot([i, i], [r["low"], r["high"]], color=color, linewidth=0.8)

    closes = data["close"].values
    for n, c in [(5, "#f39c12"), (10, "#3498db"), (20, "#9b59b6")]:
        if len(closes) >= n:
            ma = pd.Series(closes).rolling(n).mean().values
            ax_k.plot(x, ma, color=c, linewidth=1, label=f"MA{n}")

    for i, (_, r) in enumerate(data.iterrows()):
        color = "#e74c3c" if r["close"] >= r["open"] else "#26a65b"
        ax_v.bar(i, r["volume"], color=color, width=0.6, alpha=0.7, linewidth=0)

    last    = data.iloc[-1]
    chg     = last["close"] - data.iloc[0]["open"]
    chg_pct = chg / data.iloc[0]["open"] * 100
    flag    = "▲" if chg >= 0 else "▼"

    ax_k.set_title(
        f"\u9e21\u86cb\u4e3b\u529b ({SYMBOL})  1\u5206\u949fK\u7ebf\n"
        f"\u6700\u65b0: {last['close']:.2f}  {flag} {abs(chg):.2f} ({abs(chg_pct):.2f}%)"
        f"   \u66f4\u65b0: {datetime.now().strftime('%H:%M:%S')}",
        fontsize=10, loc="left"
    )

    tick_pos   = x[::5]
    tick_label = [times[i] for i in range(0, len(data), 5)]
    for ax in (ax_k, ax_v):
        ax.set_xticks(tick_pos)
        ax.set_xticklabels(tick_label, fontsize=7, rotation=30)
        ax.set_xlim(-1, len(data))
        ax.grid(axis="y", alpha=0.25, linewidth=0.5)

    ax_k.tick_params(labelbottom=False)
    ax_k.set_ylabel("价格", fontsize=9)
    ax_v.set_ylabel("成交量", fontsize=9)
    ax_k.legend(fontsize=8, loc="upper left")


def animate(frame, ax_k, ax_v):
    global api, klines
    if api is None or klines is None:
        return

    # 非阻塞轮询（deadline=0），不阻塞 matplotlib 事件循环
    api.wait_update(deadline=time.time())

    # 新 bar 或当前 bar 价格变化时重绘
    if api.is_changing(klines.iloc[-1], "datetime") or \
       api.is_changing(klines.iloc[-1], "close"):
        draw(ax_k, ax_v, klines.copy())


def main():
    setup_api()

    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax_k, ax_v) = plt.subplots(
        2, 1, figsize=(14, 8),
        gridspec_kw={"height_ratios": [3, 1]}
    )
    fig.subplots_adjust(hspace=0.05)
    fig.suptitle("天勤量化 — 鸡蛋期货 1分钟 实时K线", fontsize=12)

    draw(ax_k, ax_v, klines.copy())

    ani = animation.FuncAnimation(
        fig,
        animate,
        fargs=(ax_k, ax_v),
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
