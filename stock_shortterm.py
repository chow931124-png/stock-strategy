#!/usr/bin/env python3
"""
A股短线交易系统 v1.0 —— 5日持有 + 自学习引擎
=============================================
独立系统，与 stock_strategy_v3.py 互不干扰。

3类短线信号 + 5维自学习引擎，越用越准。

使用方式:
  python3 stock_shortterm.py                 # 扫描
  python3 stock_shortterm.py --wechat        # 扫描+推送
  python3 stock_shortterm.py --learn         # 只看自学习报告
  python3 stock_shortterm.py --review        # 回检昨日信号

数据源: mootdx(通达信) + 同花顺热点 + 腾讯财经 + Yahoo Finance
"""

import os, sys, time, random, json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

import numpy as np
import pandas as pd
import requests
from mootdx.quotes import Quotes

# ════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════

NOTIFY_CONFIG = {
    "serverchan_key": os.environ.get("SERVERCHAN_KEY", ""),
    "dingtalk_webhook": os.environ.get("DINGTALK_WEBHOOK", ""),
}

# 缓存目录（NAS持久化用 /app/cache，本地用 ./cache）
CACHE_DIR = Path("/app/cache") if Path("/app/cache").exists() else Path(__file__).parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 自学习数据文件
LEARN_DATA_PATH = CACHE_DIR / "self_learn.json"
SIGNAL_LOG_PATH = CACHE_DIR / "short_signal_log.json"

# 股票池（与v3共用）
BUILTIN_STOCKS = [
    ("002463","沪电股份","AI"),("000063","中兴通讯","AI"),("002415","海康威视","AI"),
    ("600498","烽火通信","AI"),("000636","风华高科","AI"),("002230","科大讯飞","AI"),
    ("002371","北方华创","半导体"),("603501","韦尔股份","半导体"),
    ("002409","雅克科技","半导体"),("002475","立讯精密","半导体"),
    ("002594","比亚迪","新能源"),("002850","科达利","新能源"),
    ("002074","国轩高科","新能源"),("002050","三花智控","新能源"),
    ("600309","万华化学","化工"),("600989","宝丰能源","化工"),
    ("000830","鲁西化工","化工"),("600096","云天化","化工"),
    ("603260","合盛硅业","化工"),("000792","盐湖股份","化工"),("000408","藏格矿业","化工"),
    ("601899","紫金矿业","有色"),("601600","中国铝业","有色"),
    ("603993","洛阳钼业","有色"),("600497","驰宏锌锗","有色"),
    ("002155","湖南黄金","有色"),("000960","锡业股份","有色"),
    ("002428","云南锗业","有色"),("000688","国城矿业","有色"),
    ("600760","中航沈飞","军工"),("600893","航发动力","军工"),
    ("002179","中航光电","军工"),("600879","航天电子","军工"),
    ("600519","贵州茅台","消费"),("000858","五粮液","消费"),
    ("600036","招商银行","金融"),("601398","工商银行","金融"),
    ("600438","通威股份","储能"),("601012","隆基绿能","储能"),("002459","晶澳科技","储能"),
    ("002422","科伦药业","医药"),("603259","药明康德","医药"),
    ("601137","博威合金","成长"),("000032","深桑达A","成长"),
]


# ════════════════════════════════════════════════════════
# 数据引擎
# ════════════════════════════════════════════════════════

