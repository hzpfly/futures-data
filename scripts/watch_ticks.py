"""
Tick 实时行情查看器
===================
实时查看 DuckDB 中采集器正在写入的最新 tick 数据。
可独立运行, 不影响采集器。

用法:
  # 默认刷新 (每 2 秒刷新, Ctrl+C 退出)
  python scripts/watch_ticks.py

  # 指定刷新间隔
  python scripts/watch_ticks.py --interval 5

  # 只看最新一条 (一次性)
  python scripts/watch_ticks.py --once

  # 只看指定品种
  python scripts/watch_ticks.py --symbols CZCE.CF609,DCE.jd2509

  # 更紧凑的显示 (K线风格)
  python scripts/watch_ticks.py --compact
"""

import sys
import os
import argparse
import time as _time
from datetime import datetime
from collections import defaultdict

import duckdb
import pandas as pd

# 品种配置 (名称映射)
PRODUCT_NAMES = {
    "CF": "棉花", "jd": "鸡蛋", "lh": "生猪", "CJ": "红枣", "c": "玉米",
}


def format_price(price: float, prev: float = None, tick: float = 1.0) -> str:
    """格式化价格, 标注涨跌色"""
    if prev is None or prev == 0:
        return f"{price:>10.1f}"
    diff = price - prev
    if diff > 0:
        return f"\033[91m{price:>10.1f} ↑\033[0m"  # 红色 (涨)
    elif diff < 0:
        return f"\033[92m{price:>10.1f} ↓\033[0m"  # 绿色 (跌)
    else:
        return f"{price:>10.1f}"


def format_volume(vol: int) -> str:
    """格式化成交量"""
    if vol is None or vol == 0:
        return "      -"
    if abs(vol) >= 10000:
        return f"{vol/10000:>5.1f}万"
    return f"{vol:>7,}"


def format_time(ns: int) -> str:
    """纳秒时间戳 → HH:MM:SS"""
    if ns is None or ns == 0:
        return "        "
    return pd.Timestamp(ns, unit="ns").strftime("%H:%M:%S")


def get_name(symbol: str) -> str:
    """从合约代码提取品种名"""
    for product, name in PRODUCT_NAMES.items():
        if product in symbol.replace(".", ""):
            return name
    return symbol


def get_tick_size(symbol: str) -> float:
    """获取最小变动价位"""
    sizes = {"CF": 5, "jd": 1, "lh": 5, "CJ": 5, "c": 1}
    for p, s in sizes.items():
        if p in symbol.replace(".", ""):
            return s
    return 1.0


def read_latest_tick(conn, symbol: str) -> dict:
    """读取某品种最新一条 tick"""
    row = conn.execute("""
        SELECT datetime, last_price, volume, open_interest
        FROM ticks
        WHERE symbol = ?
        ORDER BY datetime DESC
        LIMIT 1
    """, [symbol]).fetchone()

    if row is None:
        return None
    return {
        "datetime": row[0],
        "last_price": row[1],
        "volume": row[2],
        "open_interest": row[3],
    }


def read_recent_ticks(conn, symbol: str, n: int = 100) -> list:
    """读取某品种最近 n 条 tick"""
    rows = conn.execute("""
        SELECT datetime, last_price
        FROM ticks
        WHERE symbol = ?
        ORDER BY datetime DESC
        LIMIT ?
    """, [symbol, n]).fetchall()
    return [{"datetime": r[0], "last_price": r[1]} for r in reversed(rows)]


def read_stats(conn, symbols: list) -> dict:
    """读取所有品种统计"""
    rows = conn.execute("""
        SELECT
            m.symbol,
            COALESCE(t.hot_ticks, 0) AS hot_ticks,
            m.total_ticks AS total_ticks,
            m.last_tick_dt
        FROM meta m
        LEFT JOIN (
            SELECT symbol, COUNT(*) AS hot_ticks
            FROM ticks GROUP BY symbol
        ) t ON m.symbol = t.symbol
        ORDER BY m.symbol
    """).fetchall()

    return {r[0]: {"hot": r[1], "total": r[2], "last_dt": r[3]} for r in rows}


