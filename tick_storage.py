"""
Tick 数据存储系统 — 基于 DuckDB
=================================
功能:
  1. 实时 tick 数据批量写入
  2. 从 tick 聚合多周期 K 线 (1min .. weekly)
  3. 历史数据 Parquet 归档
  4. 查询接口 (tick 查询、K 线查询)

设计:
  - 主表 ticks: 每个品种每笔 tick (按 trade_date 分区)
  - 聚合表 klines: 多周期已闭合 K 线, 从 tick 聚合生成
  - 归档: >30 天的 tick 转存 Parquet, DuckDB 可直接查

用法:
  from tick_storage import TickStore

  store = TickStore("data/tick_data.db")
  # 批量写入
  store.insert_ticks("CZCE.CF609", tick_df, trade_date="2026-06-24")
  # 聚合 K 线
  store.aggregate_klines("CZCE.CF609", periods=["1min","15min","1hour","1day"])
  # 查询
  df = store.get_klines("CZCE.CF609", "15min", "2026-06-01", "2026-06-24")
"""

import os
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Union

import pandas as pd
import duckdb

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────
# 支持聚合的 K 线周期 (key → seconds)
PERIOD_SECONDS = {
    "1min":   60,
    "3min":   180,
    "5min":   300,
    "15min":  900,
    "30min":  1800,
    "1hour":  3600,
    "2hour":  7200,
    "4hour":  14400,
    "1day":   86400,
    "1week":  604800,
}

# 归档阈值: 超过此天数的 tick 可转 Parquet
ARCHIVE_DAYS = 30

# 批量写入缓冲区大小
BATCH_SIZE = 200


