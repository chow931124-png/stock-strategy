#!/usr/bin/env python3
"""
A股回调低吸策略 v3.0 —— 市场温度计 + 板块评分 + 三层精选
===========================================================
基于 v2.0 的三层个股信号，新增两个前置维度：
  ① 市场温度计（决定是否开仓）
  ② 板块评分（决定哪些板块优先）

使用方式:
  python3 stock_strategy_v3.py                          # 全量扫描+新评分
  python3 stock_strategy_v3.py --wechat                 # +微信推送
  python3 stock_strategy_v3.py --sector AI             # 只看AI赛道
  python3 stock_strategy_v3.py --debug                 # 输出详细评分数据

数据源: mootdx(通达信) + 同花顺热点 + 东财行业板块 + 东财龙虎榜 + 腾讯财经
"""

import os, sys, time, random, json, subprocess, site
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

import numpy as np
import pandas as pd
import requests
from mootdx.quotes import Quotes
from fake_useragent import UserAgent

# ════════════════════════════════════════════════════════
# 微信通知配置
# ════════════════════════════════════════════════════════
NOTIFY_CONFIG = {
    "wechat_webhook": os.environ.get("WECHAT_WEBHOOK", ""),
    "serverchan_key": os.environ.get("SERVERCHAN_KEY", ""),
    "dingtalk_webhook": os.environ.get("DINGTALK_WEBHOOK", ""),
    "notify_level": os.environ.get("NOTIFY_LEVEL", "all"),
    "push_window": (9, 22),
}


# ════════════════════════════════════════════════════════
# 策略参数
# ════════════════════════════════════════════════════════
STRATEGY = {
    "base": {"drawdown_min": 5, "drawdown_max": 35, "vol_ratio_min": 1.3, "ma_period": "ma5", "lookback_peak": 60},
    "enhanced": {"atr_min": 5.0, "atr_max": 8.0},
    "elite": {"bias_ma20_max": -3, "atr_min": 5.0, "atr_max": 8.0},
    "ambush": {
        "amplitude_min": 10, "amplitude_max": 30,
        "ma_spread_max": 15,
        "vol_ratio_min": 1.3,
        "consolidation_days": 20,
    },
}

# 市场温度计权重
MARKET_TEMP_WEIGHTS = {
    "涨停跌停比": 0.30,
    "上涨占比": 0.25,
    "北向资金": 0.25,
    "两融趋势": 0.20,
}

# 板块评分权重
SECTOR_SCORE_WEIGHTS = {
    "板块资金流向": 0.30,
    "板块涨停占比": 0.25,
    "北向板块偏好": 0.20,
    "政策催化强度": 0.15,
    "近期热度趋势": 0.10,
}

# ════════════════════════════════════════════════════════
# 股票池（82只主板，同v2.0）
# ════════════════════════════════════════════════════════
# ============================================================
# 动态股票池（自动生成 + 手动补充混合）
# 自动部分: iwencai查询主板非ST市值>30亿
# 手动部分: 45只精选核心票（作为兜底）
# 扫描时还会经过 ST/退市/流动性 二次过滤
# ============================================================
BUILTIN_STOCKS = [
    # ===== 核心精选池（45只，经3年回测验证）=====
    # AI算力通信
    ("002463", "沪电股份", "AI算力通信"), ("000063", "中兴通讯", "AI算力通信"),
    ("002415", "海康威视", "AI算力通信"), ("600498", "烽火通信", "AI算力通信"),
    ("000636", "风华高科", "AI算力通信"), ("002254", "泰和新材", "AI算力通信"),
    ("603315", "福鞍股份", "AI算力通信"), ("600186", "莲花控股", "AI算力通信"),
    ("002230", "科大讯飞", "AI算力通信"), ("600353", "旭光电子", "AI算力通信"),
    # 科技半导体
    ("002371", "北方华创", "科技半导体"), ("603501", "韦尔股份", "科技半导体"),
    ("002409", "雅克科技", "科技半导体"), ("002475", "立讯精密", "科技半导体"),
    ("002119", "康强电子", "科技半导体"), ("600060", "海信视像", "科技半导体"),
    # 新能源制造
    ("002594", "比亚迪", "新能源制造"), ("002050", "三花智控", "新能源制造"),
    ("002850", "科达利", "新能源制造"), ("600110", "诺德股份", "新能源制造"),
    ("002074", "国轩高科", "新能源制造"), ("000049", "德赛电池", "新能源制造"),
    ("600478", "科力远", "新能源制造"),
    # 化工
    ("600309", "万华化学", "化工"), ("600989", "宝丰能源", "化工"),
    ("000830", "鲁西化工", "化工"), ("600096", "云天化", "化工"),
    ("603688", "石英股份", "化工"), ("603260", "合盛硅业", "化工"),
    ("000792", "盐湖股份", "化工"), ("000408", "藏格矿业", "化工"),
    # 有色金属
    ("601899", "紫金矿业", "有色金属"), ("601600", "中国铝业", "有色金属"),
    ("603993", "洛阳钼业", "有色金属"), ("000630", "铜陵有色", "有色金属"),
    ("601168", "西部矿业", "有色金属"), ("600497", "驰宏锌锗", "有色金属"),
    ("000603", "盛达资源", "有色金属"), ("002155", "湖南黄金", "有色金属"),
    ("600711", "盛屯矿业", "有色金属"), ("601069", "西部黄金", "有色金属"),
    ("000960", "锡业股份", "有色金属"), ("600531", "豫光金铅", "有色金属"),
    ("000426", "兴业银锡", "有色金属"), ("002428", "云南锗业", "有色金属"),
    # 航天军工
    ("600760", "中航沈飞", "航天军工"), ("600893", "航发动力", "航天军工"),
    ("002179", "中航光电", "航天军工"), ("600378", "昊华科技", "航天军工"),
    ("600118", "中国卫星", "航天军工"), ("600879", "航天电子", "航天军工"),
    ("000547", "航天发展", "航天军工"), ("600862", "中航高科", "航天军工"),
    # 创新药
    ("002422", "科伦药业", "创新药"), ("002317", "众生药业", "创新药"),
    ("603392", "万泰生物", "创新药"), ("605116", "奥锐特", "创新药"),
    ("002099", "海翔药业", "创新药"),
    # 周期金融
    ("600036", "招商银行", "周期金融"), ("601398", "工商银行", "周期金融"),
    ("601288", "农业银行", "周期金融"), ("601939", "建设银行", "周期金融"),
    ("601988", "中国银行", "周期金融"), ("601857", "中国石油", "周期金融"),
    ("601138", "工业富联", "周期金融"),
    # 大市值消费
    ("600519", "贵州茅台", "大市值消费"), ("000858", "五粮液", "大市值消费"),
    ("600941", "中国移动", "大市值消费"),
    # 医药生物
    ("603259", "药明康德", "医药生物"), ("603222", "济民健康", "医药生物"),
    ("600645", "中源协和", "医药生物"),
    # 商业航天
    ("002023", "海特高新", "商业航天"), ("600990", "四创电子", "商业航天"),
    ("002829", "星网宇达", "商业航天"),
    # 储能
    ("600438", "通威股份", "储能"), ("601012", "隆基绿能", "储能"),
    ("002459", "晶澳科技", "储能"),
    # 中小盘成长
    ("601137", "博威合金", "中小盘成长"), ("603678", "火炬电子", "中小盘成长"),
    ("000032", "深桑达A", "中小盘成长"), ("000688", "国城矿业", "中小盘成长"),
    ("002654", "万润科技", "中小盘成长"),
]

# 动态池缓存（每日自动刷新）
_dynamic_pool_cache = None
_dynamic_pool_date = None

def refresh_dynamic_pool() -> list:
    """尝试用iwencai动态扩展股票池"""
    global _dynamic_pool_cache, _dynamic_pool_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _dynamic_pool_cache is not None and _dynamic_pool_date == today:
        return _dynamic_pool_cache

    try:
        from fake_useragent import UserAgent
        import subprocess, site
        ua_gen = UserAgent()
        session = requests.Session()
        session.get("https://www.iwencai.com/", timeout=10)
        js_path = None
        for p in site.getsitepackages():
            c = os.path.join(p, "pywencai", "hexin-v.bundle.js")
            if os.path.exists(c): js_path = c; break
        if not js_path: return BUILTIN_STOCKS
        r = subprocess.run(["node", js_path], capture_output=True, timeout=10)
        token = r.stdout.decode().strip()
        headers = {"hexin-v": token, "User-Agent": ua_gen.random, "Content-Type": "application/json"}
        payload = {
            "add_info": '{"urp":{"scene":1,"company":1,"business":1},"contentType":"json","searchInfo":true}',
            "perpage": "200", "page": 1,
            "source": "Ths_iwencai_Xuangu", "version": "2.0", "secondary_intent": "stock",
            "question": "沪深主板 非ST 非科创板 非创业板 市值大于30亿 日均成交额大于3000万 2026",
        }
        r = session.post("http://www.iwencai.com/customized/chart/get-robot-data", json=payload, headers=headers, timeout=15)
        res = r.json()
        rows = []
        for ans in res.get("data",{}).get("answer",[]):
            txt = ans.get("txt") or []
            if isinstance(txt, str): continue
            for item in txt:
                if not isinstance(item, dict): continue
                for comp in item.get("content",{}).get("components",[]):
                    datas = comp.get("data",{}).get("datas",[]) or []
                    rows.extend(datas)
        if rows:
            extra = {}
            for s in rows:
                code = str(s.get("股票代码","") or "").replace(".SZ","").replace(".SH","").replace(".BJ","")
                name = s.get("股票简称","") or ""
                if not code or len(code) != 6: continue
                if code.startswith(('300','301','688')): continue
                if code not in extra:
                    extra[code] = (code, name, "动态池")
            # 合并核心池 + 动态池（动态池去重）
            core_codes = {c[0] for c in BUILTIN_STOCKS}
            merged = list(BUILTIN_STOCKS) + [v for k,v in extra.items() if k not in core_codes]
            print(f"  [动态池] iwencai返回{len(rows)}只, 新增{len(merged)-len(BUILTIN_STOCKS)}只, 总计{len(merged)}只")
            _dynamic_pool_cache = merged
            _dynamic_pool_date = today
            return merged
    except Exception as e:
        print(f"  [动态池] iwencai失败, 使用核心池: {e}")

    _dynamic_pool_cache = BUILTIN_STOCKS
    _dynamic_pool_date = today
    return BUILTIN_STOCKS

# 赛道关键词
SECTOR_KEYWORDS = {
    "AI": ["AI算力通信", "科技半导体"], "tech": ["科技半导体", "AI算力通信"],
    "化学": ["化工"], "金属": ["有色金属"], "航天": ["航天军工"],
    "军工": ["航天军工"], "储能": ["储能"], "医药": ["医药生物", "创新药"],
    "finance": ["周期金融"], "新能源": ["新能源制造", "储能"],
    "all": None,
}

SECTOR_PREFERENCE = {
    "prefer": ["AI算力通信", "科技半导体", "有色金属", "航天军工"],
    "neutral": ["化工", "新能源制造", "储能", "周期金融", "商业航天", "创新药"],
    "avoid": ["医药生物", "大市值消费", "中小盘成长"],
}

