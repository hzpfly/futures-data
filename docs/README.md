# Triple Screen 期货多周期监控系统

基于 Alexander Elder 三重滤网交易系统 (Triple Screen) + 动力系统 (EIS)，使用天勤量化 (TqSdk) 实时监控中国期货市场 5 个品种的主力合约。

## 系统架构

```
triple_screen_monitor.py (主监控程序)
│
├── Set A (长线):  周线 → 日线 → 小时
│   (大趋势  /  中期回调  /  精确入场)
│
├── Set B (短线):  小时 → 15min → 3min
│   (大趋势  /  中期回调  /  精确入场)
│
├── EIS 交叉验证 (每套独立)
│   Set A: 周/日/小时 EIS 颜色
│   Set B: 小时/15min/3min EIS 颜色
│
└── 5 个合约 × 2 套滤网 = 10 个监控组合
    棉花CF / 鸡蛋JD / 生猪LH / 红枣CJ / 玉米C
```

两套滤网**完全独立**，各自给出信号和可信度裁决，不合并打分。

## 三重滤网三层筛选

```
┌──────────────────────────────────────────────────────────────┐
│  Screen 1 (大周期)  趋势方向过滤                               │
│  ├─ MACD 柱状图斜率 (主信号)                                   │
│  └─ EMA(13) 斜率 (确认)                                       │
│  → 决定只能做多 / 只能做空 / 不交易                             │
├──────────────────────────────────────────────────────────────┤
│  Screen 2 (中周期)  振荡器回调信号 (FI + 价格双重确认)         │
│  ├─ Force Index EMA(2) — 力量方向确认                          │
│  ├─ close vs EMA(5) — 价格回抽存在性确认                       │
│  └─ FI 背离检测 — 卖压/买压衰竭确认                            │
│  → 三步验证: FI<0 + close<EMA(5) = 有效回调                    │
├──────────────────────────────────────────────────────────────┤
│  Screen 3 (小周期)  精确入场                                   │
│  ├─ 买入止损 = 前一根K线最高价 + tick_size                     │
│  └─ 卖出止损 = 前一根K线最低价 - tick_size                     │
│  → 价格突破触发入场，设置初始止损                               │
├──────────────────────────────────────────────────────────────┤
│  持仓跟踪  退出规则 + 追踪止损                                 │
│  ├─ 1. 止损命中 (intrabar 极值)                                │
│  ├─ 2. Screen 1 趋势反转                                       │
│  ├─ 3. 反向背离                                                │
│  └─ 4. 追踪止损 (N-bar low/high, 单向顺移)                     │
└──────────────────────────────────────────────────────────────┘
```

## 两套时间周期组合

| 套别 | Screen 1 | Screen 2 | Screen 3 | EIS 验证周期 | 定位 |
|------|----------|----------|----------|-------------|------|
| **A_长线** | 周线 | 日线 | 小时 | 周/日/小时 | 持仓数天~数周 |
| **B_短线** | 小时 | 15min | 3min | 小时/15min/3min | 日内~持仓数小时 |

- Set A 和 Set B 共享小时线（A 的 Screen 3 = B 的 Screen 1）
- 每套独立评估，各自给出信号方向和可信度
- 5 合约 × 5 周期 = 25 个 K 线订阅

## 监控合约

| 品种 | 交易所 | 代码 | Tick | 夜盘 | 主力发现方式 |
|------|--------|------|------|------|-------------|
| 棉花 | CZCE | CF | 5 | 21:00-23:00 | 持仓量最大 |
| 鸡蛋 | DCE | jd | 1 | 无 | 持仓量最大 |
| 生猪 | DCE | lh | 5 | 无 | 持仓量最大 |
| 红枣 | CZCE | CJ | 5 | 无 | 持仓量最大 |
| 玉米 | DCE | c | 1 | 21:00-23:00 | 持仓量最大 |

主力合约通过 `discover_main_contract_generic()` 自动发现，使用正则精确匹配避免误匹配（如 `c` 不匹配 `cs`）。

## Screen 2 三步回调验证

Screen 2 不是仅看 FI，而是三步验证：

| 验证步骤 | 指标 | 回答的问题 | 角色 |
|----------|------|-----------|------|
| 1. Force Index | FI EMA(2) | 空头/多头力量强吗？ | 力量方向确认 |
| 2. 价格回抽 | close vs EMA(5) | 价格确实跌了/涨了吗？ | 回抽存在性确认 |
| 3. FI 背离 | FI vs 价格极值 | 卖压/买压衰竭了吗？ | 回抽到位确认 |

