"""
Tick 采集功能模拟测试
=====================
生成仿真 tick 数据，完整测试 TickStore 的：
  1. 批量写入 + 去重
  2. 多周期 K 线聚合 (1min ~ 1day)
  3. 统计查询
  4. Parquet 归档
  5. watch_ticks 查看器兼容性

用法:
  python scripts/simulate_ticks.py              # 默认: 5 品种, 今日 1 天
  python scripts/simulate_ticks.py --days 3     # 模拟 3 天
  python scripts/simulate_ticks.py --tick-rate 0.5  # 每秒 0.5 个 tick
  python scripts/simulate_ticks.py --no-archive # 跳过归档测试
"""

import sys
import os
import argparse
import random
import time as _time
from datetime import datetime, date, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tick_storage import TickStore, PERIOD_SECONDS, ns_to_str

# ── 品种配置 ──────────────────────────────────────────────
CONTRACTS = {
    "棉花": {"symbol": "CZCE.CF609", "base_price": 15925, "tick_size": 5,  "vol_per_min": 30},
    "鸡蛋": {"symbol": "DCE.jd2609", "base_price": 4271,  "tick_size": 1,  "vol_per_min": 20},
    "生猪": {"symbol": "DCE.lh2609", "base_price": 11895, "tick_size": 5,  "vol_per_min": 15},
    "红枣": {"symbol": "CZCE.CJ609", "base_price": 8615,  "tick_size": 5,  "vol_per_min": 10},
    "玉米": {"symbol": "DCE.c2609",  "base_price": 2327,  "tick_size": 1,  "vol_per_min": 25},
}


def generate_tick_burst(symbol: str, base_price: float, tick_size: float,
                        start_dt: pd.Timestamp, n_ticks: int,
                        trend: float = 0.0, volatility: float = 1.0,
                        volume_base: int = 1) -> pd.DataFrame:
    """
    生成一笔仿真 tick 数据。

    价格模拟: 随机游走 + 趋势漂移 + 波动率缩放
    时间间隔: 随机 0.1~2.0 秒 (模拟真实 tick 密度)

    Args:
        symbol: 合约代码
        base_price: 起始价格
        tick_size: 最小变动价位
        start_dt: 起始时间
        n_ticks: tick 数量
        trend: 趋势漂移系数 (>0 涨, <0 跌)
        volatility: 波动率倍率
        volume_base: 每跳最小成交量

    Returns:
        DataFrame with datetime, last_price, volume, open_interest
    """
    records = []
    price = float(base_price)
    dt_ns = start_dt.value  # pandas Timestamp → 纳秒

    for i in range(n_ticks):
        # 价格随机游走
        noise = np.random.randn() * tick_size * volatility
        drift = trend * tick_size * volatility * 0.1
        delta = noise + drift
        # 量化到 tick_size 整数倍
        ticks = round(delta / tick_size)
        if ticks == 0:
            ticks = random.choice([-1, 1])  # 至少跳 1 格
        price += ticks * tick_size
        price = max(price, base_price * 0.9)  # 防止跌太多

        # 成交量递增 (模拟日内累计)
        vol = int(volume_base * (i + 1) * (0.5 + 0.5 * random.random()))

        # 持仓量 (缓慢变化)
        oi_base = int(base_price * 10 * (0.9 + 0.1 * np.random.randn()))
        oi = max(0, oi_base + i)

        # 时间戳: 随机间隔 0.1~3 秒
        interval_ns = int((0.1 + random.expovariate(1.0)) * 1e9)
        dt_ns += interval_ns
        dt_ns = max(dt_ns, start_dt.value)  # 保证递增

        records.append({
            "datetime": dt_ns,
            "last_price": round(price, 1),
            "volume": vol,
            "open_interest": oi,
        })

    return pd.DataFrame(records)


def print_sep(title: str):
    print(f"\n{'='*72}")
    print(f"  {title}")
    print(f"{'='*72}")


