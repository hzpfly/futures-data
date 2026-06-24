"""
将 tqsdk K 线历史数据导出为 CSV 文件。

用法:
    # 默认: CF609, 全部 5 个周期
    python scripts/dump_klines_csv.py

    # 指定合约和周期
    python scripts/dump_klines_csv.py --symbol CZCE.CF609 --periods 1week,1day

    # 指定输出目录
    python scripts/dump_klines_csv.py --outdir ./data

    # 查看所有可用周期
    python scripts/dump_klines_csv.py --list-periods

输出:
    data/CF609_1week_2025-01-01_2026-06-23.csv
    data/CF609_1day_2025-01-01_2026-06-23.csv
    ...
"""
import sys
import os
import argparse
from datetime import datetime
from tqsdk import TqApi, TqAuth
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config_loader import get_tqsdk_auth


# ── 周期定义 ──
PERIOD_MAP = {
    "1week":  (604800, "周线"),
    "1day":   (86400,  "日线"),
    "1hour":  (3600,   "小时线"),
    "15min":  (900,    "15分钟"),
    "3min":   (180,    "3分钟"),
}

DATA_LEN = 8964  # tqsdk 免费账户上限


def fetch_all(api, symbol, periods):
    """订阅多个周期的 K 线，返回 {label: DataFrame}。"""
    klines = {}
    for label in periods:
        dur, _ = PERIOD_MAP[label]
        klines[label] = api.get_kline_serial(symbol, dur, data_length=DATA_LEN)

    # 等待数据到位
    deadline = _time.time() + 30
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time())
        if all(len(k) >= 1 for k in klines.values()):
            break

    return klines


def fmt_date(ns):
    """纳秒时间戳 → 日期字符串"""
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")
    return "0000-00-00"


def fmt_datetime(ns):
    """纳秒时间戳 → 日期时间字符串"""
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
    return "0000-00-00 00:00:00"


def save_csv(klines_map, symbol, outdir):
    """把 K 线 DataFrame 存为 CSV。"""
    os.makedirs(outdir, exist_ok=True)
    short_name = symbol.split(".")[-1]

    for label, df in klines_map.items():
        cn_name = PERIOD_MAP[label][1]

        # 过滤有效行（close > 0）
        valid = df[df["close"] > 0].copy()

        if len(valid) == 0:
            print(f"  [{cn_name}] 无有效数据，跳过")
            continue

        # 生成友好时间列
        valid["date"] = valid["datetime"].apply(fmt_date)
        valid["time"] = valid["datetime"].apply(fmt_datetime)

        t_first = valid["date"].iloc[0]
        t_last  = valid["date"].iloc[-1]
        fname = f"{short_name}_{label}_{t_first}_{t_last}.csv"
        fpath = os.path.join(outdir, fname)

        # 选列输出
        out = valid[["date", "time", "open", "high", "low", "close", "volume"]]
        out.to_csv(fpath, index=False, encoding="utf-8-sig")

        n = len(out)
        print(f"  [{cn_name:<6}] {n:5d} bars  {t_first} → {t_last}"
              f"  → {fname}")


def main():
    parser = argparse.ArgumentParser(description="导出 tqsdk K 线历史数据为 CSV")
    parser.add_argument("--symbol", default="CZCE.CF609",
                        help="合约代码 (默认: CZCE.CF609)")
    parser.add_argument("--periods", default="1week,1day,1hour,15min,3min",
                        help="周期列表，逗号分隔 (默认: 全部 5 周期)")
    parser.add_argument("--outdir", default="./data",
                        help="输出目录 (默认: ./data)")
    parser.add_argument("--list-periods", action="store_true",
                        help="列出所有可用周期后退出")
    args = parser.parse_args()

    if args.list_periods:
        print("可用周期:")
        for label, (dur, name) in PERIOD_MAP.items():
            print(f"  {label:<7} {dur:>6} 秒  ({name})")
        return

    periods = [p.strip() for p in args.periods.split(",")]
    for p in periods:
        if p not in PERIOD_MAP:
            print(f"错误: 未知周期 '{p}'，可用: {list(PERIOD_MAP.keys())}")
            return

    print(f"合约: {args.symbol}")
    print(f"周期: {', '.join(periods)}")
    print(f"输出: {os.path.abspath(args.outdir)}")
    print(f"每周期最多 {DATA_LEN} 根 bar\n")

    # 连接 tqsdk
    user, pwd = get_tqsdk_auth()
    api = TqApi(auth=TqAuth(user, pwd))
    print(f"[TqSdk] 已连接\n")

    try:
        klines_map = fetch_all(api, args.symbol, periods)
        print("数据获取完成，开始写 CSV...\n")
        save_csv(klines_map, args.symbol, args.outdir)
    finally:
        api.close()

    print(f"\n完成，文件保存在 {os.path.abspath(args.outdir)}/")


if __name__ == "__main__":
    main()
