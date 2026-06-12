#!/bin/bash
# A股策略定时任务 wrapper — 示例文件（请复制为 .stock_cron.sh 并修改）
# 用法: .stock_cron.sh [morning|lunch|close|night]
#
# 配置说明：
# 1. 复制本文件为 .stock_cron.sh
# 2. 在 ~/.zshrc 中添加环境变量：
#      export SERVERCHAN_KEY="你的SendKey"
#      export DINGTALK_WEBHOOK="你的钉钉Webhook"
# 3. 修改下面的 cd 路径为本机实际路径
# 4. 修改 python3 路径为本机实际路径

MODE=${1:-close}
export SERVERCHAN_KEY="${SERVERCHAN_KEY}"
export DINGTALK_WEBHOOK="${DINGTALK_WEBHOOK}"
export NOTIFY_LEVEL="${NOTIFY_LEVEL:-all}"

cd /path/to/your/project
/usr/bin/python3 stock_strategy_v3.py --wechat --quiet --mode $MODE >> /tmp/stock_strategy_${MODE}.log 2>&1
