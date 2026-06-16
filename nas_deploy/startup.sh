#!/bin/sh
# A股策略 · 容器启动脚本
# 自动配置mootdx + 启动定时任务

echo "=== 从 GitHub 拉取最新代码 ==="
python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/chow931124-png/stock-strategy/main/nas_deploy/stock_strategy_v3.py', '/app/stock_strategy_v3.py')" 2>/dev/null && echo "✅ 代码已更新" || echo "⚠️ 拉取失败，使用本地缓存"

echo "=== 启动依赖检查 ==="
python3 -c "from mootdx.quotes import Quotes" 2>/dev/null || pip install --no-cache-dir mootdx requests pandas numpy fake_useragent

echo "=== 配置mootdx ==="
if [ ! -f /root/.mootdx/config.json ]; then
  mkdir -p /root/.mootdx
  python3 -c "
import json
cfg = {
  'SERVER': {'HQ': [['深圳','110.41.147.114',7709],['上海','124.70.176.52',7709]]},
  'BESTIP': {'HQ': ['110.41.147.114', 7709], 'EX': '', 'GP': ''}
}
json.dump(cfg, open('/root/.mootdx/config.json','w'), indent=2)
"
fi

echo "=== 启动定时任务 ==="
nohup /app/cron_runner.sh > /tmp/cron.log 2>&1 &
echo "定时器已启动(PID: $!)"

# 保持容器运行
tail -f /dev/null
