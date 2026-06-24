#!/usr/bin/env python
"""发现所有品种的主力合约 (基于 OI 最大值)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import get_tqsdk_auth
from tqsdk import TqApi, TqAuth
from datetime import datetime

# ═══ 品种配置 (来自 tick_collector.py) ═══
ALL_MONTHS = list(range(1, 13))
ODD_MONTHS = [1, 3, 5, 7, 9, 11]

VALID_MONTHS = {
    'CF': ODD_MONTHS, 'CY': ODD_MONTHS, 'SR': ODD_MONTHS,
    'OI': ODD_MONTHS, 'RM': ODD_MONTHS,
    'CJ': [1, 3, 5, 7, 9, 12],
    'AP': [1, 3, 5, 10, 11, 12],
    'PK': [1, 3, 5, 10],
    'au': [2, 4, 6, 8, 10, 12],
    'bu': [6, 9, 12], 'fu': [1, 5, 9],
    'ru': [1, 3, 5, 7, 9, 11],
    'a': ODD_MONTHS, 'b': ODD_MONTHS,
    'm': ODD_MONTHS, 'y': ODD_MONTHS,
    'c': ODD_MONTHS, 'cs': ODD_MONTHS,
    'jd': ODD_MONTHS, 'lh': ODD_MONTHS,
}

PRODUCTS = {
    # SHFE (17)
    'cu': ('SHFE', '沪铜'), 'al': ('SHFE', '沪铝'), 'zn': ('SHFE', '沪锌'),
    'pb': ('SHFE', '沪铅'), 'ni': ('SHFE', '沪镍'), 'sn': ('SHFE', '沪锡'),
    'au': ('SHFE', '沪金'), 'ag': ('SHFE', '沪银'),
    'rb': ('SHFE', '螺纹钢'), 'hc': ('SHFE', '热卷'),
    'ru': ('SHFE', '橡胶'), 'bu': ('SHFE', '沥青'),
    'fu': ('SHFE', '燃油'), 'sp': ('SHFE', '纸浆'),
    'ss': ('SHFE', '不锈钢'), 'ao': ('SHFE', '氧化铝'),
    'wr': ('SHFE', '线材'),
    # DCE (22)
    'a': ('DCE', '豆一'), 'b': ('DCE', '豆二'),
    'm': ('DCE', '豆粕'), 'y': ('DCE', '豆油'),
    'p': ('DCE', '棕榈油'), 'c': ('DCE', '玉米'),
    'cs': ('DCE', '淀粉'), 'jd': ('DCE', '鸡蛋'),
    'lh': ('DCE', '生猪'), 'l': ('DCE', '塑料'),
    'pp': ('DCE', 'PP'), 'v': ('DCE', 'PVC'),
    'eg': ('DCE', '乙二醇'), 'eb': ('DCE', '苯乙烯'),
    'i': ('DCE', '铁矿石'), 'jm': ('DCE', '焦煤'),
    'j': ('DCE', '焦炭'), 'pg': ('DCE', '液化气'),
    'rr': ('DCE', '粳米'),
    # CZCE (19, 代码用1位年份)
    'CF': ('CZCE', '棉花'), 'CY': ('CZCE', '棉纱'),
    'SR': ('CZCE', '白糖'), 'OI': ('CZCE', '菜油'),
    'RM': ('CZCE', '菜粕'), 'AP': ('CZCE', '苹果'),
    'CJ': ('CZCE', '红枣'), 'MA': ('CZCE', '甲醇'),
    'UR': ('CZCE', '尿素'), 'SA': ('CZCE', '纯碱'),
    'FG': ('CZCE', '玻璃'), 'PF': ('CZCE', '短纤'),
    'TA': ('CZCE', 'PTA'), 'SF': ('CZCE', '硅铁'),
    'SM': ('CZCE', '锰硅'), 'PK': ('CZCE', '花生'),
    'SH': ('CZCE', '烧碱'), 'PX': ('CZCE', '对二甲苯'),
    'LR': ('CZCE', '瓶片'),
    # INE (5)
    'sc': ('INE', '原油'), 'lu': ('INE', '低硫燃油'),
    'bc': ('INE', '国际铜'), 'nr': ('INE', '20号胶'),
    'ec': ('INE', '集运指数'),
    # GFEX (2)
    'si': ('GFEX', '工业硅'), 'lc': ('GFEX', '碳酸锂'),
}

DISCOVER_CANDIDATES = 4

def main():
    username, password = get_tqsdk_auth()
    print(f"[auth] 用户名: {username}")
    api = TqApi(auth=TqAuth(username, password))
    print("[connect] 已连接天勤\n")

    now = datetime.now()
    now_y, now_m = now.year, now.month
    total = len(PRODUCTS)
    found = 0
    failed_list = []
    results = []
    start = time.time()

    for idx, (product, (exchange, name)) in enumerate(PRODUCTS.items(), 1):
        elapsed = time.time() - start
        eta = (elapsed / idx) * (total - idx) if idx > 0 else 0
        print(f"[{idx:2d}/{total}] {name:8s} ({product:4s} @ {exchange}) | elapsed:{elapsed:4.0f}s eta:{eta:4.0f}s", flush=True)

        valid_mm = VALID_MONTHS.get(product, ALL_MONTHS)

        # 生成候选代码 (前看8个月)
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

        # 用 tick_serial 批量获取
        tick_series = {}
        for code in codes:
            try:
                ts = api.get_tick_serial(code, data_length=200)
                tick_series[code] = ts
            except Exception:
                pass

        if not tick_series:
            print(f"  -> FAIL: 无可用合约", flush=True)
            failed_list.append((product, name, exchange, '无可用合约'))
            continue

        # 等数据 (最多5s)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                api.wait_update(deadline=min(time.time() + 0.5, deadline))
            except Exception:
                time.sleep(0.3)
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
            failed_list.append((product, name, exchange, 'OI=0'))

    # 汇总
    print()
    print("=" * 70)
    print(f"发现完成! 成功 {found}/{total}, 耗时 {time.time()-start:.0f}s")
    print("=" * 70)
    print()
    print("=== 成功列表 ===")
    for p, n, s, oi, ex in sorted(results, key=lambda x: x[0]):
        print(f"  {p:6s} {n:10s} {s:22s} OI={oi:>10d} @{ex}")
    print()
    if failed_list:
        print("=== 失败列表 ===")
        for p, n, ex, reason in failed_list:
            print(f"  {p:6s} {n:10s} @{ex}: {reason}")
    print()

    api.close()

if __name__ == '__main__':
    main()
