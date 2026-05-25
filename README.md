# futures-data

基于天勤量化 (TqSdk) 的期货行情实时监控系统。

## 功能

- 自动识别鸡蛋主力合约（DCE 大商所）
- 1分钟 K 线实时采集
- 终端文字模式 + Matplotlib 图形 K 线模式
- 均线叠加 (MA5/MA10/MA20) + 成交量柱状图

## 快速启动

```bash
# 1. 安装依赖
pip install tqsdk pandas matplotlib

# 2. 配置账号（重要！）
cp config.example.ini config.ini
# 编辑 config.ini，填入你的天勤量化账号和密码

# 3. 运行
python egg_futures_1min.py      # 终端模式
python egg_futures_chart.py     # 图形K线模式
```

> ⚠️ `config.ini` 包含账号密码，已加入 `.gitignore`，不会被提交到 Git。

## 文件说明

| 文件 | 说明 |
|------|------|
| `egg_futures_1min.py` | 终端文字模式，1分钟K线实时打印 |
| `egg_futures_chart.py` | Matplotlib 动态蜡烛图 + 均线 |
| `config_loader.py` | 共享配置加载模块 |
| `config.example.ini` | 配置文件模板（可提交） |
| `config.ini` | 真实配置文件（不提交，含账号密码） |
| `requirements.txt` | Python 依赖 |
| `start.bat` | Windows 双击启动脚本 |

## 环境要求

- Python 3.12+（tqsdk 3.9+）
- 天勤量化账号（免费版可用）