**信号规则**:

| Screen 1 趋势 | FI | 价格 vs EMA(5) | 信号 |
|--------------|-----|----------------|------|
| 多头 | FI < 0 | close < EMA(5) | `buy_signal` (有效回调) |
| 多头 | FI < 0 | close >= EMA(5) | `no_signal` (价格未真正回抽) |
| 多头 | FI >= 0 | — | `no_signal` (趋势延续) |
| 多头 | FI < 0 + 底背离 | close < EMA(5) | `divergence_buy` (回调到位) |
| 空头 | FI > 0 | close > EMA(5) | `sell_signal` (有效反弹) |
| 空头 | FI > 0 | close <= EMA(5) | `no_signal` (价格未真正反弹) |
| 空头 | FI <= 0 | — | `no_signal` (趋势延续) |
| 空头 | FI > 0 + 顶背离 | close > EMA(5) | `divergence_sell` (反弹到位) |

## EIS 动力系统交叉验证

信号触发时自动运行 EIS 多周期检查，与 Triple Screen 交叉验证。

**EIS 颜色规则**:

| 颜色 | 条件 | 含义 |
|------|------|------|
| GREEN | EMA(13) 上升 + MACD 柱上升 | 多头冲动，只做多 |
| RED | EMA(13) 下降 + MACD 柱下降 | 空头冲动，只做空 |
| BLUE | EMA 与 MACD 柱方向相反 | 方向不明，观望 |

**打分**:
- EIS 各周期 ±1 分 × 0.5 权重
- Triple Screen: S1 趋势 ±1 + S2 回调 ±1 + S3 入场 ±2，× 0.5 权重
- 加权总分 → 6 级可信度裁决

**可信度级别**:

| 总分 | 裁决 |
|------|------|
| >= 2.0 | 强烈可信 — 主力仓位 |
| >= 1.0 | 可信 — 正常仓位 |
| >= 0.3 | 谨慎 — 建议半仓 |
| >= -0.3 | 观望 — EIS 与 TS 矛盾，不交易 |
| >= -1.0 | 谨慎偏空 |
| >= -2.0 | 做空可信 |
| < -2.0 | 强烈做空可信 |

**风险提示**: 多周期 EIS 冲突、EIS 与 TS 方向致命冲突时自动告警。

## 指标计算

所有指标函数定义在 `egg_futures_1min.py`，与时间周期无关，可应用于任意 OHLCV 数据。

### MACD (12, 26, 9)
```python
dif = EMA(close, 12) - EMA(close, 26)
dea = EMA(dif, 9)
hist = 2 * (dif - dea)   # MACD 柱状图
```

### EMA(13)
```python
ema = close.ewm(span=13, adjust=False).mean()
```

### Force Index + EMA(2) 平滑
```python
raw_fi = (close - close.shift(1)) * volume   # 原始力度指数
fi_ema = raw_fi.ewm(span=2, adjust=False).mean()
# α = 2/(2+1) = 0.6667, 67% 权重给最新 K 线
```

### Screen 1 趋势判定
- MACD 柱斜率：近 5 根首尾差值 > 5%×平均绝对值 → 上升/下降
- EMA(13) 斜率：近 10 根首尾差值 > 0.05%×平均价格 → 上升/下降
- 柱上升 + EMA 上升/平 → 多头 | 柱下降 + EMA 下降/平 → 空头 | 其他 → 中性

### Screen 3 入场逻辑
- 做多：entry = prev_high + tick_size; stop = min(recent_lows[-N:])
- 做空：entry = prev_low - tick_size; stop = max(recent_highs[-N:])
- 当前收盘价 >= 买入止损 → triggered_long
- 当前收盘价 <= 卖出止损 → triggered_short
- N = STOP_LOOKBACK (默认 5)

## 信号触发时机

监控程序在以下情况发出通知 (Windows Toast + 控制台响铃):

| 变化类型 | 触发条件 | 含义 |
|----------|----------|------|
| Screen 3 状态变化 | no_signal → pending_* | 入场机会出现 |
| Screen 3 状态变化 | pending_* → triggered_* | 实际入场触发 |
| Screen 3 状态变化 | pending_* → cancelled | 信号取消 |
| Screen 3 状态变化 | triggered* → no_signal | 信号消失/退出 |
| Screen 1 趋势反转 | bullish <-> bearish | 决定平仓方向 |

冷却期 180 秒，避免同一 (合约, set, signal) 重复通知。

## 目录结构