def watch(db_path: str, interval: float, once: bool = False,
          symbols: list = None, compact: bool = False):
    """实时查看主循环"""

    conn = duckdb.connect(db_path, read_only=True)

    if symbols is None:
        # 自动发现所有合约
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM meta ORDER BY symbol").fetchall()]
        if not symbols:
            symbols = [r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM ticks ORDER BY symbol").fetchall()]

    if not symbols:
        print("数据库中没有 tick 数据")
        conn.close()
        return

    names = {s: get_name(s) for s in symbols}
    tick_sizes = {s: get_tick_size(s) for s in symbols}

    # 记录上一轮价格 (用于计算涨跌)
    prev_prices = {}
    prev_volumes = {}
    prev_oi = {}

    try:
        while True:
            _time.sleep(0.3)  # 等一小会儿让数据写入完成

            stats = read_stats(conn, symbols)
            now_str = datetime.now().strftime("%H:%M:%S")

            if compact:
                _print_compact(now_str, symbols, names, stats, conn,
                               prev_prices, prev_volumes, prev_oi, tick_sizes)
            else:
                _print_detail(now_str, symbols, names, stats, conn,
                              prev_prices, prev_volumes, prev_oi, tick_sizes)

            if once:
                break

            _time.sleep(interval - 0.3)

    except KeyboardInterrupt:
        print("\n退出查看")

    finally:
        conn.close()


def _print_detail(now_str, symbols, names, stats, conn,
                  prev_prices, prev_volumes, prev_oi, tick_sizes):
    """详细模式输出"""
    print(f"\n{'='*82}")
    print(f"  Tick 实时行情  [{now_str}]")
    print(f"{'='*82}")
    print(f"  {'品种':<6} {'合约':<12} {'最新价':>10} {'成交量(日内)':>10} {'持仓量':>10} {'时间':>8}  {'Tick数':>8}")
    print(f"  {'-'*78}")

    for sym in symbols:
        tick = read_latest_tick(conn, sym)
        if tick is None:
            print(f"  {names[sym]:<6} {sym:<12} {'---':>10} {'---':>10} {'---':>10} {'---':>8}")
            continue

        price = tick["last_price"]
        vol = tick["volume"]
        oi = tick["open_interest"]
        dt_str = format_time(tick["datetime"])

        prev_p = prev_prices.get(sym)
        prev_v = prev_volumes.get(sym, 0) or 0
        prev_o = prev_oi.get(sym, 0) or 0

        price_str = format_price(price, prev_p, tick_sizes[sym])

        # 成交量增量
        delta_v = (vol or 0) - prev_v if vol else 0
        vol_str = format_volume(delta_v) if delta_v else "      -"

        # 持仓量增量
        delta_oi = (oi or 0) - prev_o if oi else 0
        oi_str = f"{oi or 0:>+8,}" if oi else "      -"

        stat = stats.get(sym, {})
        tick_cnt = stat.get("hot", 0)

        print(f"  {names[sym]:<6} {sym.split('.')[-1]:<12} {price_str}    {vol_str}  {oi_str}  {dt_str}  {tick_cnt:>8,}")

        prev_prices[sym] = price
        prev_volumes[sym] = vol or prev_v
        prev_oi[sym] = oi or prev_o

    # 总计
    total_hot = sum(s.get("hot", 0) for s in stats.values())
    total_all = sum(s.get("total", 0) for s in stats.values())
    print(f"  {'-'*78}")
    print(f"  总计: {len(symbols)} 品种  |  实时 {total_hot:,} tick  |  累计 {total_all:,} tick")
    print(f"{'='*82}")


def _print_compact(now_str, symbols, names, stats, conn,
                   prev_prices, prev_volumes, prev_oi, tick_sizes):
    """紧凑模式输出 (一行一品)"""
    total = sum(s.get("hot", 0) for s in stats.values())
    print(f"\n--- {now_str}  (共 {total:,} ticks) ---")

    for sym in symbols:
        tick = read_latest_tick(conn, sym)
        if tick is None:
            print(f"  {names[sym]:<4} {sym.split('.')[-1]:<8}  ---")
            continue

        price = tick["last_price"]
        prev_p = prev_prices.get(sym)

        # 涨跌
        if prev_p and prev_p != price:
            diff = price - prev_p
            arrow = "\033[91m▲\033[0m" if diff > 0 else "\033[92m▼\033[0m"
            chg = f"{diff:+.1f}"
        else:
            arrow = " "
            chg = ""

        dt_str = format_time(tick["datetime"])
        stat = stats.get(sym, {})
        hot = stat.get("hot", 0)

        print(f"  {names[sym]:<4} {sym.split('.')[-1]:<8} {price:>10.1f} {arrow}{chg:>7}  {dt_str}  [{hot:,}]")

        prev_prices[sym] = price


def main():
    parser = argparse.ArgumentParser(description="Tick 实时行情查看器")
    parser.add_argument("--db-path", default="data/tick_data.db",
                        help="DuckDB 数据库路径")
    parser.add_argument("--interval", "-i", type=float, default=2.0,
                        help="刷新间隔 (秒), 默认 2")
    parser.add_argument("--once", "-1", action="store_true",
                        help="只查看一次 (不循环)")
    parser.add_argument("--symbols", "-s", default=None,
                        help="限定合约 (逗号分隔), 默认全部")
    parser.add_argument("--compact", "-c", action="store_true",
                        help="紧凑模式 (一行一品)")

    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}")
        print("请先启动采集器: python tick_collector.py")
        return

    watch(db_path, args.interval, args.once, symbols, args.compact)


if __name__ == "__main__":
    main()
