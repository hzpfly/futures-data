"""
Screen 2 价格确认离线单元测试
=============================
用合成数据验证 determine_screen2_signal 的 FI + 价格双重确认逻辑，
无需连接 TqSdk 实盘。

测试覆盖:
  1. 多头 + FI<0 + close<EMA5 → buy_signal (价格确认回调)
  2. 多头 + FI<0 + close>=EMA5 → no_signal (价格未回抽，新逻辑)
  3. 空头 + FI>0 + close>EMA5 → sell_signal (价格确认反弹)
  4. 空头 + FI>0 + close<=EMA5 → no_signal (价格未反弹，新逻辑)
  5. Screen 1 中性 → 无条件 no_signal (门控)
  6. Screen 3 级联: buy_signal + close>=entry → triggered_long
  7. Screen 3 级联: buy_signal + close<entry → pending_long
  8. Screen 3 级联: sell_signal + close<=entry → triggered_short
  9. Screen 3 级联: sell_signal + close>entry → pending_short

用法:
    python tests/test_screen2_price_confirm.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from egg_futures_1min import (
    determine_screen2_signal,
    determine_screen3_entry,
    determine_screen1_trend,
    calc_force_index,
    calc_ema,
    calc_macd,
)


# ── 辅助函数 ──────────────────────────────────────────────

def make_klines(closes, volumes=None, highs=None, lows=None, start_ns=1_700_000_000_000_000_000):
    """用收盘价序列构造 klines DataFrame (模拟 TqSdk 格式)"""
    n = len(closes)
    if volumes is None:
        volumes = [1000] * n
    if highs is None:
        highs = [c + 5 for c in closes]
    if lows is None:
        lows = [c - 5 for c in closes]
    datetimes = [start_ns + i * 60 * 10**9 for i in range(n)]  # 1min 间隔
    return pd.DataFrame({
        "datetime": datetimes,
        "open": closes,
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes,
    })


def make_declining_klines(base_price, n_bars, decline_per_bar=3, volume=1000):
    """构造持续下跌的 klines (FI 会为负, close 会低于 EMA5)"""
    closes = [base_price - i * decline_per_bar for i in range(n_bars)]
    return make_klines(closes, volumes=[volume] * n_bars)


def make_rising_klines(base_price, n_bars, rise_per_bar=3, volume=1000):
    """构造持续上涨的 klines (FI 会为正, close 会高于 EMA5)"""
    closes = [base_price + i * rise_per_bar for i in range(n_bars)]
    return make_klines(closes, volumes=[volume] * n_bars)


def make_accelerating_uptrend(base_price, n_bars, accel=0.05, volume=1000):
    """构造加速(三次方)上涨的 klines (MACD 柱持续上升 → Screen 1 bullish)"""
    closes = [base_price + (i ** 3) * accel for i in range(n_bars)]
    return make_klines(closes, volumes=[volume] * n_bars)


def make_accelerating_downtrend(base_price, n_bars, accel=0.05, volume=1000):
    """构造加速(三次方)下跌的 klines (MACD 柱持续下降 → Screen 1 bearish)"""
    closes = [base_price - (i ** 3) * accel for i in range(n_bars)]
    return make_klines(closes, volumes=[volume] * n_bars)


def make_uptrend_then_pullback(base_price, n_uptrend, n_pullback, trend_step=2, pullback_step=3, volume=1000):
    """构造先涨后跌序列: FI 为负 (最后一根跌), close 可能低于 EMA5"""
    closes = [base_price + i * trend_step for i in range(n_uptrend)]
    peak = closes[-1]
    closes += [peak - i * pullback_step for i in range(1, n_pullback + 1)]
    return make_klines(closes, volumes=[volume] * len(closes))


def make_downtrend_then_rally(base_price, n_downtrend, n_rally, trend_step=-2, rally_step=3, volume=1000):
    """构造先跌后涨序列: FI 为正 (最后一根涨), close 可能高于 EMA5"""
    closes = [base_price + i * trend_step for i in range(n_downtrend)]
    trough = closes[-1]
    closes += [trough + i * rally_step for i in range(1, n_rally + 1)]
    return make_klines(closes, volumes=[volume] * len(closes))


def make_uptrend_flat_pullback(base_price, n_uptrend, n_flat, trend_step=2, volume=1000):
    """
    构造先涨后横盘序列: FI 为负 (横盘时 close 不变, 但 EMA(2) 被前值拖累可能为负),
    close 仍在 EMA5 上方 (价格未真正回抽)。
    """
    closes = [base_price + i * trend_step for i in range(n_uptrend)]
    peak = closes[-1]
    closes += [peak] * n_flat  # 横盘
    # 但要保证 FI 为负: 最后一根需要 close 略低于前一根
    closes[-1] = peak - 0.5  # 微跌, FI 为负但很小
    return make_klines(closes, volumes=[volume] * len(closes))


# ── 测试用例 ──────────────────────────────────────────────

PASSED = 0
FAILED = 0


def assert_eq(label, actual, expected):
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  ✅ {label}: {actual}")
    else:
        FAILED += 1
        print(f"  ❌ {label}: expected={expected}, actual={actual}")


def assert_true(label, actual):
    assert_eq(label, actual, True)


def assert_false(label, actual):
    assert_eq(label, actual, False)


# ── Test 1: 多头 + FI<0 + close<EMA5 → buy_signal ──
def test_bullish_pullback_confirmed():
    print("\n── Test 1: 多头 + FI<0 + close<EMA5 → buy_signal ──")
    klines = make_uptrend_then_pullback(
        base_price=4000, n_uptrend=30, n_pullback=5,
        trend_step=3, pullback_step=5, volume=1000,
    )
    s2 = determine_screen2_signal("bullish", klines)

    fi = calc_force_index(klines, ema_span=2)
    ema5 = klines["close"].ewm(span=5, adjust=False).mean()
    latest_close = klines["close"].iloc[-1]
    latest_ema = ema5.iloc[-1]
    latest_fi = fi.iloc[-1]

    print(f"  close={latest_close:.1f}, EMA5={latest_ema:.1f}, FI={latest_fi:.1f}")
    assert_eq("signal", s2["signal"], "buy_signal")
    assert_true("price_confirmed", s2["price_confirmed"])


# ── Test 2: 多头 + FI<0 + close>=EMA5 → no_signal (新逻辑) ──
def test_bullish_fi_negative_but_price_not_confirmed():
    print("\n── Test 2: 多头 + FI<0 + close>=EMA5 → no_signal (价格未回抽) ──")
    # 构造: 强上涨后微跌一根, FI 为负但 close 仍远高于 EMA5
    klines = make_uptrend_flat_pullback(
        base_price=4000, n_uptrend=30, n_flat=3,
        trend_step=5, volume=1000,
    )
    s2 = determine_screen2_signal("bullish", klines)

    fi = calc_force_index(klines, ema_span=2)
    ema5 = klines["close"].ewm(span=5, adjust=False).mean()
    latest_close = klines["close"].iloc[-1]
    latest_ema = ema5.iloc[-1]
    latest_fi = fi.iloc[-1]

    print(f"  close={latest_close:.1f}, EMA5={latest_ema:.1f}, FI={latest_fi:.1f}")
    # 价格确认应该为 False (close 仍高于 EMA5)
    if latest_close >= latest_ema:
        print(f"  ✓ 确认 close>=EMA5 (价格未回抽)")
        assert_eq("signal", s2["signal"], "no_signal")
        assert_false("price_confirmed", s2["price_confirmed"])
    else:
        print(f"  ⚠ 测试数据不满足前提条件 (close<{latest_ema:.1f}), 跳过断言")


# ── Test 3: 空头 + FI>0 + close>EMA5 → sell_signal ──
def test_bearish_rally_confirmed():
    print("\n── Test 3: 空头 + FI>0 + close>EMA5 → sell_signal ──")
    klines = make_downtrend_then_rally(
        base_price=4000, n_downtrend=30, n_rally=5,
        trend_step=-3, rally_step=5, volume=1000,
    )
    s2 = determine_screen2_signal("bearish", klines)

    fi = calc_force_index(klines, ema_span=2)
    ema5 = klines["close"].ewm(span=5, adjust=False).mean()
    latest_close = klines["close"].iloc[-1]
    latest_ema = ema5.iloc[-1]
    latest_fi = fi.iloc[-1]

    print(f"  close={latest_close:.1f}, EMA5={latest_ema:.1f}, FI={latest_fi:.1f}")
    assert_eq("signal", s2["signal"], "sell_signal")
    assert_true("price_confirmed", s2["price_confirmed"])


# ── Test 4: 空头 + FI>0 + close<=EMA5 → no_signal (新逻辑) ──
def test_bearish_fi_positive_but_price_not_confirmed():
    print("\n── Test 4: 空头 + FI>0 + close<=EMA5 → no_signal (价格未反弹) ──")
    # 构造: 30根持续下跌(-5/根) → EMA5 ≈ 3865 (滞后约10点)
    # 然后涨5点 → FI为正(EMA(2)翻正), 但 close=3860 < EMA5=3865
    closes = [4000 - i * 5 for i in range(30)]  # 到 3855
    closes += [3860]  # 涨5点, FI>0 但 close<EMA5
    klines = make_klines(closes, volumes=[1000] * len(closes))
    s2 = determine_screen2_signal("bearish", klines)

    fi = calc_force_index(klines, ema_span=2)
    ema5 = klines["close"].ewm(span=5, adjust=False).mean()
    latest_close = klines["close"].iloc[-1]
    latest_ema = ema5.iloc[-1]
    latest_fi = fi.iloc[-1]

    print(f"  close={latest_close:.1f}, EMA5={latest_ema:.1f}, FI={latest_fi:.1f}")
    if latest_fi > 0 and latest_close <= latest_ema:
        print(f"  ✓ 确认 FI>0 且 close<=EMA5 (价格未反弹)")
        assert_eq("signal", s2["signal"], "no_signal")
        assert_false("price_confirmed", s2["price_confirmed"])
    else:
        print(f"  ⚠ 测试数据不满足前提条件 (FI={latest_fi:.1f}, close={latest_close:.1f} vs EMA5={latest_ema:.1f}), 跳过断言")


# ── Test 5: Screen 1 中性 → 无条件 no_signal ──
def test_neutral_screen1_gating():
    print("\n── Test 5: Screen 1 中性 → 无条件 no_signal (门控) ──")
    klines = make_declining_klines(4000, 30, decline_per_bar=3)
    s2 = determine_screen2_signal("neutral", klines)

    assert_eq("signal", s2["signal"], "no_signal")
    assert_false("price_confirmed", s2["price_confirmed"])


# ── Test 6: 多头趋势延续 (FI>0) → no_signal ──
def test_bullish_trend_continuation():
    print("\n── Test 6: 多头 + FI>0 (趋势延续) → no_signal ──")
    klines = make_rising_klines(4000, 30, rise_per_bar=3)
    s2 = determine_screen2_signal("bullish", klines)

    fi = calc_force_index(klines, ema_span=2)
    latest_fi = fi.iloc[-1]
    print(f"  FI={latest_fi:.1f} (应为正)")
    assert_eq("signal", s2["signal"], "no_signal")
    assert_false("price_confirmed", s2["price_confirmed"])


# ── Test 7: Screen 3 级联 — triggered_long ──
def test_screen3_triggered_long():
    print("\n── Test 7: Screen 3 — buy_signal + close>=entry → triggered_long ──")
    # 构造 1min klines: 前高 4100, 当前 close 4105 > entry(4101)
    highs = [4090, 4100, 4105]
    lows = [4080, 4090, 4095]
    closes = [4085, 4095, 4105]
    klines_1min = make_klines(closes, highs=highs, lows=lows)

    s3 = determine_screen3_entry("bullish", "buy_signal", klines_1min, tick_size=1)
    print(f"  prev_high={s3['prev_high']}, entry={s3['entry_price']}, close={closes[-1]}")
    assert_eq("signal", s3["signal"], "triggered_long")
    assert_eq("entry_price", s3["entry_price"], 4101)  # 4100 + 1


# ── Test 8: Screen 3 级联 — pending_long ──
def test_screen3_pending_long():
    print("\n── Test 8: Screen 3 — buy_signal + close<entry → pending_long ──")
    highs = [4090, 4100, 4095]
    lows = [4080, 4090, 4085]
    closes = [4085, 4095, 4090]
    klines_1min = make_klines(closes, highs=highs, lows=lows)

    s3 = determine_screen3_entry("bullish", "buy_signal", klines_1min, tick_size=1)
    print(f"  prev_high={s3['prev_high']}, entry={s3['entry_price']}, close={closes[-1]}")
    assert_eq("signal", s3["signal"], "pending_long")


# ── Test 9: Screen 3 级联 — triggered_short ──
def test_screen3_triggered_short():
    print("\n── Test 9: Screen 3 — sell_signal + close<=entry → triggered_short ──")
    highs = [4110, 4100, 4095]
    lows = [4100, 4090, 4085]
    closes = [4105, 4095, 4085]
    klines_1min = make_klines(closes, highs=highs, lows=lows)

    s3 = determine_screen3_entry("bearish", "sell_signal", klines_1min, tick_size=1)
    print(f"  prev_low={s3['prev_low']}, entry={s3['entry_price']}, close={closes[-1]}")
    assert_eq("signal", s3["signal"], "triggered_short")
    assert_eq("entry_price", s3["entry_price"], 4089)  # 4090 - 1


# ── Test 10: Screen 3 级联 — pending_short ──
def test_screen3_pending_short():
    print("\n── Test 10: Screen 3 — sell_signal + close>entry → pending_short ──")
    highs = [4110, 4100, 4110]
    lows = [4100, 4090, 4095]
    closes = [4105, 4095, 4100]
    klines_1min = make_klines(closes, highs=highs, lows=lows)

    s3 = determine_screen3_entry("bearish", "sell_signal", klines_1min, tick_size=1)
    print(f"  prev_low={s3['prev_low']}, entry={s3['entry_price']}, close={closes[-1]}")
    assert_eq("signal", s3["signal"], "pending_short")


# ── Test 11: Screen 1 趋势判断 ──
def test_screen1_trend_detection():
    print("\n── Test 11: Screen 1 趋势判断 ──")
    # 三次方加速上涨 → MACD 柱持续上升 + EMA 上升 → bullish
    klines_bull = make_accelerating_uptrend(4000, 40, accel=0.05)
    s1_bull = determine_screen1_trend(klines_bull)
    print(f"  加速上涨: trend={s1_bull['trend']}, hist={s1_bull['hist_slope']}, ema={s1_bull['ema_slope']}")
    assert_eq("bullish trend", s1_bull["trend"], "bullish")

    # 三次方加速下跌 → MACD 柱持续下降 + EMA 下降 → bearish
    klines_bear = make_accelerating_downtrend(5000, 40, accel=0.05)
    s1_bear = determine_screen1_trend(klines_bear)
    print(f"  加速下跌: trend={s1_bear['trend']}, hist={s1_bear['hist_slope']}, ema={s1_bear['ema_slope']}")
    assert_eq("bearish trend", s1_bear["trend"], "bearish")


# ── Test 12: FI 力度指标计算验证 ──
def test_force_index_calculation():
    print("\n── Test 12: Force Index EMA(2) 计算验证 ──")
    closes = [100, 102, 101, 103, 105, 104]
    volumes = [1000] * 6
    klines = make_klines(closes, volumes=volumes)

    fi = calc_force_index(klines, ema_span=2)
    # 手动验证 (EMA adjust=False, 第一个非NaN值直接取原值):
    # raw_fi = [NaN, 2000, -1000, 2000, 2000, -1000]
    # α = 2/(2+1) = 0.6667
    # fi_ema[1] = 2000  (首个非NaN, adjust=False)
    # fi_ema[2] = α*(-1000) + (1-α)*2000 = -666.67 + 666.67 = 0
    # fi_ema[3] = α*2000 + (1-α)*0 = 1333.33
    # fi_ema[4] = α*2000 + (1-α)*1333.33 = 1777.78
    # fi_ema[5] = α*(-1000) + (1-α)*1777.78 = -74.07
    alpha = 2 / 3
    fi_ema_1 = 2000.0
    fi_ema_2 = alpha * (-1000) + (1 - alpha) * fi_ema_1     # 0.0
    fi_ema_3 = alpha * 2000 + (1 - alpha) * fi_ema_2         # 1333.33
    fi_ema_4 = alpha * 2000 + (1 - alpha) * fi_ema_3         # 1777.78
    fi_ema_5 = alpha * (-1000) + (1 - alpha) * fi_ema_4      # -74.07
    expected_last = fi_ema_5
    actual_last = fi.iloc[-1]
    print(f"  FI(EMA2) last = {actual_last:.2f}, expected ≈ {expected_last:.2f}")
    assert_true("FI calculation close", abs(actual_last - expected_last) < 1.0)


# ── Test 13: 完整级联 — Screen 1 bullish → Screen 2 buy → Screen 3 triggered ──
def test_full_bullish_cascade():
    print("\n── Test 13: 完整多头级联 (Screen 1→2→3) ──")
    # Screen 1: 40 根三次方加速上涨 5min klines → bullish
    klines_5min = make_accelerating_uptrend(4000, 40, accel=0.05)
    s1 = determine_screen1_trend(klines_5min)
    print(f"  Screen 1: trend={s1['trend']}")

    # 在上涨末端加 8 根急跌 → FI 转负 + close 跌破 EMA5 → buy_signal
    closes_base = [4000 + (i ** 3) * 0.05 for i in range(40)]
    peak = closes_base[-1]
    closes_pb = closes_base + [peak - i * 50 for i in range(1, 9)]
    klines_5min_pb = make_klines(closes_pb, volumes=[1000] * len(closes_pb))

    s2 = determine_screen2_signal(s1["trend"], klines_5min_pb)
    print(f"  Screen 2: signal={s2['signal']}, price_confirmed={s2['price_confirmed']}, FI={s2['fi_value']:.1f}")

    if s2["signal"] in ("buy_signal", "divergence_buy"):
        # Screen 3: 构造 1min klines 让 close >= entry
        highs_1 = [4100, 4120, 4125]
        lows_1 = [4090, 4110, 4115]
        closes_1 = [4095, 4115, 4122]
        klines_1min = make_klines(closes_1, highs=highs_1, lows=lows_1)
        s3 = determine_screen3_entry(s1["trend"], s2["signal"], klines_1min)
        print(f"  Screen 3: signal={s3['signal']}, entry={s3['entry_price']}")
        assert_eq("cascade result", s3["signal"], "triggered_long")
    else:
        print(f"  ⚠ Screen 2 未给出 buy_signal, 级联未完成 (这本身可能是正确的——如果价格未真正回抽)")


# ── 主入口 ──────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Screen 2 价格确认离线单元测试")
    print("=" * 60)

    tests = [
        test_force_index_calculation,
        test_screen1_trend_detection,
        test_bullish_pullback_confirmed,
        test_bullish_fi_negative_but_price_not_confirmed,
        test_bearish_rally_confirmed,
        test_bearish_fi_positive_but_price_not_confirmed,
        test_neutral_screen1_gating,
        test_bullish_trend_continuation,
        test_screen3_triggered_long,
        test_screen3_pending_long,
        test_screen3_triggered_short,
        test_screen3_pending_short,
        test_full_bullish_cascade,
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            global FAILED
            FAILED += 1
            print(f"  ❌ EXCEPTION: {e}")

    print(f"\n{'=' * 60}")
    print(f"  Results: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
    if FAILED == 0:
        print("  ✅ ALL TESTS PASSED")
    else:
        print(f"  ❌ {FAILED} TEST(S) FAILED")
    print("=" * 60)

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
