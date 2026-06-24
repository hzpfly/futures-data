"""
主力合约 CSV 初始化脚本
======================
逻辑:
  1. 用配置的 current 合约代码作为起点
  2. 订阅其 quote, 若持仓量 > 0 → 仍是主力, 直接用
  3. 若持仓量 = 0 (已过期) → 按交割月顺序向后找, 取首个持仓量 > 0 的
  4. 为 (主力合约, 周期) 各建立/更新一个 CSV 文件
  5. 保存 main_contracts.json

用法:
    python scripts/init_main_contract_csv.py
    python scripts/init_main_contract_csv.py --dry-run
    python scripts/init_main_contract_csv.py --csv-dir data_main
"""

import sys, os, json, time as _time, argparse
import sys as _sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from collections import OrderedDict

try:
    from tqsdk import TqApi, TqAuth
    HAS_TQSDK = True
except ImportError:
    HAS_TQSDK = False

from config_loader import get_tqsdk_auth
import pandas as pd


# ── 品种配置 ─────────────────────────────────────────────
#   year_fmt: "Y"=郑商所1位年份(CF609), "YY"=大商所2位年份(jd2509)
PRODUCTS = [
    {
        "name":       "棉花",
        "exchange":   "CZCE",
        "product":    "CF",
        "code_fmt":  "CZCE.CF{Y}{MM:02d}",
        "year_fmt":  "Y",
        "months":     [1, 3, 5, 7, 9, 11],
        "tick":       5.0,
        "multiplier": 5,
        "night":      True,
        "current":    "CZCE.CF609",
    },
    {
        "name":       "鸡蛋",
        "exchange":   "DCE",
        "product":    "jd",
        "code_fmt":  "DCE.jd{YY}{MM:02d}",
        "year_fmt":  "YY",
        "months":     list(range(1, 13)),
        "tick":       1.0,
        "multiplier": 10,
        "night":      False,
        "current":    "DCE.jd2509",
    },
    {
        "name":       "生猪",
        "exchange":   "DCE",
        "product":    "lh",
        "code_fmt":  "DCE.lh{YY}{MM:02d}",
        "year_fmt":  "YY",
        "months":     list(range(1, 13)),
        "tick":       5.0,
        "multiplier": 16,
        "night":      False,
        "current":    "DCE.lh2509",
    },
    {
        "name":       "红枣",
        "exchange":   "CZCE",
        "product":    "CJ",
        "code_fmt":  "CZCE.CJ{Y}{MM:02d}",
        "year_fmt":  "Y",
        "months":     [1, 3, 5, 7, 9, 11, 12],
        "tick":       5.0,
        "multiplier": 5,
        "night":      False,
        "current":    "CZCE.CJ509",
    },
    {
        "name":       "玉米",
        "exchange":   "DCE",
        "product":    "c",
        "code_fmt":  "DCE.c{YY}{MM:02d}",
        "year_fmt":  "YY",
        "months":     list(range(1, 13)),
        "tick":       1.0,
        "multiplier": 10,
        "night":      False,
        "current":    "DCE.c2509",
    },
]

KLINE_DURS = {"1week": 604800, "1day": 86400, "1hour": 3600, "15min": 900}
DATA_LEN = 8964
PERIODS  = list(KLINE_DURS.keys())


def _flush(msg=""):
    if msg:
        _sys.stdout.write(msg + "\n")
        _sys.stdout.flush()


def fmt_day(ns):
    if ns and ns > 0:
        return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")
    return "---"


def make_code(cfg, year, mm):
    """根据 year_fmt 生成合约代码"""
    if cfg["year_fmt"] == "Y":
        return cfg["code_fmt"].format(Y=year % 10, MM=mm)
    else:
        return cfg["code_fmt"].format(YY=year % 100, MM=mm)