class DataEngine:
    def __init__(self):
        self.client = None
        self._connect()

    def _connect(self):
        for i in range(2):
            try:
                self.client = Quotes.factory(market='std')
                return True
            except:
                self.client = None
                time.sleep(0.5)
        return False

    def get_klines(self, code: str) -> Optional[pd.DataFrame]:
        for attempt in range(2):
            try:
                if self.client is None:
                    if not self._connect():
                        continue
                df = self.client.bars(symbol=code, category=4, offset=500)
                if df is None or df.empty:
                    continue
                df = df.copy()
                if 'datetime' in df.columns:
                    df['date'] = pd.to_datetime(df['datetime'])
                else:
                    df['date'] = df.index
                vc = 'vol' if 'vol' in df.columns else 'volume'
                df = df.sort_values('date').reset_index(drop=True)
                for n in [5, 10, 20, 60]:
                    df[f'ma{n}'] = df['close'].rolling(n).mean()
                df['vol_ma20'] = df[vc].rolling(20).mean()
                df['vol_ratio'] = df[vc] / df['vol_ma20'].replace(0, np.nan)
                df['pk60'] = df['close'].rolling(60).max()
                df['dd'] = (df['close'] - df['pk60']) / df['pk60'] * 100
                df['bias20'] = (df['close'] - df['ma20']) / df['ma20'] * 100
                df['tr'] = np.maximum(df['high']-df['low'], np.maximum(
                    abs(df['high']-df['close'].shift(1)),
                    abs(df['low']-df['close'].shift(1))))
                df['atr'] = df['tr'].rolling(14).mean() / df['close'] * 100
                df['chg'] = df['close'].pct_change() * 100
                df['s3'] = df['close'].pct_change(3) * 100  # 3日强度
                e12 = df['close'].ewm(span=12).mean()
                e26 = df['close'].ewm(span=26).mean()
                df['macd'] = e12 - e26
                df['macd_sig'] = df['macd'].ewm(span=9).mean()
                df['macd_h'] = df['macd'] - df['macd_sig']
                # 未来收益（用于回检）
                df['r5'] = df['close'].pct_change(5).shift(-5) * 100
                df['r10'] = df['close'].pct_change(10).shift(-10) * 100
                if len(df) >= 2 and df.iloc[-1][vc] < df[vc].tail(20).mean() * 0.05:
                    df = df.iloc[:-1]
                return df
            except:
                if attempt == 1:
                    return None
                time.sleep(0.5)
        return None

    def get_tencent_quote(self, code: str) -> dict:
        prefix = "sh" if code.startswith(("6","9")) else "sz"
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}",
                             headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
            vals = r.content.decode("gbk").split('"')[1].split("~")
            return {"name": vals[1], "price": float(vals[3]),
                    "pe": float(vals[39]) if vals[39] else None}
        except:
            return {"name": code, "price": 0, "pe": None}


# ════════════════════════════════════════════════════════
# 短线信号引擎（3类信号）
# ════════════════════════════════════════════════════════

