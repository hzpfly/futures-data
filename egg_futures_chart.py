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
from datetime import datetime, time
import warnings
warnings.filterwarnings("ignore")

from config_loader import get_tqsdk_auth
import time as _time


# ── 配置 ─────────────────────────────────────────────────
DUR_SEC  = 60
DATA_LEN = 200
SHOW_N   = 60       # 图上显示最近 60 根 K 线
VIEW_SEC = 60       # 盘后重绘间隔（秒），不频繁刷新

# DCE 鸡蛋期货交易时段（日盘，无夜盘）
TRADING_SESSIONS = [
    (time(9, 0),  time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]

api    = None
klines = None
SYMBOL = None
_last_draw_time = 0.0  # 上次重绘时间
_last_processed_dt = 0  # 哨兵：已处理的最新 bar datetime


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
    return "次日 09:00"


def discover_main_contract(api):
    """
    按持仓量（open_interest）确定鸡蛋主力合约。
    TqSdk 免费版不支持 KQ.m@DCE.JD 主力连续格式，
    因此从 DCE 未过期鸡蛋合约中选持仓量最大的。
    """
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

    # 交易状态标记
    if is_trading_time():
        status = "🟢 交易中"
    else:
        status = f"⏸️  盘后 (等 {next_trading_time()})"

    ax_k.set_title(
        f"\u9e21\u86cb\u4e3b\u529b ({SYMBOL})  1\u5206\u949fK\u7ebf  {status}\n"
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
    global api, klines, _last_draw_time, _last_processed_dt
    if api is None or klines is None:
        return

    # 非阻塞轮询（deadline=当前时间，即不等待直接返回）
    api.wait_update(deadline=_time.time())

    cur_dt = klines.iloc[-1]["datetime"]

    # 新 bar 或当前 bar 价格变化时立即重绘（哨兵：避免历史数据误触发）
    is_new_bar = cur_dt > _last_processed_dt
    is_price_change = (cur_dt == _last_processed_dt and
                       api.is_changing(klines.iloc[-1], "close"))

    if is_new_bar:
        _last_processed_dt = cur_dt

    if is_new_bar or is_price_change:
        draw(ax_k, ax_v, klines.copy())
        _last_draw_time = _time.time()
        return

    # 盘后：降低重绘频率（仅在标题状态文字变化时刷新）
    if not is_trading_time() and _time.time() - _last_draw_time > VIEW_SEC:
        draw(ax_k, ax_v, klines.copy())
        _last_draw_time = _time.time()


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
