"""
Tick 数据采集器
===============
独立运行的后台进程, 负责:
  1. 自动发现所有品种的主力合约
  2. 订阅 tick 数据并批量写入 DuckDB
  3. 定时聚合 K 线 (多周期)
  4. 自动归档历史数据 (Parquet)

用法:
  # 独立运行 (从 tqsdk 拉取实时 tick)
  python tick_collector.py

  # 指定品种 (逗号分隔)
  python tick_collector.py --products CF,jd,lh,CJ,c

  # 回填历史 tick (起始日期)
  python tick_collector.py --backfill 2026-01-01

  # 非交易时段聚合 K 线
  python tick_collector.py --aggregate-klines
"""

import sys
import os
import argparse
import logging
import time as _time
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from tqsdk import TqApi, TqAuth
    HAS_TQSDK = True
except ImportError:
    HAS_TQSDK = False

from config_loader import get_tqsdk_auth
from tick_storage import TickStore, BATCH_SIZE, ARCHIVE_DAYS

# ⚠️ 必须在所有 import 之后配置 logging (tqsdk 会覆盖 basicConfig)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,  # 强制覆盖已有的 handler
    stream=sys.stdout,  # 显式输出到 stdout
)
logger = logging.getLogger("tick_collector")


# ── 品种配置 (国内商品期货全覆盖) ──
# exchange: 交易所 (CZCE=郑州 用1位年份, 其余用2位)
# tick: 最小变动价位
# night: 是否有夜盘
PRODUCTS = {
    # ═══ 上海期货交易所 (SHFE) ═══
    "cu": {"exchange": "SHFE", "name": "沪铜",   "tick": 10,   "night": True},
    "al": {"exchange": "SHFE", "name": "沪铝",   "tick": 5,    "night": True},
    "zn": {"exchange": "SHFE", "name": "沪锌",   "tick": 5,    "night": True},
    "pb": {"exchange": "SHFE", "name": "沪铅",   "tick": 5,    "night": True},
    "ni": {"exchange": "SHFE", "name": "沪镍",   "tick": 10,   "night": True},
    "sn": {"exchange": "SHFE", "name": "沪锡",   "tick": 10,   "night": True},
    "au": {"exchange": "SHFE", "name": "沪金",   "tick": 0.02, "night": True},
    "ag": {"exchange": "SHFE", "name": "沪银",   "tick": 1,    "night": True},
    "rb": {"exchange": "SHFE", "name": "螺纹钢", "tick": 1,    "night": True},
    "hc": {"exchange": "SHFE", "name": "热卷",   "tick": 1,    "night": True},
    "ru": {"exchange": "SHFE", "name": "橡胶",   "tick": 5,    "night": True},
    "bu": {"exchange": "SHFE", "name": "沥青",   "tick": 1,    "night": True},
    "fu": {"exchange": "SHFE", "name": "燃油",   "tick": 1,    "night": True},
    "sp": {"exchange": "SHFE", "name": "纸浆",   "tick": 2,    "night": True},
    "ss": {"exchange": "SHFE", "name": "不锈钢", "tick": 5,    "night": True},
    "ao": {"exchange": "SHFE", "name": "氧化铝", "tick": 1,    "night": True},
    "wr": {"exchange": "SHFE", "name": "线材",   "tick": 1,    "night": False},
    # ═══ 大连商品交易所 (DCE) ═══
    "a":  {"exchange": "DCE",  "name": "豆一",   "tick": 1,    "night": True},
    "b":  {"exchange": "DCE",  "name": "豆二",   "tick": 1,    "night": True},
    "m":  {"exchange": "DCE",  "name": "豆粕",   "tick": 1,    "night": True},
    "y":  {"exchange": "DCE",  "name": "豆油",   "tick": 2,    "night": True},
    "p":  {"exchange": "DCE",  "name": "棕榈油", "tick": 2,    "night": True},
    "c":  {"exchange": "DCE",  "name": "玉米",   "tick": 1,    "night": True},
    "cs": {"exchange": "DCE",  "name": "淀粉",   "tick": 1,    "night": True},
    "jd": {"exchange": "DCE",  "name": "鸡蛋",   "tick": 1,    "night": False},
    "lh": {"exchange": "DCE",  "name": "生猪",   "tick": 5,    "night": False},
    "l":  {"exchange": "DCE",  "name": "塑料",   "tick": 1,    "night": True},
    "pp": {"exchange": "DCE",  "name": "PP",     "tick": 1,    "night": True},
    "v":  {"exchange": "DCE",  "name": "PVC",    "tick": 1,    "night": True},
    "eg": {"exchange": "DCE",  "name": "乙二醇", "tick": 1,    "night": True},
    "eb": {"exchange": "DCE",  "name": "苯乙烯", "tick": 1,    "night": True},
    "i":  {"exchange": "DCE",  "name": "铁矿石", "tick": 0.5,  "night": True},
    "jm": {"exchange": "DCE",  "name": "焦煤",   "tick": 0.5,  "night": True},
    "j":  {"exchange": "DCE",  "name": "焦炭",   "tick": 0.5,  "night": True},
    "pg": {"exchange": "DCE",  "name": "液化气", "tick": 1,    "night": True},
    "rr": {"exchange": "DCE",  "name": "粳米",   "tick": 1,    "night": False},
    # ═══ 郑州商品交易所 (CZCE, 代码用1位年份) ═══
    "CF": {"exchange": "CZCE", "name": "棉花",   "tick": 5,    "night": True},
    "CY": {"exchange": "CZCE", "name": "棉纱",   "tick": 5,    "night": True},
    "SR": {"exchange": "CZCE", "name": "白糖",   "tick": 1,    "night": True},
    "OI": {"exchange": "CZCE", "name": "菜油",   "tick": 1,    "night": True},
    "RM": {"exchange": "CZCE", "name": "菜粕",   "tick": 1,    "night": True},
    "AP": {"exchange": "CZCE", "name": "苹果",   "tick": 1,    "night": False},
    "CJ": {"exchange": "CZCE", "name": "红枣",   "tick": 5,    "night": False},
    "MA": {"exchange": "CZCE", "name": "甲醇",   "tick": 1,    "night": True},
    "UR": {"exchange": "CZCE", "name": "尿素",   "tick": 1,    "night": True},
    "SA": {"exchange": "CZCE", "name": "纯碱",   "tick": 1,    "night": True},
    "FG": {"exchange": "CZCE", "name": "玻璃",   "tick": 1,    "night": True},
    "PF": {"exchange": "CZCE", "name": "短纤",   "tick": 2,    "night": True},
    "TA": {"exchange": "CZCE", "name": "PTA",    "tick": 2,    "night": True},
    "SF": {"exchange": "CZCE", "name": "硅铁",   "tick": 2,    "night": False},
    "SM": {"exchange": "CZCE", "name": "锰硅",   "tick": 2,    "night": False},
    "PK": {"exchange": "CZCE", "name": "花生",   "tick": 2,    "night": False},
    "SH": {"exchange": "CZCE", "name": "烧碱",   "tick": 1,    "night": True},
    "PX": {"exchange": "CZCE", "name": "对二甲苯","tick": 2,   "night": True},
    # LR 瓶片: CZCE冷门品种, tick_serial阻塞, 暂不加入
    # ═══ 上海国际能源交易中心 (INE) ═══
    "sc": {"exchange": "INE",  "name": "原油",   "tick": 0.1,  "night": True},
    "lu": {"exchange": "INE",  "name": "低硫燃油","tick": 1,   "night": True},
    "bc": {"exchange": "INE",  "name": "国际铜", "tick": 10,   "night": True},
    "nr": {"exchange": "INE",  "name": "20号胶", "tick": 5,    "night": True},
    "ec": {"exchange": "INE",  "name": "集运指数","tick": 0.1, "night": False},
    # ═══ 广州期货交易所 (GFEX) ═══
    "si": {"exchange": "GFEX", "name": "工业硅", "tick": 5,    "night": False},
    "lc": {"exchange": "GFEX", "name": "碳酸锂", "tick": 50,   "night": False},
}