class MarketThermometer:
    """市场温度计 - 决定是否开仓、开多大仓位"""

    def __init__(self):
        self.temperature = 50    # 0-100
        self.signals = {}
        self.cache = {}

    def fetch_data(self):
        """获取温度计所需的所有数据"""
        results = {}
        ua = "Mozilla/5.0"

        # 1. 同花顺热点：强势股涨跌、涨停数
        try:
            # 尝试今天，如果数据异常就往前找最近的交易日
            today = datetime.now().strftime("%Y-%m-%d")
            rows = []
            for attempt in range(5):
                d = (datetime.now() - timedelta(days=attempt)).strftime("%Y-%m-%d")
                url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{d}/orderby/date/orderway/desc/charset/GBK/"
                r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
                data = r.json()
                rows = data.get("data") or []
                if len(rows) >= 10:  # 有足够数据
                    break
            if rows:
                # 数据有效性校验：如果所有涨跌幅都是0，说明接口返回了异常数据
                has_valid_data = any(float(row.get("zhangfu", 0) or 0) != 0 for row in rows)
                if not has_valid_data and len(rows) >= 10:
                    print(f"  ⚠️ [温度计] 同花顺返回{len(rows)}只但涨跌幅全部为0，数据异常，回退到中性值")
                    results["上涨占比"] = 0.5
                    results["涨停跌停比"] = 1.0
                else:
                    up = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) > 0)
                    down = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) < 0)
                    total = len(rows)
                    valid = up + down
                    up_ratio = up / valid if valid > 0 else 0.5
                    results["上涨占比"] = up_ratio
                    results["强势股数"] = total

                    limit_up = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) >= 9.5)
                    limit_down = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) <= -9.5)
                    results["涨停数(估)"] = limit_up
                    results["跌停数(估)"] = limit_down
                    results["涨停跌停比"] = min(limit_up / max(limit_down, 1), 10)
                    print(f"  [温度计] 强势股{total}只 涨{up}跌{down} 涨停{limit_up}跌停{limit_down}")
            else:
                results["上涨占比"] = 0.5
                results["涨停跌停比"] = 1.0
                print(f"  [温度计] 最近5天无强势股数据，使用中性值")

        except Exception as e:
            print(f"  [温度计] 同花顺数据失败: {e}")
            results["涨停跌停比"] = 1.0
            results["上涨占比"] = 0.5

        # 如果同花顺数据异常（全0或无数据），用股票池行情做备用
        need_fallback = (
            results.get("上涨占比", -1) in (0.5, -1)
            and results.get("涨停跌停比", -1) in (1.0, -1)
        )
        if need_fallback:
            pool_result = self._fetch_from_pool()
            if pool_result:
                print(f"  [温度计] 使用股票池行情备用数据（{pool_result['有效']}/{len(BUILTIN_STOCKS)}只）")
                results.update(pool_result)

        self.cache = results
        return results

    def _fetch_from_pool(self) -> dict:
        """用股票池实时行情作为温度计备用源"""
        try:
            up = down = limit_up = limit_down = valid = 0
            for code, name, sector in BUILTIN_STOCKS:
                prefix = "sh" if code.startswith(("6","9")) else "sz"
                url = f"https://qt.gtimg.cn/q={prefix}{code}"
                r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=3)
                vals = r.content.decode("gbk").split('"')[1].split("~")
                price = float(vals[3]) if vals[3] else 0
                if price <= 0:
                    continue
                valid += 1
                chg_pct = float(vals[32]) if vals[32] else 0
                if chg_pct > 0: up += 1
                elif chg_pct < 0: down += 1
                if chg_pct >= 9.5: limit_up += 1
                elif chg_pct <= -9.5: limit_down += 1
            if valid < 30:
                return None
            return {
                "上涨占比": up / max(up + down, 1),
                "涨停跌停比": min(limit_up / max(limit_down, 1), 10),
                "涨停数(估)": limit_up,
                "跌停数(估)": limit_down,
                "强势股数": valid,
                "有效": valid,
            }
        except Exception as e:
            print(f"  [温度计] 股票池备用源也失败: {e}")
            return None

    def calc_temperature(self) -> dict:
        """计算市场温度"""
        data = self.fetch_data()
        scores = {}

        # ① 涨停跌停比评分（0-100）
        zt_ratio = data.get("涨停跌停比", 1.0)
        if zt_ratio >= 3: scores["涨停跌停比"] = 80
        elif zt_ratio >= 2: scores["涨停跌停比"] = 70
        elif zt_ratio >= 1.5: scores["涨停跌停比"] = 65
        elif zt_ratio >= 1: scores["涨停跌停比"] = 55
        elif zt_ratio >= 0.5: scores["涨停跌停比"] = 40
        else: scores["涨停跌停比"] = 25

        # ② 上涨占比评分（0-100）
        up_ratio = data.get("上涨占比", 0.5)
        # 如果数据是0说明没获取到，用中性值
        if up_ratio == 0 and data.get("强势股数", 0) == 0:
            scores["上涨占比"] = 50
        else:
            scores["上涨占比"] = int(up_ratio * 100)

        # ③ 北向资金（同花顺hsgtApi实时数据）
        try:
            hsgt_url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
            hsgt_r = requests.get(hsgt_url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://data.hexin.cn/"}, timeout=8)
            hsgt_data = hsgt_r.json()
            hgt = hsgt_data.get("hgt", [])
            sgt = hsgt_data.get("sgt", [])
            if hgt and sgt:
                hgt_latest = hgt[-1] if hgt else 0
                sgt_latest = sgt[-1] if sgt else 0
                total = hgt_latest + sgt_latest
                if total > 50: scores["北向资金"] = 80
                elif total > 20: scores["北向资金"] = 70
                elif total > 10: scores["北向资金"] = 65
                elif total > 0: scores["北向资金"] = 60
                elif total > -10: scores["北向资金"] = 50
                elif total > -30: scores["北向资金"] = 35
                else: scores["北向资金"] = 20
                print(f"  [温度计] 北向: 沪{hgt_latest:.1f}亿 深{sgt_latest:.1f}亿 合计{total:.1f}亿 → {scores['北向资金']}分")
            else:
                scores["北向资金"] = 50
        except:
            scores["北向资金"] = 50

        # ④ 两融趋势（暂用模拟）
        scores["两融趋势"] = 50

        # 加权总分
        temp = sum(scores[k] * MARKET_TEMP_WEIGHTS.get(k, 0.25)
                   for k in scores) / sum(MARKET_TEMP_WEIGHTS.values())

        self.temperature = int(temp)
        self.signals = scores
        self.raw_data = data

        return {
            "temperature": self.temperature,
            "scores": scores,
            "data": data,
        }

    def get_market_state(self) -> str:
        """市场状态判定"""
        t = self.temperature
        if t >= 75: return "🔥 亢奋（注意风险）"
        elif t >= 60: return "✅ 正常（可操作）"
        elif t >= 45: return "🟡 偏冷（减半仓）"
        elif t >= 30: return "⚠️ 低迷（仅精选信号）"
        else: return "🔴 冰点（暂停策略）"

    def get_position_limit(self) -> float:
        """建议仓位上限 0-1"""
        t = self.temperature
        if t >= 75: return 0.5    # 亢奋→减仓防崩
        elif t >= 60: return 1.0  # 正常→满仓操作
        elif t >= 45: return 0.6  # 偏冷→减半
        elif t >= 30: return 0.3  # 低迷→轻仓
        else: return 0.0          # 冰点→空仓


# ════════════════════════════════════════════════════════
# 模块2：板块评分系统
# ════════════════════════════════════════════════════════
class SectorScorer:
    """板块评分 - 决定哪些板块值得关注"""

    def __init__(self):
        self.scores = {}
        self.raw = {}

    # 板块缓存（东财接口失败时使用）
    _cache_file = (Path("/app/cache") if Path("/app/cache").exists() else Path(__file__).parent / "cache") / "sector_cache.json"

    def fetch_sector_data(self):
        """获取板块数据（带缓存/同花顺fallback）"""
        ua = "Mozilla/5.0"
        board_data = {}

        # 先拉同花顺热点tags（不受代理限制）
        try:
            from collections import Counter
            today = datetime.now().strftime("%Y-%m-%d")
            url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{today}/orderby/date/orderway/desc/charset/GBK/"
            r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
            rows = r.json().get("data") or []
            # 如果rows不足，往前找
            if len(rows) < 10:
                for d in range(1, 5):
                    d2 = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
                    url2 = f"http://zx.10jqka.com.cn/event/api/getharden/date/{d2}/orderby/date/orderway/desc/charset/GBK/"
                    r2 = requests.get(url2, headers={"User-Agent": ua}, timeout=10)
                    rows2 = r2.json().get("data") or []
                    if len(rows2) >= 10: rows = rows2; break
            if rows:
                # 从reason tags推导板块热度
                reason_tags = []
                for row in rows:
                    reason = row.get("reason", "")
                    if reason: reason_tags.extend([t.strip() for t in reason.split("+") if t.strip()])
                tag_counts = Counter(reason_tags)
                # 映射到我们的13个赛道
                tag_to_sector = {
                    "AI算力":"AI算力通信", "光模块":"AI算力通信", "算力":"AI算力通信",
                    "光纤":"AI算力通信", "通信":"AI算力通信", "数据中心":"AI算力通信",
                    "芯片":"科技半导体", "半导体":"科技半导体", "先进封装":"科技半导体",
                    "存储芯片":"科技半导体", "HBM":"科技半导体",
                    "锂电池":"新能源制造", "新能源":"新能源制造", "电池":"新能源制造",
                    "新能源汽车":"新能源制造", "光伏":"新能源制造",
                    "化工":"化工", "化学":"化工", "化肥":"化工", "煤化工":"化工",
                    "有色金属":"有色金属", "黄金":"有色金属", "稀土":"有色金属",
                    "铜":"有色金属", "铝":"有色金属", "矿业":"有色金属",
                    "航天":"航天军工", "军工":"航天军工", "航空":"航天军工",
                    "医药":"医药生物", "创新药":"创新药", "医疗器械":"医药生物",
                    "银行":"周期金融", "证券":"周期金融", "金融":"周期金融",
                    "白酒":"大市值消费", "消费":"大市值消费", "食品":"大市值消费",
                    "商业航天":"商业航天", "卫星":"商业航天",
                    "储能":"储能", "电力":"储能", "电网":"储能",
                    "低空经济":"商业航天",
                }
                sector_hotness = {}
                for tag, cnt in tag_counts.items():
                    for kw, sec in tag_to_sector.items():
                        if kw in tag:
                            sector_hotness[sec] = sector_hotness.get(sec, 0) + cnt
                if sector_hotness:
                    max_hot = max(sector_hotness.values())
                    for sec in sector_hotness:
                        sector_hotness[sec] = int(50 + sector_hotness[sec] / max_hot * 40)
                    # 保存缓存
                    try:
                        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
                        self._cache_file.write_text(json.dumps(sector_hotness))
                    except: pass
                    return sector_hotness
        except: pass

        # 尝试缓存
        if self._cache_file.exists():
            try:
                return json.loads(self._cache_file.read_text())
            except: pass

        # 尝试东财接口
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": "100", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": "m:90+t:2",
                "fields": "f2,f3,f4,f12,f14,f104,f105,f136,f140",
            }
            r = requests.get(url, params=params, headers={"User-Agent": ua}, timeout=10)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            board_data = {}
            for item in items:
                name = item.get("f14", "")
                board_data[name] = {
                    "change_pct": item.get("f3", 0),
                    "up_count": item.get("f104", 0),
                    "down_count": item.get("f105", 0),
                    "leader": item.get("f140", ""),
                }
            return board_data
        except Exception as e:
            print(f"  [板块] 东财行业数据失败: {e}")
            return {}

    def map_sector_to_board(self, stock_sector: str) -> str:
        """将我们的赛道名映射到东财板块名"""
        mapping = {
            "AI算力通信": "计算机设备,通信设备,半导体,电子元件",
            "科技半导体": "半导体,芯片,电子元件",
            "新能源制造": "电池,汽车整车,新能源",
            "化工": "化学制品,化学原料,化肥",
            "有色金属": "有色金属,黄金,稀土",
            "航天军工": "航天航空,军工,船舶制造",
            "创新药": "化学制药,生物制品,医药",
            "周期金融": "银行,证券,保险",
            "大市值消费": "白酒,食品饮料,家电",
            "医药生物": "医疗器械,医疗服务",
            "商业航天": "航天航空,卫星",
            "储能": "电网设备,光伏设备,电池",
            "中小盘成长": None,
        }
        return mapping.get(stock_sector, "")

    def score_sectors(self, stock_sector_hotness: dict = None) -> dict:
        """给13个赛道打分（0-100）"""
        board_data = self.fetch_sector_data()
        # 如果是同花顺tags数据（值都>40且keys是赛道名），直接返回
        if board_data and any(k in board_data for k in ["AI算力通信","科技半导体","化工"]):
            return board_data
        scores = {}

        # 板块涨停数（从同花顺热点聚合）
        # 先用东财板块涨跌排行做基础评分
        for sector in BUILTIN_STOCKS:
            sec = sector[2]
            if sec in scores:
                continue

            board_names = self.map_sector_to_board(sec)
            if not board_names:
                scores[sec] = 50
                continue

            # 找匹配的东财板块
            matched = []
            for bn in board_names.split(","):
                for bname, bdata in board_data.items():
                    if bn in bname:
                        matched.append(bdata)
                        break

            if not matched:
                scores[sec] = 50
                continue

            # 综合评分
            avg_change = np.mean([m["change_pct"] for m in matched]) if matched else 0
            total_up = sum(m["up_count"] for m in matched)
            total_down = sum(m["down_count"] for m in matched)
            total_stocks = total_up + total_down if total_up + total_down > 0 else 1

            # 涨跌幅分（0-40）
            change_score = max(0, min(40, 40 + avg_change * 2))

            # 涨跌比分（0-30）
            ratio = total_up / max(total_down, 1)
            ratio_score = max(0, min(30, 15 + ratio * 5))

            # 活跃度分（0-30）
            active_score = min(30, total_stocks)

            total = change_score + ratio_score + active_score
            scores[sec] = int(total)

        # 缓存
        self.scores = scores
        self.raw = board_data

        return scores

    def get_top_sectors(self, n=5) -> list:
        """获取高分板块"""
        sorted_sec = sorted(self.scores.items(), key=lambda x: -x[1])
        return [(s, sc) for s, sc in sorted_sec if sc >= 50][:n]

    def get_bottom_sectors(self, n=3) -> list:
        """获取低分板块"""
        sorted_sec = sorted(self.scores.items(), key=lambda x: x[1])
        return [(s, sc) for s, sc in sorted_sec[:n]]


# ════════════════════════════════════════════════════════
# 模块3：个股数据引擎（复用v2.0逻辑）
# ════════════════════════════════════════════════════════