# ══════════════════════════════════════════════════════════
# TickStore 主类
# ══════════════════════════════════════════════════════════
class TickStore:
    """DuckDB 驱动的 tick 数据存储"""

    def __init__(self, db_path: str = "data/tick_data.db",
                 archive_dir: str = "data/tick_archive",
                 read_only: bool = False):
        """
        Args:
            db_path: DuckDB 数据库文件路径
            archive_dir: Parquet 归档目录
            read_only: 只读模式 (可用于回测脚本并发读)
        """
        self.db_path = os.path.abspath(db_path)
        self.archive_dir = os.path.abspath(archive_dir)
        self.read_only = read_only

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)

        # 连接 DuckDB
        if read_only:
            self.conn = duckdb.connect(self.db_path, read_only=True)
        else:
            self.conn = duckdb.connect(self.db_path)
            self._init_tables()
            self._init_indexes()

    def _init_tables(self):
        """创建表结构 (如果不存在)"""
        # ── ticks 主表 (按 trade_date 分区) ──
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                symbol      VARCHAR NOT NULL,
                datetime    BIGINT  NOT NULL,    -- tqsdk 纳秒时间戳
                last_price  DOUBLE,
                volume      BIGINT,
                open_interest BIGINT,
                trade_date  DATE NOT NULL,        -- 分区键 (本地日期)
                PRIMARY KEY (symbol, trade_date, datetime)
            )
        """)

        # ── K 线聚合表 ──
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS klines (
                symbol    VARCHAR NOT NULL,
                period    VARCHAR NOT NULL,       -- 1min / 15min / 1hour / 1day / 1week
                datetime  BIGINT  NOT NULL,        -- K 线起始时间 (纳秒)
                open      DOUBLE,
                high      DOUBLE,
                low       DOUBLE,
                close     DOUBLE,
                volume    BIGINT,
                oi        BIGINT,                  -- 收盘时持仓量
                trade_date DATE NOT NULL,
                PRIMARY KEY (symbol, period, datetime)
            )
        """)

        # ── 元数据表 (记录每品种最后处理时间) ──
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                symbol         VARCHAR PRIMARY KEY,
                last_tick_dt   BIGINT,             -- 最后写入的 tick datetime
                last_klines_dt BIGINT,             -- 最后聚合的 K 线 datetime
                total_ticks    BIGINT DEFAULT 0,   -- 累计 tick 数
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def _init_indexes(self):
        """创建查询加速索引"""
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_sym_date
            ON ticks(symbol, trade_date)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_dt
            ON ticks(symbol, datetime)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_klines_sym_period
            ON klines(symbol, period, datetime)
        """)

    # ══════════════════════════════════════════════════════
    # Tick 写入
    # ══════════════════════════════════════════════════════
    def insert_ticks(self, symbol: str, df: pd.DataFrame,
                     trade_date: Optional[Union[str, date]] = None) -> int:
        """
        批量写入 tick 数据.

        Args:
            symbol: 合约代码, 如 "CZCE.CF609"
            df: tick DataFrame, 必须含 datetime, last_price 列;
                volume/open_interest 选填
            trade_date: 交易日期, 默认用 dataframe 中第一根 tick 的日期

        Returns:
            写入的行数
        """
        if df.empty:
            return 0

        # 确保必要列存在
        required = {"datetime", "last_price"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame 缺少必要列: {missing}")

        # 补充可选列
        out = df[list(required)].copy()
        for col in ["volume", "open_interest"]:
            out[col] = df[col].astype("Int64") if col in df.columns else None

        # 计算 trade_date
        if trade_date is None:
            first_dt = pd.Timestamp(out.iloc[0]["datetime"], unit="ns")
            trade_date = first_dt.strftime("%Y-%m-%d")
        out["trade_date"] = str(trade_date)
        out["symbol"] = symbol

        # 列顺序对齐表结构
        cols = ["symbol", "datetime", "last_price", "volume",
                "open_interest", "trade_date"]
        out = out[cols]

        # 先查已有主键，去重
        min_dt = int(out["datetime"].min())
        max_dt = int(out["datetime"].max())

        existing = self.conn.execute("""
            SELECT datetime FROM ticks
            WHERE symbol = ? AND trade_date = ?
              AND datetime >= ? AND datetime <= ?
        """, [symbol, str(trade_date), min_dt, max_dt]).df()

        if not existing.empty:
            existing_set = set(existing["datetime"])
            before = len(out)
            out = out[~out["datetime"].isin(existing_set)]
            skipped = before - len(out)
            if skipped > 0:
                logger.debug(f"[{symbol}] 跳过 {skipped} 条重复 tick")

        if out.empty:
            return 0

        # 注册临时 DataFrame → INSERT (避免 Python 循环)
        self.conn.register("_tmp_ticks", out)
        self.conn.execute("""
            INSERT INTO ticks (symbol, datetime, last_price, volume, open_interest, trade_date)
            SELECT symbol, datetime, last_price, volume, open_interest, trade_date
            FROM _tmp_ticks
            ON CONFLICT (symbol, trade_date, datetime) DO NOTHING
        """)
        self.conn.unregister("_tmp_ticks")

        n = len(out)

        # 更新元数据 (先查是否存在, 再 INSERT/UPDATE)
        new_last_dt = int(out["datetime"].max())
        existing = self.conn.execute(
            "SELECT last_tick_dt, total_ticks FROM meta WHERE symbol = ?",
            [symbol]).fetchone()
        if existing and existing[0] is not None:
            self.conn.execute(
                "UPDATE meta SET last_tick_dt = GREATEST(COALESCE(last_tick_dt,0), ?), "
                "total_ticks = total_ticks + ?, updated_at = NOW() WHERE symbol = ?",
                [new_last_dt, n, symbol])
        else:
            self.conn.execute(
                "INSERT INTO meta (symbol, last_tick_dt, total_ticks, updated_at) "
                "VALUES (?, ?, ?, NOW()) "
                "ON CONFLICT (symbol) DO UPDATE SET "
                "last_tick_dt = GREATEST(COALESCE(last_tick_dt,0), EXCLUDED.last_tick_dt), "
                "total_ticks = total_ticks + EXCLUDED.total_ticks, "
                "updated_at = NOW()",
                [symbol, new_last_dt, n])

        return n

    # ══════════════════════════════════════════════════════
    # K 线聚合
    # ══════════════════════════════════════════════════════
    def aggregate_klines(self, symbol: str, periods: Optional[list] = None,
                         from_date: Optional[str] = None,
                         to_date: Optional[str] = None,
                         tick_size: float = 1.0,
                         replace: bool = False) -> dict:
        """
        从 tick 数据聚合指定周期的 K 线.

        使用 DuckDB 的 time_bucket 分桶, FIRST/MAX/MIN/LAST 聚合.

        Args:
            symbol: 合约代码
            periods: 周期列表, 默认全部支持周期
            from_date: 起始日期 (含)
            to_date: 结束日期 (含)
            tick_size: 最小变动价位 (仅参考, 不影响计算)
            replace: 是否替换已有 K 线 (默认跳过已有)

        Returns:
            {period: 新增行数}
        """
        if periods is None:
            periods = ["1min", "3min", "15min", "1hour", "4hour", "1day"]

        results = {}

        for period in periods:
            secs = PERIOD_SECONDS.get(period)
            if secs is None:
                logger.warning(f"不支持的周期: {period}, 跳过")
                continue

            # 构建查询条件
            conditions = ["symbol = ?"]
            params = [symbol]
            if from_date:
                conditions.append("trade_date >= ?")
                params.append(str(from_date))
            if to_date:
                conditions.append("trade_date <= ?")
                params.append(str(to_date))
            where = " AND ".join(conditions)

            # 检查是否已有 (skip if exists)
            if not replace:
                check_sql = ("SELECT MIN(trade_date), MAX(trade_date) FROM klines "
                             "WHERE symbol = ? AND period = ?")
                check_params = [symbol, period]
                if from_date:
                    check_sql += " AND trade_date >= ?"
                    check_params.append(str(from_date))
                if to_date:
                    check_sql += " AND trade_date <= ?"
                    check_params.append(str(to_date))
                existing_dates = self.conn.execute(check_sql, check_params).fetchone()
                if existing_dates[0] is not None and existing_dates[1] is not None:
                    logger.debug(f"[{symbol}] {period} K 线已存在, 跳过")
                    results[period] = 0
                    continue

            # time_bucket 聚合
            # tqsdk datetime 是纳秒, DuckDB 的 time_bucket 需要 TIMESTAMP
            sql = f"""
                INSERT INTO klines (symbol, period, datetime, open, high, low, close, volume, oi, trade_date)
                SELECT
                    symbol,
                    ? AS period,
                    epoch_ns(time_bucket(INTERVAL '{secs} SECONDS',
                          epoch_ms((datetime / 1000000)::BIGINT))) AS bucket_start,
                    FIRST(last_price)  AS open,
                    MAX(last_price)    AS high,
                    MIN(last_price)    AS low,
                    LAST(last_price)   AS close,
                    COALESCE(LAST(volume), 0) - COALESCE(FIRST(volume), 0) AS volume,
                    LAST(open_interest) AS oi,
                    trade_date
                FROM ticks
                WHERE {where}
                GROUP BY symbol, bucket_start, trade_date
                ORDER BY bucket_start
                ON CONFLICT (symbol, period, datetime) DO NOTHING
            """
            result = self.conn.execute(sql, [period] + params)

            # 更新 meta
            new_last = self.conn.execute("""
                SELECT MAX(datetime) FROM klines
                WHERE symbol = ? AND period = ?
            """, [symbol, period]).fetchone()
            if new_last and new_last[0] is not None:
                self.conn.execute("""
                    UPDATE meta SET last_klines_dt = GREATEST(COALESCE(last_klines_dt, 0), ?)
                    WHERE symbol = ?
                """, [int(new_last[0]), symbol])

            results[period] = result.fetchall()[0][0] if result.description else 0

        return results

    def aggregate_all_klines(self, symbols: Optional[list] = None,
                             periods: Optional[list] = None) -> dict:
        """
        批量聚合所有已监控品种的 K 线.

        Returns:
            {symbol: {period: count}}
        """
        if symbols is None:
            symbols = [row[0] for row in
                       self.conn.execute("SELECT DISTINCT symbol FROM ticks ORDER BY symbol")
                       .fetchall()]

        results = {}
        for sym in symbols:
            results[sym] = self.aggregate_klines(sym, periods=periods)
        return results

    # ══════════════════════════════════════════════════════
    # 查询接口
    # ══════════════════════════════════════════════════════
    def get_ticks(self, symbol: str, from_date: Optional[str] = None,
                  to_date: Optional[str] = None,
                  limit: int = 100000) -> pd.DataFrame:
        """查询 tick 数据"""
        conditions = ["symbol = ?"]
        params = [symbol]
        if from_date:
            conditions.append("trade_date >= ?")
            params.append(str(from_date))
        if to_date:
            conditions.append("trade_date <= ?")
            params.append(str(to_date))

        return self.conn.execute(f"""
            SELECT datetime, last_price, volume, open_interest, trade_date
            FROM ticks
            WHERE {' AND '.join(conditions)}
            ORDER BY datetime
            LIMIT ?
        """, params + [limit]).df()

    def get_klines(self, symbol: str, period: str,
                   from_date: Optional[str] = None,
                   to_date: Optional[str] = None,
                   limit: int = 10000, include_empty: bool = True) -> pd.DataFrame:
        """
        查询已聚合 K 线.

        Args:
            symbol: 合约代码
            period: 周期, e.g. "15min"
            from_date / to_date: 日期范围 (trade_date)
            limit: 最大行数
            include_empty: 是否填充空 bar (close=0 的 bar)

        Returns:
            DataFrame with columns: datetime, open, high, low, close, volume, oi
        """
        conditions = ["symbol = ?", "period = ?"]
        params = [symbol, period]
        if from_date:
            conditions.append("trade_date >= ?")
            params.append(str(from_date))
        if to_date:
            conditions.append("trade_date <= ?")
            params.append(str(to_date))
        if not include_empty:
            conditions.append("close > 0")

        return self.conn.execute(f"""
            SELECT datetime, open, high, low, close, volume, oi
            FROM klines
            WHERE {' AND '.join(conditions)}
            ORDER BY datetime
            LIMIT ?
        """, params + [limit]).df()

    def get_all_klines(self, symbol: str, periods: Optional[list] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None) -> dict:
        """
        一次查询多个周期的 K 线.

        Returns:
            {period: DataFrame}
        """
        if periods is None:
            periods = ["1min", "3min", "15min", "1hour", "1day"]
        return {p: self.get_klines(symbol, p, from_date, to_date)
                for p in periods}

    def get_symbols(self) -> list:
        """获取所有已存储的合约代码"""
        return [r[0] for r in
                self.conn.execute("SELECT DISTINCT symbol FROM ticks ORDER BY symbol")
                .fetchall()]

    def get_meta(self, symbol: str) -> Optional[dict]:
        """获取某品种的元数据"""
        row = self.conn.execute(
            "SELECT * FROM meta WHERE symbol = ?", [symbol]).fetchone()
        if row is None:
            return None
        col_names = ["symbol", "last_tick_dt", "last_klines_dt",
                     "total_ticks", "updated_at"]
        return dict(zip(col_names, row))

    # ══════════════════════════════════════════════════════
    # Parquet 归档
    # ══════════════════════════════════════════════════════
    def archive_to_parquet(self, before_date: Optional[str] = None,
                           symbols: Optional[list] = None) -> int:
        """
        将 >ARCHIVE_DAYS 的旧 tick 导出为 Parquet 并删除原表数据.

        Parquet 文件路径: archive_dir/{symbol}/{YYYY}/{symbol}_{YYYYMMDD}.parquet

        Args:
            before_date: 归档此日期之前的数据 (默认 ARCHIVE_DAYS 天前)
            symbols: 限定品种 (默认全部)

        Returns:
            归档的 tick 行数
        """
        if before_date is None:
            before_date = (date.today() - timedelta(days=ARCHIVE_DAYS)).isoformat()

        conditions = ["trade_date < ?"]
        params = [str(before_date)]
        if symbols is not None:
            placeholders = ",".join(["?"] * len(symbols))
            conditions.append(f"symbol IN ({placeholders})")
            params.extend(symbols)

        where = " AND ".join(conditions)

        # 获取要归档的 (symbol, trade_date) 组合
        groups = self.conn.execute(f"""
            SELECT symbol, trade_date, COUNT(*) AS cnt
            FROM ticks
            WHERE {where}
            GROUP BY symbol, trade_date
            ORDER BY symbol, trade_date
        """, params).fetchall()

        total = 0
        for sym, tdate, cnt in groups:
            sym_clean = sym.replace(".", "_")  # 文件名友好
            year = str(tdate)[:4]
            out_dir = os.path.join(self.archive_dir, sym_clean, year)
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir,
                                    f"{sym_clean}_{tdate}.parquet")

            if os.path.exists(out_path):
                logger.debug(f"  [{sym}] {tdate} 已归档, 跳过")
                continue

            # 导出
            self.conn.execute(f"""
                COPY (
                    SELECT datetime, last_price, volume, open_interest
                    FROM ticks
                    WHERE symbol = ? AND trade_date = ?
                    ORDER BY datetime
                ) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)
            """, [sym, str(tdate), out_path])

            # 从 ticks 表删除
            self.conn.execute("""
                DELETE FROM ticks WHERE symbol = ? AND trade_date = ?
            """, [sym, str(tdate)])

            total += cnt
            logger.info(f"  [{sym}] {tdate}: {cnt:,} ticks → {out_path}")

        return total

    def query_parquet(self, symbol: str, trade_date: str) -> Optional[pd.DataFrame]:
        """
        查询归档 Parquet 文件 (自动回退到主表).
        """
        sym_clean = symbol.replace(".", "_")
        year = str(trade_date)[:4]
        path = os.path.join(self.archive_dir, sym_clean, year,
                            f"{sym_clean}_{trade_date}.parquet")

        if os.path.exists(path):
            return self.conn.execute(f"""
                SELECT datetime, last_price, volume, open_interest
                FROM read_parquet(?)
                ORDER BY datetime
            """, [path]).df()
        else:
            # 回退查主表
            return self.get_ticks(symbol, trade_date, trade_date)

    # ══════════════════════════════════════════════════════
    # 统计 / 管理
    # ══════════════════════════════════════════════════════
    def stats(self) -> pd.DataFrame:
        """各品种存储统计"""
        return self.conn.execute("""
            SELECT
                m.symbol,
                COALESCE(t.total_ticks, 0) AS hot_ticks,
                m.total_ticks AS total_ticks,
                COALESCE(k.klines_count, 0) AS klines_count,
                CASE WHEN m.last_tick_dt IS NOT NULL
                     THEN to_timestamp(m.last_tick_dt / 1000000000)
                END AS last_tick_time
            FROM meta m
            LEFT JOIN (
                SELECT symbol, COUNT(*) AS total_ticks
                FROM ticks GROUP BY symbol
            ) t ON m.symbol = t.symbol
            LEFT JOIN (
                SELECT symbol, COUNT(*) AS klines_count
                FROM klines GROUP BY symbol
            ) k ON m.symbol = k.symbol
            ORDER BY m.symbol
        """).df()

    def close(self):
        """关闭数据库连接"""
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════
def ns_to_str(ns: int) -> str:
    """纳秒时间戳 → 可读字符串"""
    if ns is None or ns <= 0:
        return "---"
    return pd.Timestamp(ns, unit="ns").strftime("%Y-%m-%d %H:%M:%S")