def iter_contracts_after(cfg, start_yy, start_mm):
    """
    从 (start_yy, start_mm) 往后, 按交割月顺序生成合约代码,
    直到找到有效的为止 (由调用方控制终止)。
    """
    now = datetime.now()
    now_yy = now.year % 100
    now_y  = now.year % 10
    now_mm  = now.month

    months = cfg["months"]
    # 找到 start_mm 在 months 中的位置
    try:
        idx = months.index(start_mm)
    except ValueError:
        idx = 0

    yy = start_yy
    y  = start_yy % 10 if cfg["year_fmt"] == "Y" else start_yy % 100
    # 重新修正 y
    if cfg["year_fmt"] == "Y":
        # Y = year % 10, 需要处理跨 9→0 的边界
        base_yy = now_yy
        y = now_y
    else:
        y = yy  # 对 YY 格式, 直接用 yy

    # 更简单的实现: 从 start_mm 开始, 逐月往后
    # 但只生成交割月在 start_mm 及之后的 (同一年或下一年)
    count = 0
    max_try = 12  # 最多往后看 12 个交割月
    idx = months.index(start_mm) if start_mm in months else 0

    while count < max_try:
        mm = months[idx % len(months)]
        # 判断是否已超过当前年份的合理范围
        if idx >= len(months):
            yy += 1
            if cfg["year_fmt"] == "Y":
                y = (y + 1) % 10
            idx = 0
            continue

        code = make_code(cfg, yy, mm)
        yield code

        count += 1
        idx += 1


def find_main_contract(api, cfg):
    """
    主力发现逻辑 (修正版):
      1. 先查 current 合约是否还有效 (持仓量 > 0)
      2. 若已失效, 生成最近 8 个候选合约代码
      3. 逐个订阅 quote (短超时 5s), 收集所有 OI > 0 的
      4. 返回 OI 最大者
    返回: symbol 字符串
    """
    cur = cfg["current"]
    _flush(f"    检查配置中的合约: {cur}")

    # ── 步骤 1: 检查 current 合约 ──
    main_candidate = None
    try:
        q = api.get_quote(cur)
        deadline = _time.time() + 8
        while _time.time() < deadline:
            api.wait_update(deadline=_time.time() + 1)
            if hasattr(q, "open_interest") and q.open_interest is not None:
                oi = q.open_interest or 0
                vol = getattr(q, "volume", 0) or 0
                _flush(f"    {cur}: 持仓={oi:>8,}  成交量={vol:>8,}")
                if oi > 0:
                    _flush(f"    ✅ 仍是主力: {cur}")
                    return cur
                else:
                    _flush(f"    ⚠️  持仓量=0, 已过期, 查找新主力...")
                    main_candidate = None
                    break
    except Exception as e:
        _flush(f"    ⚠️  订阅 {cur} 失败: {e}, 查找新主力...")

    # ── 步骤 2: 生成候选代码 ──
    # 解析 cur 的年份和月份
    import re
    m = re.search(r"\.(\D*)(\d+)$", cur)
    if not m:
        _flush(f"    ⚠️  无法解析 {cur}, 使用原值")
        return cur

    num_part = m.group(2)
    if cfg["year_fmt"] == "Y":
        # 1 位年份: 609 → Y=6, MM=09
        y_digit  = int(num_part[0])
        mm_digit = int(num_part[1:3])
        # 推断完整年份
        now_y = datetime.now().year % 10
        yy = datetime.now().year
        if y_digit < now_y:
            yy += (y_digit - now_y + 10)
        else:
            yy += (y_digit - now_y)
        start_yy = yy % 100
        start_mm  = mm_digit
    else:
        # 2 位年份: 2509 → YY=25, MM=09
        yy_digit = int(num_part[0:2])
        mm_digit = int(num_part[2:4])
        now_full = datetime.now().year
        base = now_full - (now_full % 100)
        start_yy = base + yy_digit
        if start_yy < base:
            start_yy += 100
        start_yy = start_yy % 100
        start_mm  = mm_digit

    # 从下一个月开始, 生成最多 20 个候选 (覆盖 ~1.5 年)
    months = cfg["months"]
    candidates = []
    yy = start_yy
    # 找到 start_mm 的索引
    if start_mm in months:
        idx = months.index(start_mm) + 1
        if idx >= len(months):
            idx = 0
            yy = (yy + 1) % 100
    else:
        idx = 0
        yy = (yy + 1) % 100

    count = 0
    while count < 20:
        if idx >= len(months):
            yy = (yy + 1) % 100
            idx = 0
            continue
        mm = months[idx]
        code = make_code(cfg, yy, mm)
        candidates.append(code)
        count += 1
        idx += 1

    _flush(f"    候选新主力 (最多 20 个): {', '.join(candidates)}")

    # ── 步骤 3: 逐个订阅, 收集 OI > 0 的 ──
    ranked = []  # [(oi, vol, code), ...]
    for code in candidates:
        try:
            q = api.get_quote(code)
            deadline = _time.time() + 5
            got = False
            while _time.time() < deadline:
                api.wait_update(deadline=_time.time() + 1)
                if hasattr(q, "open_interest") and q.open_interest is not None:
                    oi = q.open_interest or 0
                    vol = getattr(q, "volume", 0) or 0
                    _flush(f"    {code}: 持仓={oi:>8,}  成交量={vol:>8,}")
                    if oi > 0:
                        ranked.append((oi, vol, code))
                    got = True
                    break
            if not got:
                _flush(f"    {code}: 超时, 跳过")
        except Exception as e:
            _flush(f"    {code}: 不存在, 跳过")

    # ── 步骤 4: 返回 OI 最大者 ──
    if ranked:
        ranked.sort(reverse=True)
        best = ranked[0]
        _flush(f"    ✅ 新主力: {best[2]} (持仓={best[0]:,})")
        return best[2]
    else:
        _flush(f"    ⚠️  未找到有效新主力, 使用 {cur}")
        return cur