# ── 交易时段检测 ──
NIGHT_START  = (21, 0)
NIGHT_END    = (23, 0)
DAY_START    = (9, 0)
DAY_END_MORN = (11, 30)
DAY_MID_START = (13, 30)
DAY_END_AFT  = (15, 0)


def is_trading_time() -> bool:
    """判断当前是否在交易时段 (含夜盘 21:00-23:00 和日盘 9:00-15:00)"""
    now = _time.localtime()
    hm = (now.tm_hour, now.tm_min)
    h, m = hm

    # 夜盘: 21:00 - 23:00
    if (h > 21 or (h == 21 and m >= 0)) and (h < 23):
        return True

    # 日盘上午: 9:00 - 11:30
    if (h > 9 or (h == 9 and m >= 0)) and (h < 11 or (h == 11 and m <= 30)):
        return True

    # 日盘下午: 13:30 - 15:00
    if (h == 13 and m >= 30) or h == 14 or (h == 15 and m == 0):
        return True

    return False


def is_night_session() -> bool:
    """判断当前是否在夜盘时段"""
    now = _time.localtime()
    h, m = now.tm_hour, now.tm_min
    return (h == 21 or h == 22 or (h == 23 and m == 0))


def get_next_trade_date() -> str:
    """获取当前 tick 对应的 trade_date"""
    now = datetime.now()
    # 夜盘 21:00 后 tick 属于下一个自然日
    if now.hour >= 21:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


