# A股回调低吸策略 v3.0

基于三层精选信号的回调低吸策略，附带市场温度计、板块评分、涨停潜力榜、埋伏信号榜、短线信号榜。

## 功能

- **三层精选信号** 🥉🥈💎：普通层55% / 增强层69% / 精选层90% 胜率（基于3年回测）
- **市场温度计** 🌡️：涨停数/上涨占比/北向资金/两融趋势 → 自动控制仓位上限
- **板块评分** 🏆：同花顺热点tags + 政策催化检测 → 找出当前最强赛道
- **涨停潜力榜** 🔥：量比+ATR+板块动量 → 大涨概率估算
- **埋伏信号榜** 🚀：横盘充分+均线粘合+放量异动 → 突破候选
- **短线信号榜** ⚡：放量突破MA20+MACD金叉 → 短线参考
- **三灯验证阀** 🛡️：板块共振/资金入场/趋势向上 → 信号可信度
- **双通道推送** 📱：Server酱(微信) + 钉钉机器人
- **定时推送** ⏰：交易日 8:45 / 12:30 / 15:30 / 21:00

## 数据源

- **mootdx**（通达信TCP协议）— K线 / 行情数据（免费，不封IP）
- **同花顺热点API** — 涨停数 / 题材归因 / 强势股（免费）
- **腾讯财经API** — 实时行情 / PE / PB / 市值（免费）
- **Yahoo Finance** — 美股NVDA/FCX行情（免费）

## 依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10+，Node.js 16+（iwencai动态池需要）。

## 配置

### 1. 推送密钥（二选一）

```bash
# Server酱（推送到个人微信）
export SERVERCHAN_KEY="你的SendKey"

# 钉钉机器人（推送到钉钉群）
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx"
```

### 2. 定时任务

```bash
crontab -e
# 添加以下四行：
45 8 * * 1-5 /path/to/.stock_cron.sh morning
30 12 * * 1-5 /path/to/.stock_cron.sh lunch
30 15 * * 1-5 /path/to/.stock_cron.sh close
0 21 * * 1-5 /path/to/.stock_cron.sh night
```

> ⚠️ `.stock_cron.sh` 含本地路径，请勿上传GitHub（已在.gitignore中排除）

## 使用

```bash
# 全量扫描
python3 stock_strategy_v3.py

# 指定股票
python3 stock_strategy_v3.py --codes 600519,603260

# 只看AI赛道
python3 stock_strategy_v3.py --sector AI

# 全量扫描+推送
python3 stock_strategy_v3.py --wechat
python3 stock_strategy_v3.py --wechat --mode morning

# 调试模式
python3 stock_strategy_v3.py --debug
```

## 免责声明

本系统仅供学习研究参考，所有信号均基于历史回测，不构成投资建议。实盘操作风险自担，请务必严格执行止损纪律。