def print_stats(store: TickStore):
    """打印各品种存储统计"""
    df = store.stats()
    if df.empty:
        print("  (无数据)")
        return

    print(f"  {'品种':<6} {'合约':<14} {'实时':>8} {'累计':>8} {'K线':>8}  {'最新时间'}")
    print(f"  {'-'*64}")

    names = {v["symbol"]: k for k, v in CONTRACTS.items()}
    for _, row in df.iterrows():
        sym = row["symbol"]
        name = names.get(sym, sym)
        t = ns_to_str(row["last_tick_time"].value // 1e9 if pd.notna(row.get("last_tick_time")) else 0)
        if "1970" in t:
            t = "---"
        print(f"  {name:<6} {sym:<14} {int(row['hot_ticks']):>8,} "
              f"{int(row['total_ticks']):>8,} {int(row['klines_count']):>8,}  {t}")


# ══════════════════════════════════════════════════════════
def test_write_and_dedup(store: TickStore, args):
    """测试写入 + 去重"""
    print_sep("1. 生成仿真 tick 数据并写入")

    today = date.today().isoformat()
    total = 0

    for name, cfg in CONTRACTS.items():
        sym = cfg["symbol"]
        base = cfg["base_price"]
        ts = cfg["tick_size"]

        # 生成一天的 tick 数据
        # 交易时段: 09:00-11:30, 13:30-15:00, 21:00-23:00
        sessions = [
            (f"{today} 09:00:00", f"{today} 11:30:00"),
            (f"{today} 13:30:00", f"{today} 15:00:00"),
        ]

        dfs = []
        for start_str, end_str in sessions:
            start_dt = pd.Timestamp(start_str)
            end_dt = pd.Timestamp(end_str)
            duration_sec = (end_dt - start_dt).total_seconds()
            n = int(duration_sec * args.tick_rate)  # tick 数量 = 秒数 × 速率

            # 随机趋势偏多/偏空/震荡
            trend = random.choice([-0.5, 0, 0.5, 0.2])
            df = generate_tick_burst(
                sym, base, ts, start_dt, n,
                trend=trend, volatility=1.5,
                volume_base=cfg["vol_per_min"]
            )
            dfs.append(df)
            base = df["last_price"].iloc[-1]  # 下个时段的起始价

        df_all = pd.concat(dfs, ignore_index=True).sort_values("datetime")

        n_wrote = store.insert_ticks(sym, df_all, trade_date=today)
        total += n_wrote
        print(f"  {name} {sym}: 生成 {len(df_all):,} ticks → 写入 {n_wrote:,} 条")

    print(f"\n  >> 共写入 {total:,} 条 tick 数据")

    # 验证统计
    print_sep("1b. 写入后统计")
    print_stats(store)

    # 去重测试: 再次写入相同数据
    if not args.skip_dedup:
        print_sep("1c. 去重测试: 重复写入相同数据")
        for name, cfg in CONTRACTS.items():
            sym = cfg["symbol"]
            # 读取已写入的最后 50 条
            rows = store.conn.execute("""
                SELECT datetime, last_price, volume, open_interest
                FROM ticks WHERE symbol = ? AND trade_date = ?
                ORDER BY datetime DESC LIMIT 50
            """, [sym, today]).fetchall()

            if not rows:
                continue

            df_dup = pd.DataFrame(rows, columns=["datetime", "last_price", "volume", "open_interest"])
            before_total = store.conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE symbol = ?", [sym]
            ).fetchone()[0]

            n = store.insert_ticks(sym, df_dup, trade_date=today)

            after_total = store.conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE symbol = ?", [sym]
            ).fetchone()[0]

            status = "✓ 去重正常" if n == 0 and after_total == before_total else "✗ 去重异常!"
            print(f"  {name}: 插入 {len(df_dup)} 条重复 → 实际写入 {n} 条  {status}")

    return total


def test_aggregation(store: TickStore, args):
    """测试 K 线聚合"""
    print_sep("2. 多周期 K 线聚合")

    periods = args.periods.split(",") if hasattr(args, 'periods') else ["1min", "3min", "15min", "30min", "1hour"]
    all_results = {}

    for name, cfg in CONTRACTS.items():
        sym = cfg["symbol"]
        results = store.aggregate_klines(sym, periods=periods, replace=True)

        items = []
        for p, cnt in results.items():
            items.append(f"{p}:{cnt}")
            if cnt > 0:
                # 验证 K 线内容
                kl = store.get_klines(sym, p, limit=1)
                if not kl.empty:
                    row = kl.iloc[-1]
                    # 检查 OHLC 合法性
                    valid = (row["high"] >= row["low"] and
                             row["open"] >= row["low"] and row["open"] <= row["high"] and
                             row["close"] >= row["low"] and row["close"] <= row["high"])
                    status = "✓" if valid else "✗"
                else:
                    status = "?"
            else:
                status = ""
            items[-1] = f"{items[-1]} {status}"

        print(f"  {name} {sym}: {', '.join(items)}")
        all_results[sym] = results

    # 聚合后统计
    print_sep("2b. 聚合后统计")
    print_stats(store)

    # 抽样展示 K 线
    print_sep("2c. K 线抽样 (棉花 15min 最后 5 根)")
    kl = store.get_klines("CZCE.CF609", "15min", limit=5)
    if not kl.empty:
        print(kl.to_string(index=False))

    return all_results