# ── Tick 缓冲区 (攒批写入) ──
class TickBuffer:
    """每品种攒批 Buffer, 达到阈值自动 flush"""

    def __init__(self, store: TickStore, batch_size: int = BATCH_SIZE):
        self.store = store
        self.batch_size = batch_size
        self._buffers = defaultdict(list)   # symbol → [tick dicts]
        self._last_flush = {}               # symbol → timestamp
        self._total_written = defaultdict(int)

    def add(self, symbol: str, tick: dict):
        """添加一条 tick"""
        self._buffers[symbol].append(tick)
        if len(self._buffers[symbol]) >= self.batch_size:
            self._flush(symbol)

    def flush_all(self):
        """强制 flush 所有品种"""
        for symbol in list(self._buffers.keys()):
            self._flush(symbol)

    def _flush(self, symbol: str):
        ticks = self._buffers[symbol]
        if not ticks:
            return

        import pandas as pd
        df = pd.DataFrame(ticks)

        # 推断 trade_date
        first_dt = pd.Timestamp(df.iloc[0]["datetime"], unit="ns")
        trade_date = first_dt.strftime("%Y-%m-%d")
        # 夜盘 tick 属于下一个自然日
        if first_dt.hour >= 20:
            trade_date = get_next_trade_date()

        n = self.store.insert_ticks(symbol, df, trade_date=trade_date)
        self._total_written[symbol] += n
        self._buffers[symbol] = []
        self._last_flush[symbol] = _time.time()
        if n > 0:
            logger.debug(f"[{symbol}] flushed {n} ticks (total {self._total_written[symbol]})")

    def stats(self) -> str:
        parts = []
        for sym, total in sorted(self._total_written.items()):
            pending = len(self._buffers.get(sym, []))
            parts.append(f"{sym.split('.')[-1]}={total}(+{pending})")
        return "  ".join(parts)


# ── 有效交割月份 (避免查询不存在的合约导致超时) ──
# 未列出的品种默认全部月份 (1-12)
ALL_MONTHS = list(range(1, 13))
ODD_MONTHS = [1, 3, 5, 7, 9, 11]          # 单月合约

# 有效交割月份 (避免查询不存在合约导致35s超时)
# ⚠️ 未列出品种默认使用 ALL_MONTHS, 但 DISCOVER_CANDIDATES=4 控制候选数
VALID_MONTHS = {
    # CZCE 特殊月份
    "CF": ODD_MONTHS,                       # 棉花: 单月
    "CY": ODD_MONTHS,                       # 棉纱: 单月
    "SR": ODD_MONTHS,                       # 白糖: 单月
    "OI": ODD_MONTHS,                       # 菜油: 单月
    "RM": ODD_MONTHS,                       # 菜粕: 单月
    "CJ": [1, 3, 5, 7, 9, 12],            # 红枣
    "AP": [1, 3, 5, 10, 11, 12],           # 苹果 (7月无挂盘)
    "PK": [1, 3, 5, 10],                    # 花生: 1/3/5/10月 (非单月)
    "SH": ODD_MONTHS,                       # 烧碱: 单月
    "PX": ODD_MONTHS,                       # 对二甲苯: 单月
    # SHFE 非连续月份
    "au": [2, 4, 6, 8, 10, 12],           # 黄金: 双月
    "bu": [6, 9, 12],                      # 沥青: 主力6/9/12月
    "fu": [1, 5, 9],                       # 燃油: 主力1/5/9月
    "ru": [1, 3, 5, 7, 9, 11],            # 橡胶: 单月
    # DCE 单月品种
    "a":  ODD_MONTHS, "b": ODD_MONTHS,     # 豆一/豆二: 单月
    "m":  ODD_MONTHS, "y": ODD_MONTHS,     # 豆粕/豆油: 单月
    "c":  ODD_MONTHS, "cs": ODD_MONTHS,    # 玉米/淀粉: 单月
    "jd": ODD_MONTHS, "lh": ODD_MONTHS,    # 鸡蛋/生猪: 单月
}

