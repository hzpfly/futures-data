"""检查 DuckDB tick 数据内容"""
import duckdb
from datetime import datetime

db = duckdb.connect(r'C:\Users\hzpfly\WorkBuddy\2026-05-18-task-22\data\tick_data.db')

# 查看所有表
tables = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
print("=== 数据库表 ===")
for t in tables:
    print(f"  {t[0]}")

# tick 数据统计
try:
    total = db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    print(f"\n=== ticks 表: 总计 {total} 条 ===")

    stats = db.execute("""
        SELECT symbol, COUNT(*) as cnt,
               MIN(datetime) as earliest, MAX(datetime) as latest
        FROM ticks
        GROUP BY symbol
        ORDER BY COUNT(*) DESC
    """).fetchall()

    print(f'\n{"合约":<25s} {"tick数":>8s}  {"最早时间":>22s}  {"最晚时间":>22s}')
    print("-" * 85)
    for s in stats:
        earliest = datetime.fromtimestamp(s[2] / 1e9).strftime("%m-%d %H:%M:%S") if s[2] else "N/A"
        latest = datetime.fromtimestamp(s[3] / 1e9).strftime("%m-%d %H:%M:%S") if s[3] else "N/A"
        print(f"{s[0]:<25s} {s[1]:>8d}  {earliest:>22s}  {latest:>22s}")
except Exception as e:
    print(f"ticks 表查询失败: {e}")

# 也看看 klines 表
try:
    klines_total = db.execute("SELECT COUNT(*) FROM klines_1min").fetchone()[0]
    print(f"\n=== klines_1min 表: 总计 {klines_total} 条 ===")
    kstats = db.execute("""
        SELECT symbol, COUNT(*) as cnt
        FROM klines_1min
        GROUP BY symbol
        ORDER BY COUNT(*) DESC
        LIMIT 15
    """).fetchall()
    for s in kstats:
        print(f"  {s[0]:<25s} {s[1]:>8d}")
except Exception as e:
    print(f"klines 表查询失败: {e}")

print()
db.close()