class DataEngine:
    _klines_cache_dir = None

    def __init__(self):
        self.client = None
        self._call_count = 0
        self._connect()
        self._init_cache_dir()

    @classmethod
    def _init_cache_dir(cls):
        if cls._klines_cache_dir is None:
            dir_path = CACHE_DIR / "klines_strategy"
            dir_path.mkdir(parents=True, exist_ok=True)
            cls._klines_cache_dir = dir_path

    def _cache_path(self, code: str):
        if self._klines_cache_dir is None:
            self._init_cache_dir()
        return self._klines_cache_dir / f"{code}.pkl"

    def _connect(self):
        """连接或重连通达信，最多重试2次"""
        for i in range(2):
            try:
                self.client = Quotes.factory(market='std')
                return True
            except:
                self.client = None
                time.sleep(0.5)
        return False

    def get_klines(self, code: str) -> Optional[pd.DataFrame]:
        """获取K线，磁盘缓存优先→mootdx→HTTP备用"""
        today = datetime.now().strftime("%Y-%m-%d")
        cache_file = self._cache_path(code)
        # 检查磁盘缓存：如果是今天生成的则直接加载
        if cache_file.exists():
            try:
                file_mtime = datetime.fromtimestamp(cache_file.stat().st_mtime).strftime('%Y-%m-%d')
                if file_mtime == today:
                    df = pd.read_pickle(cache_file)
                    if isinstance(df, pd.DataFrame) and len(df) > 100:
                        return df
            except:
                pass
        self._call_count += 1
        if self._call_count % 20 == 0:
            self._connect()
        for attempt in range(2):
            try:
                if self.client is None:
                    if not self._connect():
                        continue
                df = self.client.bars(symbol=code, category=4, offset=750)
                if df is None or df.empty:
                    continue
                return self._calc_indicators(df, code)
            except:
                if attempt == 1:
                    return self._get_klines_http(code)
                time.sleep(0.5)
        return self._get_klines_http(code)

    def _get_klines_http(self, code: str) -> Optional[pd.DataFrame]:
        """HTTP备用K线接口"""
        for source_url in [
            f"https://web.ifzgz.top/qt/stock/kline?code={'sh'if code.startswith(('6','9'))else'sz'}{code}&type=day&count=800",
        ]:
            try:
                r = requests.get(source_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
                d = r.json()
                rows = []
                if d.get("data"):
                    klines = d["data"].get("klines") or d["data"].get("items") or []
                    for row in klines:
                        parts = row.split(",") if isinstance(row, str) else row
                        if len(parts) >= 6:
                            rows.append({"date":parts[0],"open":float(parts[1]),"close":float(parts[2]),"high":float(parts[3]),"low":float(parts[4]),"vol":float(parts[5]),"amount":float(parts[6]) if len(parts)>6 else 0})
                if len(rows) >= 60:
                    df = pd.DataFrame(rows)
                    df['date'] = pd.to_datetime(df['date'])
                    return self._calc_indicators(df, code)
            except: pass
        return self._get_klines_baidu(code)

    def _get_klines_baidu(self, code: str) -> Optional[pd.DataFrame]:
        """百度股市通K线（备用备用）"""
        try:
            r = requests.get("https://finance.pae.baidu.com/selfselect/getstockquotation",
                params={"all":"1","isStock":"true","newFormat":"1","group":"quotation_kline_ab","code":code,"ktype":"1"},
                headers={"User-Agent":"Mozilla/5.0","Origin":"https://gushitong.baidu.com"}, timeout=15)
            d = r.json()
            md = (d.get("Result") or {}).get("newMarketData") or {}
            lines = (md.get("marketData") or "").split(";")
            if len(lines) < 60: return None
            rows = []
            for line in lines:
                v = line.split(",")
                if len(v) >= 7:
                    rows.append({"date":v[0],"open":float(v[1]),"close":float(v[2]),"high":float(v[3]),"low":float(v[4]),"vol":float(v[5]),"amount":float(v[6])})
            if len(rows) < 60: return None
            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['date'])
            return self._calc_indicators(df, code)
        except: return None

    def _calc_indicators(self, df: pd.DataFrame, code: str = "") -> pd.DataFrame:
        """统一计算技术指标"""
        try:
            df = df.copy()
            if 'datetime' in df.columns: df['date'] = pd.to_datetime(df['datetime'])
            elif 'date' not in df.columns: df['date'] = df.index
            vc = 'vol' if 'vol' in df.columns else 'volume'
            df = df.sort_values('date').reset_index(drop=True)
            for n in [5,10,20,60]: df[f'ma{n}'] = df['close'].rolling(n).mean()
            df['vol_ma20'] = df[vc].rolling(20).mean()
            df['vol_ratio_20'] = df[vc] / df['vol_ma20'].replace(0, np.nan)
            df['peak_60'] = df['close'].rolling(60).max()
            df['drawdown'] = (df['close'] - df['peak_60']) / df['peak_60'] * 100
            df['bias_ma20'] = (df['close'] - df['ma20']) / df['ma20'] * 100
            df['tr'] = np.maximum(df['high']-df['low'], np.maximum(abs(df['high']-df['close'].shift()),abs(df['low']-df['close'].shift())))
            df['atr14'] = df['tr'].rolling(14).mean()
            df['atr_ratio'] = df['atr14'] / df['close'] * 100
            df['change_pct'] = df['close'].pct_change() * 100
            e12 = df['close'].ewm(span=12).mean(); e26 = df['close'].ewm(span=26).mean()
            df['macd'] = e12 - e26; df['macd_signal'] = df['macd'].ewm(span=9).mean(); df['macd_hist'] = df['macd'] - df['macd_signal']
            df['strength_3d'] = df['close'].pct_change(3) * 100
            df['amplitude_60'] = (df['high'].rolling(60).max() - df['low'].rolling(60).min()) / df['close'] * 100
            df['ma_spread'] = (df['ma5'] - df['ma20']).abs() / df['close'] * 100
            df['gain_60d'] = df['close'].pct_change(60) * 100
            if len(df) >= 2 and df.iloc[-1][vc] < df[vc].tail(20).mean() * 0.05: df = df.iloc[:-1]
            if code:
                try:
                    df.to_pickle(self._cache_path(code))
                except:
                    pass
            return df
        except: return None
    
    def get_tencent_quote(self, code: str) -> dict:
        prefix = "sh" if code.startswith(("6","9")) else "sz"
        try:
            url = f"https://qt.gtimg.cn/q={prefix}{code}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            vals = r.content.decode("gbk").split('"')[1].split("~")
            return {"name": vals[1], "price": float(vals[3]),
                    "pe_ttm": float(vals[39]) if vals[39] else None,
                    "mcap_yi": float(vals[44]) if vals[44] else None}
        except:
            return {"name": code, "price": 0, "pe_ttm": None, "mcap_yi": None}