# 主力合约发现候选数 (减少 get_quote 调用, 避免超时)
DISCOVER_CANDIDATES = 4

# get_tick_serial 拉取的历史 tick 条数 (免费账户上限 8964)
# 200 只能覆盖约 2 分钟, 8964 可覆盖整个日盘 (4h 约 14400 tick，取最近 8964 ≈ 2.5h)
HISTORY_TICK_LENGTH = 8964

# ── 主力合约发现 (用 tick_serial 替代 get_quote, 避免阻塞超时) ──
def discover_main_contract(api, exchange: str, product: str, tick_size: int) -> str:
    """通过 tick_serial 的 OI 发现主力合约 (不阻塞)"""
    now = datetime.now()
    now_y = now.year
    now_m = now.month

    valid_mm = VALID_MONTHS.get(product, ALL_MONTHS)

    # 生成未来 6 个月内的有效交割月候选
    codes = []
    for offset in range(6):
        cand_m = now_m + offset
        cand_y = now_y
        if cand_m > 12:
            cand_m -= 12
            cand_y += 1
        if cand_m in valid_mm:
            yy = cand_y % 100
            if exchange == "CZCE":
                code = f"{exchange}.{product}{yy%10}{cand_m:02d}"
            else:
                code = f"{exchange}.{product}{yy:02d}{cand_m:02d}"
            codes.append(code)
            if len(codes) >= DISCOVER_CANDIDATES:
                break

    # 用 tick_serial 替代 get_quote (不会阻塞 35s)
    tick_series = {}
    for code in codes:
        try:
            print(f"  [discover] {product} tick_serial {code}...", flush=True)
            ts = api.get_tick_serial(code, data_length=200)
            tick_series[code] = ts
        except Exception as e:
            print(f"  [discover] {product} tick_serial {code} failed: {e}", flush=True)

    # 无可用候选 → 直接跳过
    if not tick_series:
        print(f"  [discover] {product} → 无可用合约, 跳过", flush=True)
        return None

    # 等待数据到达 (5s 超时, 单次 wait_update 最多 2s 防死等)
    print(f"  [discover] {product} waiting for tick data ({len(tick_series)} candidates)...", flush=True)
    deadline = _time.time() + 5
    while _time.time() < deadline:
        try:
            api.wait_update(deadline=min(_time.time() + 0.5, deadline))
        except Exception:
            _time.sleep(0.5)
        all_got = all(len(ts) > 0 for ts in tick_series.values())
        if all_got:
            break

    # 取 OI 最大值作为主力合约
    best_code = None
    best_oi = 0
    for code, ts in tick_series.items():
        oi = 0
        if len(ts) > 0:
            last = ts.iloc[-1]
            oi = int(last.get("open_interest", 0) or 0)
        print(f"  [discover] {code}: {len(ts)} ticks, OI={oi}", flush=True)
        if oi > best_oi:
            best_oi = oi
            best_code = code

    print(f"  [discover] {product} → {best_code} (OI={best_oi})", flush=True)
    return best_code


