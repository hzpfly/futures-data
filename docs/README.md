# Triple Screen Trading System for Chinese Futures

基于 Alexander Elder 的三重滤网交易系统 (Triple Screen Trading System)，使用天勤量化 (TqSdk) 实时监控中国期货市场。

## 系统概述

三重滤网系统的核心思想：**用大周期判断趋势方向，用中周期寻找回调机会，用小周期精确入场**。三层筛选层层过滤，只有当三层信号一致时才入场。

```
┌─────────────────────────────────────────────────────────────┐
│  Screen 1 (大周期)  趋势方向过滤                              │
│  ├─ MACD 柱状图斜率 (主信号)                                  │
│  └─ EMA(13) 斜率 (确认)                                      │
│  → 决定只能做多 / 只能做空 / 不交易                            │
├─────────────────────────────────────────────────────────────┤
│  Screen 2 (中周期)  振荡器回调信号                            │
│  ├─ Force Index EMA(2)                                       │
│  └─ 背离检测 (价格 vs FI)                                    │
│  → 顺趋势回调时入场：多头+FI<0=买入 / 空头+FI>0=卖出          │
├─────────────────────────────────────────────────────────────┤
│  Screen 3 (小周期)  精确入场                                  │
│  ├─ 买入止损 = 前一根K线最高价 + tick_size                    │
│  └─ 卖出止损 = 前一根K线最低价 - tick_size                    │
│  → 价格突破触发入场，设置初始止损                              │
├─────────────────────────────────────────────────────────────┤
│  持仓跟踪  退出规则 + 追踪止损                                │
│  ├─ 1. 止损命中 (intrabar 极值)                               │
│  ├─ 2. Screen 1 趋势反转                                      │
│  ├─ 3. 反向背离                                               │
│  └─ 4. 追踪止损 (N-bar low/high, 单向顺移)                    │
└─────────────────────────────────────────────────────────────┘
```

## 两种时间周期组合

### 1. 日内交易组合 (鸡蛋期货 jd)

| 层级 | 周期 | 用途 | 持仓时间 |
|------|------|------|----------|
| Screen 1 | 25min | 趋势方向 (潮汐) | - |
| Screen 2 | 5min | 回调信号 (波浪) | - |
| Screen 3 | 1min | 精确入场 (涟漪) | 分钟~小时 |

- 交易所: DCE (大连商品交易所)
- 合约: jd 主力 (持仓量最大)
- tick_size: 1 元/500千克
- 交易时段: 9:00-10:15 / 10:30-11:30 / 13:30-15:00 (无夜盘)

### 2. 持仓交易组合 (棉花期货 cf)

| 层级 | 周期 | 用途 | 持仓时间 |
|------|------|------|----------|
| Screen 1 | Weekly | 趋势方向 (潮汐) | - |
| Screen 2 | Daily | 回调信号 (波浪) | - |
| Screen 3 | 15min | 精确入场 (涟漪) | 天~周 |

- 交易所: CZCE (郑州商品交易所)
- 合约: CZCE.CF609 (棉花2609)
- tick_size: 5 元/吨
- 交易时段: 9:00-10:15 / 10:30-11:30 / 13:30-15:00 + 21:00-23:00 (有夜盘)

## 指标计算

