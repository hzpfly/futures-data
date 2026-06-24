#!/usr/bin/env python
"""发现剩余品种: LR, INE, GFEX"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config_loader import get_tqsdk_auth
from tqsdk import TqApi, TqAuth
from datetime import datetime

VALID_MONTHS = {
    'sc': list(range(1,13)), 'lu': list(range(1,13)), 'bc': list(range(1,13)),
    'nr': list(range(1,13)), 'ec': list(range(1,13)),
    'si': list(range(1,13)), 'lc': list(range(1,13)),
}

PRODUCTS = {
    'LR': ('CZCE', '瓶片'),
    'sc': ('INE', '原油'), 'lu': ('INE', '低硫燃油'), 'bc': ('INE', '国际铜'),
    'nr': ('INE', '20号胶'), 'ec': ('INE', '集运指数'),
    'si': ('GFEX', '工业硅'), 'lc': ('GFEX', '碳酸锂'),
}

DISCOVER_CANDIDATES = 4

def main():
    username, password = get_tqsdk_auth()
    api = TqApi(auth=TqAuth(username, password))
    print("[connect] 已连接天勤\n")

    now = datetime.now()
    now_y, now_m = now.year, now.month
    total = len(PRODUCTS)
    found = 0
    failed = []
    results = []
    start = time.time()

    for idx, (product, (exchange, name)) in enumerate(PRODUCTS.items(), 1):
        elapsed = time.time() - start
        print(f"[{idx:2d}/{total}] {name:8s} ({product:4s} @ {exchange}) | elapsed:{elapsed:4.0f}s", flush=True)

        valid_mm = VALID_MONTHS.get(product, list(range(1, 13)))
        codes = []
        for offset in range(8):
            cand_m = now_m + offset
            cand_y = now_y
            if cand_m > 12:
                cand_m -= 12
                cand_y += 1
            if cand_m in valid_mm:
                yy = cand_y % 100
                if exchange == 'CZCE':
                    code = f"{exchange}.{product}{yy%10}{cand_m:02d}"
                else:
                    code = f"{exchange}.{product}{yy:02d}{cand_m:02d}"
                codes.append(code)
                if len(codes) >= DISCOVER_CANDIDATES:
                    break

        tick_series = {}
        for code in codes:
            try:
                ts = api.get_tick_serial(code, data_length=200)
                tick_series[code] = ts
            except Exception:
                pass

        if not tick_series:
            print(f"  -> FAIL: 无可用合约", flush=True)
            failed.append((product, name, exchange, '无可用合约'))
            continue

        # 等数据 (最多 8s)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                api.wait_update(deadline=min(time.time() + 1, deadline))
            except Exception:
                time.sleep(0.5)
            if all(len(ts) > 0 for ts in tick_series.values()):
                break

        best_code, best_oi = None, 0
        for code, ts in tick_series.items():
            oi = 0
            if len(ts) > 0:
                last = ts.iloc[-1]
                oi = int(last.get("open_interest", 0) or 0)
            if oi > best_oi:
                best_oi = oi
                best_code = code

        if best_code:
            found += 1
            results.append((product, name, best_code, best_oi, exchange))
            print(f"  -> {best_code}  OI={best_oi}", flush=True)
        else:
            print(f"  -> FAIL: 所有合约OI=0", flush=True)
            failed.append((product, name, exchange, 'OI=0'))

    print()
    print("=" * 70)
    print(f"剩余品种发现完成! 成功 {found}/{total}, 耗时 {time.time()-start:.0f}s")
    for p, n, s, oi, ex in results:
        print(f"  {p:6s} {n:10s} {s:22s} OI={oi:>10d} @{ex}")
    if failed:
        print("失败:")
        for p, n, ex, r in failed:
            print(f"  {p:6s} {n:10s} @{ex}: {r}")

    api.close()

if __name__ == '__main__':
    main()