# ══════════════════════════════════════════════════════════
# 主采集循环
# ══════════════════════════════════════════════════════════
def run_collector(args):
    """运行 tick 采集主循环"""
    print("[collector] run_collector() started", flush=True)
    if not HAS_TQSDK:
        logger.error("tqsdk 未安装, 请在 venv 中 pip install tqsdk")
        return

    username, password = get_tqsdk_auth()
    msg = f"连接 TqSdk ({username})..."
    print(f"[collector] {msg}", flush=True)
    logger.info(msg)
    api = TqApi(auth=TqAuth(username, password))
    store = TickStore(db_path=args.db_path, archive_dir=args.archive_dir)

    # ── 阶段1: 批量发现主力合约 (fire all, wait once) ──
    selected = args.products.split(",") if args.products else list(PRODUCTS.keys())
    total = len(selected)

    # 1.1 生成所有合约候选代码
    print(f"[discover] 批量发现主力合约 ({total} 品种)...", flush=True)
    discover_start = _time.time()
    _now = datetime.now()
    _now_y, _now_m = _now.year, _now.month

    # product → (candidates, config) 映射
    discover_map = {}  # product → {"codes": [...], "cfg": {...}}
    for product in selected:
        if product not in PRODUCTS:
            logger.warning(f"未知品种: {product}, 跳过")
            continue
        cfg = PRODUCTS[product]
        valid_mm = VALID_MONTHS.get(product, ALL_MONTHS)
        codes = []
        for offset in range(6):
            cand_m = _now_m + offset
            cand_y = _now_y
            if cand_m > 12:
                cand_m -= 12
                cand_y += 1
            if cand_m in valid_mm:
                yy = cand_y % 100
                if cfg["exchange"] == "CZCE":
                    code = f"{cfg['exchange']}.{product}{yy%10}{cand_m:02d}"
                else:
                    code = f"{cfg['exchange']}.{product}{yy:02d}{cand_m:02d}"
                codes.append(code)
                if len(codes) >= DISCOVER_CANDIDATES:
                    break
        if codes:
            discover_map[product] = {"codes": codes, "cfg": cfg}

    # 1.2 批量 fire get_tick_serial (全部非阻塞)
    print(f"[discover] 批量订阅 {sum(len(m['codes']) for m in discover_map.values())} 个候选合约...", flush=True)
    candidate_ts = {}  # code → tick_series
    for product, info in discover_map.items():
        for code in info["codes"]:
            try:
                candidate_ts[code] = api.get_tick_serial(code, data_length=200)
            except Exception as e:
                pass

    # 1.3 单次 wait_update 获取全部数据 (最多 20s)
    print(f"[discover] 等待数据到达 ({len(candidate_ts)} 个订阅)...", flush=True)
    deadline = _time.time() + 20
    while _time.time() < deadline:
        try:
            api.wait_update(deadline=min(_time.time() + 1, deadline))
        except Exception:
            _time.sleep(0.3)
        all_got = all(len(ts) > 0 for ts in candidate_ts.values())
        if all_got:
            break

    # 1.4 选出每个品种 OI 最大的合约
    contracts = {}
    for product, info in discover_map.items():
        best_code, best_oi = None, 0
        for code in info["codes"]:
            ts = candidate_ts.get(code)
            if ts is None or len(ts) == 0:
                continue
            oi = int(ts.iloc[-1].get("open_interest", 0) or 0)
            if oi > best_oi:
                best_oi = oi
                best_code = code
        if best_code:
            contracts[product] = {
                "symbol": best_code,
                "name": info["cfg"]["name"],
                "tick": info["cfg"]["tick"],
                "night": info["cfg"]["night"],
            }
            print(f"  {info['cfg']['name']} → {best_code} (OI={best_oi})", flush=True)
            logger.info(f"  {info['cfg']['name']}: {best_code}")
        else:
            logger.warning(f"  {info['cfg']['name']}: 未找到主力合约")

    elapsed = _time.time() - discover_start
    print(f"[discover] 完成: {len(contracts)}/{total} 个品种, 耗时 {elapsed:.1f}s", flush=True)

    if not contracts:
        logger.error("未发现任何合约, 退出")
        api.close()
        return

    # ── 阶段2: 批量订阅 tick 数据 (fire all, wait once) ──
    print(f"[subscribe] 批量订阅 tick 数据 ({len(contracts)} 个合约)...", flush=True)
    tick_series = {}
    for product, c in contracts.items():
        try:
            tick_series[product] = api.get_tick_serial(c["symbol"], data_length=HISTORY_TICK_LENGTH)
        except Exception as e:
            print(f"  {c['name']} ({c['symbol']}): 订阅失败 {e}", flush=True)

    # 单次等待首批数据 (最多 30s)
    print("[subscribe] 等待首批 tick 到达...", flush=True)
    deadline = _time.time() + 30
    while _time.time() < deadline:
        try:
            api.wait_update(deadline=min(_time.time() + 1, deadline))
        except Exception:
            _time.sleep(0.3)
        all_ready = all(len(ts) > 0 for ts in tick_series.values())
        if all_ready:
            break

    for product, ts in tick_series.items():
        c = contracts[product]
        if len(ts) > 0:
            last = ts.iloc[-1]
            dt_str = pd.Timestamp(last["datetime"], unit="ns").strftime("%H:%M:%S") if hasattr(last, "datetime") else "---"
            print(f"  {c['symbol']}: {len(ts)} ticks, latest={dt_str} price={last.get('last_price', '---')}", flush=True)
        else:
            print(f"  {c['symbol']}: 无数据 (午休/非交易时段)", flush=True)

    # ── 主循环 ──
    buffer = TickBuffer(store, batch_size=args.batch_size)
    last_agg_time = _time.time()
    last_archive_check = _time.time()
    last_processed = defaultdict(int)  # product → last tick datetime (去重用)

    print("\n[collector] === 开始采集 tick 数据 (Ctrl+C 停止) ===\n", flush=True)

    try:
        while True:
            api.wait_update(deadline=_time.time() + 1)

            # ── 处理 tick 数据 ──
            for product, ts in tick_series.items():
                c = contracts[product]
                if len(ts) == 0:
                    continue

                # 获取增量 tick (上次处理位置之后的)
                new_ticks = []
                for i in range(len(ts)):
                    row = ts.iloc[i]
                    dt = int(row["datetime"])
                    if dt <= last_processed[product]:
                        continue
                    new_ticks.append({
                        "datetime": dt,
                        "last_price": float(row.get("last_price", 0)),
                        "volume": int(row.get("volume", 0)) if row.get("volume") > 0 else None,
                        "open_interest": int(row.get("open_interest", 0)) if row.get("open_interest") > 0 else None,
                    })
                    last_processed[product] = dt

                for tick in new_ticks:
                    buffer.add(c["symbol"], tick)

            # ── 定时 flush (每 5 秒兜底) ──
            if _time.time() - buffer._last_flush.get(product, 0) > 5:
                buffer.flush_all()

            # ── 每小时聚合 K 线 (非交易时段) ──
            if _time.time() - last_agg_time > 3600 and not is_trading_time():
                logger.info("非交易时段, 执行 K 线聚合...")
                for product, c in contracts.items():
                    try:
                        store.aggregate_klines(c["symbol"])
                    except Exception as e:
                        logger.error(f"  {c['symbol']} 聚合失败: {e}")
                last_agg_time = _time.time()

            # ── 每天归档一次 (凌晨) ──
            now = datetime.now()
            if (now.hour == 3 and now.minute < 5 and
                    _time.time() - last_archive_check > 3600):
                logger.info("执行 Parquet 归档...")
                try:
                    n = store.archive_to_parquet()
                    logger.info(f"归档完成: {n} 条 tick")
                except Exception as e:
                    logger.error(f"归档失败: {e}")
                last_archive_check = _time.time()

            # ── 每分钟打印统计 ──
            if int(_time.time()) % 60 < 2:
                stats = buffer.stats()
                if stats:
                    ts = "交易" if is_trading_time() else "等待"
                    print(f"[collector:{ts}] tick: {stats}", flush=True)

    except KeyboardInterrupt:
        logger.info("\n收到中断信号, 停止采集...")

    finally:
        logger.info("flush 剩余数据...")
        buffer.flush_all()
        store.close()
        api.close()
        logger.info("采集器已停止")


