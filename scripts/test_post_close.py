"""测试收盘后 tick 数据可用性 — 用大 data_length 拉取日盘完整数据"""
import sys, time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, r"C:\Users\hzpfly\WorkBuddy\2026-05-18-task-22")
from config_loader import get_tqsdk_auth
from tqsdk import TqApi, TqAuth

username, password = get_tqsdk_auth()
api = TqApi(auth=TqAuth(username, password))

products = [
    ("SHFE.cu2608", "沪铜", 8964),
    ("DCE.m2609", "豆粕", 8964),
    ("CZCE.SA609", "纯碱", 8964),
    ("SHFE.ag2608", "沪银", 5000),
]

print("=== 收盘后 tick 数据可用性测试 ===")
print(f"当前时间: {time.strftime('%H:%M:%S')} (北京时间)")
print()

tz_bj = timezone(timedelta(hours=8))

for symbol, name, dl in products:
    try:
        ts = api.get_tick_serial(symbol, data_length=dl)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                api.wait_update(deadline=min(time.time() + 1, deadline))
            except Exception:
                time.sleep(0.3)
            if len(ts) > 0:
                break
        if len(ts) == 0:
            print(f"  {symbol:20s} {name:6s} 无数据")
            continue
        datetimes = ts["datetime"].values
        earliest = datetime.fromtimestamp(datetimes[0] / 1e9, tz=tz_bj).strftime("%m-%d %H:%M:%S")
        latest = datetime.fromtimestamp(datetimes[-1] / 1e9, tz=tz_bj).strftime("%m-%d %H:%M:%S")
        fp = ts["last_price"].iloc[0]
        lp = ts["last_price"].iloc[-1]
        # 按小时统计分布
        hours = {}
        for dt_val in datetimes:
            h = datetime.fromtimestamp(dt_val / 1e9, tz=tz_bj).strftime("%H")
            hours[h] = hours.get(h, 0) + 1
        dist = " | ".join(f"{h}h:{c}" for h, c in sorted(hours.items()))
        print(f"  {symbol:20s} {name:6s} {len(ts):>6d} ticks  {earliest} ~ {latest}")
        print(f"    price: {fp} -> {lp}  分布: {dist}")
    except Exception as e:
        print(f"  {symbol:20s} {name:6s} ERROR: {e!s}")

api.close()