# 美股隔夜行情查询（Yahoo Finance免费接口）
def get_us_stock_change(symbol: str) -> float:
    """查美股最新涨跌幅%（实时价优先，无实时价用最近收盘价）"""
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        results = data.get("chart", {}).get("result", [])
        if not results: return 0
        meta = results[0].get("meta", {})
        prev_close = meta.get("chartPreviousClose", 0)
        if not prev_close: return 0
        # 优先用实时价（盘中时返回实时，收盘后自动等于收盘价）
        curr = meta.get("regularMarketPrice", 0)
        if not curr:
            closes = [c for c in results[0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c]
            curr = closes[-1] if closes else prev_close
        return round((curr - prev_close) / prev_close * 100, 2) if prev_close else 0
    except:
        return 0

def get_nvda_change() -> float:
    return get_us_stock_change("NVDA")

def get_fcx_change() -> float:
    return get_us_stock_change("FCX")


# ════════════════════════════════════════════════════════
# 模块3.8：信号追踪器（记录/回溯信号表现）
# ════════════════════════════════════════════════════════
CACHE_DIR = Path("/app/cache") if Path("/app/cache").exists() else (Path(__file__).parent / "cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_LOG_PATH = CACHE_DIR / "signal_log.json"

def load_signal_log() -> dict:
    """加载历史信号日志"""
    if SIGNAL_LOG_PATH.exists():
        try:
            return json.loads(SIGNAL_LOG_PATH.read_text())
        except:
            return {}
    return {}

def save_signal_log(log: dict):
    """保存信号日志"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True); SIGNAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))

def record_today_signals(results: list, mode: str = ""):
    """记录今日信号到日志"""
    log = load_signal_log()
    today = datetime.now().strftime("%Y-%m-%d")
    today_entry = log.get(today, [])
    for r in results:
        today_entry.append({
            "code": r["code"],
            "name": r["name"],
            "sector": r.get("sector", ""),
            "tier": r["tier"],
            "composite": r["composite"],
            "surge_score": r.get("surge_score", 0),
            "ambush_score": r.get("ambush_score", 0),
            "kelly_pct": r.get("kelly_pct", 0.15),
            "price": r["price"],
            "mode": mode,
        })
    log[today] = today_entry
    save_signal_log(log)

def get_yesterday_tracking() -> list:
    """获取昨日信号的今日表现"""
    log = load_signal_log()
    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 如果今天还没有交易日，往前找最近的交易日
    y_signals = log.get(yesterday, [])
    if not y_signals:
        for i in range(2, 5):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            y_signals = log.get(d, [])
            if y_signals:
                yesterday = d
                break
    
    if not y_signals:
        return []
    
    # 获取当前价格对比
    from mootdx.quotes import Quotes as _Q
    c = _Q.factory(market='std')
    
    results = []
    for s in y_signals:
        code = s["code"]
        try:
            df = c.bars(symbol=code, category=4, offset=2)
            if df is not None and not df.empty:
                current_price = df.iloc[-1]['close']
                entry_price = s["price"]
                change_pct = round((current_price - entry_price) / entry_price * 100, 2)
                results.append({
                    "code": code,
                    "name": s["name"],
                    "tier": s["tier"],
                    "entry": entry_price,
                    "current": current_price,
                    "change_pct": change_pct,
                    "is_win": change_pct > 0,
                    "kelly_pct": s.get("kelly_pct", 0.15),
                })
        except:
            pass
    
    return results

# ════════════════════════════════════════════════════════
# 模块4：三层个股信号（复用v2.0逻辑）
# ════════════════════════════════════════════════════════
class StockScorer:
    """个股三层评分 + AI产业链加分"""

    # ═══ AI产业链逆向映射：code → (节点名, 层级, 综合分) ═══
    # 综合分 = 卡位×0.35 + 独占×0.25 + 势头×0.25 + 国产替代×0.15
    # chain_boost = 综合分×4 (0-20分), 加到 final_sort
    CHAIN_MAP = {
        # ── Layer 0: 基础原材料 ──
        # 硅片/衬底 (3.5)
        "688126": ("硅片/衬底", 0, 3.5), "600703": ("硅片/衬底", 0, 3.5),
        # 电子特气 (3.0)
        "300346": ("电子特气", 0, 3.0),
        # 电子布/玻纤 (3.2)
        "600176": ("电子布/玻纤", 0, 3.2),
        # 覆铜板/CCL (3.2)
        "603002": ("覆铜板/CCL", 0, 3.2), "600183": ("覆铜板/CCL", 0, 3.2),
        # 战略金属/稀土 (3.55)
        "600010": ("战略金属/稀土", 0, 3.55), "000970": ("战略金属/稀土", 0, 3.55),
        "002056": ("战略金属/稀土", 0, 3.55),
        "000657": ("战略金属/稀土", 0, 3.55), "002378": ("战略金属/稀土", 0, 3.55),
        "603993": ("战略金属/稀土", 0, 3.55), "601958": ("战略金属/稀土", 0, 3.55),
        "002155": ("战略金属/稀土", 0, 3.55), "002428": ("战略金属/稀土", 0, 3.55),
        "601899": ("战略金属/稀土", 0, 3.55), "601600": ("战略金属/稀土", 0, 3.55),
        "000630": ("战略金属/稀土", 0, 3.55), "601168": ("战略金属/稀土", 0, 3.55),
        "000603": ("战略金属/稀土", 0, 3.55),
        "600497": ("战略金属/稀土", 0, 3.55), "600711": ("战略金属/稀土", 0, 3.55),
        "000960": ("战略金属/稀土", 0, 3.55), "600531": ("战略金属/稀土", 0, 3.55),
        "000426": ("战略金属/稀土", 0, 3.55),

        # ── Layer 1: 芯片设计/制造 ──
        # AI芯片/GPU (5.0) — 产业链最核心卡点
        "688041": ("AI芯片/GPU", 1, 5.0), "688256": ("AI芯片/GPU", 1, 5.0),
        # 存储芯片 (4.25)
        "002049": ("存储芯片", 1, 4.25), "603986": ("存储芯片", 1, 4.25),
        "688525": ("存储芯片", 1, 4.25),
        # 模拟芯片 (3.65)
        "688798": ("模拟芯片", 1, 3.65), "603501": ("模拟芯片", 1, 3.65),
        # 先进封装 (3.75)
        "688012": ("先进封装", 1, 3.75), "002156": ("先进封装", 1, 3.75),
        "603005": ("先进封装", 1, 3.75),

        # ── Layer 2: 核心元器件 ──
        # 光模块 (3.95)
        "300308": ("光模块", 2, 3.95), "300502": ("光模块", 2, 3.95),
        "300394": ("光模块", 2, 3.95), "688313": ("光模块", 2, 3.95),
        # PCB (3.55)
        "002916": ("PCB", 2, 3.55), "002463": ("PCB", 2, 3.55),
        "603228": ("PCB", 2, 3.55), "002579": ("PCB", 2, 3.55),
        "002384": ("PCB", 2, 3.55),
        # 连接器 (3.1)
        "002475": ("连接器", 2, 3.1), "300570": ("连接器", 2, 3.1),
        "601137": ("连接器", 2, 3.1),
        # 封测材料 (3.4)
        "002409": ("封测材料", 2, 3.4), "300236": ("封测材料", 2, 3.4),
        "300604": ("封测材料", 2, 3.4),
        # MLCC (3.5)
        "300408": ("MLCC", 2, 3.5), "000636": ("MLCC", 2, 3.5),
        # 光芯片 (4.15)
        "688498": ("光芯片", 2, 4.15), "300548": ("光芯片", 2, 4.15),

        # ── Layer 3: 算力基础设施 ──
        # AI服务器 (3.25)
        "601138": ("AI服务器", 3, 3.25), "000977": ("AI服务器", 3, 3.25),
        # 液冷散热 (3.6)
        "603105": ("液冷散热", 3, 3.6), "600481": ("液冷散热", 3, 3.6),
        "688408": ("液冷散热", 3, 3.6),
        # 算力电力 (3.45)
        "600089": ("算力电力", 3, 3.45), "600406": ("算力电力", 3, 3.45),
        "601567": ("算力电力", 3, 3.45),

        # ── Layer 4: AI应用 ──
        # 机器人 (2.65)
        "688017": ("机器人", 4, 2.65), "603728": ("机器人", 4, 2.65),
        "002050": ("机器人", 4, 2.65),
    }

    def check_base(self, row: pd.Series) -> bool:
        cfg = STRATEGY['base']
        if pd.isna(row['drawdown']) or row['drawdown'] >= 0: return False
        if not (cfg['drawdown_min'] <= abs(row['drawdown']) <= cfg['drawdown_max']): return False
        if pd.isna(row['vol_ratio_20']) or row['vol_ratio_20'] < cfg['vol_ratio_min']: return False
        ma = row.get(cfg['ma_period'])
        if pd.isna(ma) or row['close'] <= ma: return False
        if pd.notna(row['change_pct']) and row['change_pct'] < -5: return False
        return True

    def check_enhanced(self, row: pd.Series) -> bool:
        if not self.check_base(row): return False
        return STRATEGY['enhanced']['atr_min'] <= row['atr_ratio'] < STRATEGY['enhanced']['atr_max']

    def check_elite(self, row: pd.Series) -> bool:
        if not self.check_base(row): return False
        cfg = STRATEGY['elite']
        if not (cfg['atr_min'] <= row['atr_ratio'] < cfg['atr_max']): return False
        if row['bias_ma20'] >= cfg['bias_ma20_max']: return False
        return True

    def calc_surge_potential(self, row: pd.Series, sector_score: int, hot_tags: dict = None) -> dict:
        """
        涨停质量分析（专家版）
        三级分类: 🟢高质量板 / 🟡中质量板 / 🔴低质量板
        专家共识: 烂板识别比好板识别更有价值
        """
        score = 0; reasons = []; risks = []; bad_board = []
        code = row.get('stock_code', '')
        vr = row['vol_ratio_20']
        chg = row['change_pct']
        atr = row['atr_ratio']
        dd = row['drawdown']

        # ── 🔴 烂板检测（优先级最高，一票否决权）──
        # ① 偷鸡板检测：尾盘拉升+量比异常
        #    （没有封板时间数据，用量比>2+涨幅<5%但没封死替代）
        if pd.notna(vr) and pd.notna(chg):
            if chg >= 7 and vr > 3:
                # 封住了但分歧巨大
                bad_board.append("分歧板⚠️")
                risks.append("巨量分歧")
                score -= 20
            elif 3 <= chg < 7 and vr > 2.5:
                # 拉了没封住，量还很大
                bad_board.append("冲板回落🔴")
                risks.append("冲高回落")
                score -= 25
            elif chg > 0 and vr < 0.5:
                # 没量没涨幅
                bad_board.append("僵尸板💀")
        # ② 孤狼检测：单只涨停无板块联动
        if pd.notna(chg) and chg >= 5:
            # 用板块评分推断：板块<50是孤狼
            if sector_score < 55:
                bad_board.append("孤狼板🐺")
                risks.append("无跟风")
                score -= 15

        # ── 正常评分（有烂板标记时权重减半）──
        weight = 0.5 if bad_board else 1.0

        # ① 量价关系（0-15）
        if pd.notna(vr):
            if 1.0 <= vr <= 2.0:
                score += 15; reasons.append("健康放量✅")
            elif 2.0 < vr <= 3.0:
                score += 10; reasons.append("明显放量")
            elif vr > 3.0:
                score += 5; reasons.append("巨量")
            elif vr >= 0.6:
                score += 8
        # ② 弹性（0-15）
        if pd.notna(atr):
            if 5 <= atr <= 8:
                score += 15; reasons.append("弹性好")
            elif atr >= 8:
                score += 12; reasons.append("高弹性")
            elif atr >= 4:
                score += 8
        # ③ 题材热度（0-25）
        tag_hotness = 0; tag_match = ""
        if hot_tags and code in hot_tags:
            tags = hot_tags[code].get("reason", "").split("+")
            tc = sum(hot_tags.get("_count", {}).get(t.strip(), 0) for t in tags if t.strip())
            tag_hotness = min(25, tc * 2)
            tag_match = tags[0] if tags else ""
            if tag_hotness >= 20: reasons.append("主线题材🔥")
            elif tag_hotness >= 10: reasons.append("热门题材")
        if tag_hotness == 0:
            if sector_score >= 90: tag_hotness = 25; reasons.append("板块龙头🔥")
            elif sector_score >= 80: tag_hotness = 20; reasons.append("板块强势")
            elif sector_score >= 70: tag_hotness = 15
            elif sector_score >= 60: tag_hotness = 10
            else: tag_hotness = 5
        score += tag_hotness
        # ④ 板块地位（0-15）
        if sector_score >= 90: score += 15; reasons.append("板块龙头")
        elif sector_score >= 80: score += 12; reasons.append("板块核心")
        elif sector_score >= 70: score += 8
        elif sector_score >= 60: score += 5
        # ⑤ 形态（0-15）
        if pd.notna(dd):
            d = abs(dd)
            if d >= 15: score += 15; reasons.append("深跌反弹")
            elif d >= 5: score += 8; reasons.append("回调企稳")
        # ⑥ MACD辅助（0-15）
        macd = row.get('macd', None); macd_sig = row.get('macd_signal', None)
        if pd.notna(macd) and pd.notna(macd_sig):
            if macd > 0 and macd > macd_sig: score += 15; reasons.append("MACD金叉")
            elif macd > 0: score += 8

        # 量比修正：缩量封板加分，放量封板减分
        if pd.notna(vr) and pd.notna(chg):
            if vr < 1.3 and chg >= 5: score += 10; reasons.append("缩量封板💎")  # 高手锁仓
            if vr > 3 and chg >= 5: score -= 10; risks.append("放量分歧")  # 烂板

        score = int(score * weight)
        score = max(0, min(100, score))

        # ── 三级分类 ──
        if bad_board:
            bbt = "|".join(bad_board[:2])
            cls = f"🔴 低质量板({bbt})"
        elif score >= 75:
            cls = "🟢 高质量板"
        elif score >= 50:
            cls = "🟡 中质量板"
        else:
            cls = "🔴 低质量板(评分不足)"

        # 持股建议
        advice = ""
        if "分歧板" in str(bad_board): advice = "次日不涨停就走"
        elif "冲板回落" in str(bad_board): advice = "短线回避"
        elif "孤狼板" in str(bad_board): advice = "不参与"
        elif "缩量封板" in reasons: advice = "中线持有"
        elif score >= 75: advice = "可持有观察"
        elif score >= 50: advice = "等回调再入"
        else: advice = "观望"

        return {
            "score": score,
            "class": cls,
            "reasons": "|".join(reasons[:4]) if reasons else "",
            "risks": "|".join(risks[:2]) if risks else "",
            "tag": tag_match,
            "advice": advice,
            "bad": "|".join(bad_board) if bad_board else "",
        }

    def calc_ambush(self, row: pd.Series, sector_score: int) -> int:
        """🚀 埋伏评分0-100：横盘充分+均线粘合+量价异动"""
        cfg = STRATEGY["ambush"]
        score = 0
        amp = row.get('amplitude_60', None)
        if pd.notna(amp):
            if cfg['amplitude_min'] <= amp < cfg['amplitude_max']: score += 30
            elif amp < cfg['amplitude_min']: score += 15
            elif amp < 40: score += 20
            else: score += 5
        spread = row.get('ma_spread', None)
        if pd.notna(spread):
            if spread < 5: score += 25
            elif spread < 10: score += 20
            elif spread < cfg['ma_spread_max']: score += 15
            elif spread < 25: score += 8
            else: score += 2
        v = row.get('vol_ratio_20', None)
        c = row.get('change_pct', None)
        if pd.notna(v) and pd.notna(c):
            if v >= cfg['vol_ratio_min'] and 0 < c < 7: score += 25
            elif v >= cfg['vol_ratio_min']: score += 10
            elif 0 < c < 7: score += 10
        elif pd.notna(v) and v >= 2: score += 12
        if sector_score >= 70: score += 20
        elif sector_score >= 60: score += 15
        elif sector_score >= 50: score += 10
        elif sector_score >= 40: score += 5
        if pd.notna(c):
            if c >= 7: score = int(score * 0.4)
            elif c >= 5: score = int(score * 0.6)
        g = row.get('gain_60d', None)
        if pd.notna(g) and g >= 30: score = int(score * 0.5)
        return min(score, 100)

    def calc_short_term(self, row: pd.Series, sector_score: int) -> dict:
        score = 0
        reasons = []
        v = row.get('vol_ratio_20', None)
        ma20 = row.get('ma20', None)
        close = row.get('close', None)
        if pd.notna(v) and pd.notna(ma20) and pd.notna(close):
            if v >= 1.3 and close > ma20:
                score += 25
                reasons.append("突破MA20")
        macd = row.get('macd', None)
        macd_sig = row.get('macd_signal', None)
        if pd.notna(macd) and pd.notna(macd_sig):
            if macd > 0 and macd > macd_sig:
                score += 20
                reasons.append("MACD金叉")
            elif macd > 0:
                score += 10
                reasons.append("MACD多头")
        s3 = row.get('strength_3d', None)
        if pd.notna(s3):
            if s3 >= 5: score += 20; reasons.append("强势上攻")
            elif s3 >= 3: score += 15; reasons.append("走强")
            elif s3 >= 1: score += 10; reasons.append("微涨")
            elif s3 >= 0: score += 5
        if sector_score >= 70: score += 15; reasons.append("板块火热")
        elif sector_score >= 60: score += 10; reasons.append("板块偏强")
        elif sector_score >= 50: score += 5
        dd = row.get('drawdown', None)
        if pd.notna(dd) and dd < -10:
            score = int(score * 0.7)
        score = min(score, 100)
        return {"short_score": score, "short_reasons": "|".join(reasons[:3]) if reasons else ""}

    def calc_chain_boost(self, code: str) -> tuple:
        """AI产业链加分：判断股票是否属于关键产业链环节
        Returns (chain_name, layer, boost_points)
        """
        if code in self.CHAIN_MAP:
            name, layer, comp = self.CHAIN_MAP[code]
            # 综合分0-5 → 加分0-20分
            boost = int(comp * 4)
            return (name, layer, boost)
        return ("", -1, 0)

    def calc_kelly_position(self, row: pd.Series, tier_score: int) -> float:
        """
        凯利公式计算仓位比例
        f* = (p * b - q) / b
        p = 胜率, q = 1-p, b = 盈亏比(平均盈利/平均亏损)
        """
        # 基于回测(2024-01~2026-06, 590交易日)校准的真实胜率
        # 精选层55.1% 增强层49.8% 普通层60.6%
        tier_cfg = {55: (0.60, 1.5), 69: (0.50, 1.8), 90: (0.55, 2.0)}
        p, b = tier_cfg.get(tier_score, (0.50, 1.0))
        q = 1 - p
        kelly = (p * b - q) / b if b > 0 else 0
        # 安全限制：最大仓位80%，半凯利(更保守)使用
        return round(min(max(kelly * 0.6, 0.05), 0.8), 2)

    def verify_signal(self, row: pd.Series, sector_score: int) -> dict:
        """
        三灯验证阀：验证信号可信度
        板块共振 + 资金验证 + 趋势配合 → 可信度评级
        """
        lights = []
        details = []

        # ① 板块共振灯
        sec_ok = sector_score >= 50  # 板块不弱
        chg_ok = pd.notna(row['change_pct']) and row['change_pct'] > 0  # 个股在涨
        if sec_ok and chg_ok:
            lights.append("🟢")
            details.append("板块共振")
        elif sec_ok:
            lights.append("🟡")
            details.append("板块强但个股未涨")
        else:
            lights.append("🔴")
            details.append("板块偏弱")

        # ② 资金验证灯
        vol_ok = pd.notna(row['vol_ratio_20']) and row['vol_ratio_20'] >= 1.0  # 有量能
        ma5_ok = pd.notna(row['ma5']) and row['close'] > row['ma5']  # 站上MA5
        if vol_ok and ma5_ok:
            lights.append("🟢")
            details.append("资金入场")
        elif vol_ok or ma5_ok:
            lights.append("🟡")
            details.append("量价部分配合")
        else:
            lights.append("🔴")
            details.append("量价背离")

        # ③ 趋势配合灯
        ma10_ok = pd.notna(row['ma10']) and row['close'] > row['ma10'] if 'ma10' in row else False
        bias_ok = pd.notna(row['bias_ma20']) and row['bias_ma20'] > -5  # 不远离20日线
        if ma10_ok and bias_ok:
            lights.append("🟢")
            details.append("趋势向上")
        elif ma10_ok or bias_ok:
            lights.append("🟡")
            details.append("趋势中性")
        else:
            lights.append("🔴")
            details.append("趋势偏弱")

        # 综合评级
        green = lights.count("🟢")
        if green >= 2:
            level = "🟢 高可信"
        elif green >= 1:
            level = "🟡 中等"
        else:
            level = "🔴 低可信"

        return {
            "level": level,
            "lights": "".join(lights),
            "details": " | ".join(details),
            "green": green,
        }

    def score(self, row: pd.Series, sector_score: int, code: str = "") -> dict:
        """返回个股评分 + 综合评分（含AI产业链加分）"""
        levels = []
        elite = self.check_elite(row)
        enhanced = self.check_enhanced(row)
        base = self.check_base(row)

        tier = None
        tier_score = 0
        if elite:
            tier = "💎 精选层"
            tier_score = 90
        elif enhanced:
            tier = "🥈 增强层"
            tier_score = 69
        elif base:
            tier = "🥉 普通层"
            tier_score = 55
        else:
            return None

        # 综合评分 = 个股基础分 × 0.6 + 板块分 × 0.4
        sector_weight = 0.4 if sector_score >= 60 else (0.2 if sector_score >= 40 else 0.1)
        composite = int(tier_score * (1 - sector_weight) + sector_score * sector_weight)

        # 综合排序分（用于最终排名，每天只看前3-5名）
        surge = self.calc_surge_potential(row, sector_score, None)
        surge_sc = surge.get("score", 0) if isinstance(surge, dict) else surge
        verify = self.verify_signal(row, sector_score)
        green_lights = verify.get("green", 0)
        ambush = self.calc_ambush(row, sector_score)

        # AI产业链加分：卡位越核心，加分越多（0-20分）
        chain_name, chain_layer, chain_boost = self.calc_chain_boost(code)
        chain_tag = f"🤖{chain_name}" if chain_name else ""

        # ATR动态止损：止损 = ATR × 2, 范围 5%-15%
        atr_val = row.get('atr_ratio', None)
        if pd.notna(atr_val) and atr_val > 0:
            atr_stop = min(0.15, max(0.05, atr_val * 2 / 100))
        else:
            atr_stop = 0.08  # 默认 -8%

        final_sort = int(
            composite * 0.30
            + surge_sc * 0.18
            + green_lights * 10 * 0.18      # 3灯×10=30分，权重18%
            + sector_score * 0.12
            + ambush * 0.10
            + chain_boost * 0.12            # 产业链加分，权重12%
        )

        return {
            "tier": tier,
            "tier_score": tier_score,
            "sector_score": sector_score,
            "composite": composite,
            "sector_weight": sector_weight,
            "surge_score": self.calc_surge_potential(row, sector_score, None),
            "ambush_score": self.calc_ambush(row, sector_score),
            "verify": self.verify_signal(row, sector_score),
            "kelly_pct": self.calc_kelly_position(row, tier_score),
            "short_term": self.calc_short_term(row, sector_score),
            "final_sort": final_sort,
            "atr_stop_pct": atr_stop,
            "chain_boost": chain_boost,
            "chain_name": chain_name,
            "chain_tag": chain_tag,
        }


# ════════════════════════════════════════════════════════
# 模块5：微信通知器
# ════════════════════════════════════════════════════════
class WeChatNotifier:
    def __init__(self):
        self.webhook = NOTIFY_CONFIG["wechat_webhook"]
        self.sckey = NOTIFY_CONFIG["serverchan_key"]
        self.dingtalk = NOTIFY_CONFIG["dingtalk_webhook"]

    def _should_push(self, force=False) -> bool:
        now = datetime.now()
        start, end = NOTIFY_CONFIG["push_window"]
        if force:
            return True
        if not (start <= now.hour < end): return False
        if now.weekday() >= 5: return False
        return True

    def notify(self, results: list, market_info: dict, sector_info: dict, mode: str = "", all_results: list = None, hot_breakout: list = None, force_push: bool = False):
        if not results:
            print("  [通知] 无信号，跳过")
            return
        if not self._should_push(force=force_push):
            print("  [通知] 非推送时段，跳过")
            return

        mode_label = {"morning": "早报", "lunch": "午盘", "close": "收盘", "night": "复盘"}.get(mode, "")

        lines = []
        temp = market_info.get("temperature", 50)

        # ── 头部 ──
        mode_tag = f" [{mode_label}]" if mode_label else ""
        pos_pct = market_info.get('position_limit', 1) * 100
        sectors_str = '\n'.join(f'{s}({sc})' for s, sc in (sector_info.get('top') or [])[:3])
        lines.append(f"【A股策略{mode_tag} {datetime.now().strftime('%m/%d %H:%M')}】")
        lines.append(f"🌡️ {temp}°  仓位 {pos_pct:.0f}%")
        lines.append(f"🏆\n  {sectors_str}")
        # ── 信号概览 ──
        tiers_count = Counter(r["tier"] for r in results)
        elite_n = tiers_count.get("💎 精选层", 0)
        enhanced_n = tiers_count.get("🥈 增强层", 0)
        normal_n = tiers_count.get("🥉 普通层", 0)
        parts = []
        if elite_n: parts.append(f"💎{elite_n}")
        if enhanced_n: parts.append(f"🥈{enhanced_n}")
        parts.append(f"🥉{normal_n}")
        lines.append(f"📈 {'+'.join(parts)}={len(results)}个")
        lines.append("")

        # ── 重点 TOP 3 ──
        top3 = results[:3]
        lines.append("━━━ 重点观察 TOP 3 ━━━")
        for r in top3:
            v = r.get("verify", {})
            gl = v.get("green", 0) if v else 0
            lights = "🟢" * gl + "🟡" * (3 - gl)
            kp = r.get('kelly_pct', 0.15) * 100
            dd = r.get('drawdown', 0)
            sec = r.get('sector', '')
            sec_sc = r.get('sector_score', 0)
            surge = r.get('surge_score', {}) or {}
            surge_sc = surge.get('score', 0) if isinstance(surge, dict) else surge
            ambush = r.get('ambush_score', 0)
            line = f"{r['name']}({r['code']}) {lights}"
            line += f" | {sec}{sec_sc}分 | 综合{r['composite']} | 回撤{dd:+.0f}%"
            extras = []
            if surge_sc >= 30: extras.append(f"🔥{surge_sc}")
            if ambush >= 60: extras.append(f"🚀{ambush}")
            if r.get('short_score', 0) >= 50: extras.append(f"⚡{r['short_score']}")
            lines.append(line)
            stop_str = f"止损-{r.get('atr_stop_pct', 0.08)*100:.0f}%"
            lines.append(f"  仓位{kp:.0f}% {stop_str} +10%后回撤6% {' '.join(extras)}")
        lines.append("")

        # ── 全市场扫描 ──
        ar = all_results or results
        if len(ar) > 3:
            lines.append("━━━ 全市场扫描 ━━━")

            short_sigs = sorted(ar, key=lambda r: -r.get('short_score', 0))[:3]
            if short_sigs and short_sigs[0].get('short_score', 0) >= 50:
                parts = [f"{r['name']}({r.get('short_score',0)}分)" for r in short_sigs]
                lines.append(f"⚡ 短线: {' '.join(parts)}")

            surge_top = sorted(ar, key=lambda r: -(r.get('surge_score', {}) or {}).get('score', 0))[:3]
            if surge_top and (surge_top[0].get('surge_score', {}) or {}).get('score', 0) >= 30:
                parts = []
                for r in surge_top:
                    s = r.get('surge_score', {}) or {}
                    sc = s.get('score', 0) if isinstance(s, dict) else s
                    parts.append(f"{r['name']}({sc}分)")
                lines.append(f"🔥 涨停: {' '.join(parts)}")

            ambush_top = sorted(ar, key=lambda r: -r.get('ambush_score', 0))[:3]
            if ambush_top and ambush_top[0].get('ambush_score', 0) >= 60:
                parts = [f"{r['name']}({r.get('ambush_score',0)}分)" for r in ambush_top]
                lines.append(f"🚀 埋伏: {' '.join(parts)}")

            sector_counts = Counter(r['sector'] for r in ar)
            top_sec = sector_counts.most_common(3)
            if top_sec:
                parts = [f"{s}({c}只)" for s, c in top_sec]
                lines.append(f"📌 信号集中: {' '.join(parts)}")

            # 热门板块突破观察
            if hot_breakout:
                hb_sorted = sorted(hot_breakout, key=lambda x: -x.get('short_score', 0))[:3]
                parts = [f"{r['name']}({r.get('short_score',0)}分)" for r in hb_sorted]
                lines.append(f"🔥 突破: {' '.join(parts)}")

        # ── 尾部 ──
        if mode == "morning":
            tracking = get_yesterday_tracking()
            if tracking:
                wins = sum(1 for t in tracking if t["is_win"])
                total = len(tracking)
                avg_ret = sum(t["change_pct"] for t in tracking) / total
                lines.append("")
                lines.append(f"**📊 昨日追踪** {wins}/{total}胜 均{avg_ret:+.2f}%")
                for t in tracking[:5]:
                    emoji = "✅" if t["is_win"] else "❌"
                    lines.append(f"> {emoji} {t['tier']} {t['name']}({t['code']}) {t['change_pct']:+.2f}%")

        # ── 美股参考 ──
        nvda_chg = get_nvda_change()
        fcx_chg = get_fcx_change()
        us_parts = []
        if abs(nvda_chg) >= 0.3: us_parts.append(f"NVDA {nvda_chg:+.2f}%")
        if abs(fcx_chg) >= 0.5: us_parts.append(f"FCX {fcx_chg:+.2f}%")
        if us_parts:
            lines.append("")
            lines.append(f"**🇺🇸 隔夜** | {' '.join(us_parts)}")

        lines.append("")
        lines.append(f"---")
        if results:
            avg_stop = sum(r.get('atr_stop_pct', 0.08) for r in results) / len(results)
            lines.append(f"🛑 止损ATR({avg_stop*100:.0f}%均值) 移动止盈+10%回撤6% 持有20日 仓位{market_info.get('position_limit',1)*100:.0f}%")
        else:
            lines.append(f"🛑 仓位: {market_info.get('position_limit',1)*100:.0f}% | 仅供参考，不构成投资建议")

        content = "\n".join(lines)

        # 推送：Server酱
        title_prefix = mode_label
        pushed = False
        if self.sckey:
            try:
                r = requests.post(f"https://sctapi.ftqq.com/{self.sckey}.send",
                    data={"title": f"{title_prefix}A股策略 {datetime.now().strftime('%m-%d')} 温度{temp}/100",
                          "desp": content}, timeout=10)
                pushed = r.json().get("code") == 0 or r.json().get("errno") == 0
                print(f"  [通知] Server酱: {'✅' if pushed else '❌'}")
            except Exception as e:
                print(f"  [通知] Server酱推送异常: {e}")

        # 推送：钉钉机器人
        if self.dingtalk:
            try:
                dd_content = content.replace("\n", "\n\n")
                dd_payload = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": f"{title_prefix}A股策略 {datetime.now().strftime('%m-%d')}",
                        "text": dd_content,
                    }
                }
                r = requests.post(self.dingtalk, json=dd_payload,
                    headers={"Content-Type": "application/json"}, timeout=10)
                dd_ok = r.json().get("errcode") == 0
                print(f"  [通知] 钉钉: {'✅' if dd_ok else '❌'} {r.json().get('errmsg','')}")
            except Exception as e:
                print(f"  [通知] 钉钉推送异常: {e}")

        if not pushed and not self.dingtalk:
            print("  [通知] 未推送成功，内容如下:")
            print(content)



# ════════════════════════════════════════════════════════
# 模块6：自动板块映射（替代固定股票池）
# ════════════════════════════════════════════════════════
class AutoSectorMapper:
    """用东财实时板块接口自动归类股票赛道"""

    BOARD_TO_SECTOR = {
        "计算机设备": "AI算力通信", "通信设备": "AI算力通信", "通信服务": "AI算力通信",
        "通信运营": "AI算力通信", "IT服务": "AI算力通信", "软件开发": "AI算力通信",
        "云服务": "AI算力通信", "数据中心": "AI算力通信",
        "人工智能": "AI算力通信", "算力": "AI算力通信",
        "半导体": "科技半导体", "芯片": "科技半导体", "电子元件": "科技半导体",
        "电子": "科技半导体", "半导体设备": "科技半导体", "集成电路": "科技半导体",
        "电池": "新能源制造", "新能源汽车": "新能源制造", "汽车整车": "新能源制造",
        "汽车零部件": "新能源制造", "锂电池": "新能源制造", "光伏设备": "新能源制造",
        "新能源": "新能源制造", "充电桩": "新能源制造",
        "化学制品": "化工", "化学原料": "化工", "化工原料": "化工",
        "农化制品": "化工", "化肥": "化工", "聚氨酯": "化工", "煤化工": "化工",
        "有色金属": "有色金属", "黄金": "有色金属", "铜": "有色金属",
        "铝": "有色金属", "工业金属": "有色金属", "稀有金属": "有色金属",
        "稀土": "有色金属", "小金属": "有色金属", "贵金属": "有色金属",
        "矿业": "有色金属", "稀缺资源": "有色金属",
        "航天航空": "航天军工", "军工": "航天军工", "国防军工": "航天军工",
        "航空装备": "航天军工", "船舶制造": "航天军工",
        "银行": "周期金融", "证券": "周期金融", "保险": "周期金融",
        "金融": "周期金融", "多元金融": "周期金融",
        "化学制药": "创新药",
        "生物制品": "医药生物", "医疗器械": "医药生物", "医疗服务": "医药生物",
        "医药": "医药生物", "中药": "医药生物",
        "卫星": "商业航天",
        "电网设备": "储能", "光伏": "储能", "储能": "储能",
        "白酒": "大市值消费", "食品饮料": "大市值消费", "家电": "大市值消费",
    }

    _cache = {}

    @classmethod
    def get_sector(cls, code: str, name: str = "") -> tuple:
        """返回 (赛道, 板块标签列表, 数据来源)"""
        if code in cls._cache:
            return cls._cache[code]

        blocks = cls._fetch_blocks(code)
        if not blocks:
            sector = "科技半导体" if code.startswith("002") else "其他"
            result = (sector, [], "规则后备")
        else:
            sector = cls._match_sector(blocks)
            result = (sector, blocks, "东财实时")

        cls._cache[code] = result
        time.sleep(random.uniform(0.1, 0.2))
        return result

    @classmethod
    def _fetch_blocks(cls, code: str) -> list:
        market_code = 1 if code.startswith("6") else 0
        url = "https://push2.eastmoney.com/api/qt/slist/get"
        params = {"fltt":"2","invt":"2","secid":f"{market_code}.{code}",
                  "spt":"3","pi":"0","pz":"200","po":"1","fields":"f12,f14"}
        try:
            r = requests.get(url, params=params,
                headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"}, timeout=10)
            d = r.json()
            items = (d.get("data") or {}).get("diff") or {}
            if isinstance(items, dict): items = items.values()
            return [it.get("f14","") for it in items if it.get("f14")]
        except:
            return []

    @classmethod
    def _match_sector(cls, blocks: list) -> str:
        for block in blocks:
            for keyword, sector in cls.BOARD_TO_SECTOR.items():
                if keyword in block:
                    return sector
        return "其他"

    @classmethod
    def clear_cache(cls):
        cls._cache = {}


# ════════════════════════════════════════════════════════
# 模块7：政策催化检测器（用同花顺reason tags自动检测）
# ════════════════════════════════════════════════════════
class PolicyCatalystDetector:
    """检测同花顺热点reason tags中政策类关键词的突变"""

    POLICY_KEYWORDS = {
        "央企": ["周期金融", "大市值消费"],
        "国企改革": ["周期金融"],
        "商业航天": ["商业航天", "航天军工"],
        "低空经济": ["商业航天", "航天军工"],
        "军工": ["航天军工"],
        "航天": ["航天军工", "商业航天"],
        "创新药": ["创新药", "医药生物"],
        "机器人": ["科技半导体", "AI算力通信"],
        "人形机器人": ["科技半导体"],
        "AI算力": ["AI算力通信"],
        "算力租赁": ["AI算力通信"],
        "储能": ["储能", "新能源制造"],
        "固态电池": ["新能源制造", "储能"],
        "人工智能": ["AI算力通信"],
        "光通信": ["AI算力通信"],
        "CPO": ["AI算力通信"],
        "半导体材料": ["科技半导体"],
        "先进封装": ["科技半导体"],
        "存储芯片": ["科技半导体"],
        "核电": ["新能源制造"],
        "氢能源": ["新能源制造", "化工"],
        "有色金属": ["有色金属"],
        "稀土": ["有色金属"],
        "黄金": ["有色金属"],
        "化工": ["化工"],
    }

    def __init__(self, lookback_days=5):
        self.lookback = lookback_days
        self.daily_tags = {}
        self.catalyst_sectors = {}

    def fetch_recent_tags(self):
        for i in range(self.lookback, 0, -1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{d}/orderby/date/orderway/desc/charset/GBK/"
            try:
                r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
                rows = r.json().get("data") or []
                tags = Counter()
                for row in rows:
                    reason = row.get("reason","")
                    if reason:
                        tags.update([t.strip() for t in reason.split("+") if t.strip()])
                self.daily_tags[d] = tags
            except:
                pass

    def detect(self) -> dict:
        """返回 {赛道: 催化强度0-100}"""
        self.fetch_recent_tags()
        if len(self.daily_tags) < 2:
            return {}

        sorted_dates = sorted(self.daily_tags.keys())
        today = sorted_dates[-1]
        today_tags = self.daily_tags[today]

        avg_tags = Counter()
        count = 0
        for d in sorted_dates[:-1]:
            avg_tags += self.daily_tags[d]
            count += 1
        if count == 0:
            return {}
        for k in avg_tags:
            avg_tags[k] = avg_tags[k] / count

        sector_impact = defaultdict(list)
        for keyword, affected_sectors in self.POLICY_KEYWORDS.items():
            today_count = today_tags.get(keyword, 0)
            avg_count = avg_tags.get(keyword, 0)

            if avg_count < 1 and today_count >= 3:
                intensity = min(80, today_count * 8)
                for s in affected_sectors:
                    sector_impact[s].append(intensity)
            elif avg_count >= 1 and today_count > avg_count * 1.8 and today_count >= 5:
                intensity = min(60, int(today_count / avg_count * 20))
                for s in affected_sectors:
                    sector_impact[s].append(intensity)

        result = {}
        for sector, intensities in sector_impact.items():
            result[sector] = min(100, sum(intensities))
        return result


# ════════════════════════════════════════════════════════
# 模块8：组合风控过滤器
# ════════════════════════════════════════════════════════
def portfolio_risk_filter(signals: list, max_total_positions: int = 5,
                          max_sector_pct: float = 0.30) -> list:
    """
    组合风控：确保不会过度集中在单一行业或标的。

    规则：
    1. 同一行业最多取排序分最高的 2 只
    2. 总信号数不超过 max_total_positions
    3. 单票凯利 > 50% 的砍到 50%（极端保守）
    """
    if not signals:
        return signals

    # 按 final_sort 降序
    sorted_sig = sorted(signals, key=lambda r: -r.get("final_sort", r.get("composite", 0)))

    # 行业集中度控制
    sector_count = {}
    filtered = []
    for r in sorted_sig:
        sec = r.get("sector", "其他")
        sector_count.setdefault(sec, 0)
        if sector_count[sec] >= 2:
            continue  # 同一行业最多 2 只
        sector_count[sec] += 1

        # 凯利上限：单票不超过 50%
        kelly = r.get("kelly_pct", 0.15)
        if kelly > 0.50:
            r["kelly_pct"] = 0.50

        filtered.append(r)
        if len(filtered) >= max_total_positions:
            break

    return filtered


# ════════════════════════════════════════════════════════
# 模块9：自学习反馈
# ════════════════════════════════════════════════════════
def apply_self_learning_feedback(sector_scores: dict) -> dict:
    """
    从自学习数据中读取板块历史胜率，对胜率低的板块降权。

    仅在板块有 >= 10 笔已检查交易且胜率 < 40% 时触发。
    """
    try:
        learn_path = CACHE_DIR / "self_learn.json"
        if not learn_path.exists():
            return sector_scores

        learn_data = json.loads(learn_path.read_text())
        stats = learn_data.get("stats", {})
        sector_stats = stats.get("by_sector", {})

        if not sector_stats:
            return sector_scores

        adjusted = dict(sector_scores)
        for sector, st in sector_stats.items():
            if st.get("total", 0) >= 10 and st.get("wr_5d", 50) < 40:
                if sector in adjusted:
                    old = adjusted[sector]
                    adjusted[sector] = int(old * 0.7)
                    print(f"  [自学习] {sector} 历史胜率{st['wr_5d']:.0f}%({st['total']}笔) → 板块评分 {old}→{adjusted[sector]} (打7折)")
        return adjusted
    except Exception as e:
        if "debug" in dir() or True:
            pass  # 静默失败，不影响主流程
        return sector_scores


# ════════════════════════════════════════════════════════
# 模块10：板块轮动检测
# ════════════════════════════════════════════════════════
def detect_sector_rotation(sector_scores: dict) -> dict:
    """
    对比今日与昨日的板块评分，检测资金轮动方向。

    返回:
        {"gaining": [(板块, 变化量, 当前分), ...],
         "cooling": [(板块, 变化量, 当前分), ...],
         "available": bool}
    """
    try:
        rotation_path = CACHE_DIR / "sector_rotation.json"
        today_str = datetime.now().strftime("%Y-%m-%d")

        history = {}
        if rotation_path.exists():
            history = json.loads(rotation_path.read_text())

        # 找最近的一个交易日数据
        yesterday_scores = {}
        for d in range(1, 10):
            check = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
            if check in history and check != today_str:
                yesterday_scores = history[check]
                break

        # 保存今天的记录
        history[today_str] = {k: v for k, v in sector_scores.items()}
        while len(history) > 30:
            history.pop(next(iter(history)))
        rotation_path.write_text(json.dumps(history, ensure_ascii=False, indent=2))

        if not yesterday_scores:
            return {"gaining": [], "cooling": [], "available": False}

        changes = []
        for sector, score in sector_scores.items():
            if sector in yesterday_scores and yesterday_scores[sector] > 0:
                delta = score - yesterday_scores[sector]
                changes.append((sector, delta, score, yesterday_scores[sector]))

        changes.sort(key=lambda x: -abs(x[1]))
        gaining = [(s, d, sc) for s, d, sc, _ in changes if d >= 8][:5]
        cooling = [(s, d, sc) for s, d, sc, _ in changes if d <= -8][:5]

        return {"gaining": gaining, "cooling": cooling, "available": bool(gaining or cooling)}
    except Exception as e:
        if os.environ.get("DEBUG_ROTATION"):
            print(f"  [轮动] 检测异常: {e}")
        return {"gaining": [], "cooling": [], "available": False}


# ════════════════════════════════════════════════════════
# 模块11：盘中预警监控
# ════════════════════════════════════════════════════════
def _run_monitor(args):
    """
    盘中预警监控：持续扫描，检测信号/板块/温度变化时提醒。

    通过子进程执行安静扫描，解析输出中关键变化行。
    无需改动main()中的扫描逻辑，独立运行互不干扰。
    """
    import hashlib
    interval = max(60, args.interval)
    prev_sig_hash = ""

    print(f"\n🔍 盘中预警监控启动 (轮询间隔 {interval}s)")
    print(f"   按 Ctrl+C 停止\n")

    while True:
        try:
            now = datetime.now()
            if now.weekday() >= 5:
                print(f"  💤 周末暂停 ({now.strftime('%Y-%m-%d %H:%M')})")
                time.sleep(1800)
                continue
            if now.hour < 9 or now.hour >= 15:
                wait = 600 if now.hour >= 15 else ((9 - now.hour) * 3600 - now.minute * 60 - now.second + 60)
                time.sleep(min(wait, 1800))
                continue

            cmd = [sys.executable, __file__, '--quiet', '--mode', args.mode]
            if args.sector:
                cmd += ['--sector', args.sector]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            output = r.stdout

            sig_lines = []
            for line in output.split('\n'):
                if any(kw in line for kw in ['信号', '🌡️', '短线', '突破', '综合', '🏆', '轮动',
                                              '板块', '精选', '增强']):
                    if line.strip():
                        sig_lines.append(line.strip()[-120:])
            sig_text = '\n'.join(sorted(set(sig_lines)))
            cur_hash = hashlib.md5(sig_text.encode()).hexdigest()

            if prev_sig_hash:
                if cur_hash != prev_sig_hash:
                    print(f"\n[{now.strftime('%H:%M')}] 🔔 扫描结果变化!")
                    for line in output.split('\n'):
                        if any(kw in line for kw in ['🆕', '⚡', '🔥 热门', '轮动', '资金流入',
                                                      '资金流出', '新信号', '🌡️', '🧊']):
                            if line.strip():
                                print(f"  {line.strip()}")
                    for line in output.split('\n'):
                        if '🌡️' in line and '温度' in line:
                            print(f"  {line.strip()}")
                            break
            else:
                print(f"  [{now.strftime('%H:%M')}] ✅ 首次扫描完成:")
                for line in output.split('\n'):
                    if '🌡️' in line or '📈 策略信号' in line or '🏆 热门板块' in line:
                        print(f"    {line.strip()}")

            prev_sig_hash = cur_hash

        except KeyboardInterrupt:
            print(f"\n  ⏹️ 监控停止 ({datetime.now().strftime('%H:%M')})")
            break
        except subprocess.TimeoutExpired:
            print(f"  [{now.strftime('%H:%M')}] ⏰ 扫描超时(300s), 跳过本轮")
        except Exception as e:
            print(f"  [{now.strftime('%H:%M')}] ⚠️ 扫描异常: {e}")

        time.sleep(interval)


# ════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="A股策略v3.0 - 温度计+板块评分+三层精选")
    parser.add_argument("--sector", type=str, help="赛道过滤: AI/化学/金属/航天/军工/储能/医药/all")
    parser.add_argument("--wechat", action="store_true", help="微信推送")
    parser.add_argument("--debug", action="store_true", help="输出详细评分")
    parser.add_argument("--codes", type=str, help="指定股票代码")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--force-push", action="store_true", help="强制推送(跳过周末/时间检查)")
    parser.add_argument("--mode", type=str, default="",
                        help="推送模式: morning(早报)/lunch(午盘)/close(收盘)/night(复盘)")
    parser.add_argument("--monitor", action="store_true",
                        help="盘中预警监控: 持续扫描发现新信号/板块突变时提醒")
    parser.add_argument("--interval", type=int, default=300,
                        help="监控轮询间隔(秒), 默认300秒(5分钟)")
    args = parser.parse_args()
    args.force_push = getattr(args, 'force_push', False)

    # ── 盘中预警监控模式 ──
    if args.monitor:
        return _run_monitor(args)

    # 根据模式调整推送窗口
    mode_label = {"lunch": "午盘", "close": "收盘", "night": "复盘"}.get(args.mode, "")

    if not args.quiet:
        tag = f"【{mode_label}】" if mode_label else ""
        print("\n" + "█" * 60)
        print(f"  A股策略 v3.0 {tag}— 市场温度计 + 板块评分 + 三层精选")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("  数据源: 同花顺热点 | 东财板块 | mootdx K线")
        print("█" * 60)

    # ── 步骤1：市场温度计 ──
    print("\n📡 ① 市场温度计...")
    thermometer = MarketThermometer()
    temp_result = thermometer.calc_temperature()
    market_state = thermometer.get_market_state()
    pos_limit = thermometer.get_position_limit()

    if not args.quiet:
        print(f"\n  🌡️ 温度: {temp_result['temperature']}/100 | 状态: {market_state}")
        print(f"  📊 建议仓位: {pos_limit*100:.0f}%")
        print(f"  📈 涨停/跌停估: {temp_result['data'].get('涨停数(估)',0)}/{temp_result['data'].get('跌停数(估)',0)}")
        print(f"  📈 上涨占比: {temp_result['data'].get('上涨占比',0)*100:.0f}%")
        if args.debug:
            print(f"  📊 温度子项: {temp_result['scores']}")

    # ── 步骤2：板块评分 + 政策催化检测 ──
    print("\n📡 ② 板块评分系统 + 政策催化检测...")
    sector_scorer = SectorScorer()
    sector_scores = sector_scorer.score_sectors()

    # 叠加政策催化检测
    print("  [催化] 分析同花顺reason tags突变...")
    catalyst = PolicyCatalystDetector()
    catalyst_scores = catalyst.detect()
    if catalyst_scores:
        print(f"  [催化] 检测到 {len(catalyst_scores)} 个赛道有政策催化信号")
        for sec, cscore in catalyst_scores.items():
            if sec in sector_scores:
                old = sector_scores[sec]
                sector_scores[sec] = min(100, old + int(cscore * 0.3))
                print(f"    {sec}: {old} + 催化{cscore} → {sector_scores[sec]}")

    # ── 隔夜NVDA映射（仅午盘/对AI板块生效） ──
    if args.mode == "lunch":
        nvda_chg = get_nvda_change()
        nvda_impact = ""
        for sec in ["AI算力通信", "科技半导体"]:
            if sec in sector_scores:
                old = sector_scores[sec]
                if nvda_chg >= 3:
                    sector_scores[sec] = min(100, old + 8)
                    nvda_impact = f"NVDA+{nvda_chg}%大涨 → 板块+8分"
                elif nvda_chg >= 2:
                    sector_scores[sec] = min(100, old + 5)
                    nvda_impact = f"NVDA+{nvda_chg}% → 板块+5分"
                elif nvda_chg <= -3:
                    sector_scores[sec] = max(0, old - 8)
                    nvda_impact = f"NVDA{nvda_chg}%大跌 → 板块-8分"
                elif nvda_chg <= -2:
                    sector_scores[sec] = max(0, old - 5)
                    nvda_impact = f"NVDA{nvda_chg}% → 板块-5分"
        if nvda_impact:
            print(f"  [美股] {nvda_impact}")
        else:
            print(f"  [美股] NVDA {nvda_chg:+.2f}% → 影响不大，不调整")

    print("  [板块] 东财板块评分完成，叠加政策/美股修正")

    top_sectors_list = sorted(sector_scores.items(), key=lambda x: -x[1])
    bottom_sectors_list = sorted(sector_scores.items(), key=lambda x: x[1])

    if not args.quiet:
        print(f"\n  🏆 热门板块 TOP5:")
        for s, sc in top_sectors_list[:5]:
            bar = "█" * (sc // 10) + "░" * (10 - sc // 10)
            hint = "⭐优先" if s in SECTOR_PREFERENCE.get("prefer", []) else ("⚠️谨慎" if s in SECTOR_PREFERENCE.get("avoid", []) else "")
            print(f"    {bar} {s}: {sc}分 {hint}")
        print(f"\n  📉 弱势板块:")
        for s, sc in bottom_sectors_list[:3]:
            print(f"    {s}: {sc}分")
        if args.debug:
            print(f"\n  全板块评分: {sector_scores}")

    # 后续使用
    top_sectors = top_sectors_list[:5]
    bottom_sectors = bottom_sectors_list[:3]

    # ── 自学习反馈：对历史胜率低的板块降权 ──
    sector_scores = apply_self_learning_feedback(sector_scores)

    # ── 板块轮动检测（对比昨日板块评分变化）──
    rotation = detect_sector_rotation(sector_scores)
    if not args.quiet and rotation.get("available"):
        gaining = rotation.get("gaining", [])
        cooling = rotation.get("cooling", [])
        print(f"\n🔄 板块轮动检测:")
        if gaining:
            print(f"  🔥 资金流入: {' | '.join(f'{s}({d:+.0f}→{sc})' for s,d,sc in gaining)}")
        if cooling:
            print(f"  🧊 资金流出: {' | '.join(f'{s}({d:.0f}→{sc})' for s,d,sc in cooling)}")

    # ── 步骤3：个股扫描 ──
    # 确定扫描范围
    if args.codes:
        stock_list = [(c.strip(), "", "") for c in args.codes.split(",")]
    elif args.sector and args.sector != "all":
        keywords = SECTOR_KEYWORDS.get(args.sector, [args.sector])
        stock_list = [(c, n, s) for c, n, s in BUILTIN_STOCKS if s in keywords]
    else:
        stock_list = BUILTIN_STOCKS

    # 温度过低时限制扫描范围
    if temp_result['temperature'] < 30:
        if not args.quiet:
            print(f"\n⚠️ 市场温度<30，仅扫描优选板块")
        stock_list = [(c, n, s) for c, n, s in stock_list if s in SECTOR_PREFERENCE["prefer"]]
    elif temp_result['temperature'] < 45:
        if not args.quiet:
            print(f"\n🟡 市场偏冷，仅扫描优选+中性板块")
        allowed = SECTOR_PREFERENCE["prefer"] + SECTOR_PREFERENCE["neutral"]
        stock_list = [(c, n, s) for c, n, s in stock_list if s in allowed]

    if not args.quiet:
        print(f"\n📡 ③ 个股扫描 ({len(stock_list)}只) ...")

    de = DataEngine()
    stock_scorer = StockScorer()
    results = []
    all_scanned = []
    skipped_st = 0
    skipped_low_liquidity = 0

    for i, (code, name, fixed_sector) in enumerate(stock_list):
        # 硬过滤：排除创业板(300/301)和科创板(688)
        if code.startswith(('300', '301', '688')):
            continue

        # 智能过滤1：先拉取行情，检查名称中是否含ST/*ST/退市
        quote = de.get_tencent_quote(code)
        stock_name = quote.get("name", name) or ""
        if any(kw in stock_name for kw in ['ST', '*ST', '退市', '退', 'N']):
            skipped_st += 1
            if args.debug:
                print(f"    [过滤] {code} {stock_name}: ST/退市股跳过")
            continue

        # 智能过滤2：获取K线判断流动性
        df = de.get_klines(code)
        if df is None or len(df) < 120:
            if args.debug:
                print(f"    [数据] {code}: K线获取失败或不足({len(df) if df is not None else 0}条)")
            continue
        row = df.iloc[-1]
        vol_col = 'vol' if 'vol' in df.columns else 'volume'
        recent_vol = df[vol_col].tail(20).mean() if len(df) >= 20 else 0
        # 日均成交额 < 3000万 → 流动性不足（垃圾股特征）
        recent_amount = df['amount'].tail(20).mean() if 'amount' in df.columns and len(df) >= 20 else 0
        if recent_amount > 0 and recent_amount < 3000_0000:  # 3000万
            skipped_low_liquidity += 1
            if args.debug:
                print(f"    [过滤] {code} {stock_name}: 日均成交额{recent_amount/10000:.0f}万<3000万, 流动性不足")
            continue

        # 用AutoSectorMapper动态获取板块（替代固定分类）
        dyn_sector, blocks, src = AutoSectorMapper.get_sector(code, quote.get("name", ""))
        sector = dyn_sector if dyn_sector != "其他" else fixed_sector
        if args.debug:
            print(f"    [板块] {code} {quote.get('name','')}: {sector} (来源:{src})")

        sec_score = sector_scores.get(sector, 50)
        score_result = stock_scorer.score(row, sec_score, code)

        # 记录所有扫描过的股票（用于突破观察）
        if score_result:
            short_term = score_result.get("short_term", {})
            short_score = short_term.get("short_score", 0)
            short_reasons = short_term.get("short_reasons", "")
        else:
            short_info = stock_scorer.calc_short_term(row, sec_score)
            short_score = short_info.get("short_score", 0) if short_info else 0
            short_reasons = short_info.get("short_reasons", "") if short_info else ""
        all_scanned.append({
            "code": code, "name": quote.get("name", name) or code,
            "sector": sector, "sector_score": sec_score,
            "short_score": short_score, "short_reasons": short_reasons,
            "price": row["close"], "tier": "无信号",
            "vol_ratio": row["vol_ratio_20"], "atr_ratio": row["atr_ratio"],
        })

        if score_result:
            composite = score_result["composite"]
            # 温度低时提高入选门槛
            if temp_result['temperature'] < 30 and composite < 70:
                continue
            if temp_result['temperature'] < 45 and score_result["tier"] == "🥉 普通层" and composite < 50:
                continue

            results.append({
                "code": code,
                "name": quote.get("name", name) or code,
                "sector": sector,
                "tier": score_result["tier"],
                "tier_score": score_result["tier_score"],
                "sector_score": sec_score,
                "composite": score_result["composite"],
                "surge_score": score_result.get("surge_score", {"score": 0}),
                "ambush_score": score_result.get("ambush_score", 0),
                "gain_60d": row.get("gain_60d", 0),
                "kelly_pct": score_result.get("kelly_pct", 0.15),
                "short_score": score_result.get("short_term", {}).get("short_score", 0),
                "short_reasons": score_result.get("short_term", {}).get("short_reasons", ""),
                "verify": score_result.get("verify", {}),
                "price": row["close"],
                "drawdown": row["drawdown"],
                "vol_ratio": row["vol_ratio_20"],
                "atr_ratio": row["atr_ratio"],
                "amplitude_60": row.get("amplitude_60", 0),
                "ma_spread": row.get("ma_spread", 0),
                "final_sort": score_result.get("final_sort", 0),
                "atr_stop_pct": score_result.get("atr_stop_pct", 0.08),
                "chain_boost": score_result.get("chain_boost", 0),
                "chain_name": score_result.get("chain_name", ""),
            })

        if not args.quiet:
            progress = f"[{i+1}/{len(stock_list)}]"
            status = f"✅ {score_result['tier']} 综合{score_result['composite']}" if score_result else "无信号"
            if score_result and score_result["tier"] in ("💎 精选层", "🥈 增强层"):
                print(f"  {progress} {code} {status}")
        elif args.quiet and score_result:
            print(f"{score_result['tier']} {quote.get('name',code)}({code}) [{sector}] "
                  f"¥{row['close']:.2f} 综合{score_result['composite']}")

        time.sleep(random.uniform(0.1, 0.2))

    # ── 过滤统计 ──
    if (skipped_st > 0 or skipped_low_liquidity > 0) and not args.quiet:
        print(f"\n  [过滤] ST退市: {skipped_st}只 | 流动性不足: {skipped_low_liquidity}只 | 有效扫描: {len(stock_list)-skipped_st-skipped_low_liquidity}只")

    # ── 漏网之鱼分析 ──
    if not args.quiet and stock_list:
        try:
            # 拉同花顺今日强势股
            mq = "Mozilla/5.0"
            today_str = datetime.now().strftime("%Y-%m-%d")
            u = f"http://zx.10jqka.com.cn/event/api/getharden/date/{today_str}/orderby/date/orderway/desc/charset/GBK/"
            rr = requests.get(u, headers={"User-Agent": mq}, timeout=10)
            hot_rows = rr.json().get("data") or []
            if len(hot_rows) < 10:
                for dd in range(1, 5):
                    u2 = f"http://zx.10jqka.com.cn/event/api/getharden/date/{(datetime.now()-timedelta(days=dd)).strftime('%Y-%m-%d')}/orderby/date/orderway/desc/charset/GBK/"
                    rr2 = requests.get(u2, headers={"User-Agent": mq}, timeout=10)
                    hot_rows = rr2.json().get("data") or []
                    if len(hot_rows) >= 10: break
            if hot_rows:
                # 强势股TOP15代码列表
                hot_codes = set()
                for row in hot_rows[:20]:
                    c = row.get("code","")
                    reason = row.get("reason","")
                    if c and len(c) == 6: hot_codes.add(c)
                # 扫描池代码
                pool_codes = {c for c, _, _ in stock_list if not c.startswith(('300','301','688'))}
                signal_codes = {r["code"] for r in results}
                # 在池中但没出信号
                missed = pool_codes & hot_codes - signal_codes
                # 不在池中但强势
                not_in_pool = hot_codes - pool_codes - signal_codes
                # 只保留主板
                not_in_pool = {c for c in not_in_pool if not c.startswith(('300','301','688'))}
                
                if missed or not_in_pool:
                    print(f"\n🔍 漏网之鱼分析")
                    if missed:
                        names_missed = []
                        for c, n, s in stock_list:
                            if c in missed:
                                # 看看差在哪里
                                names_missed.append(n or c)
                        print(f"  📌 池中未出信号但强势: {', '.join(list(missed)[:5])}")
                        print(f"     (在池子里但条件没触发，可能是量比/回撤差一点)")
                    if not_in_pool:
                        ni = list(not_in_pool)[:5]
                        print(f"  🆕 不在池中但连续强势: {', '.join(ni)}")
                        print(f"     (建议关注是否需要补入股票池)")
        except:
            pass

    # ── 排序——按综合排序分降序（综合分×0.35 + 涨停质量×0.2 + 三灯×0.2 + 板块×0.15 + 埋伏×0.1）
    results.sort(key=lambda r: -r.get("final_sort", r["composite"]))

    # ── 保存全量结果用于分析，再过滤出推荐信号 ──
    all_results = list(results)
    # 合并有短线突破信号的非信号股（用于热门板块突破观察）
    result_codes = {r['code'] for r in all_results}
    for s in all_scanned:
        if s['code'] not in result_codes and s['short_score'] >= 50:
            all_results.append(s)
    # 短线信号榜也从all_results取

    old_count = len(results)
    results = portfolio_risk_filter(results, max_total_positions=8)
    if len(results) < old_count:
        print(f"\n  🛡️ [风控] 行业集中度过滤: {old_count} → {len(results)} 个信号")
        print(f"  🛡️  同一行业最多2只，总信号最多8只")

    # ── 输出 ──
    print(f"\n{'='*60}")
    print(f"📊 最终结果")
    print(f"{'='*60}")
    print(f"🌡️ 市场温度: {temp_result['temperature']}/100 ({market_state})")
    print(f"📊 建议仓位: {pos_limit*100:.0f}%")
    print(f"🏆 热门板块: {', '.join(f'{s}({sc})' for s,sc in top_sectors[:3])}")
    print(f"📈 策略信号: {len(results)} 个")
    print()

    # 按层级分组输出
    for tier_name in ["💎 精选层", "🥈 增强层", "🥉 普通层"]:
        tier_results = [r for r in results if r["tier"] == tier_name]
        if not tier_results:
            continue
        print(f"  {tier_name}: {len(tier_results)} 个")
        for r in tier_results:
            sector_tag = f"[{r['sector']}]({r['sector_score']}分)" if r['sector'] else ""
            chain_tag = f" {r.get('chain_tag','')}" if r.get('chain_name') else ""
            v = r.get("verify", {})
            v_tag = f" {v.get('level','')} {v.get('lights','')}" if v else ""
            print(f"    {v_tag} {r['name']}({r['code']}) {sector_tag}{chain_tag} "
                  f"¥{r['price']:.2f} 回撤{r['drawdown']:+.0f}% "
                  f"综合{r['composite']}分")
        print()

    # 建议
    if results:
        # ── 重点观察（按综合排序分取前3名） ──
        top3 = results[:3]
        print("\n🎯 重点观察 TOP 3（综合排序分 | 只看这些）")
        print(f"    {'':12s} {'排序分':>6s} {'层级':>8s} {'板块分':>6s} {'涨停':>5s} {'灯':>3s} {'建议仓位'}")
        print("    " + "-" * 60)
        for r in top3:
            fs = r.get("final_sort", r["composite"])
            s = r.get("surge_score", {})
            ss = s.get("score", 0) if isinstance(s, dict) else s
            v = r.get("verify", {})
            gl = v.get("green", 0) if v else 0
            kp = r.get("kelly_pct", 0.15) * 100
            bar = "█" * (fs // 10) + "░" * (10 - fs // 10)
            print(f"    {r['name']:10s}({r['code']}) {bar} {fs:>3d}分 "
                  f"{r['tier']} {r.get('sector_score',0):>3d}分 "
                  f"{'🟢'*gl}{'🟡'*(3-gl)}  {kp:.0f}%")
            stop_pct = r.get('atr_stop_pct', 0.08) * 100
            print(f"    {'':12s} 止损-{stop_pct:.0f}% | +10%后回撤6%自动走 | 持有≤20日")
        print(f"    💡 只做这3只，其余忽略。今日总仓位上限: {pos_limit*100:.0f}%")
        print()

        # ── 短线信号榜（基于全量信号）──
        short_sigs = sorted(all_results, key=lambda r: -r.get('short_score', 0))[:3]
        if short_sigs and short_sigs[0].get('short_score', 0) >= 50:
            print(f"\n⚡ 短线信号榜 TOP3（持有1-5日）:")
            for r in short_sigs:
                bar = "█" * (r['short_score'] // 10) + "░" * (10 - r['short_score'] // 10)
                reasons = r.get('short_reasons', '')
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}] "
                      f"短线{r['short_score']}分 | {reasons}")
            print(f"  📌 放量突破+MACD金叉+板块共振，短线机会")

        # ── 热门板块突破观察（板块评分≥80的强势突破补充）──
        hot_breakout = []
        for r in all_results:
            if r.get('sector_score', 0) >= 80 and r.get('short_score', 0) >= 60:
                if r['tier'] == "无信号":
                    hot_breakout.append(r)
        if hot_breakout:
            print(f"\n🔥 热门板块突破观察（板块评分≥80 | 强势不回调品种）:")
            for r in sorted(hot_breakout, key=lambda x: -x['short_score'])[:5]:
                reasons = r.get('short_reasons', '')
                print(f"  ⚡ {r['name']}({r['code']}) [{r['sector']}] "
                      f"短线{r['short_score']}分 | {reasons}")
            print(f"  📌 热门板块强势突破，不等回调直接关注")

        # ── 涨停质量分析 TOP3（基于全量信号）──
        surge_top = sorted(all_results, key=lambda r: -r.get('surge_score', {}).get('score', 0))[:3]
        if surge_top and surge_top[0].get('surge_score', {}).get('score', 0) >= 30:
            print(f"\n🔥 涨停质量分析 TOP3")
            print(f"  评级: 🟢高质量板 | 🟡中质量板 | 🔴低质量板（烂板别碰）")
            for r in surge_top:
                s = r.get('surge_score', {})
                sc = s.get('score', 0) if isinstance(s, dict) else s
                bar = "█" * (sc // 10) + "░" * (10 - sc // 10)
                cls = s.get('class', '') if isinstance(s, dict) else ''
                adv = s.get('advice', '') if isinstance(s, dict) else ''
                s_risks = s.get('risks', '') if isinstance(s, dict) else ''
                s_tag = s.get('tag', '') if isinstance(s, dict) else ''
                bad = s.get('bad', '') if isinstance(s, dict) else ''
                tag_str = f" | {s_tag}" if s_tag else ""
                risk_str = f" | ⚠️ {s_risks}" if s_risks else ""
                bad_str = f" | ❌ {bad}" if bad else ""
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}]")
                print(f"     {cls} {sc}分  量比{r['vol_ratio']:.1f}x ATR{r['atr_ratio']:.1f}%{tag_str}{bad_str}")
                print(f"     {s.get('reasons','')}{risk_str}")
                print(f"     💡 {adv}")
            print(f"  ⚠️ 烂板识别比好板识别更重要—避开低质量板")

        # ── 埋伏信号榜（基于全量信号）──
        ambush_top = sorted(all_results, key=lambda r: -r.get('ambush_score', 0))[:3]
        if ambush_top and ambush_top[0].get('ambush_score', 0) >= 60:
            print(f"\n🚀 埋伏信号榜 TOP3（横盘突破候选）:")
            for r in ambush_top:
                amb = r.get('ambush_score', 0)
                bar = "█" * (amb // 10) + "░" * (10 - amb // 10)
                amp_str = f"振幅{r.get('amplitude_60',0):.0f}%" if r.get('amplitude_60',0) else ""
                spread_str = f"粘合{r.get('ma_spread',0):.1f}%" if r.get('ma_spread',0) else ""
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}] "
                      f"{amp_str} {spread_str} 量比{r['vol_ratio']:.1f}x "
                      f"埋伏{amb}分")
            print(f"  📌 横盘充分+均线粘合+放量异动，有可能突破向上")

        avg_stop = sum(r.get('atr_stop_pct', 0.08) for r in results) / len(results) if results else 0.08
        print(f"\n🛑 止损: ATR动态({avg_stop*100:.0f}%均值)  移动止盈: +10%后回撤6%离场  持有期: 20日  总仓位上限: {pos_limit*100:.0f}%")

        # ── 昨日信号追踪（仅早报模式） ──
    if args.mode == "morning":
        tracking = get_yesterday_tracking()
        if tracking:
            wins = sum(1 for t in tracking if t["is_win"])
            total = len(tracking)
            avg_ret = sum(t["change_pct"] for t in tracking) / total
            print(f"\n📊 昨日信号追踪（{yesterday}）:")
            print(f"{'信号':25s} {'推时价':>8s} {'现价':>8s} {'涨跌':>8s} {'盈亏':>6s} {'仓位':>6s}")
            print("-" * 65)
            for t in tracking:
                emoji = "✅" if t["is_win"] else "❌"
                ret_str = f"{t['change_pct']:+.2f}%"
                print(f"  {emoji} {t['tier']} {t['name']:12s} {t['entry']:>8.2f} {t['current']:>8.2f} {ret_str:>8s} {t.get('kelly_pct',0.15)*100:>5.0f}%")
            print(f"  {'─'*60}")
            print(f"  胜率: {wins}/{total} ({wins/total*100:.0f}%) | 平均收益: {avg_ret:+.2f}%")
            if wins/total > 0.6:
                print(f"  ✅ 系统近期表现可靠")
            elif wins/total > 0.4:
                print(f"  🟡 表现正常，继续观察")
            else:
                print(f"  ❌ 近期胜率偏低，建议减少仓位")
            print()
    
    # ── 市场分析解读 ──
        print(f"\n📝 市场分析解读")
        print(f"{'─'*60}")

        # 板块集中度分析（基于全量信号）
        sector_counts = Counter(r['sector'] for r in all_results)
        top_sector = sector_counts.most_common(1)
        if top_sector and top_sector[0][1] >= 3:
            sec_name, sec_count = top_sector[0]
            ratio = sec_count / len(all_results) * 100
            print(f"  🎯 信号集中在【{sec_name}】({sec_count}/{len(all_results)}, {ratio:.0f}%)")
            if ratio > 60:
                print(f"     高度集中，注意板块回调风险，单板块仓位不要超过30%")
            elif ratio > 40:
                print(f"     相对集中，说明当前市场主线清晰，可适度聚焦")
            else:
                print(f"     不算过度集中，分散布局风险可控")
        else:
            print(f"  🎯 信号分散在多个板块，属于结构性机会")

        # 隔夜美股行情
        nvda_chg = get_nvda_change()
        fcx_chg = get_fcx_change()
        if nvda_chg > 2:
            print(f"  🇺🇸 NVDA +{nvda_chg}% → AI算力/半导体板块情绪偏正面")
        elif nvda_chg < -2:
            print(f"  🇺🇸 NVDA {nvda_chg}% → AI算力/半导体板块注意风险")
        elif nvda_chg != 0:
            print(f"  🇺🇸 NVDA {nvda_chg:+.2f}% → 影响不大")
        if abs(fcx_chg) >= 1.5:
            print(f"  🥉 FCX {fcx_chg:+.2f}% → 有色金属板块隔夜偏{'强' if fcx_chg > 0 else '弱'}")

        # 温度计解读
        t = temp_result['temperature']
        if t >= 60:
            print(f"  🌡️ 市场温度{t}分 —— 正常可操作，按三层策略正常建仓")
        elif t >= 45:
            print(f"  🌡️ 市场温度{t}分 —— 偏冷，控制仓位在50%以内")
        elif t >= 30:
            print(f"  🌡️ 市场温度{t}分 —— 低迷，只做精选/增强层信号，仓位不超过30%")
        else:
            print(f"  🌡️ 市场温度{t}分 —— 冰点，暂停策略等待")

        # 信号层级质量分析
        elite_count = sum(1 for r in all_results if r['tier'] == '💎 精选层')
        enhanced_count = sum(1 for r in all_results if r['tier'] == '🥈 增强层')
        normal_count = sum(1 for r in all_results if r['tier'] == '🥉 普通层')
        print(f"  📊 信号质量: 精选{elite_count} / 增强{enhanced_count} / 普通{normal_count}")
        # 三灯验证汇总
        v_levels = Counter(r.get("verify", {}).get("level","") for r in all_results)
        v_high = v_levels.get("🟢 高可信", 0)
        v_mid = v_levels.get("🟡 中等", 0)
        v_low = v_levels.get("🔴 低可信", 0)
        if v_high > 0 or v_mid > 0:
            print(f"  🛡️ 信号可信度: 🟢{v_high} / 🟡{v_mid} / 🔴{v_low}")
        if elite_count >= 1:
            print(f"     有精选层信号！确定性最高的机会，重点关注")
        if enhanced_count >= 2:
            print(f"     增强层信号较多，弹性品种回调机会值得参与")
        if normal_count >= 5 and enhanced_count == 0:
            print(f"     全部是普通层信号，胜率有限，注意控制单票仓位")

        # 趋势判断
        if surge_top and (surge_top[0].get('surge_score', {}) or {}).get('score', 0) >= 50:
            print(f"  ⚡ 存在高潜力品种({surge_top[0]['name']} {(surge_top[0].get('surge_score', {}) or {}).get('score', 0)}分)")
            print(f"     放量充分+弹性好，若明日开盘不跳空可关注")

        # 操作建议总结
        best = results[0] if results else None
        if best:
            print(f"\n  💡 重点关注: {best['name']}({best['code']}) [{best['sector']}]")
            print(f"     {best['tier']} 综合{best['composite']}分 | 涨停潜力{(best.get('surge_score', {}) or {}).get('score', 0)}分")
            print(f"     ⏰ 明日观察: 是否高开>3%则等回调，平开/低开可关注")

        print(f"{'─'*60}")
        print(f"  ⚠️ 以上分析仅供参考，不构成投资建议")
        print(f"  ⚠️ 严格执行-8%止损")

    print(f"{'='*60}")

    # ── 保存信号日志 ──
    if results:
        record_today_signals(results, args.mode)

    # ── 自学习：回检历史信号 ──
    try:
        from self_learn import SelfLearnEngine
        sle = SelfLearnEngine()
        sle.check_signals()
    except Exception as e:
        if args.debug:
            print(f"  [自学习] 跳过: {e}")

    # ── 微信推送 ──
    if args.wechat and results:
        notifier = WeChatNotifier()
        market_info = {
            "temperature": temp_result['temperature'],
            "state": market_state,
            "position_limit": pos_limit,
        }
        sector_info = {"top": top_sectors}
        hot_breakout_list = locals().get('hot_breakout', []) or []
        notifier.notify(results, market_info, sector_info, mode=args.mode,
                        all_results=all_results, hot_breakout=hot_breakout_list,
                        force_push=getattr(args, 'force_push', False))


if __name__ == "__main__":
    main()
