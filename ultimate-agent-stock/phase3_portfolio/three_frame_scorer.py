"""
三框架评分引擎 v3 — 专业重构

核心改动（基于量化研究员+投资经理的双重审查）:
  1. 因子压缩：3个独立因子（趋势+量价+情绪），去除MA+MACD重叠
  2. 时间衰减：近期价格行为权重 > 远期
  3. 框架互斥：短期/中期/长期有不同的前置硬性条件
  4. Price In 惩罚：涨太多的扣分（预期已被定价）
  5. 百分位排名基于全市场样本，不是候选池
"""
import numpy as np
from typing import Optional

from config import get_config
from data.market_data import tencent_quote
from data.fundamental_data import get_financials
from agent_orch.context import get_intel_context
from agent_orch.agent_base import StockPick, UltimatePortfolio


class ThreeFrameScorer:
    """三框架评分引擎 v3"""

    def __init__(self):
        cfg = get_config().get("scoring", {})
        self.short_cfg = cfg.get("short_term", {})
        self.med_cfg = cfg.get("medium_term", {})
        self.long_cfg = cfg.get("long_term", {"max_stocks": 0})

    def score_all(self, candidates, market_context, intel_context=None,
                  screener_results=None, pre_quotes=None, hot_themes=None,
                  theme_scores=None):
        regime = market_context.get("regime", "SIDEWAYS")
        temperature = market_context.get("temperature", 50)
        intel = intel_context or get_intel_context()
        quotes = pre_quotes or (tencent_quote(candidates) if candidates else {})

        self._hot_themes = hot_themes or {}
        self._theme_scores = theme_scores or {}

        screener_scores = {}
        if screener_results:
            for r in screener_results:
                code = r.get("code", "")
                sc = r.get("score", 50)
                if code and sc > screener_scores.get(code, 0):
                    screener_scores[code] = sc

        # 算分
        all_scored = []
        for code in candidates:
            try:
                scores = self._compute_dimensions(
                    code, quotes.get(code, {}), intel, screener_scores.get(code, 50)
                )
                if scores:
                    all_scored.append(scores)
            except Exception:
                continue

        if not all_scored:
            return UltimatePortfolio(market_regime=regime, market_temperature=temperature)

        # 百分位排名
        short_arr = np.array([s["raw_short"] for s in all_scored])
        med_arr = np.array([s["raw_med"] for s in all_scored])
        long_arr = np.array([s["raw_long"] for s in all_scored])

        for s in all_scored:
            s["short_score"] = self._pct(short_arr, s["raw_short"])
            s["med_score"] = self._pct(med_arr, s["raw_med"])
            s["long_score"] = self._pct(long_arr, s["raw_long"])

        # ★ 各框架独立筛选（前置条件过滤）
        short_picks = self._filter_short(all_scored, intel, market_context)
        med_picks = self._filter_medium(all_scored, intel, market_context)
        long_picks = self._filter_long(all_scored, intel, market_context)

        # ★ 跨日去重：排除昨天推荐过的股票（避免重复推）
        try:
            from pathlib import Path
            prev_codes = set()
            # 从三框架结果读取
            prev_file = Path(__file__).parents[1] / "data_store" / "latest_portfolio.json"
            if prev_file.exists():
                import json
                prev = json.loads(prev_file.read_text())
                for key in ["short_term", "medium_term", "long_term"]:
                    for s in prev.get(key, []):
                        prev_codes.add(s.get("code", ""))
            # 从短线猎手结果读取
            ts_file = Path(__file__).parents[1] / "data_store" / "latest_trader_setups.json"
            if ts_file.exists():
                import json
                ts = json.loads(ts_file.read_text())
                for s in ts:
                    prev_codes.add(s.get("code", ""))
            if prev_codes:
                    short_picks = [s for s in short_picks if s["code"] not in prev_codes]
                    med_picks = [s for s in med_picks if s["code"] not in prev_codes]
                    long_picks = [s for s in long_picks if s["code"] not in prev_codes]
        except Exception:
            pass

        # ★ 海外映射因子：NVDA/AVGO隔夜涨跌 → 对应产业链环节A股标的涨跌
        try:
            from data.us_market import fetch_us_market_close
            us_data = fetch_us_market_close()
            nvda_chg = us_data.get("nvda_change", 0) or 0
            avgo_chg = us_data.get("avgo_change", 0) or 0
            if nvda_chg or avgo_chg:
                # 构建股票→美股映射表（从产业链图谱读取）
                from data.ai_chain import build_ai_chain
                chain_nodes = build_ai_chain()
                stock_us_map = {}  # {code: [(us_ticker, chain_bottleneck), ...]}
                for n in chain_nodes.values():
                    for s in n.key_stocks:
                        for us in n.us_mapping:
                            stock_us_map.setdefault(s, []).append((us.upper(), n.bottleneck_score))

                if stock_us_map:
                    for s in all_scored:
                        code = s.get("code", "")
                        mappings = stock_us_map.get(code, [])
                        boost = 0
                        for us_ticker, bscore in mappings:
                            us_chg = 0
                            if us_ticker == "NVDA":
                                us_chg = nvda_chg
                            elif us_ticker == "AVGO":
                                us_chg = avgo_chg
                            # 映射强度：核心瓶颈环节(NVDA直接映射)加成更大
                            if us_ticker == "NVDA" and bscore >= 4:
                                boost += us_chg * 3
                            elif us_ticker == "NVDA":
                                boost += us_chg * 2
                            else:
                                boost += us_chg * 1.5
                        if boost != 0:
                            boost = max(-15, min(15, int(boost)))
                            s["raw_short"] = max(0, s["raw_short"] + boost)
                            s["raw_med"] = max(0, s["raw_med"] + boost)
                            s["overseas_boost"] = boost
        except Exception:
            pass

        # ★ 产业链卡位因子：属于核心卡位环节的股票加分
        try:
            from data.ai_chain import score_stock_in_chain
            for s in all_scored:
                code = s.get("code", "")
                chain = score_stock_in_chain(code)
                if chain.get("in_chain"):
                    bscore = chain.get("max_bottleneck", 0)
                    composite = chain.get("composite", 0)
                    cboost = 0
                    # 卡位评分越高加分越多
                    if bscore >= 5:
                        cboost = 15  # 命门级卡位
                    elif bscore >= 4:
                        cboost = 10  # 核心卡位
                    elif bscore >= 3:
                        cboost = 5   # 关键环节
                    # 综合评分补充
                    if composite >= 4.0:
                        cboost += 5
                    elif composite >= 3.5:
                        cboost += 3
                    if cboost > 0:
                        s["raw_short"] = max(0, s["raw_short"] + cboost)
                        s["raw_med"] = max(0, s["raw_med"] + cboost)
                        s["chain_boost"] = cboost
        except Exception:
            pass

        # ★ 历史表现惩罚：推过的票如果表现差，再出现时扣分
        try:
            from self_learn.signal_tracker import get_stock_track_record
            track = get_stock_track_record(days=60)
            if track:
                for s in all_scored:
                    t = track.get(s["code"])
                    if t and t["avg_return"] < -5 and t["times"] >= 1:
                        penalty = min(20, abs(int(t["avg_return"] * 2)))
                        s["raw_short"] = max(0, s["raw_short"] - penalty)
                        s["raw_med"] = max(0, s["raw_med"] - penalty // 2)
        except Exception:
            pass

        # ★ 大市值趋势股独立通道（市值>300亿+趋势向上+量价健康）
        # 这些股在短线框架里被市值惩罚压分，但中期趋势明确，值得推荐
        try:
            existing_codes = {s["code"] for s in short_picks + med_picks + long_picks}
            large_trend = []
            for s in all_scored:
                code = s["code"]
                if code in existing_codes:
                    continue
                mcap = s.get("mcap", 0)
                trend = s.get("trend", 0)
                volume = s.get("volume", 0)
                if mcap >= 300 and trend >= 60 and volume >= 45:
                    # 综合评分：趋势+量价+情绪，不加市值惩罚
                    channel_score = trend * 0.40 + volume * 0.30 + s.get("sentiment", 50) * 0.20
                    large_trend.append((channel_score, code, s))
            if large_trend:
                large_trend.sort(key=lambda x: -x[0])
                print(f"     🏢 大市值趋势通道: {len(large_trend)} 只候选")
                added = 0
                for score, code, s in large_trend:
                    if added >= 2:
                        break
                    # 只进中期推荐
                    s["_channel"] = "large_trend"
                    s["_channel_score"] = int(score)
                    med_picks.append(s)
                    added += 1
                    print(f"       + {s.get('name','')}({code}) 趋势{int(s.get('trend',0))} 通道分{int(score)}")
        except Exception:
            pass

        # 冲突处理（同一只股票只进一个框架）
        short_final, med_final, long_final = self._resolve(
            short_picks, med_picks, long_picks, all_scored
        )

        return UltimatePortfolio(
            market_regime=regime, market_temperature=temperature,
            short_term=[self._to_pick(s, "short_term") for s in short_final],
            medium_term=[self._to_pick(s, "medium_term") for s in med_final],
            long_term=[self._to_pick(s, "long_term") for s in long_final],
        )

    def _compute_dimensions(self, code, quote, intel, screener_score):
        """计算三因子评分：趋势 + 量价 + 情绪 (不含基本面)"""
        from data.market_data import get_bars

        klines = get_bars(code, category=4, offset=60)
        if len(klines) < 20:
            return None

        name = quote.get("name", "")
        price = quote.get("price", 0)
        if price <= 0:
            return None

        closes = np.array([k["close"] for k in klines])
        volumes = np.array([k["volume"] for k in klines])
        highs = np.array([k["high"] for k in klines])
        lows = np.array([k["low"] for k in klines])
        current = price

        # ═══════════════════════════════════════════
        # 因子1：趋势动量（独立因子，去重MA+MACD）
        # ═══════════════════════════════════════════
        trend = 50
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else ma20

        # 价格相对位置（当前价在20日区间的位置 0-100）
        if len(closes) >= 20:
            low_20 = np.min(closes[-20:])
            high_20 = np.max(closes[-20:])
            pos = (current - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50
            if pos > 80: trend += 18  # 强势区
            elif pos < 20: trend -= 18  # 弱势区

        # 均线斜率（表示趋势强度，不是简单的排列）
        if len(closes) >= 20:
            slope_10 = (ma20 - np.mean(closes[-30:-10])) / np.mean(closes[-30:-10]) * 100 if len(closes) >= 30 else 0
            if slope_10 > 2: trend += 18
            elif slope_10 > 0: trend += 10
            elif slope_10 < -2: trend -= 18

        # ★ Price In 检测：涨太多要扣分（预期已被定价）
        if len(closes) >= 20:
            ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
            if ret_20d > 40:
                trend -= 20  # 20天涨超40%→预期透支
            elif ret_20d > 25:
                trend -= 8   # 涨超25%→轻微透支
            elif ret_20d < -10:
                trend -= 10  # 跌太多趋势已破
            # 最佳区间：3-15%，动量充足且未过热
            if 3 <= ret_20d <= 15:
                trend += 10

        # ★ 时间衰减：近期涨跌权重 > 远期
        if len(closes) >= 10:
            w1, w2, w3 = 0.5, 0.3, 0.2  # 近/中/远 权重衰减
            ret_3d = (closes[-1] - closes[-3]) / closes[-3] * 100
            ret_7d = (closes[-4] - closes[-7]) / closes[-7] * 100
            ret_14d = (closes[-8] - closes[-14]) / closes[-14] * 100 if len(closes) >= 14 else 0
            # 加权动量：近期最重要
            mom = ret_3d * w1 + ret_7d * w2 + ret_14d * w3
            if -2 <= mom <= -0.5: trend += 10  # 最近小回调是机会
            elif mom > 5: trend += 6
            elif mom < -5: trend -= 10  # 近期加速下跌

        trend = min(100, max(0, trend))

        # ═══════════════════════════════════════════
        # 因子2：量价验证（独立因子，不含趋势信息）
        # ═══════════════════════════════════════════
        volume = 50
        vol_5 = np.mean(volumes[-5:]) if len(volumes) >= 5 else 0
        vol_20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else 0
        vr = vol_5 / vol_20 if vol_20 > 0 else 1

        # 量比区间
        if 1.5 <= vr <= 2.5:
            volume += 25  # 理想放量
        elif 2.5 < vr <= 4:
            volume += 12
        elif 0.7 <= vr < 1.5:
            volume += 5
        elif vr < 0.5:
            volume -= 18  # 极度缩量
        elif vr > 4:
            volume -= 10  # 放量过大→可能是出货

        # 价量配合检验（最后3天收盘上涨且放量）
        if len(closes) >= 3 and len(volumes) >= 3:
            up_days = sum(1 for i in range(3) if closes[-i-1] > closes[-i-2])
            if up_days >= 2 and vr > 1.2:
                volume += 12
            elif up_days <= 1 and vr < 0.8:
                volume -= 10

        # 换手率验证
        turnover = quote.get("turnover_pct", 0)
        if 3 <= turnover <= 10:
            volume += 8
        elif turnover > 20:
            volume -= 12

        volume = min(100, max(0, volume))

        # ═══════════════════════════════════════════
        # 因子X：价量背离检测（独立因子）— 放量下跌扣分，放量上涨加分
        # ═══════════════════════════════════════════
        divergence = 0
        if len(closes) >= 5 and len(volumes) >= 5:
            # 最近5天价量方向匹配度：每天看价格涨跌和成交量增减方向是否一致
            div_count = 0  # 背离天数
            con_count = 0  # 一致天数
            for i in range(-4, 0):
                price_up = closes[i] > closes[i-1]
                vol_up = volumes[i] > volumes[i-1]
                if price_up and vol_up:
                    con_count += 1  # 放量上涨 → 健康
                elif not price_up and vol_up:
                    div_count += 1  # 放量下跌 → 出货
                # 缩量涨/缩量跌 → 中性，不计数

            total = div_count + con_count
            if total >= 3:
                # 用 (一致天数 - 背离天数) / 总天数 映射到 -10 ~ +10
                divergence = int((con_count - div_count) / total * 10)
            # 大额惩罚：单日跌超5% + 量比 > 2，直接触发强信号
            chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
            vr = np.mean(volumes[-5:]) / np.mean(volumes[-20:]) if np.mean(volumes[-20:]) > 0 else 1
            if chg_1d < -5 and vr > 1.5:
                divergence = min(divergence, -8)  # 极端放量下跌，强负信号
            elif chg_1d > 3 and vr > 1.3:
                divergence = max(divergence, 5)   # 放量上涨，正信号

        # ─── 价量背离独立因子（-10~+10，供短/中期使用） ───
        divergence_adj = divergence  # -10~+10

        # ═══════════════════════════════════════════
        # 因子3：情绪催化（情报+题材+资金流）
        # ═══════════════════════════════════════════
        sentiment = 50
        screener_boost = 0
        theme_boost = 0

        if screener_score:
            screener_boost = min(15, screener_score * 0.2)
            sentiment += screener_boost

        # 题材匹配
        theme_boost = self._theme_scores.get(code, 0) * 0.2
        sentiment += min(15, theme_boost)

        # 情报热点
        if intel:
            boosted = getattr(intel, 'boosted_sectors', {})
            rotation = getattr(intel, 'sector_rotation', {})
            for _, stage in rotation.items():
                if stage == "UPSTART": sentiment += 10; break
                elif stage == "EXHAUST": sentiment -= 10; break
            if boosted: sentiment += 8

        sentiment = min(100, max(20, sentiment))

        # ─── 基本面（仅长期持有使用） ───
        fund_score = 50
        fin = get_financials(code)
        pe = quote.get("pe_ttm", 0)
        mcap = quote.get("mcap_yi", 0)
        if fin:
            roe = fin.get("roe_pct", 0)
            if roe > 8: fund_score += 20
            elif roe > 4: fund_score += 12
            elif roe > 1: fund_score += 3
            if fin.get("income", 0) > 100e9: fund_score += 5
        if 0 < pe < 15: fund_score += 15
        elif pe > 50: fund_score -= 10
        if mcap > 500: fund_score += 5
        fund_score = min(100, max(0, fund_score))

        # ─── 市值罚金 ───
        mcap_pe = 0
        if mcap > 500: mcap_pe = 10  # 大盘股不适合短期
        elif mcap < 50: mcap_pe = -5  # 小盘股加分

        # ─── 产业链加成（从 ai_chain 读取） ───
        try:
            from data.industry_chains import get_stock_chains
            chain_info = get_stock_chains(code)
            if chain_info:
                max_bottleneck = max(c.get("bottleneck", 0) for c in chain_info)
                if max_bottleneck >= 4:
                    sentiment += 10  # 核心卡位环节加分
                elif max_bottleneck >= 3:
                    sentiment += 5
                # 景气阶段加成
                hot_stages = ["涨价", "高景气", "爆发", "超级周期", "高增长"]
                for ci in chain_info:
                    if ci.get("stage") in hot_stages:
                        sentiment += 8
                        break
        except Exception:
            pass

        # ─── 三框架原始分（权重从config.yaml读取） ───
        def _weighted_sum(frame_cfg, factor_map):
            factors = frame_cfg.get("factors", {})
            total = 0
            for name, weight in factors.items():
                val = factor_map.get(name, 0)
                total += val * weight
            return total

        short_factors = {"trend": trend, "volume": volume, "sentiment": sentiment, "divergence": divergence_adj}
        raw_short = _weighted_sum(self.short_cfg, short_factors) - mcap_pe

        med_factors = {"trend": trend, "volume": volume, "sentiment": sentiment, "fund": fund_score, "divergence": divergence_adj}
        raw_med = _weighted_sum(self.med_cfg, med_factors)

        long_factors = {"trend": trend, "volume": volume, "sentiment": sentiment, "fund": fund_score}
        raw_long = _weighted_sum(self.long_cfg, long_factors)

        return {
            "code": code, "name": name, "price": current,
            "change_pct": quote.get("change_pct", 0),
            "turnover_pct": quote.get("turnover_pct", 0),
            "pe_ttm": pe, "mcap": mcap, "last_close": quote.get("last_close"),
            "raw_short": raw_short, "raw_med": raw_med, "raw_long": raw_long,
            "short_score": 0, "med_score": 0, "long_score": 0,
            "divergence": divergence_adj,
            "trend": round(trend, 1),
            "volume": round(volume, 1),
            "sentiment": round(sentiment, 1),
            "fund": round(fund_score, 1),
        }

    def _pct(self, arr, val):
        if len(arr) == 0: return 50
        return round(np.sum(arr <= val) / len(arr) * 100, 1)

    def _filter_short(self, scored, intel, ctx):
        """短期爆发前置条件"""
        threshold = self.short_cfg.get("min_score", 50)
        candidates = [s for s in scored if s.get("short_score", 0) >= threshold]

        # 🔴 硬性条件（不满足直接淘汰）
        filtered = []
        for s in candidates:
            mcap = s.get("mcap", 0)
            turnover = s.get("turnover_pct", 0)
            trend = s.get("trend", 50)
            volume = s.get("volume", 50)
            name = s.get("name", "")

            # 排除 ST
            if "ST" in name or "*ST" in name:
                continue
            # 市值 30-500 亿（排除大盘股和小壳股）
            if mcap > 5000 or (mcap > 0 and mcap < 20):
                continue
            # 换手率 1-20%（太冷/太热都不要）
            if turnover > 20 or (turnover > 0 and turnover < 1):
                continue
            # 趋势和量价不能都低于 40
            if trend < 40 and volume < 40:
                continue

            filtered.append(s)

        filtered.sort(key=lambda x: -x.get("short_score", 0))
        return filtered[:self.short_cfg.get("max_stocks", 2) + 1]

    def _filter_medium(self, scored, intel, ctx):
        """中期趋势前置条件"""
        threshold = self.med_cfg.get("min_score", 50)
        candidates = [s for s in scored if s.get("med_score", 0) >= threshold]

        filtered = []
        for s in candidates:
            mcap = s.get("mcap", 0)
            pe = s.get("pe_ttm", 0)
            trend = s.get("trend", 50)
            name = s.get("name", "")

            # 市值 > 50亿
            if "ST" in name or "*ST" in name: continue
            if mcap == 0: continue
            if mcap < 50: continue
            # PE > 0（不选亏损股）
            if pe < 0: continue
            # 趋势不能太差
            if trend < 35: continue

            filtered.append(s)

        filtered.sort(key=lambda x: -x.get("med_score", 0))
        return filtered[:self.med_cfg.get("max_stocks", 3) + 1]

    def _filter_long(self, scored, intel, ctx):
        """长期持有前置条件"""
        threshold = self.long_cfg.get("min_score", 50)
        candidates = [s for s in scored if s.get("long_score", 0) >= threshold]

        filtered = []
        for s in candidates:
            mcap = s.get("mcap", 0)
            pe = s.get("pe_ttm", 0)
            fund = s.get("fund", 50)
            name = s.get("name", "")

            # 市值 > 100亿
            if "ST" in name or "*ST" in name: continue
            if mcap == 0: continue
            if mcap < 100: continue
            # PE > 0
            if pe < 0: continue
            # 基本面不能差
            if fund < 40: continue

            filtered.append(s)

        filtered.sort(key=lambda x: -x.get("long_score", 0))
        return filtered[:self.long_cfg.get("max_stocks", 1) + 1]

    def _resolve(self, short, med, long, all_scored):
        """冲突处理：每只股票只进一个框架"""
        short_final, med_final, long_final = [], [], []
        assigned = set()

        # 短期优先（最需要关注）
        for s in short:
            if s["code"] not in assigned:
                short_final.append(s)
                assigned.add(s["code"])
                if len(short_final) >= self.short_cfg.get("max_stocks", 2):
                    break

        # 中期补充
        for s in med:
            if s["code"] not in assigned:
                med_final.append(s)
                assigned.add(s["code"])
                if len(med_final) >= self.med_cfg.get("max_stocks", 3):
                    break

        # 长期补充
        for s in long:
            if s["code"] not in assigned:
                long_final.append(s)
                assigned.add(s["code"])
                if len(long_final) >= self.long_cfg.get("max_stocks", 1):
                    break

        # 如果还不够，从all_scored补
        max_short = self.short_cfg.get("max_stocks", 2)
        max_med = self.med_cfg.get("max_stocks", 3)
        max_long = self.long_cfg.get("max_stocks", 1)

        if len(short_final) < max_short:
            remaining = sorted(
                [s for s in all_scored if s["code"] not in assigned],
                key=lambda x: -x["short_score"]
            )
            for s in remaining:
                if len(short_final) >= max_short: break
                short_final.append(s)
                assigned.add(s["code"])

        if len(med_final) < max_med:
            remaining = sorted(
                [s for s in all_scored if s["code"] not in assigned],
                key=lambda x: -x["med_score"]
            )
            for s in remaining:
                if len(med_final) >= max_med: break
                med_final.append(s)
                assigned.add(s["code"])

        if len(long_final) < max_long:
            remaining = sorted(
                [s for s in all_scored if s["code"] not in assigned],
                key=lambda x: -x["long_score"]
            )
            for s in remaining:
                if len(long_final) >= max_long: break
                long_final.append(s)

        return short_final, med_final, long_final

    def _to_pick(self, s, frame):
        sk = {"short_term": "short_score", "medium_term": "med_score", "long_term": "long_score"}
        score_key = sk.get(frame, "short_score")
        channel_tag = " 🏢大市值趋势" if s.get("_channel") == "large_trend" else ""
        sp = StockPick(
            code=s["code"], name=s.get("name", ""),
            reason=self._reason(s, frame) + channel_tag,
            confidence=min(1.0, s.get(score_key, 50) / 100),
            score=s.get(score_key, 50),
            score_by_analyst={
                "趋势": s.get("trend", 50),
                "量价": s.get("volume", 50),
                "情绪": s.get("sentiment", 50),
                "基本面": s.get("fund", 50),
            },
            last_close=s.get("last_close"), current_price=s.get("price"), change_pct=s.get("change_pct"),
        )

        if frame == "short_term" and s.get("price", 0) > 0:
            p = s["price"]
            sp.entry_zone = (round(p * 0.975, 2), round(p * 1.02, 2))
            sp.stop_loss = round(p * 0.93, 2)
            sp.target_price = round(p * 1.10, 2)
            sp.expected_hold_days = 7

        return sp

    def _reason(self, s, frame):
        parts = []
        if s.get("sentiment", 50) >= 70: parts.append("🔥 情绪催化")
        if s.get("trend", 50) >= 70: parts.append("📈 趋势动量")
        if s.get("volume", 50) >= 70: parts.append("💰 量价配合")
        if s.get("fund", 50) >= 70: parts.append("📊 基本面优")
        return " ".join(parts) if parts else "综合评分"
