#!/bin/sh
# A股策略定时任务启动脚本
# 放在 nas_deploy/ 目录，挂载到容器内 /app/startup.sh
# Docker 命令: sh /app/startup.sh

# 每天第一次自动装依赖
pip install --no-cache-dir mootdx requests pandas numpy fake_useragent 2>/dev/null

# 自动配置mootdx（容器重启后/root/.mootdx/会丢失）
mkdir -p /root/.mootdx
cat > /root/.mootdx/config.json << 'MCFG'
{"SERVER":{"HQ":[["深圳","110.41.147.114",7709],["上海","124.70.176.52",7709]]},"BESTIP":{"HQ":["110.41.147.114",7709],"EX":"","GP":""}}
MCFG

# 清理K线缓存（每天自动换新的）
rm -f /app/cache/klines_strategy/*.pkl 2>/dev/null

while true; do
  H=$(date +%H:%M)
  case $H in
    08:45|08:46|08:47|08:48)
      python3 /app/stock_strategy_v3.py --wechat --quiet --mode morning ;;
    12:30|12:31|12:32)
      python3 /app/stock_strategy_v3.py --wechat --quiet --mode lunch ;;
    15:30|15:31|15:32)
      python3 /app/stock_strategy_v3.py --wechat --quiet --mode close ;;
    21:00|21:01|21:02)
      python3 /app/stock_strategy_v3.py --wechat --quiet --mode night ;;
  esac
  sleep 60
done