所有指标函数定义在 `egg_futures_1min.py`，**与时间周期无关**，可应用于任意 OHLCV 数据。

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
```

### Screen 1 趋势判定逻辑
- MACD 柱状图斜率：取近 5 根，首尾差值 > 5%×平均绝对值 → 上升/下降
- EMA(13) 斜率：取近 10 根，首尾差值 > 0.05%×平均价格 → 上升/下降
- 趋势判定：
  - 柱上升 + EMA 上升/平 → **多头** (仅做多)
  - 柱下降 + EMA 下降/平 → **空头** (仅做空)
  - 其他组合 → **中性** (不交易)

### Screen 2 回调信号逻辑
- Screen 1 中性 → 立即返回 no_signal (门控)
- Screen 1 多头 + FI<0 → buy_signal (回调买入)
- Screen 1 多头 + FI<0 + 底背离 → divergence_buy
- Screen 1 空头 + FI>0 → sell_signal (反弹卖出)
- Screen 1 空头 + FI>0 + 顶背离 → divergence_sell
- 背离检测：使用 window=3 查找局部极值，比较最近两个峰/谷

### Screen 3 入场逻辑
- 做多：entry = prev_high + tick_size; stop = min(recent_lows[-N:])
- 做空：entry = prev_low - tick_size; stop = max(recent_highs[-N:])
- 当前收盘价 ≥ 买入止损 → triggered_long
- 当前收盘价 ≤ 卖出止损 → triggered_short
- N = `STOP_LOOKBACK` (默认 5)，用于初始止损和追踪止损

## 退出规则 (持仓跟踪)

持仓状态保存在 `Position` 类中（paper trading，不发真实订单）。

**退出优先级**（持仓时按顺序检查）：

| 优先级 | 规则 | 触发条件 | 平仓价 |
|--------|------|----------|--------|
| 1 | 止损命中 | 1min low ≤ current_stop (多头) / high ≥ current_stop (空头) | current_stop |
| 2 | Screen 1 反转 | 趋势不再支持持仓（含中性） | 当前收盘价 |
| 3 | 反向背离 | Screen 2 出现反向 divergence | 当前收盘价 |
| 4 | 追踪止损 | 仍持仓则更新 current_stop（只能顺向移动） | 不平仓 |

**追踪止损**：取最近 N 根**已完成** bar 的 low (多头) / high (空头)，单向顺移（多头只升不降，空头只降不升）。

## 文件说明

### 核心监控脚本

| 文件 | 模式 | 说明 |
|------|------|------|
| `egg_futures_1min.py` | 终端 | 鸡蛋期货三周期实时监控 + 三重滤网 + 持仓跟踪 |
| `egg_futures_chart.py` | 图形 | Matplotlib 五行子图：25min K线 / MACD / 5min K线 / FI / 1min K线 |

### 测试与回测脚本

| 文件 | 说明 |
|------|------|
| `test_bullish_signal.py` | 多头级联回测：S1多头 → S2 buy_signal → S3 triggered_long |
| `test_bearish_signal.py` | 空头级联回测：S1空头 → S2 sell_signal → S3 triggered_short |
| `test_exit_rules.py` | 退出规则回测：完整入场→退出生命周期，统计胜率/PnL |
| `backtest_cf609_wd15.py` | CF609 周/日/15min 级联回测 |

### 探针脚本

| 文件 | 说明 |
|------|------|
| `probe_cf609.py` | 探测 CF609 在 25min/5min/1min 的三重滤网信号 |
| `probe_cf609_wd15.py` | 探测 CF609 在 周/日/15min 的三重滤网信号 |

### 配置与辅助

| 文件 | 说明 |
|------|------|
| `config_loader.py` | 共享配置加载模块（从 config.ini 读取天勤账号） |
| `config.example.ini` | 配置文件模板 |
| `config.ini` | 真实配置（不提交，含账号密码） |
| `requirements.txt` | Python 依赖 |
| `start.bat` | Windows 双击启动 |

## 快速启动

```bash
# 1. 安装依赖 (需 Python 3.12+)
pip install tqsdk pandas numpy matplotlib

# 2. 配置账号
cp config.example.ini config.ini
# 编辑 config.ini，填入天勤量化账号和密码

# 3. 运行监控
python egg_futures_1min.py      # 终端模式
python egg_futures_chart.py     # 图形模式

# 4. 运行回测
python test_bullish_signal.py   # 多头级联回测
python test_bearish_signal.py   # 空头级联回测
python test_exit_rules.py       # 退出规则回测