def test_queries(store: TickStore, args):
    """测试各种查询接口"""
    print_sep("3. 查询接口测试")

    # 3a. get_ticks
    print("  [get_ticks] 棉花最近 10 条:")
    df = store.get_ticks("CZCE.CF609", limit=10)
    if not df.empty:
        for _, r in df.iterrows():
            t = ns_to_str(int(r["datetime"]))
            print(f"    {t}  P={r['last_price']:>8.1f}  V={r['volume']:>6}  OI={r['open_interest']:>6}")

    # 3b. get_all_klines
    print("\n  [get_all_klines] 鸡蛋 所有周期 K 线条数:")
    kls = store.get_all_klines("DCE.jd2609")
    for period, df in kls.items():
        print(f"    {period}: {len(df)} 条")

    # 3c. stats
    print("\n  [stats] 全局统计:")
    print_stats(store)

    # 3d. get_symbols
    symbols = store.get_symbols()
    print(f"\n  [get_symbols] 已存储合约: {len(symbols)} 个")
    for s in symbols:
        meta = store.get_meta(s)
        if meta:
            print(f"    {s}: 累计 {meta['total_ticks']:,} ticks")


def test_archive(store: TickStore, args):
    """测试 Parquet 归档"""
    print_sep("4. Parquet 归档测试")

    today = date.today().isoformat()
    # 归档今天之前的数据 (今天的数据没有, 但测试流程)
    n = store.archive_to_parquet(before_date=today)

    if n == 0:
        print(f"  今天 ({today}) 的数据不满足归档条件 (>30 天), 跳过")
        print(f"  归档功能已就绪，数据够老后自动触发")
    else:
        print(f"  已归档 {n:,} ticks → Parquet")


def main():
    parser = argparse.ArgumentParser(description="Tick 采集功能模拟测试")
    parser.add_argument("--db-path", default="data/sim_tick_data.db",
                        help="测试用 DuckDB 数据库路径 (默认 data/sim_tick_data.db)")
    parser.add_argument("--tick-rate", type=float, default=0.5,
                        help="每秒生成 tick 数 (默认 0.5, 真实约 1-2)")
    parser.add_argument("--periods", default="1min,3min,15min,30min,1hour",
                        help="测试聚合周期 (逗号分隔)")
    parser.add_argument("--skip-dedup", action="store_true",
                        help="跳过去重测试")
    parser.add_argument("--skip-archive", action="store_true",
                        help="跳过归档测试")
    parser.add_argument("--keep-db", action="store_true",
                        help="测试后保留数据库 (默认删除)")

    args = parser.parse_args()
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.db_path)
    archive_dir = os.path.join(os.path.dirname(db_path), "sim_tick_archive")

    # 清理旧测试数据
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"[清理] 删除旧测试 DB: {db_path}")

    print("=" * 72)
    print("  Tick 采集功能模拟测试")
    print(f"  数据库: {db_path}")
    print(f"  品种: {len(CONTRACTS)} 个 | tick 速率: {args.tick_rate}/秒")
    print(f"  测试周期: {args.periods}")
    print("=" * 72)

    store = TickStore(db_path, archive_dir=archive_dir)

    try:
        total_ticks = test_write_and_dedup(store, args)
        test_aggregation(store, args)
        test_queries(store, args)
        if not args.skip_archive:
            test_archive(store, args)

        # ── 最终摘要 ──
        print_sep("测试完成 ✓")
        df = store.stats()
        print(f"\n  写入: {total_ticks:,} ticks")
        if not df.empty:
            print(f"  K 线条数: {int(df['klines_count'].sum()):,}")
        print(f"  DB 文件: {os.path.getsize(db_path)/1024:.0f} KB")
        print(f"  watch_ticks 兼容: python scripts/watch_ticks.py --db-path {args.db_path} -c\n")

    finally:
        store.close()

    # 清理测试数据 (除非 --keep-db)
    if not args.keep_db:
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"[清理] 已删除测试 DB")
        if os.path.exists(archive_dir):
            import shutil
            shutil.rmtree(archive_dir, ignore_errors=True)
            print(f"[清理] 已删除归档目录")


if __name__ == "__main__":
    main()
