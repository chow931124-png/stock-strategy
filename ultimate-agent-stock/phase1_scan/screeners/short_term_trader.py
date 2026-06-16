"""
短线博弈引擎 v4 — 猎手模式（专业重构）

核心理念：
  猎手不预测猎物往哪跑，猎手判断"此刻扣扳机是否有利可图"

  重构 vs v3：
  1. 百分位排名代替绝对阈值 — 猎物肥不肥要看同林子里其他猎物
  2. 市场状态自适应权重 — 不同季节用不同陷阱
  3. 三维交叉否决 — 题材/资金/技术互证，不共振就放弃
  4. 多条件一票否决 — 识别猎物的假动作
  5. 动态出击数量 — 猎物多就多打，少就收枪
  6. 每枪都记录 — 下次就知道哪个陷阱好用
"""
from agent_orch.agent_base import BaseScreener
from data.market_data import get_bars, tencent_quote
from data.capital_data import stock_fund_flow_120d, daily_dragon_tiger
from data.tick_data import analyze_tick, format_tick_summary
from data.sentiment_data import ths_hot_reason
from agent_orch.context import get_intel_context
from phase4_position.short_term_exit import build_exit_plan
import numpy as np
import json
from datetime import datetime, timedelta
from pathlib import Path


class ShortTermTrader(BaseScreener):
    """短线博弈引擎 v4 — 猎手模式"""

    def __init__(self):
        super().__init__("ShortTermTrader")
        self.last_setups = []
        self.data_status = {"ths": False, "tick": False, "tiger": False, "fund_flow": False}

    # ──────────────────────────────────────────────
    #  主流程
    # ──────────────────────────────────────────────
    async def screen(self, candidates: list[str], market_context: dict, mode: str = "close") -> list[str]:
        """
        执行短线猎手扫描

        参数:
            candidates: 候选股票代码列表
            market_context: 市场上下文
            mode: "fast"(盘中快扫，不调资金流) | "close"(尾盘/收盘全量)
        """
        regime = market_context.get("regime", "SIDEWAYS")
        temp = market_context.get("temperature", 50)

        # 如果 main.py 已经设置了 _scan_mode（通过 trader._scan_mode=），用它
        if hasattr(self, "_scan_mode") and self._scan_mode in ("fast", "close", "scan"):
            mode = self._scan_mode
        else:
            self._scan_mode = mode

        # 冷市不出手
        if regime == "CRISIS":
            return []
        if regime == "BEAR" and temp < 30:
            return []

        # 非交易日检查
        now = datetime.now()
        if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
            test_tick = analyze_tick("600519")
            if not test_tick:
                print(f"     [跳过] 非交易日")
                return []

        # 获取环境数据
        hot_themes, hot_codes = self._get_hot_themes()
        tiger = daily_dragon_tiger()
        tiger_map = {}
        for s in tiger.get("stocks", []):
            c = s.get("code", "")
            if c:
                tiger_map[c] = s
        tiger_codes = set(tiger_map.keys())
        if tiger_codes:
            self.data_status["tiger"] = True

        # ───── 第1阶段：全量评估，收集原始因子值 ─────
        # 批量预取行情（减少HTTP请求，从N次→1次）
        batch_quotes = tencent_quote(candidates)
        all_scored = []
        total = len(candidates)
        report_interval = max(1, total // 10)  # 每10%报告一次
        for i, code in enumerate(candidates):
            try:
                result = self._evaluate(code, hot_themes, hot_codes, tiger_map, regime, temp, batch_quotes, mode)
                if result:
                    all_scored.append(result)
            except Exception:
                continue
            if (i + 1) % report_interval == 0:
                print(f"     短线评估: {i+1}/{total}", flush=True)

        if not all_scored:
            return []

        # ───── 第2阶段：百分位排名 ─────
        self._rank_percentiles(all_scored)

        # ───── 第3阶段：自适应权重 + 最终分 ─────
        # 权重随市场状态变化
        if regime == "BULL":
            w_t, w_f, w_tech = 0.25, 0.30, 0.45   # 牛市重技术，题材溢价低
        elif regime == "BEAR":
            w_t, w_f, w_tech = 0.50, 0.35, 0.15   # 熊市重题材（抱团取暖），技术不可靠
        elif regime == "VOLATILE":
            w_t, w_f, w_tech = 0.25, 0.50, 0.25   # 震荡看资金，资金决定方向
        else:  # SIDEWAYS
            w_t, w_f, w_tech = 0.35, 0.30, 0.35   # 横盘均衡

        for s in all_scored:
            raw = w_t * s["theme_pct"] + w_f * s["fund_pct"] + w_tech * s["tech_pct"]
            # 共振修正
            s["resonance"] = self._resonance_bonus(s)
            s["final_score"] = raw + s["resonance"]
            s["weights"] = (w_t, w_f, w_tech)

        # ───── 第4阶段：一票否决 ─────
        all_scored = [s for s in all_scored if not self._check_vetoes(s)]

        if not all_scored:
            return []

        # ───── 第5阶段：动态选取 ─────
        all_scored.sort(key=lambda x: -x["final_score"])
        final = self._select_dynamic(all_scored, temp)

        # ───── 第6阶段：记录 + 回检 ─────
        self._record_signals(final, market_context)
        self._check_yesterday_signals()

        self.last_setups = final
        return [s["code"] for s in final]

    # ──────────────────────────────────────────────
    #  第1阶段：单只股票原始因子采集
    # ──────────────────────────────────────────────
    def _evaluate(self, code, hot_themes, hot_codes, tiger_map, regime, temp, pre_quotes=None, mode="close"):
        """返回原始因子值，所有评分在后续阶段完成"""
        # 优先使用预取行情，没有再单独拉
        if pre_quotes and code in pre_quotes:
            quote = pre_quotes[code]
        else:
            quote = tencent_quote([code]).get(code, {})
        name = quote.get("name", "")
        price = quote.get("price", 0)
        mcap = quote.get("mcap_yi", 0)
        turnover = quote.get("turnover_pct", 0)
        open_p = quote.get("open", 0)
        last_close = quote.get("last_close", 0)
        limit_up = quote.get("limit_up", 0)
        amplitude = quote.get("amplitude_pct", 0)

        if price <= 0:
            return None

        # ── 硬性门槛（不改，保持稳定） ──
        if mcap <= 0 or mcap > 5000:
            return None
        if turnover < 0.3 or turnover > 30:
            return None

        klines = get_bars(code, 4, 60)
        if len(klines) < 20:
            return None

        closes = np.array([k["close"] for k in klines])
        highs = np.array([k["high"] for k in klines])
        lows = np.array([k["low"] for k in klines])
        volumes = np.array([k["volume"] for k in klines])
        current = price

        # ── 基础衍生指标 ──
        chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        chg_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
        vol_5 = np.mean(volumes[-5:]) if len(volumes) >= 5 else 0
        vol_20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else 0
        vr = vol_5 / vol_20 if vol_20 > 0 else 1
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else current
        above_ma20 = current > ma20
        gap_at_open = ((open_p - last_close) / last_close * 100) if last_close > 0 else 0

        # ATR
        if len(closes) >= 15:
            tr = np.maximum(
                highs[-14:] - lows[-14:],
                np.abs(highs[-14:] - closes[-15:-1]),
                np.abs(lows[-14:] - closes[-15:-1]),
            )
            atr_pct = np.mean(tr) / current * 100
        else:
            atr_pct = 0

        # ── 连板检测 ──
        consec_limit = 0
        for i in range(1, min(5, len(closes))):
            if (closes[-i] - closes[-i-1]) / closes[-i-1] * 100 >= 9.0:
                consec_limit += 1
            else:
                break

        # ── 缩量检测（连板后缩量 = 买不进去 = 接到就是顶） ──
        shrinking_vol = False
        if consec_limit >= 2 and len(volumes) >= consec_limit + 1:
            if volumes[-1] < volumes[-2] * 0.7:
                shrinking_vol = True

        # ── 逐笔 ──
        tick = analyze_tick(code)
        has_tick = bool(tick)
        if tick:
            self.data_status["tick"] = True
        big_net = tick.get("big_net", 0) if tick else 0
        last30_dir = tick.get("last30_dir", "neutral") if tick else "neutral"
        last30_big_net = tick.get("last30_big_net", 0) if tick else 0
        suspicious = tick.get("suspicious", []) if tick else []

        # ── 龙虎榜详情 ──
        tiger_info = tiger_map.get(code, {})
        tiger_net = tiger_info.get("net_buy", 0) if tiger_info else 0

        # ── 资金流（fast模式跳过，省时间） ──
        flow = stock_fund_flow_120d(code) if mode != "fast" else None
        net_3d = 0
        if flow and len(flow) >= 3:
            net_3d = sum(f["main_net"] for f in flow[-3:])

        # ── 回归：原始因子值，不给分 ──
        return {
            "code": code,
            "name": name,
            "price": current,
            "current_price": current,  # 兼容web
            "change_pct": round(chg_1d, 1),  # 兼容web
            "mcap": mcap,
            "turnover": turnover,
            "change_1d": round(chg_1d, 1),
            "change_5d": round(chg_5d, 1),
            "vol_ratio": round(vr, 2),
            "atr_pct": round(atr_pct, 1),
            "amplitude": amplitude,
            "mode": "短线猎手",  # 兼容web
            # 题材因子
            "in_hot_code": code in hot_codes,
            "hot_match_count": 1 if code in hot_codes else 0,
            # 资金因子（原始值）
            "big_net": big_net,
            "last30_dir": last30_dir,
            "last30_big_net": last30_big_net,
            "has_tick": has_tick,
            "is_tiger": code in tiger_map,
            "tiger_net": tiger_net,
            "fund_net_3d": net_3d,
            # 技术因子（原始值）
            "above_ma20": above_ma20,
            "gap_at_open": round(gap_at_open, 1),
            "consec_limit_ups": consec_limit,
            "shrinking_vol": shrinking_vol,
            "ma20": round(ma20, 2),
            # 否决用
            "suspicious": suspicious,
            # 止损
            "stop_loss": round(current * 0.93, 2),
            # 占位
            "theme_pct": 0, "fund_pct": 0, "tech_pct": 0,
            "theme_score": 0, "fund_score": 0, "tech_score": 0,
            "final_score": 0, "resonance": 0,
        }

    # ──────────────────────────────────────────────
    #  第2阶段：百分位排名
    # ──────────────────────────────────────────────
    def _rank_percentiles(self, scored):
        """对每个维度做百分位排名，替代绝对阈值"""
        n = len(scored)
        if n < 3:
            for s in scored:
                s["theme_pct"] = 50
                s["fund_pct"] = 50
                s["tech_pct"] = 50
                s["theme_score"] = 50
                s["fund_score"] = 50
                s["tech_score"] = 50
            return

        # ── 题材分：基于是否在热点+同花顺热度 ──
        raw_theme = [(
            50  # 基础
            + (25 if s["in_hot_code"] else 0)
        ) for s in scored]
        theme_arr = np.array(raw_theme)
        for s in scored:
            raw = 50 + (25 if s["in_hot_code"] else 0)
            s["theme_score"] = int(np.sum(theme_arr <= raw) / n * 100)

        # ── 资金分：基于大单净额+龙虎榜+资金流+尾盘方向 ──
        raw_fund = []
        for s in scored:
            f = 50  # 基础
            if s["has_tick"]:
                if s["big_net"] > 2000: f += 20
                elif s["big_net"] > 800: f += 10
                elif s["big_net"] > 0: f += 3
                elif s["big_net"] < -1000: f -= 15

                if s["last30_dir"] == "strong_buy": f += 15
                elif s["last30_dir"] == "buy": f += 6
                elif s["last30_dir"] == "strong_sell": f -= 15

                if s["last30_big_net"] > 500: f += 6
                elif s["last30_big_net"] < -500: f -= 12

            if s["is_tiger"]:
                f += 15
                if s["tiger_net"] < 0:
                    f -= 20  # 龙虎榜净卖出
            if s["fund_net_3d"] > 200: f += 8
            elif s["fund_net_3d"] < -300: f -= 8

            s["fund_score"] = max(0, f)
            raw_fund.append(max(0, f))

        fund_arr = np.array(raw_fund)
        for i, s in enumerate(scored):
            s["fund_pct"] = int(np.sum(fund_arr <= raw_fund[i]) / n * 100)

        # ── 技术分：基于量比+均线位置+涨幅+弹性 ──
        raw_tech = []
        for s in scored:
            t = 50
            vr = s["vol_ratio"]
            if 1.5 <= vr <= 2.5: t += 20
            elif 2.5 < vr <= 4: t += 10
            elif 0.7 <= vr < 1.5: t += 5
            elif vr < 0.5: t -= 15
            elif vr > 4: t -= 8

            if s["above_ma20"]: t += 12
            else: t -= 8

            c1 = s["change_1d"]
            if 2 <= c1 < 6: t += 10
            elif 6 <= c1 < 10: t += 5
            elif c1 < -3: t -= 12

            c5 = s["change_5d"]
            if 5 <= c5 <= 20: t += 5
            elif c5 > 30: t -= 10

            atr = s["atr_pct"]
            if 4 <= atr <= 10: t += 5
            elif atr > 15: t -= 5

            s["tech_score"] = max(0, min(100, t))
            raw_tech.append(max(0, min(100, t)))

        tech_arr = np.array(raw_tech)
        for i, s in enumerate(scored):
            s["tech_pct"] = int(np.sum(tech_arr <= raw_tech[i]) / n * 100)

    # ──────────────────────────────────────────────
    #  第3阶段：共振修正（交叉验证）
    # ──────────────────────────────────────────────
    def _resonance_bonus(self, s):
        """三维共振检测：正反馈加分，矛盾扣分"""
        tp = s["theme_pct"]
        fp = s["fund_pct"]
        techp = s["tech_pct"]

        # 各自所处的区间
        t_strong = tp >= 70
        t_weak = tp < 30
        f_strong = fp >= 70
        f_weak = fp < 30
        tech_strong = techp >= 70
        tech_weak = techp < 30

        # ── 共振加分 ──
        if t_strong and f_strong and tech_strong:
            return 15  # 三维全强 → 确定性最高
        if (t_strong and f_strong) or (f_strong and tech_strong) or (t_strong and tech_strong):
            return 8   # 二维共振 → 可以信赖

        # ── 矛盾扣分 ──
        if t_strong and f_weak:
            return -12  # 有题材没资金 → 掩护出货嫌疑
        if t_strong and tech_weak:
            return -8   # 题材好但技术破位 → 别碰
        if f_strong and tech_weak:
            return -6   # 有资金但趋势向下 → 可能是自救

        # 中性
        return 0

    # ──────────────────────────────────────────────
    #  第4阶段：一票否决
    # ──────────────────────────────────────────────
    def _check_vetoes(self, s):
        """多个否决条件，任一触发就放弃"""
        reasons = []

        # ① 利好出货：开盘跳空>5% + 量比>3
        if s["gap_at_open"] > 5 and s["vol_ratio"] > 3:
            reasons.append("利好出货")

        # ② 缩量连板：连续涨停且最后一天缩量<70%
        if s["consec_limit_ups"] >= 3 and s["shrinking_vol"]:
            reasons.append("缩量连板买不到")
        if s["consec_limit_ups"] >= 4:
            reasons.append("高位连板风险")

        # ③ 偷鸡板：振幅<2% + 尾盘拉升
        if s["has_tick"] and s["amplitude"] < 2 and s["last30_dir"] == "strong_buy":
            reasons.append("偷鸡板")

        # ④ 龙虎榜主力出逃
        if s["is_tiger"] and s["tiger_net"] < -500:
            reasons.append("龙虎榜净卖出")

        # ⑤ 尾盘砸盘（已有，补充到统一否决）
        if s["has_tick"] and "尾盘大单砸盘" in str(s["suspicious"]):
            reasons.append("尾盘砸盘")

        # ⑥ 跌破支撑：低于MA20超过5%
        if not s["above_ma20"] and s["ma20"] > 0:
            pct_below = (s["ma20"] - s["price"]) / s["ma20"] * 100
            if pct_below > 5:
                reasons.append(f"跌破MA20({pct_below:.0f}%)")

        if reasons:
            s["veto_reason"] = "|".join(reasons[:3])
            return True
        return False

    # ──────────────────────────────────────────────
    #  第5阶段：动态选取
    # ──────────────────────────────────────────────
    def _select_dynamic(self, scored, temp):
        """基于信号质量动态决定出击数量"""
        if not scored:
            return []

        # 温度低时适当收紧，但不至于全灭
        temp_adj = 0
        if temp < 30:
            temp_adj = 8  # 冰点：只出最确定的
        elif temp < 40:
            temp_adj = 3  # 偏冷：略收紧
        elif temp > 70:
            temp_adj = -5  # 火热：放宽

        min_score = 48 + temp_adj
        eligible = [s for s in scored if s["final_score"] >= min_score]

        if not eligible:
            # 分数普遍低但有候选，至少给1只最好的
            scored.sort(key=lambda x: -x["final_score"])
            if scored[0]["final_score"] >= 42:
                eligible = scored[:1]

        # 弹性上限：最多8只，保证至少出1只
        max_picks = max(1, min(8, len(eligible) // 2 + 1))

        selected = eligible[:max_picks]
        # 判断当前时段
        now = datetime.now()
        hour = now.hour
        is_after_market = hour >= 15
        is_late_afternoon = 13 <= hour < 15
        is_midday = 9 <= hour < 13

        # 根据模式+时间决定建议文案
        mode = getattr(self, "_scan_mode", "close")
        for s in selected:
            s["score"] = int(s["final_score"])
            if mode == "fast":
                s["entry_advice"] = "盘中异动"
                s["position_pct"] = "5%"
            elif is_after_market:
                s["entry_advice"] = "明日观察"
                s["position_pct"] = "—"
            elif is_late_afternoon:
                s["entry_advice"] = "尾盘买入" if s["final_score"] >= 58 else ("明日观察" if s["final_score"] >= 48 else "观望")
                s["position_pct"] = f"{max(3, min(8, s['final_score'] // 10))}%"
            else:  # 早盘/中午
                s["entry_advice"] = "盘中观察" if s["final_score"] >= 55 else "跟踪"
                s["position_pct"] = "—"
            s["scan_mode"] = mode
            # reasons / risks 给web展示
            s["reasons"] = []
            if s.get("in_hot_code"): s["reasons"].append("🔥今日强势")
            if s.get("is_tiger"): s["reasons"].append("🐅龙虎榜")
            if s.get("resonance", 0) >= 8: s["reasons"].append("🔗三维共振")
            if s.get("fund_net_3d", 0) > 200: s["reasons"].append("主力流入")
            s["risks"] = []
            if s.get("turnover", 0) > 20: s["risks"].append("换手过高")
            if s.get("change_5d", 0) > 30: s["risks"].append("短期涨幅大")
            # dimensions
            s["dimensions"] = {
                "题材百分位": s.get("theme_pct", 0),
                "资金百分位": s.get("fund_pct", 0),
                "技术百分位": s.get("tech_pct", 0),
                "共振修正": s.get("resonance", 0),
                "权重": f"{s['weights'][0]:.0f}/{s['weights'][1]:.0f}/{s['weights'][2]:.0f}" if s.get("weights") else "自适应",
            }
            s["follow_up"] = "次日不涨停就走 | 涨停则持有"
            if s.get("veto_reason"):
                s["follow_up"] += f" | 否决: {s['veto_reason']}"

        return selected

    # ──────────────────────────────────────────────
    #  第6阶段：信号记录 + 昨日回检
    # ──────────────────────────────────────────────
    def _get_signal_path(self):
        try:
            from config import get_config
            cache_dir = Path(get_config().get("cache_dir", "./cache"))
        except Exception:
            cache_dir = Path(__file__).parents[2] / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / "short_term_signals.json"

    def _record_signals(self, signals, market_context):
        """记录今日推荐信号到文件，用于后续自学习"""
        if not signals:
            return
        path = self._get_signal_path()
        try:
            history = {}
            if path.exists():
                history = json.loads(path.read_text())
            today = datetime.now().strftime("%Y-%m-%d")
            history[today] = [
                {
                    "code": s["code"],
                    "name": s["name"],
                    "entry": s["price"],
                    "final_score": s["final_score"],
                    "theme_pct": s["theme_pct"],
                    "fund_pct": s["fund_pct"],
                    "tech_pct": s["tech_pct"],
                    "resonance": s["resonance"],
                    "regime": market_context.get("regime", "UNKNOWN"),
                    "temp": market_context.get("temperature", 50),
                    "change_1d": s["change_1d"],
                    "vol_ratio": s["vol_ratio"],
                    "advice": "尾盘买入" if s["final_score"] >= 65 else "观察",
                }
                for s in signals
            ]
            # 只保留30天
            dates = sorted(history.keys())
            for d in dates[:-30]:
                del history[d]
            path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def _check_yesterday_signals(self):
        """回检昨日推荐信号，统计盈亏"""
        path = self._get_signal_path()
        if not path.exists():
            return
        try:
            history = json.loads(path.read_text())
            dates = sorted(history.keys())
            if len(dates) < 2:
                return
            yesterday = dates[-2]
            records = history.get(yesterday, [])
            if not records:
                return

            codes = [r["code"] for r in records]
            quotes = tencent_quote(codes)
            total_pnl = 0
            wins = 0
            details = []
            for r in records:
                q = quotes.get(r["code"], {})
                current_price = q.get("price", 0)
                if current_price <= 0 or r.get("entry", 0) <= 0:
                    continue
                pnl = (current_price - r["entry"]) / r["entry"] * 100
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                details.append(f"    {r['name']}({r['code']}) 推{r['entry']:.2f} 现{current_price:.2f} {pnl:+.1f}%")

            n = len(details)
            if n > 0:
                avg_pnl = total_pnl / n
                wr = wins / n * 100
                print(f"\n  📊 昨日信号追踪 ({yesterday}): {wins}/{n}胜 {wr:.0f}% 均{avg_pnl:+.1f}%")
                for d in details[:5]:
                    print(d)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    #  辅助：热点题材
    # ──────────────────────────────────────────────
    def _get_hot_themes(self):
        """获取今日热点题材"""
        hot_themes = {}
        hot_codes = set()
        today = datetime.now().strftime("%Y%m%d")
        df = ths_hot_reason(today)
        if not df.empty:
            self.data_status["ths"] = True
            for _, r in df.iterrows():
                code = str(r.get("代码", "")).strip()
                reason = str(r.get("题材归因", ""))
                if code and reason != "nan":
                    hot_codes.add(code)
                    for tag in reason.split("+"):
                        tag = tag.strip()
                        if tag:
                            hot_themes[tag] = hot_themes.get(tag, 0) + 1

        intel = get_intel_context()
        for pc in getattr(intel, 'policy_catalysts', []):
            kw = pc.get("keyword", "")
            intensity = pc.get("intensity", 0)
            if kw and intensity >= 30:
                hot_themes[f"政策:{kw}"] = intensity

        sorted_themes = sorted(hot_themes.items(), key=lambda x: -x[1])
        return sorted_themes[:8], hot_codes

    # ──────────────────────────────────────────────
    #  输出
    # ──────────────────────────────────────────────
    def print_setups(self):
        if not self.last_setups:
            print("  (暂无短线信号)")
            return
        print(f"\n  ⚡ 短线猎手 v4 ({len(self.last_setups)} 只)")
        print("  " + "─" * 60)
        for i, s in enumerate(self.last_setups, 1):
            dims = f"题材P{s['theme_pct']} 资金P{s['fund_pct']} 技术P{s['tech_pct']}"
            resonance = f" 共振{s['resonance']:+.0f}" if s.get("resonance", 0) != 0 else ""
            w = s.get("weights", (0, 0, 0))
            icon = "🟢" if s["final_score"] >= 65 else "🟡"
            print(f"  {i}. {s['code']} {s['name']:8s}  评分 {s['final_score']:.0f} "
                  f"(w={w[0]:.0f}/{w[1]:.0f}/{w[2]:.0f}){resonance}")
            print(f"     {dims}")
            print(f"     {icon} 仓位{8 if s['final_score']>=65 else 5}% "
                  f"止损{s['stop_loss']}  涨跌{s['change_1d']:+.1f}%")
            if s.get("in_hot_code"):
                print(f"     🔥 今日强势股")
            if s.get("is_tiger"):
                print(f"     🐅 龙虎榜")
            print(f"     📋 次日不涨停就走 | 涨停则持有")
            print()