# ══════════════════════════════════════════════════════════
# K 线聚合模式 (非实时, 批量)
# ══════════════════════════════════════════════════════════
def run_aggregate_klines(args):
    """批量聚合已有 tick 的 K 线"""
    store = TickStore(db_path=args.db_path, read_only=False)
    symbols = store.get_symbols()
    if not symbols:
        logger.warning("数据库中没有 tick 数据, 请先运行采集器")
        store.close()
        return

    logger.info(f"为 {len(symbols)} 个合约聚合 K 线...")
    for sym in symbols:
        try:
            result = store.aggregate_klines(sym)
            counts = {k: v for k, v in result.items() if v > 0}
            if counts:
                logger.info(f"  {sym}: {counts}")
            else:
                logger.info(f"  {sym}: 已有数据, 跳过")
        except Exception as e:
            logger.error(f"  {sym}: 聚合失败 {e}")

    # 打印统计
    stats = store.stats()
    print("\n存储统计:")
    print(stats.to_string(index=False))

    store.close()


# ══════════════════════════════════════════════════════════
# 命令行入口
# ══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Tick 数据采集器")
    parser.add_argument("--db-path", default="data/tick_data.db",
                        help="DuckDB 数据库路径 (默认 data/tick_data.db)")
    parser.add_argument("--archive-dir", default="data/tick_archive",
                        help="Parquet 归档目录 (默认 data/tick_archive)")
    parser.add_argument("--products", default=None,
                        help="限定品种 (逗号分隔), 默认全部. 例: CF,jd,lh")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"批量写入大小 (默认 {BATCH_SIZE})")
    parser.add_argument("--aggregate-klines", action="store_true",
                        help="仅聚合已有 tick 的 K 线 (非实时模式)")

    args = parser.parse_args()

    if args.aggregate_klines:
        run_aggregate_klines(args)
    else:
        run_collector(args)


if __name__ == "__main__":
    # 需要 pandas (tqsdk 自带但不保证)
    import pandas as pd
    main()