def _merge_klines(old_df, new_df):
    """合并新旧 K 线: 以 datetime 为 key, new_df 优先"""
    old_df = old_df.copy()
    new_df = new_df.copy()
    old_df["datetime"] = old_df["datetime"].astype("int64")
    new_df["datetime"] = new_df["datetime"].astype("int64")
    old_only = old_df[~old_df["datetime"].isin(new_df["datetime"])].copy()
    merged = pd.concat([old_only, new_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["datetime"], keep="last")
    merged = merged.sort_values("datetime").reset_index(drop=True)
    return merged


def download_and_save(api, symbol, csv_dir, periods=None):
    """下载 symbol 的各周期 K 线, 保存/增量更新 CSV"""
    if periods is None:
        periods = PERIODS
    os.makedirs(csv_dir, exist_ok=True)
    _flush(f"    下载 {symbol} K 线...")

    subs = {}
    for p in periods:
        subs[p] = api.get_kline_serial(symbol, KLINE_DURS[p], data_length=DATA_LEN)

    # 等待数据到达
    deadline = _time.time() + 90
    reported = False
    while _time.time() < deadline:
        api.wait_update(deadline=_time.time() + 1)
        all_ok = True
        for p in periods:
            k = subs[p]
            if len(k) < 50 or (len(k) > 0 and k.iloc[-1]["close"] <= 0):
                all_ok = False
                break
        if all_ok:
            break
        if not reported and _time.time() - (deadline - 90) > 5:
            for p in periods:
                k = subs[p]
                _flush(f"      等待 {p}: {len(k)} bars...")
            reported = True
    else:
        _flush(f"    ⚠️  数据下载超时, 部分周期可能不完整")

    result = {}
    for p in periods:
        fname = f"{symbol}_{p}.csv"
        fpath = os.path.join(csv_dir, fname)
        new_df = subs[p].copy()

        if os.path.exists(fpath):
            old_df = pd.read_csv(fpath)
            if "datetime" in old_df.columns:
                old_df["datetime"] = old_df["datetime"].astype("int64")
            merged = _merge_klines(old_df, new_df)
            merged.to_csv(fpath, index=False)
            old_valid = len(old_df[old_df["close"] > 0])
            new_valid = len(merged[merged["close"] > 0])
            added = new_valid - old_valid
            t0 = fmt_day(merged.iloc[0]["datetime"]) if len(merged) > 0 else "---"
            t1 = fmt_day(merged.iloc[-1]["datetime"]) if len(merged) > 0 else "---"
            if added > 0:
                _flush(f"    {p:6s}: 更新 +{added} 新 bar  ({old_valid}→{new_valid})  {t0}..{t1}")
            else:
                _flush(f"    {p:6s}: 已最新 (有效 {new_valid})")
            result[p] = merged
        else:
            new_df.to_csv(fpath, index=False)
            valid = len(new_df[new_df["close"] > 0])
            t0 = fmt_day(new_df.iloc[0]["datetime"]) if len(new_df) > 0 else "---"
            t1 = fmt_day(new_df.iloc[-1]["datetime"]) if len(new_df) > 0 else "---"
            _flush(f"    {p:6s}: 首次保存 {len(new_df)} bars (有效 {valid})  {t0}..{t1}")
            result[p] = new_df

    return result


def load_main_records(json_path):
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_main_records(json_path, records):
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="主力合约 CSV 初始化")
    parser.add_argument("--dry-run",   action="store_true", help="只打印主力合约, 不下载数据")
    parser.add_argument("--csv-dir",  default="data_main",    help="CSV 输出目录 (默认 data_main/)")
    parser.add_argument("--json-path", default=None,              help="主力合约记录 JSON 路径")
    parser.add_argument("--products", nargs="*",                help="只处理指定品种")
    args = parser.parse_args()

    csv_dir   = args.csv_dir
    json_path = args.json_path or os.path.join(csv_dir, "main_contracts.json")
    os.makedirs(csv_dir, exist_ok=True)

    if not HAS_TQSDK:
        _flush("错误: tqsdk 未安装")
        sys.exit(1)

    products = PRODUCTS
    if args.products:
        products = [p for p in products if p["name"] in args.products]
        if not products:
            _flush(f"错误: 未找到品种 {args.products}")
            sys.exit(1)

    username, password = get_tqsdk_auth()
    _flush(f"连接 Tqsdk ({username})...")
    api = TqApi(auth=TqAuth(username, password))
    _flush("连接成功\n")

    old_records = load_main_records(json_path)
    new_records = {}

    try:
        for cfg in products:
            name = cfg["name"]
            _flush(f"{'─'*58}")
            _flush(f"  [{name}] {cfg['exchange']}.{cfg['product']}")
            _flush(f"{'─'*58}")

            symbol = find_main_contract(api, cfg)
            if symbol is None:
                _flush(f"  ⚠️  未找到主力合约, 跳过\n")
                continue

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec = {
                "symbol":     symbol,
                "exchange":   cfg["exchange"],
                "product":    cfg["product"],
                "name":       name,
                "tick":       cfg["tick"],
                "multiplier": cfg["multiplier"],
                "night":      cfg["night"],
                "updated_at": now_str,
            }

            old_sym = old_records.get(name, {}).get("symbol", "")
            if old_sym and old_sym != symbol:
                _flush(f"    🔃 主力切换: {old_sym} → {symbol}")
            elif old_sym == symbol:
                _flush(f"    主力未变: {symbol}")

            if args.dry_run:
                _flush("")
                continue

            _flush(f"    开始下载 K 线...")
            klines = download_and_save(api, symbol, csv_dir, periods=PERIODS)
            rec["bars"] = {p: int(len(k)) for p, k in klines.items()}
            _flush("")
            new_records[name] = rec

        if not args.dry_run and new_records:
            save_main_records(json_path, new_records)
            _flush(f"{'─'*58}")
            _flush(f"  主力合约记录已保存: {json_path}")
            _flush(f"  CSV 数据目录: {os.path.abspath(csv_dir)}/")
            _flush(f"{'─'*58}\n")
            _flush("  主力合约汇总:")
            for name, rec in new_records.items():
                bars_info = ", ".join(f"{p}={rec['bars'][p]}" for p in PERIODS if p in rec.get("bars", {}))
                _flush(f"    {name}: {rec['symbol']}  ({bars_info})")

    except KeyboardInterrupt:
        _flush("\n中断")
    finally:
        try:
            api.close()
        except Exception:
            pass
        _flush("完成")


if __name__ == "__main__":
    main()
