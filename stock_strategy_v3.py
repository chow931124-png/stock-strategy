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
                up = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) > 0)
                down = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) < 0)
                total = len(rows)
                valid = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) != 0)
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

        self.cache = results
        return results

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
    def __init__(self):
        self.client = None
        self._call_count = 0
        self._connect()

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
        """获取K线，mootdx优先，失败自动切HTTP备用"""
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
CACHE_DIR = Path("/app/cache") if Path("/app/cache").exists() else Path(__file__).parent / "cache"
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
    """个股三层评分"""

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

    def calc_surge_potential(self, row: pd.Series, sector_score: int) -> int:
        """
        大涨潜力评分（0-100）—— 涨停/大涨5%+的概率估算
        因子：
        - 量比 (放量程度) 25% — 资金关注度
        - ATR (弹性) 25% — 能涨多猛
        - 板块热度 20% — 热点板块容易出涨停
        - 回撤修复形态 15% — 深跌反弹力度
        - 当日涨幅 15% — 已经在涨了
        """
        score = 0

        # ① 量比评分（0-25）
        vr = row['vol_ratio_20']
        if pd.notna(vr):
            if vr >= 3.0: score += 25
            elif vr >= 2.5: score += 22
            elif vr >= 2.0: score += 18
            elif vr >= 1.5: score += 12
            elif vr >= 1.0: score += 8
            else: score += 3

        # ② ATR弹性评分（0-25）
        atr = row['atr_ratio']
        if pd.notna(atr):
            if atr >= 8: score += 25
            elif atr >= 6: score += 20
            elif atr >= 5: score += 16
            elif atr >= 4: score += 12
            elif atr >= 3: score += 8
            else: score += 3

        # ③ 板块热度评分（0-20）
        if sector_score >= 80: score += 20
        elif sector_score >= 70: score += 16
        elif sector_score >= 60: score += 12
        elif sector_score >= 50: score += 8
        else: score += 4

        # ④ 回撤修复形态（0-15）深跌后放量反弹=强信号
        dd = row['drawdown']
        if pd.notna(dd):
            dd_depth = abs(dd)
            if dd_depth >= 25 and pd.notna(vr) and vr >= 2.0:
                score += 15  # 深跌+巨量=最强反弹信号
            elif dd_depth >= 20:
                score += 12
            elif dd_depth >= 15:
                score += 9
            elif dd_depth >= 10:
                score += 6
            else:
                score += 3

        # ⑤ 当日涨幅势头（0-15）
        chg = row['change_pct']
        if pd.notna(chg):
            if chg >= 5: score += 15  # 已经在拉
            elif chg >= 3: score += 12
            elif chg >= 1: score += 8
            elif chg >= 0: score += 4

        # 加权修正：如果量比极低，即使其他条件好也不容易涨停
        if pd.notna(vr) and vr < 1.0:
            score = int(score * 0.6)

        return min(score, 100)

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

    def calc_kelly_position(self, row: pd.Series, tier_score: int) -> float:
        """
        凯利公式计算仓位比例
        f* = (p * b - q) / b
        p = 胜率, q = 1-p, b = 盈亏比(平均盈利/平均亏损)
        """
        tier_cfg = {55: (0.55, 1.5), 69: (0.69, 1.8), 90: (0.90, 2.0)}
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

    def score(self, row: pd.Series, sector_score: int) -> dict:
        """返回个股评分 + 综合评分"""
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

        return {
            "tier": tier,
            "tier_score": tier_score,
            "sector_score": sector_score,
            "composite": composite,
            "sector_weight": sector_weight,
            "surge_score": self.calc_surge_potential(row, sector_score),
            "ambush_score": self.calc_ambush(row, sector_score),
            "verify": self.verify_signal(row, sector_score),
            "kelly_pct": self.calc_kelly_position(row, tier_score),
            "short_term": self.calc_short_term(row, sector_score),
        }