```
2026-05-18-task-22/
├── triple_screen_monitor.py   # 主监控程序 (5合约 × 2套滤网 + EIS交叉验证)
├── egg_futures_1min.py        # 核心指标库 + 鸡蛋单合约监控
├── egg_futures_chart.py       # Matplotlib 图形模式
├── eis_monitor.py             # EIS 25min+日线 双周期监控
├── weekly_eis.py              # 周线/日线 EIS 分析
├── config_loader.py           # 配置加载
├── config.ini                 # 天勤账号配置 (不提交)
├── config.example.ini         # 配置模板
├── requirements.txt           # Python 依赖
├── start.bat                  # Windows 启动脚本
│
├── scripts/                   # 一次性分析脚本
│   ├── cross_verify_jd.py     # 交叉验证 (Set A/B 独立裁决)
│   ├── probe_cf609.py         # CF609 25min/5min/1min 信号探测
│   ├── probe_cf609_wd15.py    # CF609 周/日/15min 信号探测
│   └── backtest_cf609_wd15.py # CF609 周/日/15min 级联回测
│
├── tests/                     # 测试与回测
│   ├── test_bullish_signal.py # 多头级联回测
│   ├── test_bearish_signal.py # 空头级联回测
│   └── test_exit_rules.py     # 退出规则回测
│
└── docs/                      # 文档
    ├── README.md              # 本文件
    └── README_EIS.md          # EIS 动力系统说明
```

## 快速启动

```bash
# 1. 安装依赖 (需 Python 3.12+)
pip install tqsdk pandas numpy matplotlib

# 2. 配置账号
cp config.example.ini config.ini
# 编辑 config.ini，填入天勤量化账号和密码

# 3. 运行主监控 (推荐)
python triple_screen_monitor.py

# 4. 其他工具
python egg_futures_1min.py       # 鸡蛋单合约终端监控
python egg_futures_chart.py      # 图形模式
python eis_monitor.py            # EIS 双周期监控
python weekly_eis.py             # 周线/日线 EIS 分析

# 5. 交叉验证
python scripts/cross_verify_jd.py

# 6. 回测
python tests/test_bullish_signal.py
python tests/test_bearish_signal.py
python tests/test_exit_rules.py
```

Windows 用户可双击桌面 `TripleScreen.bat` 快捷方式启动。

## 退出规则 (持仓跟踪)

持仓状态保存在 `Position` 类中（paper trading，不发真实订单）。

**退出优先级**（持仓时按顺序检查）:

| 优先级 | 规则 | 触发条件 | 平仓价 |
|--------|------|----------|--------|
| 1 | 止损命中 | 1min low <= current_stop (多头) / high >= current_stop (空头) | current_stop |
| 2 | Screen 1 反转 | 趋势不再支持持仓（含中性） | 当前收盘价 |
| 3 | 反向背离 | Screen 2 出现反向 divergence | 当前收盘价 |
| 4 | 追踪止损 | 仍持仓则更新 current_stop（只能顺向移动） | 不平仓 |

追踪止损：取最近 N 根已完成 bar 的 low (多头) / high (空头)，单向顺移。

## 系统设计特点

1. **无状态信号计算** — 所有 `determine_screenN_*` 函数纯函数式，便于回测和并行化
2. **级联门控** — Screen 2 在 Screen 1 为中性时立即返回 no_signal，Screen 3 在 Screens 1+2 不一致时返回 none
3. **FI + 价格双重确认** — Screen 2 不只看 FI，还需 close vs EMA(5) 确认价格真正回抽，过滤假回调
4. **Set A/B 完全独立** — 两套滤网各自评估，不合并打分，避免长短线信号互相稀释
5. **EIS 交叉验证** — 信号触发时自动多周期 EIS 检查，方向一致才高可信，冲突时强烈警告
6. **Paper Trading** — 不发真实订单，仅用于监控和回测
7. **追踪止损单向顺移** — 多头止损只升不降，空头止损只降不升

## 环境要求

- Python 3.12+ (tqsdk 3.9+)
- 天勤量化账号（免费版可用，支持 DCE/CZCE/SHFE/CFFEX 等交易所）
- 依赖: tqsdk, pandas, numpy, matplotlib
- Windows (使用了 winsound 和 Windows Toast 通知)

## 理论依据

Alexander Elder:
- *Trading for a Living* (1993) — 三重滤网系统、Force Index、追踪止损
- *Come Into My Trading Room* (2002) — Elder Impulse System (EIS)

> 本系统仅用于学习和监控，不构成投资建议。期货交易风险巨大，请谨慎操作。