class ShortTermSignals:
    """3类短线信号，每类独立评分"""

    # 信号A: 放量突破（动量型）
    def signal_breakout(self, row: pd.Series) -> dict:
        score = 0
        reasons = []
        v = row.get('vol_ratio', 0)
        c = row.get('close', 0)
        ma10 = row.get('ma10', 0)
        s3 = row.get('s3', 0)
        if pd.isna(v) or pd.isna(c) or pd.isna(ma10):
            return {"score": 0, "type": "", "reasons": ""}
        # 量比
        if v >= 1.5:
            score += 30; reasons.append("放量")
        elif v >= 1.3:
            score += 20; reasons.append("小幅放量")
        elif v >= 1.0:
            score += 10
        # 站上MA10
        if c > ma10:
            score += 25; reasons.append("站上MA10")
        # 3日强度
        if pd.notna(s3):
            if s3 >= 3: score += 25; reasons.append("强势上攻")
            elif s3 >= 1: score += 15; reasons.append("走强")
            elif s3 >= 0: score += 5
        # MACD辅助
        macd = row.get('macd', 0)
        macd_sig = row.get('macd_sig', 0)
        if pd.notna(macd) and pd.notna(macd_sig) and macd > 0 and macd > macd_sig:
            score += 20; reasons.append("MACD金叉")
        if not reasons:
            return {"score": 0, "type": "", "reasons": ""}
        return {"score": min(score, 100), "type": "放量突破", "reasons": "|".join(reasons[:3])}

    # 信号B: 强势回调（回调低吸的短线版）
    def signal_pullback(self, row: pd.Series) -> dict:
        score = 0
        reasons = []
        dd = row.get('dd', 0)
        v = row.get('vol_ratio', 0)
        c = row.get('close', 0)
        ma5 = row.get('ma5', 0)
        if pd.isna(dd) or pd.isna(c):
            return {"score": 0, "type": "", "reasons": ""}
        # 浅回调5-15%
        if pd.notna(dd) and dd < 0:
            dd_depth = abs(dd)
            if 5 <= dd_depth <= 15:
                score += 30; reasons.append(f"浅回调{dd_depth:.0f}%")
            elif 15 < dd_depth <= 25:
                score += 20; reasons.append(f"回调{dd_depth:.0f}%")
            elif dd_depth > 25:
                score += 10; reasons.append("深回调风险")
            else:
                return {"score": 0, "type": "", "reasons": ""}
        else:
            return {"score": 0, "type": "", "reasons": ""}
        # 放量
        if pd.notna(v):
            if v >= 1.5: score += 25; reasons.append("放量")
            elif v >= 1.3: score += 18; reasons.append("小幅放量")
            elif v >= 1.0: score += 10
        # 站回MA5
        if pd.notna(ma5) and c > ma5:
            score += 25; reasons.append("站回MA5")
        # ATR弹性
        atr = row.get('atr', 0)
        if pd.notna(atr) and atr >= 4:
            score += 20; reasons.append("弹性充足")
        return {"score": min(score, 100), "type": "强势回调", "reasons": "|".join(reasons[:3])}

    # 信号C: MACD确认（短线动能源）
    def signal_macd(self, row: pd.Series) -> dict:
        score = 0
        reasons = []
        macd = row.get('macd', 0)
        macd_sig = row.get('macd_sig', 0)
        macd_h = row.get('macd_h', 0)
        v = row.get('vol_ratio', 0)
        c = row.get('close', 0)
        ma20 = row.get('ma20', 0)
        if pd.isna(macd) or pd.isna(macd_sig):
            return {"score": 0, "type": "", "reasons": ""}
        # MACD水上金叉（最强）
        if macd > 0 and macd > macd_sig and pd.notna(macd_h) and macd_h > 0:
            score += 35; reasons.append("水上金叉")
        elif macd > 0:
            score += 15; reasons.append("MACD多头")
        elif macd > macd_sig and macd_h > 0:
            score += 20; reasons.append("水下金叉启动")
        else:
            return {"score": 0, "type": "", "reasons": ""}
        # 站上MA20
        if pd.notna(c) and pd.notna(ma20) and c > ma20:
            score += 25; reasons.append("站上MA20")
        # 量比
        if pd.notna(v):
            if v >= 1.3: score += 20; reasons.append("放量")
            elif v >= 1.0: score += 10
        # 近3日方向
        s3 = row.get('s3', 0)
        if pd.notna(s3) and s3 > 0:
            score += 20; reasons.append("方向向上")
        return {"score": min(score, 100), "type": "MACD确认", "reasons": "|".join(reasons[:3])}

    def score_all(self, row: pd.Series) -> dict:
        """三类信号同时打分，取最优"""
        results = [
            self.signal_breakout(row),
            self.signal_pullback(row),
            self.signal_macd(row),
        ]
        best = max(results, key=lambda r: r["score"])
        if best["score"] < 30:
            return None
        # 可信度评估
        confident = "🟢 高" if best["score"] >= 70 else ("🟡 中" if best["score"] >= 50 else "🔴 低")
        return {
            "score": best["score"],
            "type": best["type"],
            "reasons": best["reasons"],
            "confident": confident,
        }


# ════════════════════════════════════════════════════════
# 自学习引擎（5维学习器）
# ════════════════════════════════════════════════════════

