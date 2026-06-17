# futures-data

基于天勤量化 (TqSdk) 的鸡蛋期货行情实时监控系统。

## 功能

- 自动识别 DCE 鸡蛋主力合约（按持仓量最大）
- 三周期 K 线：1 分钟 / 5 分钟 / 25 分钟
- MACD 指标（12, 26, 9）叠加在 25 分钟 K 线上，含金叉/死叉检测
- 均线叠加：MA5 / MA10 / MA20
- 终端文字模式：逐分钟打印，适合程序化盯盘
- 图形模式：Matplotlib 四行子图（蜡烛图 + MACD + 多周期联动）
- 盘后自动休眠，交易时段到达自动恢复

## 快速启动

```bash
# 1. 安装依赖
pip install tqsdk pandas matplotlib

# 2. 配置账号
cp config.example.ini config.ini
# 编辑 config.ini，填入天勤量化账号和密码

# 3. 运行
python egg_futures_1min.py      # 终端模式（三周期 + MACD 文字输出）
python egg_futures_chart.py     # 图形模式（四行蜡烛图 + MACD 子图）
```

Windows 用户可直接双击 `start.bat`。

> 注意：需要 Python 3.12+，且 tqsdk 安装在对应版本下。

## 文件说明

| 文件 | 说明 |
|------|------|
| `egg_futures_1min.py` | 终端版：三周期 K 线 + 25 分钟 MACD 实时打印 |
| `egg_futures_chart.py` | 图形版：4 行子图（25min 蜡烛图 / MACD / 5min / 1min） |
| `config_loader.py` | 共享配置加载模块 |
| `config.example.ini` | 配置文件模板（可提交） |
| `config.ini` | 真实配置文件（不提交，含账号密码） |
| `requirements.txt` | Python 依赖 |
| `start.bat` | Windows 双击启动 |

## 环境要求

- Python 3.12+（tqsdk 3.9+）
- 天勤量化账号（免费版可用）
- 鸡蛋期货仅白天盘：9:00-10:15 / 10:30-11:30 / 13:30-15:00
