"""系统健康检查 + 自修复 — 检测各组件状态，失败自动降级

设计：
  1. 每个数据源/组件有状态探测函数
  2. 探测失败→记录日志→降级到备用方案
  3. 提供 self-diagnosis 命令报告系统健康度
  4. 关键操作有超时保护，不阻塞整体流程

降级链：
  东财限流 → 跳过东财数据，使用腾讯/mootdx
  LLM失效 → 跳过分析师，纯定量评分
  iwencai失效 → 跳过全市场扩展，只用预设池
  THS热点失效 → 跳过热点扫描，继续其他扫描器
"""
import time, sys, json
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import get_config


class HealthChecker:
    """系统健康检查"""

    def __init__(self):
        self.status = {
            "data_sources": {},
            "llm": {"ok": False, "reason": ""},
            "scanners": {},
            "last_scan_time": None,
            "overall": "UNKNOWN",
        }

    def check_all(self) -> dict:
        """全面检查所有组件"""
        self._check_tencent()
        self._check_mootdx()
        self._check_eastmoney()
        self._check_llm()
        self._check_yahoo()
        self._check_ths()

        # 综合判定
        critical = ["tencent", "mootdx"]
        critical_ok = all(self.status["data_sources"].get(c, {}).get("ok", False) for c in critical)
        self.status["overall"] = "HEALTHY" if critical_ok else "DEGRADED"

        return self.status

    def _check_tencent(self):
        """腾讯行情（核心，不封IP）"""
        try:
            import requests
            r = requests.get(
                "https://qt.gtimg.cn/q=sh600519",
                timeout=8
            )
            ok = r.status_code == 200 and "贵州茅台" in r.text
            self.status["data_sources"]["tencent"] = {
                "ok": ok, "latency_ms": int(r.elapsed.total_seconds() * 1000),
            }
        except Exception as e:
            self.status["data_sources"]["tencent"] = {"ok": False, "reason": str(e)[:60]}

    def _check_mootdx(self):
        """mootdx TCP（核心，不封IP）"""
        try:
            from mootdx.quotes import Quotes
            client = Quotes.factory(market='std')
            bars = client.bars(symbol='600519', category=4, offset=5)
            ok = bars is not None and len(bars) > 0
            self.status["data_sources"]["mootdx"] = {"ok": ok}
        except Exception as e:
            self.status["data_sources"]["mootdx"] = {"ok": False, "reason": str(e)[:60]}

    def _check_eastmoney(self):
        """东财（有风控，可能被封）"""
        try:
            from data.market_data import em_get
            r = em_get("https://push2.eastmoney.com/api/qt/stock/get",
                       params={"secid": "1.600519", "fields": "f58"})
            ok = r.status_code == 200
            self.status["data_sources"]["eastmoney"] = {"ok": ok, "status_code": r.status_code}
        except Exception as e:
            self.status["data_sources"]["eastmoney"] = {"ok": False, "reason": str(e)[:60]}

    def _check_llm(self):
        """LLM API"""
        cfg = get_config().get("llm", {})
        key = cfg.get("api_key", "")
        if not key:
            self.status["llm"] = {"ok": False, "reason": "API Key 未配置"}
            return
        try:
            import requests
            r = requests.post(
                f"{cfg.get('api_base','https://api.deepseek.com/v1').rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": cfg.get("model", "deepseek-chat"),
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 5},
                timeout=15,
            )
            ok = r.status_code == 200
            self.status["llm"] = {"ok": ok, "status_code": r.status_code}
            if r.status_code == 401:
                self.status["llm"]["reason"] = "API Key 无效或已过期"
        except Exception as e:
            self.status["llm"] = {"ok": False, "reason": str(e)[:60]}

    def _check_yahoo(self):
        """美股数据"""
        try:
            from data.us_market import fetch_us_market_close
            result = fetch_us_market_close()
            ok = result.get("sp500_change") is not None
            self.status["data_sources"]["yahoo"] = {"ok": ok}
        except Exception:
            self.status["data_sources"]["yahoo"] = {"ok": False, "reason": "连接失败"}

    def _check_ths(self):
        """同花顺热点（短线核心数据源）"""
        try:
            from datetime import datetime
            import requests
            today = datetime.now().strftime("%Y-%m-%d")
            url = "http://zx.10jqka.com.cn/event/api/getharden/date/{}/orderby/date/orderway/desc/charset/GBK/"
            r = requests.get(url.format(today), headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = r.json().get("data") or []
            ok = len(items) >= 10
            self.status["data_sources"]["ths"] = {
                "ok": ok, "count": len(items),
                "note": "短线热点数据源" if ok else "数据异常",
            }
        except Exception as e:
            self.status["data_sources"]["ths"] = {"ok": False, "reason": str(e)[:60]}


# ═══════════════════════════════════════════════════
# 全局降级控制
# ═══════════════════════════════════════════════════

class DegradationController:
    """系统降级控制器 — 当组件失败时自动切换到备选方案

    用法:
        dc = DegradationController()
        if dc.is_available('eastmoney'):
            data = use_eastmoney()
        else:
            data = use_fallback()  # 自动降级
    """

    def __init__(self):
        self._status = {}
        self._last_check = 0

    def refresh(self):
        """每 5 分钟检查一次状态"""
        now = time.time()
        if now - self._last_check < 300:
            return
        self._last_check = now
        hc = HealthChecker()
        self._status = hc.check_all()

    def is_available(self, component: str) -> bool:
        """检查组件是否可用"""
        self.refresh()
        if component == "tencent":
            return self._status.get("data_sources", {}).get("tencent", {}).get("ok", False)
        elif component == "mootdx":
            return self._status.get("data_sources", {}).get("mootdx", {}).get("ok", False)
        elif component == "eastmoney":
            return self._status.get("data_sources", {}).get("eastmoney", {}).get("ok", False)
        elif component == "llm":
            return self._status.get("llm", {}).get("ok", False)
        elif component == "yahoo":
            return self._status.get("data_sources", {}).get("yahoo", {}).get("ok", False)
        return True  # 未知组件默认可用

    def get_status(self) -> dict:
        """获取完整状态"""
        self.refresh()
        return self._status


_DEGRADATION = DegradationController()


def get_degradation() -> DegradationController:
    """获取降级控制器单例"""
    return _DEGRADATION
