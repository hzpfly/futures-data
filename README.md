# futures-data

基于天勤量化 (TqSdk) 的期货行情实时监控系统。

## 功能

- 自动识别鸡蛋主力合约（DCE 大商所）
- 1分钟 K 线实时采集
- 终端文字模式 + Matplotlib 图形 K 线模式
- 均线叠加 (MA5/MA10/MA20) + 成交量柱状图

## 快速启动

```bash
pip install tqsdk pandas matplotlib

# 终端模式
python egg_futures_1min.py

# 图形K线模式
python egg_futures_chart.py
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `egg_futures_1min.py` | 终端文字模式，1分钟K线实时打印 |
| `egg_futures_chart.py` | Matplotlib 动态蜡烛图 + 均线 |
| `requirements.txt` | Python 依赖 |
| `start.bat` | Windows 双击启动脚本 |

## 环境要求

- Python 3.12+（tqsdk 3.9+）
- 天勤量化账号（免费版可用）