# ════════════════════════════════════════════════════════
# 模块5：微信通知器
# ════════════════════════════════════════════════════════
class WeChatNotifier:
    def __init__(self):
        self.webhook = NOTIFY_CONFIG["wechat_webhook"]
        self.sckey = NOTIFY_CONFIG["serverchan_key"]
        self.dingtalk = NOTIFY_CONFIG["dingtalk_webhook"]

    def _should_push(self) -> bool:
        now = datetime.now()
        start, end = NOTIFY_CONFIG["push_window"]
        if not (start <= now.hour < end): return False
        if now.weekday() >= 5: return False
        return True

    def notify(self, results: list, market_info: dict, sector_info: dict, mode: str = ""):
        if not results:
            print("  [通知] 无信号，跳过")
            return
        if not self._should_push():
            print("  [通知] 非推送时段，跳过")
            return

        mode_label = {"morning": "早报", "lunch": "午盘", "close": "收盘", "night": "复盘"}.get(mode, "")

        lines = []
        temp = market_info.get("temperature", 50)
        state = market_info.get("state", "")

        # ── 头部 ──
        lines.append(f"## {mode_label}A股策略 {datetime.now().strftime('%m/%d %H:%M')}")
        lines.append(f"**🌡️ 温度**: {temp}/100 {state}　**📊 仓位**: {market_info.get('position_limit',1)*100:.0f}%")
        lines.append("")

        # ── 板块TOP ──
        if sector_info.get("top"):
            lines.append("**🏆 热门板块**")
            for s, sc in sector_info["top"][:3]:
                bar = "█" * (sc // 10) + "░" * (10 - sc // 10)
                lines.append(f"> {s} {bar} {sc}分")
            lines.append("")

        # ── 信号概览 ──
        tiers_count = Counter(r["tier"] for r in results)
        elite_n = tiers_count.get("💎 精选层", 0)
        enhanced_n = tiers_count.get("🥈 增强层", 0)
        normal_n = tiers_count.get("🥉 普通层", 0)
        v_levels = Counter(r.get("verify",{}).get("level","") for r in results)
        v_high = v_levels.get("🟢 高可信", 0)
        v_mid = v_levels.get("🟡 中等", 0)

        parts = []
        if elite_n: parts.append(f"💎{elite_n}")
        if enhanced_n: parts.append(f"🥈{enhanced_n}")
        parts.append(f"🥉{normal_n}")
        v_tag = f"{'🟢'*v_high}{'🟡'*v_mid}" if v_high or v_mid else ""
        lines.append(f"**📈 信号**: {'+'.join(parts)}={len(results)}个　**🛡️** {v_tag}")
        lines.append("")

        # ── 精选层/增强层详情 ──
        for r in results:
            if r["tier"] in ("💎 精选层", "🥈 增强层"):
                v = r.get("verify", {})
                v_tag_md = f" `{v.get('level','')}`" if v and v.get('level') else ""
                sec_tag = f"`{r['sector']}`" if r.get('sector') else ""
                details = f"¥{r['price']} 回撤{r['drawdown']:+.0f}% 量比{r['vol_ratio']:.1f}x 综合{r['composite']}分"
                lines.append(
                    f"- {r['tier']} **{r['name']}({r['code']})** {sec_tag}{v_tag_md}  \n  {details}"
                )

        if normal_n > 0:
            for r in results:
                if r["tier"] == "🥉 普通层":
                    v = r.get("verify", {})
                    v_tag = f"`{v.get('level','')}`" if v and v.get('level') else ""
                    sec_tag = f"`{r['sector']}`" if r.get('sector') else ""
                    lines.append(
                        f"- 🥉 **{r['name']}({r['code']})** {sec_tag}{v_tag} "
                        f"¥{r['price']} 回撤{r['drawdown']:+.0f}%"
                    )

        # ── 涨停潜力榜 ──
        surge_top = sorted(results, key=lambda r: -r.get('surge_score', 0))[:3]
        if surge_top and surge_top[0].get('surge_score', 0) >= 30:
            lines.append("")
            lines.append("**🔥 涨停潜力榜**")
            for r in surge_top:
                lines.append(f"> {r['name']}({r['code']}) 量比{r.get('vol_ratio',0):.1f}x 潜力{r.get('surge_score',0)}分")

        # ── 埋伏信号榜 ──
        ambush_top = sorted(results, key=lambda r: -r.get('ambush_score', 0))[:3]
        if ambush_top and ambush_top[0].get('ambush_score', 0) >= 60:
            lines.append("")
            lines.append("**🚀 埋伏信号榜**")
            for r in ambush_top:
                lines.append(f"> {r['name']}({r['code']}) {r['sector']} 振幅{r.get('amplitude_60',0):.0f}% 埋伏{r.get('ambush_score',0)}分")

        # ── 昨日信号追踪（仅早报） ──
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

        # ── 模式专属建议 ──
        lines.append("")
        if mode == "lunch":
            lines.append("**📌 下午操作**")
            lines.append("1. 精选/增强层可入场")
            lines.append("2. 普通层等尾盘确认")
            lines.append("3. 冲高不追，等回调")
        elif mode == "close":
            lines.append("**📌 收盘总结**")
            lines.append("1. 今日信号明日可操作")
            lines.append("2. 龙虎榜数据更新中")
            lines.append("3. 明早关注新催化")
        elif mode == "morning":
            lines.append("**📌 开盘前参考**")
            lines.append("1. 隔夜美股NVDA/FCX已收盘")
            lines.append("2. 昨日信号仍有效，开盘观察")
            lines.append("3. 今日关注板块: 科技/AI/有色")
        elif mode == "night":
            lines.append("**📌 明日计划**")
            lines.append("1. 精选/增强层开盘观察")
            lines.append("2. 高开>3%等回调")
            lines.append("3. 止损设在-8%")

        lines.append("")
        lines.append(f"---")
        lines.append(f"**🛑 仓位**: {market_info.get('position_limit',1)*100:.0f}% | **止损**: -8% | 仅供参考")

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
    parser.add_argument("--mode", type=str, default="",
                        help="推送模式: morning(早报)/lunch(午盘)/close(收盘)/night(复盘)")
    args = parser.parse_args()

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
        score_result = stock_scorer.score(row, sec_score)

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
                "surge_score": score_result.get("surge_score", 0),
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
            })

        if not args.quiet and not args.quiet:
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

    # ── 排序——按综合分降序 ──
    results.sort(key=lambda r: -r["composite"])

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
            v = r.get("verify", {})
            v_tag = f" {v.get('level','')} {v.get('lights','')}" if v else ""
            print(f"    {v_tag} {r['name']}({r['code']}) {sector_tag} "
                  f"¥{r['price']:.2f} 回撤{r['drawdown']:+.0f}% "
                  f"综合{r['composite']}分")
        print()

    # 建议
    if results:
        print("📋 操作建议:")
        for r in results[:5]:
            kp = r.get("kelly_pct", 0.15) * 100
            pos_label = "轻仓" if kp <= 15 else ("中仓" if kp <= 30 else "重仓")
            print(f"  {r['name']}({r['code']}): {r['tier']} 综合{r['composite']}分 → {pos_label}({kp:.0f}%)")

        # ── 短线信号榜 ──
        short_sigs = sorted(results, key=lambda r: -r.get('short_score', 0))[:3]
        if short_sigs and short_sigs[0].get('short_score', 0) >= 50:
            print(f"\n⚡ 短线信号榜 TOP3（持有1-5日）:")
            for r in short_sigs:
                bar = "█" * (r['short_score'] // 10) + "░" * (10 - r['short_score'] // 10)
                reasons = r.get('short_reasons', '')
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}] "
                      f"短线{r['short_score']}分 | {reasons}")
            print(f"  📌 放量突破+MACD金叉+板块共振，短线机会")

        # ── 涨停潜力榜 ──
        surge_top = sorted(results, key=lambda r: -r['surge_score'])[:3]
        if surge_top and surge_top[0]['surge_score'] >= 30:
            print(f"\n🔥 涨停潜力榜 TOP3（大涨概率估算）:")
            for r in surge_top:
                bar = "█" * (r['surge_score'] // 10) + "░" * (10 - r['surge_score'] // 10)
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}] "
                      f"量比{r['vol_ratio']:.1f}x ATR{r['atr_ratio']:.1f}% "
                      f"潜力{r['surge_score']}分")
            print(f"  ⚠️ 仅供参考，涨停预测准确率有限")

        # ── 埋伏信号榜 ──
        ambush_top = sorted(results, key=lambda r: -r.get('ambush_score', 0))[:3]
        if ambush_top and ambush_top[0].get('ambush_score', 0) >= 60:
            print(f"\n🚀 埋伏信号榜 TOP3（横盘突破候选）:")
            for r in ambush_top:
                bar = "█" * (r['ambush_score'] // 10) + "░" * (10 - r['ambush_score'] // 10)
                amp_str = f"振幅{r.get('amplitude_60',0):.0f}%" if r.get('amplitude_60',0) else ""
                spread_str = f"粘合{r.get('ma_spread',0):.1f}%" if r.get('ma_spread',0) else ""
                print(f"  {bar} {r['name']}({r['code']}) [{r['sector']}] "
                      f"{amp_str} {spread_str} 量比{r['vol_ratio']:.1f}x "
                      f"埋伏{r['ambush_score']}分")
            print(f"  📌 横盘充分+均线粘合+放量异动，有可能突破向上")

        print(f"\n🛑 止损: -8%  持有期: 20日  总仓位上限: {pos_limit*100:.0f}%")

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

        # 板块集中度分析
        sector_counts = Counter(r['sector'] for r in results)
        top_sector = sector_counts.most_common(1)
        if top_sector and top_sector[0][1] >= 3:
            sec_name, sec_count = top_sector[0]
            ratio = sec_count / len(results) * 100
            print(f"  🎯 信号集中在【{sec_name}】({sec_count}/{len(results)}, {ratio:.0f}%)")
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
        elite_count = sum(1 for r in results if r['tier'] == '💎 精选层')
        enhanced_count = sum(1 for r in results if r['tier'] == '🥈 增强层')
        normal_count = sum(1 for r in results if r['tier'] == '🥉 普通层')
        print(f"  📊 信号质量: 精选{elite_count} / 增强{enhanced_count} / 普通{normal_count}")
        # 三灯验证汇总
        v_levels = Counter(r.get("verify", {}).get("level","") for r in results)
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
        if surge_top and surge_top[0]['surge_score'] >= 50:
            print(f"  ⚡ 存在高潜力品种({surge_top[0]['name']} {surge_top[0]['surge_score']}分)")
            print(f"     放量充分+弹性好，若明日开盘不跳空可关注")

        # 操作建议总结
        best = results[0] if results else None
        if best:
            print(f"\n  💡 重点关注: {best['name']}({best['code']}) [{best['sector']}]")
            print(f"     {best['tier']} 综合{best['composite']}分 | 涨停潜力{best['surge_score']}分")
            print(f"     ⏰ 明日观察: 是否高开>3%则等回调，平开/低开可关注")

        print(f"{'─'*60}")
        print(f"  ⚠️ 以上分析仅供参考，不构成投资建议")
        print(f"  ⚠️ 严格执行-8%止损")

    print(f"{'='*60}")

    # ── 保存信号日志 ──
    if results:
        record_today_signals(results, args.mode)

    # ── 微信推送 ──
    if args.wechat and results:
        notifier = WeChatNotifier()
        market_info = {
            "temperature": temp_result['temperature'],
            "state": market_state,
            "position_limit": pos_limit,
        }
        sector_info = {"top": top_sectors}
        notifier.notify(results, market_info, sector_info, mode=args.mode)


if __name__ == "__main__":
    main()