class SelfLearningEngine:
    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        if LEARN_DATA_PATH.exists():
            try:
                return json.loads(LEARN_DATA_PATH.read_text())
            except: pass
        return {
            "stocks": {},       # 个股记忆体
            "params": {         # 参数优化器
                "vol_min": 1.3, "atr_min": 3.0, "atr_max": 7.0,
                "best_hold_days": 5, "last_optimized": "",
            },
            "market_env": [],   # 环境匹配器
            "combos": {},       # 组合挖掘
            "updated_at": "",
        }

    def save(self):
        self.data["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        LEARN_DATA_PATH.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    # ── 学习器①：个股记忆体 ──
    def record_signal(self, code: str, name: str, signal: dict, row: pd.Series):
        """记录本次信号的完整快照"""
        now = datetime.now().strftime("%Y-%m-%d")
        stock = self.data["stocks"].setdefault(code, {
            "name": name, "signals": [], "stats_5d": {"total": 0, "wins": 0},
            "stats_10d": {"total": 0, "wins": 0},
        })
        stock["signals"].append({
            "date": now,
            "score": signal["score"],
            "type": signal["type"],
            "price": float(row["close"]) if pd.notna(row["close"]) else 0,
            "check_5d": None,  # 5天后回检
            "check_10d": None,
        })
        # 只保留最近50次
        if len(stock["signals"]) > 50:
            stock["signals"] = stock["signals"][-50:]

    # ── 学习器②：参数优化器 ──
    def optimize_params(self, recent_signals: list):
        """用近期信号自动调优参数"""
        if len(recent_signals) < 15:
            return  # 数据不够，不调优
        params = self.data["params"]
        # 按不同量比阈值分组
        vol_groups = defaultdict(list)
        for s in recent_signals:
            if s["vol_ratio"] > 0:
                for vt in [1.0, 1.3, 1.5, 2.0]:
                    if s["vol_ratio"] >= vt:
                        vol_groups[vt].append(s)
        best_vol = params["vol_min"]
        best_wr = 0
        for vt, ss in vol_groups.items():
            if len(ss) >= 10:
                wr = sum(1 for s in ss if s.get("r5_result") and s["r5_result"] > 0) / len(ss)
                if wr > best_wr:
                    best_wr = wr; best_vol = vt
        if best_vol != params["vol_min"]:
            params["vol_min"] = best_vol
            params["last_optimized"] = f"量比{best_vol}"

    # ── 学习器③：市场环境匹配器 ──
    def record_market_env(self, temperature: int, signals_result: list):
        """记录不同市场温度下的信号表现"""
        record = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "temp": temperature,
            "signal_count": len([s for s in signals_result if s]),
            "best_score": max([s.get("score", 0) for s in signals_result if s] or [0]),
        }
        self.data["market_env"].append(record)
        if len(self.data["market_env"]) > 100:
            self.data["market_env"] = self.data["market_env"][-100:]

    # ── 学习器④：衰减遗忘（在get_stock_stats中隐式应用）───
    def _weight(self, days_ago: int) -> float:
        """时间衰减权重"""
        if days_ago <= 30: return 1.0
        elif days_ago <= 90: return 0.6
        else: return 0.3

    # ── 学习器⑤：组合挖掘 ──
    def mine_combos(self, recent_signals: list):
        """自动发现什么组合最有效"""
        combo_groups = defaultdict(list)
        for s in recent_signals:
            if not s.get("r5_result"): continue
            # 生成各种组合标签
            tags = []
            if s.get("has_breakout"): tags.append("放量突破")
            if s.get("has_macd"): tags.append("MACD金叉")
            if s.get("has_pullback"): tags.append("强势回调")
            if s.get("sector_hot"): tags.append("板块火热")
            # 单标签
            for t in tags:
                combo_groups[t].append(s["r5_result"])
            # 双标签组合
            if len(tags) >= 2:
                key = "+".join(tags[:2])
                combo_groups[key].append(s["r5_result"])
        for key, results in combo_groups.items():
            if len(results) >= 5:
                wr = sum(1 for r in results if r > 0) / len(results)
                avg = np.mean(results)
                old = self.data["combos"].get(key, {})
                # 平滑更新
                if old.get("count", 0) > 0:
                    wr = (wr + old["wr"]) / 2
                    avg = (avg + old["avg"]) / 2
                self.data["combos"][key] = {"wr": round(wr, 2), "avg": round(avg, 2), "count": len(results)}

    # ── 获取个股历史统计 ──
    def get_stock_stats(self, code: str) -> dict:
        """获取个股记忆体中的统计信息"""
        stock = self.data["stocks"].get(code)
        if not stock:
            return {"total": 0, "wins": 0, "wr": 0, "best_hold": 5, "trend": "🟡 无数据"}
        sigs = stock["signals"]
        # 加权统计（时间衰减）
        total_5d = 0; wins_5d = 0; total_10d = 0; wins_10d = 0
        for s in sigs:
            if not s.get("check_5d"): continue
            days_ago = (datetime.now() - datetime.strptime(s["date"], "%Y-%m-%d")).days
            w = self._weight(days_ago)
            result_5d = s["check_5d"] > 0
            total_5d += w; wins_5d += w if result_5d else 0
        if total_5d > 0: wr_5d = wins_5d / total_5d
        else: wr_5d = 0.5
        # 判断性格
        wr_5d_pct = wr_5d * 100
        if wr_5d_pct >= 65: personality = "爆发型 ⚡"
        elif wr_5d_pct >= 50: personality = "稳健型 ✅"
        else: personality = "乏力型 ❌"
        # 近3次趋势
        recent = [s for s in sigs if s.get("check_5d")][-3:]
        if len(recent) >= 3:
            trend_vals = [s["check_5d"] for s in recent]
            trend = "🟢 持续走强" if trend_vals[-1] > trend_vals[0] else "🔴 逐步走弱"
        else:
            trend = "🟡 数据积累中"
        return {
            "total": sum(1 for s in sigs if s.get("check_5d")),
            "wins": sum(1 for s in sigs if isinstance(s.get("check_5d"), (int, float)) and s["check_5d"] > 0),
            "wr": round(wr_5d_pct, 1),
            "best_hold": 5,
            "personality": personality,
            "trend": trend,
        }

    # ── 回检5日收益 ──
    def review_yesterday(self, df_dict: dict) -> list:
        """检查昨日信号的今日表现"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        reviews = []
        for code, stock in self.data["stocks"].items():
            for s in stock["signals"]:
                if s.get("check_5d") is not None:
                    continue
                signal_date = s["date"]
                days_passed = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(signal_date, "%Y-%m-%d")).days
                if days_passed >= 5 and code in df_dict:
                    df = df_dict[code]
                    # 找到信号日的索引
                    signal_idx = None
                    for i, row in df.iterrows():
                        if str(row['date'].date()) == signal_date:
                            signal_idx = i; break
                    if signal_idx is not None:
                        end_idx = min(signal_idx + 5, len(df) - 1)
                        entry = df.iloc[signal_idx]['close']
                        exit_p = df.iloc[end_idx]['close']
                        ret = (exit_p - entry) / entry * 100
                        s["check_5d"] = ret
                        s["check_10d"] = None
                        reviews.append({"code": code, "name": stock["name"], "date": signal_date, "ret": round(ret, 2)})
        if reviews:
            self.save()
        return reviews

    # ── 生成自学习报告 ──
    def generate_report(self) -> list:
        """生成自学习报告文本"""
        lines = []
        # 组合发现
        good_combos = {k: v for k, v in self.data["combos"].items() if v["wr"] > 0.55 and v["count"] >= 5}
        if good_combos:
            best = max(good_combos, key=lambda k: good_combos[k]["wr"])
            lines.append(f"🧠 最佳组合: {best} 胜率{good_combos[best]['wr']*100:.0f}%({good_combos[best]['count']}次)")
        # 参数优化
        params = self.data["params"]
        if params.get("last_optimized"):
            lines.append(f"🔧 参数已优化: {params['last_optimized']}")
        # 环境匹配
        env = self.data["market_env"]
        if len(env) >= 5:
            recent = env[-5:]
            avg_temp = sum(e["temp"] for e in recent) / len(recent)
            avg_count = sum(e["signal_count"] for e in recent) / len(recent)
            lines.append(f"🌡️ 近期最优: 温度{avg_temp:.0f}分，日均{avg_count:.1f}个信号")
        # 个股表现TOP
        stocks_with_data = []
        for code, stock in self.data["stocks"].items():
            stats = self.get_stock_stats(code)
            if stats["total"] >= 3:
                stocks_with_data.append((code, stock["name"], stats))
        if stocks_with_data:
            best_stock = max(stocks_with_data, key=lambda x: x[2]["wr"])
            worst_stock = min(stocks_with_data, key=lambda x: x[2]["wr"])
            lines.append(f"🏆 最佳短线: {best_stock[1]} 胜率{best_stock[2]['wr']:.0f}%({best_stock[2]['total']}次)")
            if worst_stock[2]["wr"] < 40:
                lines.append(f"⚠️ 注意回避: {worst_stock[1]} 胜率仅{worst_stock[2]['wr']:.0f}%")
        return lines


# ════════════════════════════════════════════════════════
# 市场温度计（简化版）
# ════════════════════════════════════════════════════════

def get_market_temp() -> dict:
    """快速获取市场温度"""
    ua = "Mozilla/5.0"
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{today}/orderby/date/orderway/desc/charset/GBK/"
        r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
        rows = r.json().get("data") or []
        if len(rows) < 10:
            for d in range(1, 5):
                d2 = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
                r2 = requests.get(f"http://zx.10jqka.com.cn/event/api/getharden/date/{d2}/orderby/date/orderway/desc/charset/GBK/",
                                  headers={"User-Agent": ua}, timeout=10)
                rows = r2.json().get("data") or []
                if len(rows) >= 10: break

        limit_up = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) >= 9.5) if rows else 0
        limit_down = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) <= -9.5) if rows else 0
        up = sum(1 for row in rows if float(row.get("zhangfu", 0) or 0) > 0) if rows else 0
        total = len(rows) or 1
        up_ratio = up / total

        # 北向
        try:
            r3 = requests.get("https://qt.gtimg.cn/q=sh601318", headers={"User-Agent": ua}, timeout=5)
            beixiang = 50
        except:
            beixiang = 50

        zt_score = min(80, max(25, (limit_up / max(limit_down, 1)) * 15))
        up_score = int(up_ratio * 100)
        temp = int((zt_score * 0.4 + up_score * 0.3 + 50 * 0.3))
        temp = max(10, min(95, temp))

        state = "🔥 亢奋" if temp >= 75 else ("✅ 正常" if temp >= 60 else ("🟡 偏冷" if temp >= 45 else ("⚠️ 低迷" if temp >= 30 else "🔴 冰点")))
        limit = 0.5 if temp >= 75 else (1.0 if temp >= 60 else (0.6 if temp >= 45 else (0.3 if temp >= 30 else 0.0)))
        return {"temp": temp, "state": state, "limit": limit}
    except:
        return {"temp": 50, "state": "🟡 未知", "limit": 0.5}


# ════════════════════════════════════════════════════════
# 推送器
# ════════════════════════════════════════════════════════

def push_notification(title: str, content: str):
    """推送到微信和钉钉"""
    pushed = False
    sckey = NOTIFY_CONFIG.get("serverchan_key")
    if sckey:
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{sckey}.send",
                              data={"title": title, "desp": content}, timeout=10)
            ok = r.json().get("code") == 0 or r.json().get("errno") == 0
            print(f"  微信推送: {'✅' if ok else '❌'}")
            pushed = ok
        except Exception as e:
            print(f"  微信推送异常: {e}")
    webhook = NOTIFY_CONFIG.get("dingtalk_webhook")
    if webhook:
        try:
            dd_content = content.replace("\n", "\n\n")
            r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"title": title[:30], "text": dd_content}},
                              headers={"Content-Type": "application/json"}, timeout=10)
            ok = r.json().get("errcode") == 0
            print(f"  钉钉推送: {'✅' if ok else '❌'}")
            pushed = pushed or ok
        except Exception as e:
            print(f"  钉钉推送异常: {e}")
    return pushed


# ════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="A股短线交易系统 v1.0 - 5日持有+自学习引擎")
    parser.add_argument("--wechat", action="store_true", help="推送微信/钉钉")
    parser.add_argument("--codes", type=str, help="指定股票代码")
    parser.add_argument("--review", action="store_true", help="回检昨日信号")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    args = parser.parse_args()

    now = datetime.now()
    if not args.quiet:
        print("\n" + "█" * 60)
        print(f"  ⚡ 短线交易系统 v1.0 — 5日持有 + 自学习")
        print(f"  {now.strftime('%Y-%m-%d %H:%M')}")
        print("█" * 60)

    # ── 市场温度 ──
    market = get_market_temp()
    temp, state, limit = market["temp"], market["state"], market["limit"]
    if not args.quiet:
        print(f"\n  🌡️ 温度: {temp}/100 {state} | 仓位上限: {limit*100:.0f}%")

    # ── 个股扫描 ──
    if args.codes:
        stock_list = [(c.strip(), "", "") for c in args.codes.split(",")]
    else:
        stock_list = BUILTIN_STOCKS

    if not args.quiet:
        print(f"\n📡 扫描 {len(stock_list)} 只股票...")

    de = DataEngine()
    sss = ShortTermSignals()
    sle = SelfLearningEngine()

    # 先回检（每天首次运行时检查昨日信号）
    if args.review:
        all_dfs = {}
        for code, name, sector in stock_list:
            df = de.get_klines(code)
            if df is not None:
                all_dfs[code] = df
        reviews = sle.review_yesterday(all_dfs)
        if reviews:
            print(f"\n📊 昨日信号回检 ({len(reviews)}个):")
            wins = sum(1 for r in reviews if r['ret'] > 0)
            avg = np.mean([r['ret'] for r in reviews]) if reviews else 0
            print(f"  胜率: {wins}/{len(reviews)} ({wins/len(reviews)*100:.0f}%) | 均收益: {avg:+.2f}%")
            for r in reviews[:8]:
                emoji = "✅" if r['ret'] > 0 else "❌"
                if not args.quiet:
                    print(f"  {emoji} {r['name']}({r['code']}) {r['date']} {r['ret']:+.2f}%")
        else:
            print("  暂无待回检信号")
        sle.save()
        return

    # 正式扫描
    signals = []
    signal_details = []

    for i, (code, name, sector) in enumerate(stock_list):
        if code.startswith(('300', '301', '688')):
            continue
        df = de.get_klines(code)
        if df is None or len(df) < 60:
            continue
        row = df.iloc[-1]
        quote = de.get_tencent_quote(code)
        stock_name = quote.get("name", name) or code

        sig = sss.score_all(row)
        if sig:
            # 获取自学习历史
            stats = sle.get_stock_stats(code)
            # 结合自学习调整评分
            final_score = sig["score"]
            if stats["wr"] >= 65:
                final_score = min(100, final_score + 8)
            elif stats["wr"] <= 40:
                final_score = int(final_score * 0.8)
            final_score = min(100, final_score)

            signals.append({
                "code": code, "name": stock_name, "sector": sector,
                "score": final_score, "type": sig["type"], "reasons": sig["reasons"],
                "confident": sig["confident"],
                "price": float(row["close"]) if pd.notna(row["close"]) else 0,
                "vol_ratio": float(row["vol_ratio"]) if pd.notna(row.get("vol_ratio", 0)) else 0,
                "atr": float(row["atr"]) if pd.notna(row.get("atr", 0)) else 0,
                "dd": float(row["dd"]) if pd.notna(row.get("dd", 0)) else 0,
                "stats": stats,
            })
            # 记录到自学习
            sle.record_signal(code, stock_name, sig, row)

        if not args.quiet and sig:
            stats_tag = f" 历史{stats['wr']:.0f}%" if stats["total"] >= 3 else ""
            print(f"  [{i+1}/{len(stock_list)}] ⚡ {sig['type']} {stock_name}({code}) {sig['score']}分 {sig['confident']}{stats_tag}")

        time.sleep(0.08)

    # ── 排序 ──
    signals.sort(key=lambda r: -r["score"])

    # ── 自学习：参数优化 ──
    recent_all = []
    for code, stock in sle.data["stocks"].items():
        for s in stock["signals"]:
            if s.get("check_5d") is not None:
                recent_all.append({
                    "vol_ratio": 0, "r5_result": s["check_5d"],
                    "has_breakout": s.get("type") == "放量突破",
                    "has_macd": s.get("type") == "MACD确认",
                    "has_pullback": s.get("type") == "强势回调",
                    "sector_hot": False,
                })
    sle.optimize_params(recent_all)
    sle.mine_combos(recent_all)

    # ── 记录市场环境 ──
    sle.record_market_env(temp, signals)
    sle.save()

    # ── 输出 ──
    print(f"\n{'='*60}")
    print(f"⚡ 短线信号总览")
    print(f"🌡️ 温度: {temp}/100 {state} | 仓位: {limit*100:.0f}%")
    print(f"📈 短线信号: {len(signals)} 个")
    print(f"{'='*60}")

    for s in signals[:10]:  # TOP10
        stats = s["stats"]
        bar = "█" * (s["score"] // 10) + "░" * (10 - s["score"] // 10)
        stats_tag = f" 历史{stats['wr']:.0f}%({stats['total']}次) {stats.get('personality','')}" if stats["total"] >= 3 else ""
        print(f"\n  {bar} {s['name']}({s['code']}) [{s['sector']}]")
        print(f"     {s['type']} ⚡{s['score']}分 | {s['reasons']}")
        print(f"     ¥{s['price']} 量比{s['vol_ratio']:.1f}x ATR{s['atr']:.1f}% 回撤{s['dd']:+.0f}%")
        if stats_tag:
            print(f"     🧠 {stats_tag} | {stats.get('trend','')}")
    print()

    # ── 自学习报告 ──
    report = sle.generate_report()
    if report:
        print("📊 自学习报告")
        for line in report:
            print(f"  {line}")
        print()

    # ── 推送 ──
    if args.wechat and signals:
        lines = []
        lines.append(f"## ⚡ 短线策略 {now.strftime('%m/%d %H:%M')}")
        lines.append(f"**🌡️ 温度**: {temp}/100 {state}　**📊 仓位**: {limit*100:.0f}%")
        lines.append("")
        lines.append(f"**📈 信号**: {len(signals)}个")

        for s in signals[:5]:
            stats = s["stats"]
            sec_tag = f"`{s['sector']}`" if s.get('sector') else ""
            hist = f" 历史胜率{stats['wr']:.0f}%" if stats["total"] >= 3 else ""
            lines.append(
                f"- {s['type']} **{s['name']}({s['code']})** {sec_tag} {s['confident']}"
                f"  \n  ⚡{s['score']}分 ¥{s['price']} 量比{s['vol_ratio']:.1f}x {hist}"
            )
            if stats.get("personality"):
                lines[-1] += f" {stats['personality']}"

        report = sle.generate_report()
        if report:
            lines.append("")
            lines.append("**🧠 自学习**")
            for line in report:
                lines.append(f"> {line}")

        lines.append("")
        lines.append(f"---\n🛑 **仓位**: {limit*100:.0f}% | **止损**: -5% | 持有5日")

        content = "\n".join(lines)
        push_notification(f"⚡短线 {now.strftime('%m/%d')} 温度{temp}", content)
        print("  推送完成")
    else:
        print("  --wechat 未启用，不推送")


if __name__ == "__main__":
    main()
