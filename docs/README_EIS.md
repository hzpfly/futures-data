# Elder Impulse System (EIS)

The **Elder Impulse System (EIS)** is a technical analysis tool created by Dr. Alexander Elder and introduced in his highly acclaimed book, *Come Into My Trading Room*.

Dr. Elder designed the system to identify **inflection points** where a trend is either accelerating or decelerating. The core philosophy of the system is highly professional: **"Enter cautiously, but exit fast."** It forces traders to only stay in a trade as long as the market momentum is strongly moving in their favor.

Here is a complete breakdown of how the system works, its components, and how to use it:

---

## The Core Components

The Impulse System is built by combining two different indicators to measure two distinct market forces: **Inertia (Trend)** and **Power (Momentum)**.

1. **13-day Exponential Moving Average (EMA):** This identifies the *trend*. If the slope of the 13-day EMA is rising, the trend is up; if it is falling, the trend is down.
2. **MACD-Histogram (Moving Average Convergence Divergence):** This measures *momentum*. If the slope of the histogram is rising (each bar is higher than the previous one), bullish momentum is increasing. If it is falling, bearish momentum is taking over.

---

## The Color-Coded Signals

The genius of the Elder Impulse System is that it synthesizes these two indicators and automatically **color-codes the price bars (or candlesticks)** on your chart, making it visually instantaneous to read:

| Bar Color | Technical Condition | Market Meaning | Trading Action Permitted |
| --- | --- | --- | --- |
| **Green** | 13-day EMA is **Rising** AND MACD-Histogram is **Rising** | Bulls dominate both trend and momentum. Market is impulsively surging up. | **Only Long (Buy)** positions are allowed. Shorting is forbidden. |
| **Red** | 13-day EMA is **Falling** AND MACD-Histogram is **Falling** | Bears dominate both trend and momentum. Market is impulsively dropping. | **Only Short (Sell)** positions are allowed. Buying is forbidden. |
| **Blue** | The 13-day EMA and MACD-Histogram are **moving in opposite directions** | Mixed signals. Trend and momentum are in conflict (e.g., price is rising but momentum is fading). | **Neutral.** No restrictions, but usually signals a time to wait or a warning to tighten stops. |

---

## Dr. Elder's Trading Rules

To trade successfully with the Impulse System, Dr. Elder insists on combining it with a **Multi-Timeframe Analysis**, specifically using a **Factor of 5** rule.

### 1. Identify the Long-Term Trend First

You must look at a timeframe that is roughly five times larger than your trading timeframe (called the *Intermediate timeframe*).

* *Example:* If you trade on **Daily** charts, your long-term timeframe is the **Weekly** chart. If you trade on **10-minute** charts, your long-term timeframe is the **60-minute** chart.
* **The Rule:** You are *only* allowed to take daily buy signals if the weekly trend is clearly bullish.

### 2. Market Entry (Buy / Short)

* **Go Long:** Enter when the long-term trend is bullish, and your trading chart transitions from a blue bar to a **Green bar**.
* **Go Short:** Enter when the long-term trend is bearish, and your trading chart transitions from a blue bar to a **Red bar**.

### 3. Market Exit (The "Fast Exit" Rule)

Because the system is designed to catch short, powerful "impulses" rather than riding a years-long trend, you must exit the moment that impulse dies down.

* **If you are Long:** You do not wait for the chart to turn Red to exit. **You exit the moment the Green bar turns back to Blue.** This protects your profits before a reversal occurs.

---

## Pros and Cons of the System

**The Advantages:**

* **Prevents Over-trading:** It strictly forbids you from buying during down-legs or shorting during up-legs.
* **Visual Clarity:** You don't have to look at multiple messy sub-windows; the colors on the candles tell you everything.
* **Excellent Filter:** It keeps traders out of choppy, sideways consolidation markets (which display mostly Blue bars).

**The Limitations:**

* **Chasing the Market:** Because it requires confirmation from both indicators, you will never buy at the exact bottom or sell at the exact top.
* **False Signals in Ranging Markets:** If a market has absolutely no trend, the color changes can whip-saw back and forth, leading to minor losses if stop-losses are too tight.

---

## Integration in This Project

EIS is used as a **cross-verification system** alongside the Triple Screen trading system. The two systems are independent — Triple Screen identifies entry/exit timing, while EIS confirms market momentum direction.

### Per-Set Independent Verification

Each Triple Screen set has its own EIS period configuration:

| Triple Screen Set | EIS Periods | Purpose |
|-------------------|-------------|---------|
| **A_长线** (Weekly→Daily→Hourly) | Weekly / Daily / Hourly | Macro trend confirmation |
| **B_短线** (Hourly→15min→3min) | Hourly / 15min / 3min | Micro momentum confirmation |

Set A and Set B are evaluated **completely independently** — their scores are never merged.

### Scoring Formula

```
Total Score = EIS Score × 0.5  +  Triple Screen Score × 0.5
```

**EIS Score**: Each period gives +1 (GREEN), -1 (RED), or 0 (BLUE). Sum across all periods for the set.

**Triple Screen Score**: S1 trend (±1) + S2 pullback signal (±1) + S3 entry signal (±2). Range: [-4, +4].

### Verdict Levels

| Total Score | Verdict | Action |
|-------------|---------|--------|
| >= 2.0 | Strongly Confident | Full position |
| >= 1.0 | Confident | Normal position |
| >= 0.3 | Cautious | Half position |
| >= -0.3 | Wait | No trade (EIS/TS conflict) |
| >= -1.0 | Cautious Short | |
| >= -2.0 | Short Confident | |
| < -2.0 | Strongly Short Confident | |

### Risk Warnings

The system automatically detects and warns about:

1. **Multi-period EIS conflict** — GREEN and RED appear simultaneously across periods → trend is not unified
2. **Too many BLUE periods** — 2+ BLUE periods → trend is ambiguous, avoid heavy positions
3. **Fatal EIS/TS direction conflict** — Triple Screen says long but EIS unanimously says short (or vice versa) → signal is unreliable, do not enter

### Related Files

| File | Role |
|------|------|
| `weekly_eis.py` | `determine_eis_color()` — core EIS calculation function |
| `triple_screen_monitor.py` | `compute_eis_cross_verify()` — integrates EIS into the monitor |
| `eis_monitor.py` | Standalone EIS dual-period monitor (25min + daily) |
| `scripts/cross_verify_jd.py` | One-shot cross-verification script with Set A/B independent verdicts |