# 5. CF609 分析
python probe_cf609.py           # 25min/5min/1min 信号
python probe_cf609_wd15.py      # 周/日/15min 信号
python backtest_cf609_wd15.py   # 周/日/15min 级联回测
```

Windows 用户可直接双击 `start.bat`。

## 回测结果摘要

### 鸡蛋期货 (DCE.jd2608) 25min/5min/1min

**数据范围**: 2,000 bars × 3 时间周期 (25min: 9个月, 5min: 2个月, 1min: 9天)

#### 级联统计 (1,950 根 25min bar)

| 指标 | 多头 | 空头 |
|------|------|------|
| Screen 1 趋势 | 523 (26.8%) | 467 (23.9%) |
| Screen 1 中性 | 960 (49.2%) | - |
| Screen 2 信号 | 217 (208 buy + 9 div) | 156 (147 sell + 9 div) |
| Screen 3 触发 | 8 long | 33 short |
| 不变量违反 | 0 | 0 |

#### 退出规则回测 (STOP_LOOKBACK=5, 34 笔交易)

| 指标 | 值 |
|------|-----|
| 总交易数 | 34 |
| 退出原因 | 31 止损命中 / 3 趋势反转 |
| 胜率 | 26.5% (9/34) |
| 平均盈利 | +12.3 点 |
| 平均亏损 | -5.4 点 |
| 风险回报比 | 2.28 |
| 总 PnL | -24 点 |
| 平均持仓 | 7.2 bars |

### 棉花期货 (CZCE.CF609) 周/日/15min

**数据范围**: 1,000 bars × 3 时间周期 (周: ~18年, 日: 4年, 15min: 2个月)

#### 级联统计 (950 周)

| 指标 | 值 |
|------|-----|
| Screen 1 多头 | 27 (2.8%) |
| Screen 1 空头 | 2 (0.2%) |
| Screen 1 中性 | 921 (96.9%) |
| Screen 2 buy_signal | 52 + 5 背离 |
| Screen 2 sell_signal | 2 + 0 背离 |
| Screen 3 triggered_long | 26 |
| Screen 3 triggered_short | 0 (15min数据窗口未覆盖空头周) |
| 不变量违反 | 0 |

**注**: 棉花 18 年历史中 96.9% 的周为中性，符合持仓交易"大部分时间等待"的特征。

## 系统设计特点

### 1. 无状态信号计算
所有 `determine_screenN_*` 函数纯函数式，每次调用从当前数据计算，不依赖跨调用状态。便于回测和并行化。

### 2. 级联门控
Screen 2 在 Screen 1 为中性时立即返回 no_signal，Screen 3 在 Screens 1+2 不一致时返回 none。层层过滤，绝不违反方向纪律。

### 3. Paper Trading 持仓跟踪
`Position` 类记录入场价、初始止损、追踪止损、峰值利润、退出原因。不发真实订单，仅用于监控和回测。

### 4. 追踪止损单向顺移
多头止损只升不降，空头止损只降不升。防止止损在错误方向爬行。`STOP_LOOKBACK` 可调（默认 5）。

### 5. 多合约多周期支持
指标函数与时间周期无关，同一套代码支持鸡蛋 25min/5min/1min 和棉花 周/日/15min。仅需调整 `tick_size` 和 `KLINE_DURS`。

## 环境要求

- Python 3.12+ (tqsdk 3.9+)
- 天勤量化账号（免费版可用，支持 DCE/CZCE/SHFE/CFFEX 等交易所）
- 依赖: tqsdk, pandas, numpy, matplotlib

## 理论依据

Alexander Elder, *Trading for a Living* (1993):
- 三重滤网系统：用不同时间周期替代单一指标的多次确认
- Force Index：价格变动 × 成交量，反映市场"力度"
- 追踪止损：跟随趋势移动止损，锁定利润

> 本系统仅用于学习和监控，不构成投资建议。期货交易风险巨大，请谨慎操作。
